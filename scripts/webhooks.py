#!/usr/bin/env python3
"""MoiraiCore — Webhooks / Event Triggers (Feature 8 Enhanced, July 2026).

Feature 8 enhancements (Add-on to original webhook system):
  - Dead-letter queue: failed deliveries stored for manual replay
  - Payload validation: webhooks can define JSON Schema for payload validation
  - Selective event filtering: filter by payload properties (status, agent, etc.)
  - Rate limiting: per-webhook max deliveries per minute
  - Webhook update: update webhook config without delete/recreate
  - Dead-letter replay: replay individual or all dead-letter entries

See the original docstring (below) for base architecture.
"""

import os
import json
import time
import uuid
import hmac
import hashlib
import threading
from pathlib import Path
from typing import Optional, Any
from collections import defaultdict
from datetime import datetime, timezone

AGENT_OS_ROOT = Path(os.environ.get("AGENT_OS_ROOT", str(Path(__file__).resolve().parents[1])))
CONFIG_DIR = AGENT_OS_ROOT / "config"
WEBHOOKS_FILE = CONFIG_DIR / "webhooks.json"
DELIVERIES_LOG = CONFIG_DIR / "webhook_deliveries.jsonl"
DEAD_LETTER_FILE = CONFIG_DIR / "webhook_dead_letter.jsonl"

# ── Event catalogue ──────────────────────────────────────────────────────────
EVENT_TYPES = [
    ("goal.created",        "A new goal was decomposed and registered"),
    ("goal.completed",      "A goal finished all tasks successfully"),
    ("goal.failed",         "A goal failed or hit a contract error"),
    ("task.executed",       "An agent finished executing a task"),
    ("task.failed",         "A task execution failed"),
    ("merge.queued",        "Generated code was submitted to the merge queue"),
    ("merge.merged",        "A merge request was approved and merged"),
    ("merge.rejected",      "A merge request was rejected by the ScrumMaster"),
    ("verification.completed", "GoalVerifier finished judging a task output"),
    ("contract.violation",  "A PIPELINE CONTRACT was violated (structural gate)"),
    ("agent.registered",    "A new agent was registered in the Agent Registry"),
    ("agent.deleted",       "An agent was removed from the Agent Registry"),
    ("ping",                "Synthetic test event (no real source)"),
]

EVENT_NAMES = [e[0] for e in EVENT_TYPES]

try:
    from activity_log import log_activity as _log_activity
except Exception:
    _log_activity = None


# ── Registry persistence ──────────────────────────────────────────────────────
def _ensure_dirs():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def load_webhooks() -> list:
    try:
        if not WEBHOOKS_FILE.exists():
            return []
        data = json.loads(WEBHOOKS_FILE.read_text(encoding="utf-8") or "[]")
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_webhooks(hooks: list) -> None:
    _ensure_dirs()
    # Atomic write via temp file
    tmp = WEBHOOKS_FILE.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(hooks, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(WEBHOOKS_FILE))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── CRUD ───────────────────────────────────────────────────────────────────────
def create_webhook(name: str, url: str, events: list, secret: str = "",
                   active: bool = True, schema: dict = None,
                   filters: dict = None, max_rate: int = 0) -> dict:
    """Create a new webhook with optional schema validation, filtering, and rate limiting.

    Args:
        name: Human-readable name
        url: Target URL (must be http(s)://)
        events: List of event types to subscribe to
        secret: HMAC signing secret (optional)
        active: Whether the webhook is active
        schema: Optional JSON Schema dict for payload validation
        filters: Optional dict of payload property filters (e.g. {"status": ["completed", "failed"]})
        max_rate: Max deliveries per minute (0 = unlimited)

    Returns:
        The created webhook dict.
    """
    name = (name or "").strip()
    url = (url or "").strip()
    if not name:
        raise ValueError("webhook 'name' is required")
    if not url or not url.startswith(("http://", "https://")):
        raise ValueError("webhook 'url' must be an http(s) URL")
    events = [e for e in (events or []) if e in EVENT_NAMES]
    if not events:
        raise ValueError("webhook must subscribe to at least one valid event")
    if filters and not isinstance(filters, dict):
        raise ValueError("webhook 'filters' must be a dict")
    if schema and not isinstance(schema, dict):
        raise ValueError("webhook 'schema' must be a dict")
    if max_rate and (not isinstance(max_rate, int) or max_rate < 0):
        raise ValueError("webhook 'max_rate' must be a non-negative integer")

    hook = {
        "id": "wh-" + uuid.uuid4().hex[:12],
        "name": name,
        "url": url,
        "secret": secret or "",
        "events": events,
        "active": bool(active),
        "schema": schema or {},
        "filters": filters or {},
        "max_rate": max_rate,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "last_status": None,
        "total_deliveries": 0,
        "successful_deliveries": 0,
        "failed_deliveries": 0,
    }
    hooks = load_webhooks()
    hooks.append(hook)
    save_webhooks(hooks)
    return hook


