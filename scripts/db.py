"""MoiraiCore DB — Persistence Layer (Feature 10, July 2026).

Provides atomic JSON writes and a SQLite store for the core data models
(goals, tasks, kanban). Replaces the non-atomic `write_text()` calls in
server.py with safe atomic writes (temp file + os.replace).

Usage:
    from db import atomic_save_json, init_db, get_goals, save_goals

    # Atomic JSON write (safe even on crash)
    atomic_save_json(path, data)

    # SQLite store (optional, degrades gracefully)
    init_db()
    save_goals([{"id": "g1", "title": "Build feature"}])
    goals = get_goals()
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


# ── Paths ──
AGENT_OS_ROOT = Path(os.environ.get("AGENT_OS_ROOT", str(Path(__file__).resolve().parents[1])))
DB_DIR = AGENT_OS_ROOT / "config"
DB_PATH = DB_DIR / "moiraicore.db"
TASKS_FILE = DB_DIR / "tasks.json"
GOALS_FILE = DB_DIR / "goals.json"
KANBAN_FILE = DB_DIR / "kanban.json"

# ── Thread-local SQLite connection ──
_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    """Get a thread-local SQLite connection."""
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(str(DB_PATH))
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA synchronous=NORMAL")
    return _local.conn


# ── Atomic JSON I/O ──


def atomic_save_json(path: Path, data: Any, indent: int = 2) -> None:
    """Write JSON to a file atomically using temp file + os.replace.

    This prevents data corruption if the process crashes mid-write.
    Falls back to direct write if os.replace is unavailable (e.g. cross-device).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp.{os.getpid()}")
    try:
        tmp.write_text(json.dumps(data, indent=indent, default=str), encoding="utf-8")
        os.replace(str(tmp), str(path))
    except Exception:
        # Fallback: direct write
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        path.write_text(json.dumps(data, indent=indent, default=str), encoding="utf-8")


def atomic_load_json(path: Path, default: Any = None) -> Any:
    """Load JSON from a file with graceful fallback on corruption."""
    if not path.exists():
        return default if default is not None else ([] if str(path).endswith("tasks.json") else {})
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data
    except (json.JSONDecodeError, ValueError):
        # File is corrupt — try to recover from backup (.tmp files)
        tmp = path.with_suffix(f".tmp.*")
        import glob
        backups = sorted(glob.glob(str(tmp)), key=os.path.getmtime, reverse=True)
        for backup_path in backups:
            try:
                return json.loads(Path(backup_path).read_text(encoding="utf-8"))
            except Exception:
                continue
        return default if default is not None else ([] if str(path).endswith("tasks.json") else {})


# ── SQLite Schema ──


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS goals (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    status TEXT DEFAULT 'active',
    priority INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    metadata TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    goal_id TEXT,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    status TEXT DEFAULT 'backlog',
    tier INTEGER DEFAULT 2,
    priority INTEGER DEFAULT 0,
    agent TEXT DEFAULT '',
    output TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    metadata TEXT DEFAULT '{}',
    FOREIGN KEY (goal_id) REFERENCES goals(id)
);

