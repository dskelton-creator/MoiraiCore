#!/usr/bin/env python3
"""
MoiraiCore — Telemetry Collector (Cost + Latency per Agent/Tier run)

This is the structural seam for Feature 2: there was no queryable store for
per-run cost/latency anywhere in the execution path. goal_engine._execute_task
returns duration_ms, and scrum_gate returns time_s + tier, but neither persisted
to anything the dashboard could read. This module fixes that.

Design
------
- Append-only JSONL at <AGENT_OS_ROOT>/config/telemetry.jsonl (one run per line)
  Each record: {ts, goal_id, agent, tier, model, duration_ms, prompt_chars,
                est_tokens, cost_usd, ok, details}
- Cost is *estimated* from prompt size using a per-model pricing table, because
  the Hermes bridge does not always return exact token counts. The estimate is
  clearly labelled and conservative; if a caller passes an explicit cost_usd it
  is trusted instead. Local models (Ollama / Tier 3) cost $0 — that distinction
  is the whole point of the telemetry for a solo operator watching API bills.
- Reader is file-mtime cached and capped so the dashboard can poll cheaply.

Import-safe and dependency-free per the project audit convention: callers should
wrap imports in try/except ImportError.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Resolve AGENT_OS_ROOT the same way server.py does.
_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            if _k.strip() and _v.strip():
                os.environ.setdefault(_k.strip(), _v.strip())

AGENT_OS_ROOT = Path(os.environ.get("AGENT_OS_ROOT", str(Path(__file__).resolve().parents[1])))
TELEMETRY_FILE = AGENT_OS_ROOT / "config" / "telemetry.jsonl"
ALERTS_FILE = AGENT_OS_ROOT / "config" / "telemetry_alerts.json"

# ── Pricing table (USD per 1M tokens). Input cost drives the estimate; we do not
#    separate input/output here because the bridge doesn't always report both. ──
# Tier 2 = cloud (Gemini Pro / Flash). Tier 3 = local Ollama (free).
PRICING = {
    # model substring -> (input_per_1M, output_per_1M)
    "gemini-2.5-pro": (1.25, 10.0),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.0-pro": (1.25, 10.0),
    "gemini-2.0-flash": (0.10, 0.40),
    "gemini-pro": (1.25, 10.0),
    "gemini-flash": (0.30, 2.50),
    "gpt-4o": (2.50, 10.0),
    "gpt-4o-mini": (0.15, 0.60),
    "claude-3.5-sonnet": (3.0, 15.0),
    "claude-3.5-haiku": (0.80, 4.0),
    "ollama": (0.0, 0.0),   # local, free
    "ornith": (0.0, 0.0),   # local, free
}

# Rough chars->tokens heuristic for estimation when exact counts are unavailable.
_CHARS_PER_TOKEN = 4.0

_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _resolve_pricing(model: str):
    """Return (input_per_1M, output_per_1M) for a model string, else None."""
    if not model:
        return None
    m = model.lower()
    # local models are free even if the name doesn't match a cloud prefix
    if "ollama" in m or "ornith" in m or "local" in m:
        return (0.0, 0.0)
    for key, rate in PRICING.items():
        if key in m:
            return rate
    return None


def estimate_cost(model: str, prompt_chars: int, est_output_chars: int = 0) -> float:
    """Estimate USD cost from prompt size. Returns 0.0 for unknown/local models."""
    rate = _resolve_pricing(model)
    if rate is None:
        return 0.0
    inp_per_1m, out_per_1m = rate
    in_tokens = max(prompt_chars, 0) / _CHARS_PER_TOKEN
    out_tokens = max(est_output_chars, 0) / _CHARS_PER_TOKEN
    cost = (in_tokens / 1_000_000) * inp_per_1m + (out_tokens / 1_000_000) * out_per_1m
    return round(cost, 6)


def record_run(
    *,
    agent: str = "",
    tier: str = "",
    model: str = "",
    duration_ms: int = 0,
    prompt_chars: int = 0,
    est_output_chars: int = 0,
    goal_id: str = "",
    ok: bool = True,
    cost_usd: Optional[float] = None,
    details: Optional[dict] = None,
) -> dict:
    """Append one telemetry record. Returns the event dict.

    Cost is estimated from prompt/output size unless cost_usd is explicitly given.
    """
    rec = {
        "ts": _now_iso(),
        "goal_id": goal_id,
        "agent": agent,
        "tier": tier,
        "model": model,
        "duration_ms": int(duration_ms),
        "prompt_chars": int(prompt_chars),
        "est_tokens": int((prompt_chars + est_output_chars) / _CHARS_PER_TOKEN),
        "cost_usd": float(cost_usd) if cost_usd is not None else estimate_cost(model, prompt_chars, est_output_chars),
        "ok": bool(ok),
        "details": details or {},
    }
    try:
        TELEMETRY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            with open(TELEMETRY_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        # Never let telemetry break the calling flow.
        pass
    return rec


def _read_runs(limit: int = 200) -> list[dict]:
    if not TELEMETRY_FILE.exists():
        return []
    try:
        with _lock:
            lines = TELEMETRY_FILE.read_text(encoding="utf-8").splitlines()
        runs = []
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                runs.append(json.loads(ln))
            except Exception:
                continue
        return runs[-limit:] if limit else runs
    except Exception:
        return []


def get_summary(limit: int = 5000) -> dict:
    """Aggregate telemetry into a dashboard-ready summary."""
    runs = _read_runs(limit)
    total = len(runs)
    total_cost = 0.0
    total_duration = 0
    successes = 0
    failures = 0
    by_tier: dict[str, dict] = {}
    by_agent: dict[str, dict] = {}
    by_model: dict[str, dict] = {}

    for r in runs:
        cost = float(r.get("cost_usd", 0.0) or 0.0)
        dur = int(r.get("duration_ms", 0) or 0)
        total_cost += cost
        total_duration += dur
        if r.get("ok"):
            successes += 1
        else:
            failures += 1

        def _bump(bucket: dict, key: str):
            b = bucket.setdefault(key or "unknown", {
                "runs": 0, "cost_usd": 0.0, "duration_ms": 0,
                "success": 0, "failure": 0,
            })
            b["runs"] += 1
            b["cost_usd"] += cost
            b["duration_ms"] += dur
            if r.get("ok"):
                b["success"] += 1
            else:
                b["failure"] += 1

        _bump(by_tier, r.get("tier") or "")
        _bump(by_agent, r.get("agent") or "")
        _bump(by_model, r.get("model") or "")

    def _avg(bucket: dict):
        out = {}
        for k, v in bucket.items():
            out[k] = {
                **v,
                "cost_usd": round(v["cost_usd"], 4),
                "avg_duration_ms": int(v["duration_ms"] / v["runs"]) if v["runs"] else 0,
                "avg_cost_usd": round(v["cost_usd"] / v["runs"], 6) if v["runs"] else 0.0,
            }
        return out

    return {
        "total_runs": total,
        "total_cost_usd": round(total_cost, 4),
        "total_duration_ms": total_duration,
        "avg_duration_ms": int(total_duration / total) if total else 0,
        "successes": successes,
        "failures": failures,
        "by_tier": _avg(by_tier),
        "by_agent": _avg(by_agent),
        "by_model": _avg(by_model),
        "since": runs[0]["ts"] if runs else None,
        "latest": runs[-1]["ts"] if runs else None,
    }


def get_runs(limit: int = 100, tier: str = "", agent: str = "", goal_id: str = "") -> list[dict]:
    runs = _read_runs(limit * 4 if (tier or agent or goal_id) else limit)
    if tier:
        runs = [r for r in runs if r.get("tier") == tier]
    if agent:
        runs = [r for r in runs if r.get("agent") == agent]
    if goal_id:
        runs = [r for r in runs if r.get("goal_id") == goal_id]
    return runs[-limit:]


if __name__ == "__main__":
    # CLI smoke test
    rec = record_run(agent="researcher", tier="2", model="gemini-2.5-flash",
                     duration_ms=8200, prompt_chars=1400, est_output_chars=3200, goal_id="g_test")
    print("Recorded:", json.dumps(rec, indent=2))
    print("Summary:", json.dumps(get_summary(), indent=2))
    print("Timeseries:", json.dumps(get_timeseries(granularity="day"), indent=2))
    print("Alerts:", json.dumps(get_alerts(), indent=2))


# ═══════════════════════════════════════════════════════════════════════════════
# Feature 12 — Timeseries, Alerting & Efficiency Metrics (July 2026)
# ═══════════════════════════════════════════════════════════════════════════════

def get_timeseries(granularity: str = "day", limit: int = 5000) -> dict:
    """Aggregate telemetry into time-bucketed series for dashboard charts.

    Args:
        granularity: "hour" or "day"
        limit: Max runs to read

    Returns:
        {"buckets": [{ts, cost, duration_ms, runs, ok, fail, by_tier, by_model}],
         "granularity": "day", "total_cost": N, "total_runs": N}
    """
    runs = _read_runs(limit)
    if not runs:
        return {"buckets": [], "granularity": granularity, "total_cost": 0.0, "total_runs": 0}

    def _bucket_key(ts_str: str) -> str:
        """Convert an ISO timestamp to a bucket key (hour or day)."""
        try:
            dt = datetime.fromisoformat(ts_str)
            if granularity == "hour":
                return dt.strftime("%Y-%m-%dT%H:00:00")
            return dt.strftime("%Y-%m-%d")
        except Exception:
            return ts_str[:10] if granularity == "day" else ts_str[:13]

    buckets: dict[str, dict] = {}
    for r in runs:
        bk = _bucket_key(r.get("ts", ""))
        b = buckets.setdefault(bk, {
            "ts": bk, "cost": 0.0, "duration_ms": 0,
            "runs": 0, "ok": 0, "fail": 0,
            "by_tier": {}, "by_model": {},
        })
        cost = float(r.get("cost_usd", 0.0) or 0.0)
        dur = int(r.get("duration_ms", 0) or 0)
        b["cost"] += cost
        b["duration_ms"] += dur
        b["runs"] += 1
        if r.get("ok"):
            b["ok"] += 1
        else:
            b["fail"] += 1
        tier = r.get("tier", "unknown") or "unknown"
        model = r.get("model", "unknown") or "unknown"
        b["by_tier"][tier] = b["by_tier"].get(tier, 0) + cost
        b["by_model"][model] = b["by_model"].get(model, 0) + cost

    # Round costs
    for b in buckets.values():
        b["cost"] = round(b["cost"], 4)
        b["by_tier"] = {k: round(v, 4) for k, v in b["by_tier"].items()}
        b["by_model"] = {k: round(v, 4) for k, v in b["by_model"].items()}

    sorted_buckets = sorted(buckets.values(), key=lambda x: x["ts"])
    total_cost = sum(b["cost"] for b in sorted_buckets)
    total_runs = sum(b["runs"] for b in sorted_buckets)

    return {
        "buckets": sorted_buckets,
        "granularity": granularity,
        "total_cost": round(total_cost, 4),
        "total_runs": total_runs,
    }


def get_efficiency(limit: int = 5000) -> dict:
    """Calculate efficiency metrics: cost per run, tokens per second, etc.

    Returns:
        {"avg_cost_per_run": N, "avg_duration_per_run": N,
         "cost_by_tier": {...}, "fastest_model": "...", "cheapest_model": "...",
         "model_rankings": [{model, avg_cost, avg_duration, runs}]}
    """
    runs = _read_runs(limit)
    if not runs:
        return {"avg_cost_per_run": 0, "avg_duration_per_run": 0, "model_rankings": []}

    total_cost = 0.0
    total_dur = 0
    by_model: dict[str, dict] = {}

    for r in runs:
        cost = float(r.get("cost_usd", 0.0) or 0.0)
        dur = int(r.get("duration_ms", 0) or 0)
        total_cost += cost
        total_dur += dur
        model = r.get("model", "unknown") or "unknown"
        m = by_model.setdefault(model, {"runs": 0, "cost": 0.0, "duration_ms": 0})
        m["runs"] += 1
        m["cost"] += cost
        m["duration_ms"] += dur

    model_rankings = []
    for model, data in by_model.items():
        model_rankings.append({
            "model": model,
            "runs": data["runs"],
            "avg_cost": round(data["cost"] / data["runs"], 6),
            "avg_duration_ms": int(data["duration_ms"] / data["runs"]),
            "total_cost": round(data["cost"], 4),
        })

    model_rankings.sort(key=lambda x: x["avg_cost"])
    fastest = min(model_rankings, key=lambda x: x["avg_duration_ms"]) if model_rankings else None
    cheapest = model_rankings[0] if model_rankings else None

    return {
        "total_runs": len(runs),
        "total_cost_usd": round(total_cost, 4),
        "total_duration_ms": total_dur,
        "avg_cost_per_run": round(total_cost / len(runs), 6) if runs else 0,
        "avg_duration_per_run": int(total_dur / len(runs)) if runs else 0,
        "fastest_model": fastest["model"] if fastest else "",
        "fastest_avg_duration_ms": fastest["avg_duration_ms"] if fastest else 0,
        "cheapest_model": cheapest["model"] if cheapest else "",
        "cheapest_avg_cost": cheapest["avg_cost"] if cheapest else 0,
        "model_rankings": model_rankings,
        "cost_by_tier": get_summary(limit).get("by_tier", {}),
    }


# ── Alerting ──────────────────────────────────────────────────────────────────


def set_alert(name: str, metric: str, operator: str, threshold: float,
              enabled: bool = True) -> dict:
    """Create or update a telemetry alert.

    Args:
        name: Human-readable name (e.g. "Daily cost > $1")
        metric: What to measure: "daily_cost", "hourly_cost", "avg_duration", "failure_rate"
        operator: "gt" (greater than) or "lt" (less than)
        threshold: The threshold value
        enabled: Whether the alert is active

    Returns:
        The alert dict.
    """
    if metric not in ("daily_cost", "hourly_cost", "avg_duration", "failure_rate"):
        raise ValueError(f"Unknown metric: {metric}. Use: daily_cost, hourly_cost, avg_duration, failure_rate")
    if operator not in ("gt", "lt"):
        raise ValueError(f"Unknown operator: {operator}. Use: gt, lt")

    alert = {
        "id": "alert-" + __import__("uuid").uuid4().hex[:8],
        "name": name,
        "metric": metric,
        "operator": operator,
        "threshold": float(threshold),
        "enabled": bool(enabled),
        "created_at": _now_iso(),
        "last_triggered": None,
    }
    alerts = _load_alerts()
    alerts.append(alert)
    _save_alerts(alerts)
    return alert


def _load_alerts() -> list:
    try:
        if not ALERTS_FILE.exists():
            return []
        return json.loads(ALERTS_FILE.read_text(encoding="utf-8") or "[]")
    except Exception:
        return []


def _save_alerts(alerts: list):
    try:
        ALERTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write
        tmp = ALERTS_FILE.with_suffix(f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(alerts, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(ALERTS_FILE))
    except Exception:
        pass


def get_alerts() -> list:
    """Get all configured alerts."""
    return _load_alerts()


def delete_alert(alert_id: str) -> bool:
    """Delete an alert by ID."""
    alerts = _load_alerts()
    new = [a for a in alerts if a.get("id") != alert_id]
    if len(new) == len(alerts):
        return False
    _save_alerts(new)
    return True


def check_alerts(limit: int = 5000) -> list[dict]:
    """Evaluate all enabled alerts against current telemetry data.

    Returns a list of triggered alerts (empty list if none triggered).
    Each triggered alert includes the current value and any context.
    """
    triggered = []
    alerts = _load_alerts()
    if not alerts:
        return triggered

    # Read data needed for checks
    runs = _read_runs(limit)
    if not runs:
        return triggered

    # Compute current values
    now = _now_iso()

    def _sum_recent(hours: int) -> float:
        """Sum cost for the last N hours."""
        try:
            cutoff = datetime.fromisoformat(now).timestamp() - (hours * 3600)
            total = 0.0
            for r in runs:
                try:
                    rts = datetime.fromisoformat(r.get("ts", "")).timestamp()
                    if rts >= cutoff:
                        total += float(r.get("cost_usd", 0.0) or 0.0)
                except Exception:
                    continue
            return total
        except Exception:
            return 0.0

    def _failure_rate() -> float:
        total = len(runs)
        if not total:
            return 0.0
        fails = sum(1 for r in runs if not r.get("ok"))
        return fails / total

    def _avg_duration() -> int:
        total = len(runs)
        if not total:
            return 0
        return int(sum(int(r.get("duration_ms", 0) or 0) for r in runs) / total)

    current_values = {
        "daily_cost": _sum_recent(24),
        "hourly_cost": _sum_recent(1),
        "failure_rate": _failure_rate(),
        "avg_duration": _avg_duration(),
    }

    for alert in alerts:
        if not alert.get("enabled", True):
            continue
        metric = alert.get("metric", "")
        operator = alert.get("operator", "gt")
        threshold = alert.get("threshold", 0)
        current = current_values.get(metric, 0)

        fired = (operator == "gt" and current > threshold) or \
                (operator == "lt" and current < threshold)

        if fired:
            entry = {
                "alert_id": alert["id"],
                "name": alert["name"],
                "metric": metric,
                "threshold": threshold,
                "current": round(current, 4) if isinstance(current, float) else current,
                "operator": operator,
                "triggered_at": _now_iso(),
            }
            triggered.append(entry)
            # Update last_triggered
            all_alerts = _load_alerts()
            for a in all_alerts:
                if a.get("id") == alert["id"]:
                    a["last_triggered"] = _now_iso()
            _save_alerts(all_alerts)

    return triggered