def update_webhook(webhook_id: str, **kwargs) -> Optional[dict]:
    """Update a webhook's configuration.

    Args:
        webhook_id: The webhook ID to update
        **kwargs: Fields to update (name, url, events, secret, active, schema, filters, max_rate)

    Returns:
        The updated webhook dict, or None if not found.
    """
    hooks = load_webhooks()
    for h in hooks:
        if h.get("id") == webhook_id:
            # Validate fields
            if "name" in kwargs:
                h["name"] = (kwargs["name"] or "").strip()
            if "url" in kwargs:
                url = (kwargs["url"] or "").strip()
                if not url.startswith(("http://", "https://")):
                    raise ValueError("'url' must be an http(s) URL")
                h["url"] = url
            if "events" in kwargs:
                events = [e for e in (kwargs["events"] or []) if e in EVENT_NAMES]
                if not events:
                    raise ValueError("must subscribe to at least one valid event")
                h["events"] = events
            if "secret" in kwargs:
                h["secret"] = kwargs["secret"] or ""
            if "active" in kwargs:
                h["active"] = bool(kwargs["active"])
            if "schema" in kwargs:
                if kwargs["schema"] and not isinstance(kwargs["schema"], dict):
                    raise ValueError("'schema' must be a dict")
                h["schema"] = kwargs["schema"] or {}
            if "filters" in kwargs:
                if kwargs["filters"] and not isinstance(kwargs["filters"], dict):
                    raise ValueError("'filters' must be a dict")
                h["filters"] = kwargs["filters"] or {}
            if "max_rate" in kwargs:
                if not isinstance(kwargs["max_rate"], int) or kwargs["max_rate"] < 0:
                    raise ValueError("'max_rate' must be a non-negative integer")
                h["max_rate"] = kwargs["max_rate"]
            h["updated_at"] = _now_iso()
            save_webhooks(hooks)
            return h
    return None


def get_webhook(webhook_id: str) -> Optional[dict]:
    for h in load_webhooks():
        if h.get("id") == webhook_id:
            return h
    return None


def delete_webhook(webhook_id: str) -> bool:
    hooks = load_webhooks()
    new = [h for h in hooks if h.get("id") != webhook_id]
    if len(new) == len(hooks):
        return False
    save_webhooks(new)
    return True


def set_active(webhook_id: str, active: bool) -> bool:
    hooks = load_webhooks()
    changed = False
    for h in hooks:
        if h.get("id") == webhook_id:
            h["active"] = bool(active)
            changed = True
    if changed:
        save_webhooks(hooks)
    return changed