CREATE INDEX IF NOT EXISTS idx_tasks_goal_id ON tasks(goal_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_goals_status ON goals(status);

CREATE TABLE IF NOT EXISTS kanban (
    id TEXT PRIMARY KEY,
    lane TEXT NOT NULL DEFAULT 'backlog',
    task_id TEXT,
    goal_id TEXT,
    position INTEGER DEFAULT 0,
    title TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_kanban_lane ON kanban(lane);
CREATE INDEX IF NOT EXISTS idx_kanban_task_id ON kanban(task_id);
"""


def init_db() -> bool:
    """Initialize the SQLite database with schema.

    Returns True if SQLite is available, False if it should fall back to JSON.
    """
    try:
        DB_DIR.mkdir(parents=True, exist_ok=True)
        conn = _get_conn()
        conn.executescript(SCHEMA_SQL)
        conn.commit()

        # Check/update schema version
        cur = conn.execute("SELECT MAX(version) FROM schema_version")
        row = cur.fetchone()
        current_version = row[0] if row and row[0] else 0

        if current_version < 1:
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (1, datetime.utcnow().isoformat()),
            )
            conn.commit()
        return True
    except Exception:
        return False


# ── Goals CRUD ──


def get_goals() -> list[dict]:
    """Get all goals from SQLite, falling back to JSON file."""
    if not _sqlite_available():
        return atomic_load_json(GOALS_FILE, [])
    try:
        conn = _get_conn()
        cur = conn.execute(
            "SELECT id, title, description, status, priority, "
            "created_at, updated_at, metadata FROM goals ORDER BY priority"
        )
        goals = []
        for row in cur.fetchall():
            g = dict(row)
            try:
                g["metadata"] = json.loads(g.get("metadata", "{}"))
            except Exception:
                g["metadata"] = {}
            goals.append(g)
        return goals
    except Exception:
        return atomic_load_json(GOALS_FILE, [])


def save_goals(goals: list[dict]) -> None:
    """Save goals to SQLite (with JSON file fallback)."""
    if not _sqlite_available():
        atomic_save_json(GOALS_FILE, goals)
        return
    try:
        conn = _get_conn()
        conn.execute("DELETE FROM goals")
        for g in goals:
            meta = json.dumps(g.get("metadata", {}), default=str)
            conn.execute(
                """INSERT OR REPLACE INTO goals
                   (id, title, description, status, priority, created_at, updated_at, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    g.get("id", ""),
                    g.get("title", ""),
                    g.get("description", ""),
                    g.get("status", "active"),
                    g.get("priority", 0),
                    g.get("created_at", datetime.utcnow().isoformat()),
                    datetime.utcnow().isoformat(),
                    meta,
                ),
            )
        conn.commit()
    except Exception:
        atomic_save_json(GOALS_FILE, goals)


# ── Tasks CRUD ──


def get_tasks() -> list[dict]:
    """Get all tasks from SQLite, falling back to JSON file."""
    if not _sqlite_available():
        return atomic_load_json(TASKS_FILE, [])
    try:
        conn = _get_conn()
        cur = conn.execute(
            "SELECT id, goal_id, title, description, status, tier, priority, "
            "agent, output, created_at, updated_at, metadata "
            "FROM tasks ORDER BY priority"
        )
        tasks = []
        for row in cur.fetchall():
            t = dict(row)
            try:
                t["metadata"] = json.loads(t.get("metadata", "{}"))
            except Exception:
                t["metadata"] = {}
            tasks.append(t)
        return tasks
    except Exception:
        return atomic_load_json(TASKS_FILE, [])


def save_tasks(tasks: list[dict]) -> None:
    """Save tasks to SQLite (with JSON file fallback)."""
    if not _sqlite_available():
        atomic_save_json(TASKS_FILE, tasks)
        return
    try:
        conn = _get_conn()
        conn.execute("DELETE FROM tasks")
        for t in tasks:
            meta = json.dumps(t.get("metadata", {}), default=str)
            conn.execute(
                """INSERT OR REPLACE INTO tasks
                   (id, goal_id, title, description, status, tier, priority,
                    agent, output, created_at, updated_at, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    t.get("id", ""),
                    t.get("goal_id", ""),
                    t.get("title", ""),
                    t.get("description", ""),
                    t.get("status", "backlog"),
                    t.get("tier", 2),
                    t.get("priority", 0),
                    t.get("agent", ""),
                    t.get("output", ""),
                    t.get("created_at", datetime.utcnow().isoformat()),
                    datetime.utcnow().isoformat(),
                    meta,
                ),
            )
        conn.commit()
    except Exception:
        atomic_save_json(TASKS_FILE, tasks)


# ── Kanban CRUD ──


def get_kanban() -> dict[str, list]:
    """Get kanban board from SQLite, falling back to JSON file."""
    if not _sqlite_available():
        return atomic_load_json(KANBAN_FILE, {"backlog": [], "progress": [], "review": [], "done": []})
    try:
        conn = _get_conn()
        cur = conn.execute(
            "SELECT id, lane, task_id, goal_id, position, title, created_at "
            "FROM kanban ORDER BY position"
        )
        lanes = {"backlog": [], "progress": [], "review": [], "done": []}
        for row in cur.fetchall():
            r = dict(row)
            lane = r.get("lane", "backlog")
            if lane not in lanes:
                lanes[lane] = []
            lanes[lane].append(r)
        return lanes
    except Exception:
        return atomic_load_json(KANBAN_FILE, {"backlog": [], "progress": [], "review": [], "done": []})


def save_kanban(board: dict[str, list]) -> None:
    """Save kanban board to SQLite (with JSON file fallback)."""
    if not _sqlite_available():
        atomic_save_json(KANBAN_FILE, board)
        return
    try:
        conn = _get_conn()
        conn.execute("DELETE FROM kanban")
        for lane, items in board.items():
            for pos, item in enumerate(items):
                task_id = item.get("task_id", "") if isinstance(item, dict) else ""
                goal_id = item.get("goal_id", "") if isinstance(item, dict) else ""
                title = item.get("title", "") if isinstance(item, dict) else str(item)
                item_id = item.get("id", f"{lane}-{pos}") if isinstance(item, dict) else f"{lane}-{pos}"
                conn.execute(
                    """INSERT INTO kanban (id, lane, task_id, goal_id, position, title)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (item_id, lane, task_id, goal_id, pos, title),
                )
        conn.commit()
    except Exception:
        atomic_save_json(KANBAN_FILE, board)


# ── Query Helpers ──


def get_tasks_by_status(status: str) -> list[dict]:
    """Get tasks filtered by status."""
    if not _sqlite_available():
        return [t for t in get_tasks() if t.get("status") == status]
    try:
        conn = _get_conn()
        cur = conn.execute(
            "SELECT id, goal_id, title, status FROM tasks WHERE status = ? ORDER BY priority",
            (status,),
        )
        return [dict(row) for row in cur.fetchall()]
    except Exception:
        return [t for t in get_tasks() if t.get("status") == status]


def get_tasks_by_goal(goal_id: str) -> list[dict]:
    """Get tasks for a specific goal."""
    if not _sqlite_available():
        return [t for t in get_tasks() if t.get("goal_id") == goal_id]
    try:
        conn = _get_conn()
        cur = conn.execute(
            "SELECT id, goal_id, title, status FROM tasks WHERE goal_id = ? ORDER BY priority",
            (goal_id,),
        )
        return [dict(row) for row in cur.fetchall()]
    except Exception:
        return [t for t in get_tasks() if t.get("goal_id") == goal_id]


def get_task_count() -> dict[str, int]:
    """Get task counts by status."""
    if not _sqlite_available():
        tasks = get_tasks()
        counts = {}
        for t in tasks:
            s = t.get("status", "unknown")
            counts[s] = counts.get(s, 0) + 1
        return counts
    try:
        conn = _get_conn()
        cur = conn.execute("SELECT status, COUNT(*) as cnt FROM tasks GROUP BY status")
        return {row["status"]: row["cnt"] for row in cur.fetchall()}
    except Exception:
        return {}


# ── Internal Helpers ──


def _sqlite_available() -> bool:
    """Check if SQLite is initialized and working."""
    try:
        conn = _get_conn()
        conn.execute("SELECT 1")
        return True
    except Exception:
        return False


def migrate_from_json() -> dict[str, Any]:
    """One-time migration: load existing JSON files into SQLite.

    Returns a summary of what was migrated.
    """
    summary = {"goals": 0, "tasks": 0, "kanban": 0}
    try:
        init_db()

        # Migrate goals
        goals = atomic_load_json(GOALS_FILE, [])
        if goals:
            save_goals(goals)
            summary["goals"] = len(goals)

        # Migrate tasks
        tasks = atomic_load_json(TASKS_FILE, [])
        if tasks:
            save_tasks(tasks)
            summary["tasks"] = len(tasks)

        # Migrate kanban
        kanban = atomic_load_json(KANBAN_FILE, {"backlog": [], "progress": [], "review": [], "done": []})
        if kanban and any(kanban.values()):
            save_kanban(kanban)
            summary["kanban"] = sum(len(v) for v in kanban.values())

        return summary
    except Exception as e:
        return {"error": str(e), **summary}