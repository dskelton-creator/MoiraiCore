"""
Conversation Memory Layer — SQLite-backed session + long-term memory.

Stores:
  - Conversation history per session
  - User preferences and facts across sessions
  - Interaction patterns for proactive suggestions
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from jarvis import CONVERSATIONS_DB


class ConversationMemory:
    """Manages conversation sessions and long-term user memory."""

    def __init__(self, db_path: Path = CONVERSATIONS_DB):
        self.db_path = db_path
        self._init_db()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self):
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL DEFAULT 'default',
                    title TEXT,
                    started_at REAL NOT NULL,
                    ended_at REAL,
                    message_count INTEGER DEFAULT 0,
                    metadata TEXT DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    metadata TEXT DEFAULT '{}',
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                );

                CREATE TABLE IF NOT EXISTS user_memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL DEFAULT 'default',
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    source TEXT DEFAULT 'conversation',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(user_id, key)
                );

                CREATE TABLE IF NOT EXISTS user_preferences (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL DEFAULT 'default',
                    category TEXT NOT NULL,
                    preference TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE(user_id, category)
                );

                CREATE INDEX IF NOT EXISTS idx_messages_session
                    ON messages(session_id, timestamp);
                CREATE INDEX IF NOT EXISTS idx_user_memory
                    ON user_memory(user_id);
            """)

    # ── Session Management ──

    def create_session(self, user_id: str = "default", title: str = None) -> str:
        """Create a new conversation session."""
        import uuid
        session_id = f"sess-{uuid.uuid4().hex[:12]}"
        if not title:
            title = f"Conversation {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sessions (id, user_id, title, started_at) VALUES (?,?,?,?)",
                (session_id, user_id, title, time.time()),
            )
        return session_id

    def get_session(self, session_id: str) -> Optional[dict]:
        """Get session metadata."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row:
                return dict(row)
        return None

    def get_recent_sessions(self, user_id: str = "default", limit: int = 10) -> list[dict]:
        """Get recent sessions for a user."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM sessions
                   WHERE user_id = ?
                   ORDER BY started_at DESC
                   LIMIT ?""",
                (user_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def end_session(self, session_id: str):
        """Mark a session as ended."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET ended_at = ? WHERE id = ?",
                (time.time(), session_id),
            )

    # ── Message Management ──

    def add_message(self, session_id: str, role: str, content: str, metadata: dict = None):
        """Add a message to a session."""
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO messages (session_id, role, content, timestamp, metadata)
                   VALUES (?,?,?,?,?)""",
                (session_id, role, content, time.time(), json.dumps(metadata or {})),
            )
            # Update message count
            conn.execute(
                """UPDATE sessions SET message_count = message_count + 1
                   WHERE id = ?""",
                (session_id,),
            )

    def get_conversation(self, session_id: str, limit: int = None) -> list[dict]:
        """Get the full conversation history for a session."""
        with self._connect() as conn:
            query = """SELECT role, content, timestamp, metadata
                       FROM messages WHERE session_id = ?
                       ORDER BY timestamp ASC"""
            params: list[Any] = [session_id]
            if limit:
                query += " LIMIT ?"
                params.append(limit)
            rows = conn.execute(query, params).fetchall()
            return [
                {
                    "role": r["role"],
                    "content": r["content"],
                    "timestamp": r["timestamp"],
                    "metadata": json.loads(r["metadata"]),
                }
                for r in rows
            ]

    def get_last_n_messages(self, session_id: str, n: int = 5) -> list[dict]:
        """Get the last N messages (for context window)."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT role, content, timestamp, metadata
                   FROM messages WHERE session_id = ?
                   ORDER BY timestamp DESC
                   LIMIT ?""",
                (session_id, n),
            ).fetchall()
            return [
                {
                    "role": r["role"],
                    "content": r["content"],
                    "timestamp": r["timestamp"],
                    "metadata": json.loads(r["metadata"]),
                }
                for r in reversed(rows)
            ]

    # ── Long-Term User Memory ──

    def remember(self, key: str, value: str, user_id: str = "default", source: str = "conversation") -> None:
        """Store a fact about the user."""
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO user_memory
                   (user_id, key, value, source, created_at, updated_at)
                   VALUES (?,?,?,?,?,?)""",
                (user_id, key, value, source, now, now),
            )

    def recall(self, key: str, user_id: str = "default") -> Optional[str]:
        """Recall a stored fact."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM user_memory WHERE user_id = ? AND key = ?",
                (user_id, key),
            ).fetchone()
            return row["value"] if row else None

    def recall_all(self, user_id: str = "default") -> dict:
        """Get all stored facts for a user."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT key, value FROM user_memory WHERE user_id = ?",
                (user_id,),
            ).fetchall()
            return {r["key"]: r["value"] for r in rows}

    def forget(self, key: str, user_id: str = "default"):
        """Remove a stored fact."""
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM user_memory WHERE user_id = ? AND key = ?",
                (user_id, key),
            )

    # ── Context Assembly ──

    def build_context(self, session_id: str, user_id: str = "default") -> str:
        """Build a context block from memory for injection into prompts."""
        parts = []

        # Long-term memory
        facts = self.recall_all(user_id)
        if facts:
            fact_lines = [f"- {k}: {v}" for k, v in facts.items()]
            parts.append("[User Facts]\n" + "\n".join(fact_lines))

        # Recent conversation history
        history = self.get_last_n_messages(session_id, 10)
        if history:
            history_lines = [
                f"[{m['role']}]: {m['content'][:200]}"
                for m in history
            ]
            parts.append("[Recent Conversation]\n" + "\n".join(history_lines))

        return "\n\n".join(parts) if parts else ""