# ── Payload Validation ─────────────────────────────────────────────────────────
def _validate_payload(payload: dict, schema: dict) -> tuple[bool, str]:
    """Validate a payload against a JSON Schema (lightweight implementation).

    Supports a subset of JSON Schema: type, required, properties, enum, pattern.
    Returns (is_valid, error_message).
    """
    if not schema or not isinstance(schema, dict):
        return True, ""

    # Check required fields
    required = schema.get("required", [])
    for field in required:
        if field not in payload:
            return False, f"Missing required field: '{field}'"

    # Check property types
    properties = schema.get("properties", {})
    for field, field_schema in properties.items():
        if field not in payload:
            continue
        value = payload[field]
        field_type = field_schema.get("type")

        if field_type == "string" and not isinstance(value, str):
            return False, f"Field '{field}' should be string, got {type(value).__name__}"
        if field_type == "integer" and not isinstance(value, int):
            return False, f"Field '{field}' should be integer, got {type(value).__name__}"
        if field_type == "boolean" and not isinstance(value, bool):
            return False, f"Field '{field}' should be boolean, got {type(value).__name__}"
        if field_type == "array" and not isinstance(value, list):
            return False, f"Field '{field}' should be array, got {type(value).__name__}"
        if field_type == "object" and not isinstance(value, dict):
            return False, f"Field '{field}' should be object, got {type(value).__name__}"

        # Check enum values
        enum_values = field_schema.get("enum", [])
        if enum_values and value not in enum_values:
            return False, f"Field '{field}' value '{value}' not in allowed values: {enum_values}"

        # Check pattern (string)
        if field_type == "string":
            pattern = field_schema.get("pattern", "")
            if pattern and not __import__("re").match(pattern, str(value)):
                return False, f"Field '{field}' does not match pattern '{pattern}'"

    return True, ""


# ── Event Filtering ────────────────────────────────────────────────────────────
def _matches_filter(payload: dict, filters: dict) -> bool:
    """Check if a payload matches a webhook's event filters.

    Filters are key-value pairs where the value is a list of acceptable values.
    All specified filters must match (AND logic).
    Example: {"status": ["completed", "failed"], "agent": ["developer"]}
    """
    if not filters:
        return True
    for key, allowed_values in filters.items():
        if not allowed_values:
            continue
        actual = payload.get(key)
        if actual is not None and actual not in allowed_values:
            return False
    return True


# ── Rate Limiting ──────────────────────────────────────────────────────────────
_rate_limit_tracker: dict[str, list[float]] = {}
_rate_limit_lock = threading.Lock()


def _check_rate_limit(webhook_id: str, max_rate: int) -> bool:
    """Check if a webhook is within its rate limit.

    Args:
        webhook_id: The webhook ID
        max_rate: Max deliveries per minute (0 = unlimited)

    Returns:
        True if the webhook can deliver, False if rate limited.
    """
    if max_rate <= 0:
        return True

    now = time.time()
    with _rate_limit_lock:
        if webhook_id not in _rate_limit_tracker:
            _rate_limit_tracker[webhook_id] = []

        # Remove timestamps older than 60 seconds
        cutoff = now - 60
        _rate_limit_tracker[webhook_id] = [
            ts for ts in _rate_limit_tracker[webhook_id] if ts > cutoff
        ]

        if len(_rate_limit_tracker[webhook_id]) >= max_rate:
            return False

        _rate_limit_tracker[webhook_id].append(now)
        return True


# ── Delivery log ───────────────────────────────────────────────────────────────
def _log_delivery(entry: dict) -> None:
    try:
        _ensure_dirs()
        with DELIVERIES_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def _log_dead_letter(entry: dict) -> None:
    """Write a dead-letter entry for failed deliveries."""
    try:
        _ensure_dirs()
        with DEAD_LETTER_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def get_deliveries(limit: int = 100) -> list:
    try:
        if not DELIVERIES_LOG.exists():
            return []
        lines = DELIVERIES_LOG.read_text(encoding="utf-8").splitlines()
        out = []
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except Exception:
                continue
        out.reverse()
        return out[:limit]
    except Exception:
        return []


def get_dead_letters(limit: int = 100) -> list:
    """Get dead-letter entries (failed deliveries after all retries)."""
    try:
        if not DEAD_LETTER_FILE.exists():
            return []
        lines = DEAD_LETTER_FILE.read_text(encoding="utf-8").splitlines()
        out = []
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except Exception:
                continue
        out.reverse()
        return out[:limit]
    except Exception:
        return []


