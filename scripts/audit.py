#!/usr/bin/env python3
"""
MoiraiCore — Audit Logging
Immutable audit trail for all security-relevant actions.

Usage:
    from audit import audit_log
    audit_log("auth.login", user="<username>", status="success")
    audit_log("file.write", path="/path/to/file", task_id="abc123", status="success")

Storage:
    memory-vault/audit/YYYY-MM-DD.jsonl (daily rotated)
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

AGENT_OS_ROOT = Path(os.environ.get("AGENT_OS_ROOT", str(Path(__file__).resolve().parents[1])))
AUDIT_DIR = AGENT_OS_ROOT / "memory-vault" / "audit"


def _get_audit_file() -> Path:
    """Get today's audit log file path."""
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    return AUDIT_DIR / f"{today}.jsonl"


def audit_log(
    action: str,
    user: Optional[str] = None,
    task_id: Optional[str] = None,
    path: Optional[str] = None,
    status: str = "success",
    details: Optional[dict] = None,
    agent: Optional[str] = None,
    goal_id: Optional[str] = None,
    cost_usd: Optional[float] = None,
    latency_ms: Optional[int] = None,
) -> str:
    """Log an audit event.

    Args:
        action: Dot-notation action (e.g., "auth.login", "file.write", "task.assign")
        user: Username or ID performing the action
        task_id: Related task ID if applicable
        path: File path if applicable
        status: "success" or "failure"
        details: Additional context (redacted for PII)
        agent: Agent/component that performed the action (e.g. "researcher", "scrumgate")
        goal_id: Related goal ID if applicable
        cost_usd: Estimated cost of the action in USD (telemetry-linked)
        latency_ms: Duration of the action in milliseconds (telemetry-linked)

    Returns:
        Event ID (UUID)
    """
    event_id = f"audit-{datetime.now().strftime('%Y%m%d%H%M%S')}-{os.urandom(3).hex()[:6]}"
    
    event = {
        "id": event_id,
        "timestamp": datetime.now().isoformat(),
        "action": action,
        "user": user,
        "task_id": task_id,
        "path": path,
        "status": status,
        "agent": agent,
        "goal_id": goal_id,
        "cost_usd": cost_usd,
        "latency_ms": latency_ms,
        "details": details or {},
    }
    
    audit_file = _get_audit_file()
    with open(audit_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")
    
    return event_id


def get_audit_events(
    action: Optional[str] = None,
    user: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    since_days: int = 3,
) -> list[dict]:
    """Query audit events.

    Args:
        action: Filter by action prefix
        user: Filter by user
        limit: Max events to return
        offset: Skip N events (for pagination)
        since_days: Only include events from the last N days (default 3).
                    Pass 0 or a negative value to disable the time window.

    Returns:
        List of audit events (most recent first)
    """
    from datetime import timedelta

    cutoff = None
    cutoff_date_str = None
    if since_days and since_days > 0:
        cutoff = datetime.now() - timedelta(days=since_days)
        cutoff_date_str = cutoff.strftime("%Y-%m-%d")

    events = []
    audit_files = sorted(AUDIT_DIR.glob("*.jsonl"), reverse=True)

    for audit_file in audit_files:
        # Skip whole daily files older than the window (filename is YYYY-MM-DD).
        if cutoff_date_str and audit_file.stem < cutoff_date_str:
            continue
        try:
            with open(audit_file, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        try:
                            event = json.loads(line)
                            # Apply filters
                            if action and not event["action"].startswith(action):
                                continue
                            if user and event["user"] != user:
                                continue
                            # Time-window filter (per-event, for same-day precision)
                            if cutoff is not None:
                                ts = event.get("timestamp", "")
                                try:
                                    if datetime.fromisoformat(ts) < cutoff:
                                        continue
                                except (ValueError, TypeError):
                                    pass  # keep events with unparseable timestamps
                            events.append(event)
                        except json.JSONDecodeError:
                            continue
        except FileNotFoundError:
            continue

    # Sort by timestamp descending
    events.sort(key=lambda e: e["timestamp"], reverse=True)

    return events[offset:offset + limit]


def get_audit_stats() -> dict:
    """Get audit statistics."""
    events = get_audit_events(limit=10000)
    
    total = len(events)
    by_status = {"success": 0, "failure": 0}
    by_action = {}
    
    for e in events:
        status = e.get("status", "unknown")
        by_status[status] = by_status.get(status, 0) + 1
        
        action = e.get("action", "unknown")
        by_action[action] = by_action.get(action, 0) + 1
    
    return {
        "total_events": total,
        "by_status": by_status,
        "by_action": by_action,
        "audit_dir": str(AUDIT_DIR),
    }


if __name__ == "__main__":
    # CLI for testing
    import sys
    print(f"Audit dir: {AUDIT_DIR}")
    stats = get_audit_stats()
    print(f"Total events: {stats['total_events']}")
    print(f"By action: {json.dumps(stats['by_action'], indent=2)}")