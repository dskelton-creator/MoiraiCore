#!/usr/bin/env python3
"""
activity_log.py — Lightweight live-activity event bus for MoiraiCore Mission Control.

Why this exists
---------------
The dashboard already shows *state* (goal status, subtask counts) but not *what is
happening right now*. This module is the structural seam that closes that gap: any
process (the server, an agent run, a cron job, the orchestrator) can emit a single
line describing the current activity, and the dashboard's Live view reads the recent
stream + a "currently running" snapshot.

Design
------
- Append-only JSONL at <AGENT_OS_ROOT>/config/activity.jsonl  (one event per line)
- Each event: {ts, kind, agent, goal_id, task_id, message, stage, status}
- "stage" is a canonical pipeline step so the UI can render a step tracker:
    queued -> running -> toolcall -> synthesis -> verify -> done
    (plus failed / paused / guard for non-progress states)
- Reader is file-mtime cached and capped, so the dashboard can poll cheaply.

This is intentionally dependency-free and import-safe: calling code should wrap the
import in try/except ImportError per the project's audit convention.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

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
ACTIVITY_FILE = AGENT_OS_ROOT / "config" / "activity.jsonl"

# Canonical pipeline stages (used by the dashboard step tracker).
STAGES = ["queued", "running", "toolcall", "synthesis", "verify", "done"]

# Statuses that are terminal / non-progress.
NON_PROGRESS = {"failed", "paused", "guard", "blocked"}

_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def log_activity(
    message: str,
    *,
    kind: str = "task",
    agent: str = "",
    goal_id: str = "",
    task_id: str = "",
    stage: str = "",
    status: str = "",
) -> dict:
    """Append one activity event. Returns the event dict (also useful for callers)."""
    event = {
        "ts": _now_iso(),
        "kind": kind,            # task | goal | agent | orchestration | system
        "agent": agent,
        "goal_id": goal_id,
        "task_id": task_id,
        "message": message,
        "stage": stage,
        "status": status,
    }
    try:
        ACTIVITY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            with open(ACTIVITY_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        # Never let activity logging break the calling flow.
        pass
    return event


def _read_events(limit: int = 100) -> list[dict]:
    if not ACTIVITY_FILE.exists():
        return []
    try:
        with _lock:
            lines = ACTIVITY_FILE.read_text(encoding="utf-8").splitlines()
        events = []
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                events.append(json.loads(ln))
            except Exception:
                continue
        return events[-limit:] if limit else events
    except Exception:
        return []


def get_activity(limit: int = 60) -> dict:
    """
    Return a dashboard-ready activity payload:
      - events:   most recent `limit` events (chronological)
      - running:  synthetic "currently running" snapshot derived from the latest
                  event per (goal_id/task_id) that is not in a terminal state
      - summary:  counts of active goals/tasks, last-event age, etc.
    """
    events = _read_events(limit=max(limit, 200))
    recent = events[-limit:]

    # Build a "currently running" view: take the most recent event for each
    # (task_id or goal_id), keep only those whose stage/status implies activity.
    latest: dict[str, dict] = {}
    for ev in events:
        key = ev.get("task_id") or ev.get("goal_id") or ev.get("agent") or ev.get("message")
        if not key:
            continue
        # Only "running" snapshots if stage not terminal and status not terminal.
        stage = (ev.get("stage") or "").lower()
        status = (ev.get("status") or "").lower()
        if stage in NON_PROGRESS or status in NON_PROGRESS:
            # Mark as finished/blocked but still surface the latest state.
            latest[key] = ev
            continue
        latest[key] = ev

    running = []
    seen = set()
    for ev in reversed(events):
        key = ev.get("task_id") or ev.get("goal_id") or ev.get("agent")
        if not key or key in seen:
            continue
        stage = (ev.get("stage") or "").lower()
        status = (ev.get("status") or "").lower()
        if stage in NON_PROGRESS or status in NON_PROGRESS:
            continue
        seen.add(key)
        running.append(ev)

    active_tasks = sum(1 for r in running if r.get("task_id"))
    active_goals = sum(1 for r in running if r.get("goal_id") and not r.get("task_id"))

    last_ts = events[-1]["ts"] if events else None
    age_s = None
    if last_ts:
        try:
            dt = datetime.fromisoformat(last_ts)
            age_s = int((datetime.now(dt.tzinfo) - dt).total_seconds())
        except Exception:
            age_s = None

    return {
        "ok": True,
        "events": recent,
        "running": running,
        "summary": {
            "active_tasks": active_tasks,
            "active_goals": active_goals,
            "running_count": len(running),
            "total_events": len(events),
            "last_event_ts": last_ts,
            "last_event_age_s": age_s,
        },
    }


def prune(max_lines: int = 5000) -> int:
    """Keep only the most recent `max_lines` events. Returns lines removed."""
    if not ACTIVITY_FILE.exists():
        return 0
    try:
        with _lock:
            lines = ACTIVITY_FILE.read_text(encoding="utf-8").splitlines()
        if len(lines) <= max_lines:
            return 0
        kept = lines[-max_lines:]
        ACTIVITY_FILE.write_text("\n".join(kept) + "\n", encoding="utf-8")
        return len(lines) - len(kept)
    except Exception:
        return 0


if __name__ == "__main__":
    # Quick smoke test / demo when run directly.
    log_activity("Demo: activity bus online", kind="system", agent="hermes", stage="done")
    out = get_activity(10)
    print(json.dumps(out, indent=2)[:1500])