def replay_dead_letter(dl_id: str) -> dict:
    """Replay a single dead-letter entry.

    Re-creates the delivery attempt and removes the dead letter on success.
    On failure, the dead letter remains.

    Returns:
        {"ok": True, "delivery": ...} or {"ok": False, "error": "..."}
    """
    try:
        if not DEAD_LETTER_FILE.exists():
            return {"ok": False, "error": "No dead letters"}

        lines = DEAD_LETTER_FILE.read_text(encoding="utf-8").splitlines()
        remaining = []
        target = None
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                entry = json.loads(ln)
                if entry.get("delivery_id") == dl_id and target is None:
                    target = entry
                else:
                    remaining.append(ln)
            except Exception:
                remaining.append(ln)

        if target is None:
            return {"ok": False, "error": f"Dead letter '{dl_id}' not found"}

        # Re-deliver
        result = _deliver_once(
            hook=target.get("hook", {}),
            event=target.get("event", "unknown"),
            payload=target.get("payload", {}),
        )
        if result.get("ok"):
            # Remove from dead letter file
            _ensure_dirs()
            DEAD_LETTER_FILE.write_text("\n".join(remaining) + "\n", encoding="utf-8")
            return {"ok": True, "delivery": result}
        return {"ok": False, "error": result.get("error", "Re-delivery failed"), "delivery": result}

    except Exception as e:
        return {"ok": False, "error": str(e)}


def replay_all_dead_letters() -> dict:
    """Replay all dead-letter entries.

    Returns summary of replay attempts.
    """
    try:
        if not DEAD_LETTER_FILE.exists():
            return {"ok": True, "total": 0, "replayed": 0, "failed": 0}

        lines = DEAD_LETTER_FILE.read_text(encoding="utf-8").splitlines()
        remaining = []
        replayed = 0
        failed = 0

        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                entry = json.loads(ln)
                result = _deliver_once(
                    hook=entry.get("hook", {}),
                    event=entry.get("event", "unknown"),
                    payload=entry.get("payload", {}),
                )
                if result.get("ok"):
                    replayed += 1
                else:
                    failed += 1
                    remaining.append(ln)
            except Exception:
                remaining.append(ln)

        # Write back only the still-failed ones
        _ensure_dirs()
        DEAD_LETTER_FILE.write_text("\n".join(remaining) + ("\n" if remaining else ""), encoding="utf-8")

        return {"ok": True, "total": replayed + failed, "replayed": replayed, "failed": failed}

    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Dispatch ───────────────────────────────────────────────────────────────────
def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _deliver_once(hook: dict, event: str, payload: dict) -> dict:
    """Perform one HTTP POST delivery. Returns a delivery-log entry. Never raises."""
    delivery_id = "dl-" + uuid.uuid4().hex[:12]
    body = json.dumps({
        "event": event,
        "delivery_id": delivery_id,
        "timestamp": _now_iso(),
        "data": payload,
    }, separators=(",", ":")).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "MoiraiCore-Webhooks/1.0",
        "X-MoiraiCore-Event": event,
        "X-MoiraiCore-Delivery": delivery_id,
    }
    if hook.get("secret"):
        headers["X-MoiraiCore-Signature"] = _sign(hook["secret"], body)

    entry = {
        "ts": _now_iso(),
        "delivery_id": delivery_id,
        "webhook_id": hook.get("id"),
        "name": hook.get("name"),
        "url": hook.get("url"),
        "event": event,
        "attempt": 1,
        "status_code": None,
        "ok": False,
        "error": None,
        "duration_ms": 0,
    }
    t0 = time.monotonic()
    try:
        import urllib.request
        import urllib.error
        req = urllib.request.Request(hook["url"], data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.getcode()
        entry["status_code"] = status
        entry["ok"] = 200 <= status < 300
    except Exception as e:
        entry["error"] = str(e)[:300]
    finally:
        entry["duration_ms"] = int((time.monotonic() - t0) * 1000)
        _log_delivery(entry)
        # Update hook stats
        try:
            hooks = load_webhooks()
            for h in hooks:
                if h.get("id") == hook.get("id"):
                    h["last_status"] = {
                        "ok": entry["ok"],
                        "status_code": entry["status_code"],
                        "error": entry["error"],
                        "ts": entry["ts"],
                    }
                    h["total_deliveries"] = (h.get("total_deliveries", 0) or 0) + 1
                    if entry["ok"]:
                        h["successful_deliveries"] = (h.get("successful_deliveries", 0) or 0) + 1
                    else:
                        h["failed_deliveries"] = (h.get("failed_deliveries", 0) or 0) + 1
            save_webhooks(hooks)
        except Exception:
            pass
    return entry


def _dispatch(hook: dict, event: str, payload: dict) -> None:
    """Thread worker: deliver with up to 3 attempts (exponential backoff).

    If all attempts fail, promotes to dead-letter queue.
    """
    last = None
    for attempt in range(1, 4):
        last = _deliver_once({**hook, "_attempt": attempt}, event, payload)
        last["attempt"] = attempt
        if last["ok"]:
            break
        if attempt < 3:
            time.sleep(2 ** (attempt - 1))  # 1s, 2s

    if not last or not last["ok"]:
        # Promote to dead-letter queue
        dl_entry = {
            "ts": _now_iso(),
            "delivery_id": last.get("delivery_id", "unknown") if last else "unknown",
            "webhook_id": hook.get("id"),
            "name": hook.get("name"),
            "url": hook.get("url"),
            "event": event,
            "payload": payload,
            "hook": {k: v for k, v in hook.items() if k != "secret"},
            "last_error": last.get("error") if last else "Unknown",
            "last_status_code": last.get("status_code") if last else None,
            "attempts": 3,
        }
        _log_dead_letter(dl_entry)

        if _log_activity is not None:
            try:
                _log_activity(
                    message=f"Webhook delivery failed (dead-letter): {hook.get('name')} <- {event}",
                    kind="integration",
                    agent=hook.get("name", "webhook"),
                    goal_id="", task_id="", stage=event,
                    status="failed",
                )
            except Exception:
                pass


def emit(event: str, payload: Optional[dict] = None) -> int:
    """Fire an event to every active webhook subscribed to it.

    Enhanced with:
    - Payload schema validation (rejects if payload doesn't match schema)
    - Event filtering (checks payload against filters)
    - Rate limiting (per-webhook max deliveries/minute)
    - Dead-letter queue (failed after all retries)

    Returns the number of webhooks targeted.
    """
    payload = payload or {}
    hooks = load_webhooks()
    targeted = 0
    for hook in hooks:
        if not hook.get("active", True):
            continue
        if event not in hook.get("events", []):
            continue

        # Schema validation
        schema = hook.get("schema", {})
        if schema:
            valid, error = _validate_payload(payload, schema)
            if not valid:
                # Log validation failure and skip
                _log_delivery({
                    "ts": _now_iso(),
                    "delivery_id": "vl-" + uuid.uuid4().hex[:12],
                    "webhook_id": hook.get("id"),
                    "name": hook.get("name"),
                    "url": hook.get("url"),
                    "event": event,
                    "attempt": 0,
                    "status_code": None,
                    "ok": False,
                    "error": f"Schema validation failed: {error}",
                    "duration_ms": 0,
                })
                continue

        # Event filtering
        filters = hook.get("filters", {})
        if filters and not _matches_filter(payload, filters):
            continue

        # Rate limiting
        max_rate = hook.get("max_rate", 0)
        if not _check_rate_limit(hook.get("id", ""), max_rate):
            continue

        targeted += 1
        t = threading.Thread(target=_dispatch, args=(hook, event, payload), daemon=True)
        t.start()
    return targeted


if __name__ == "__main__":
    # Smoke test
    print("EVENT_TYPES:", len(EVENT_TYPES))
    h = create_webhook("test", "https://example.com/hook", ["goal.created", "ping"],
                       secret="s3cr3t", schema={"type": "object", "required": ["goal_id"]})
    print("created:", h["id"], "events:", h["events"], "schema:", bool(h["schema"]))

    # Test schema validation
    n = emit("goal.created", {"goal_id": "g1"})  # Should pass
    print("emit with valid payload:", n, "targeted")
    n = emit("goal.created", {"bad": "data"})  # Should fail validation
    print("emit with invalid payload:", n, "targeted")

    print("deliveries:", len(get_deliveries()))
    print("dead letters:", len(get_dead_letters()))
    delete_webhook(h["id"])
    print("after delete:", len(load_webhooks()))