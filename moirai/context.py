"""
Context Engine — Assembles real-time context for Moirai prompts.

Gathers signals from:
  - Time of day, day of week
  - Recent MoiraiCore activity (goals, tasks)
  - User memory (facts, preferences)
  - Active session history
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from moirai.memory import ConversationMemory


class ContextEngine:
    """Builds rich context blocks for LLM prompts."""

    def __init__(self, memory: ConversationMemory):
        self.memory = memory

    def build_system_context(
        self,
        session_id: str,
        user_id: str = "default",
        personality_prompt: str = "",
    ) -> str:
        """Build the full system-level context for a conversation turn."""
        parts = []

        # Personality
        if personality_prompt:
            parts.append(personality_prompt)

        # Temporal context
        now = datetime.now()
        time_desc = self._describe_time(now)
        parts.append(f"[Current Time]\n{time_desc}")

        # User memory + conversation history
        memory_ctx = self.memory.build_context(session_id, user_id)
        if memory_ctx:
            parts.append(memory_ctx)

        return "\n\n".join(parts)

    def build_user_context(
        self,
        session_id: str,
        user_id: str = "default",
    ) -> str:
        """Build user-specific context only (shorter, for mid-conversation)."""
        parts = []
        memory_ctx = self.memory.build_context(session_id, user_id)
        if memory_ctx:
            parts.append(memory_ctx)
        return "\n\n".join(parts) if parts else ""

    def _describe_time(self, dt: datetime) -> str:
        """Generate a natural language time description."""
        hour = dt.hour
        day_name = dt.strftime("%A")
        date_str = dt.strftime("%B %d, %Y")

        if hour < 6:
            period = "late night"
        elif hour < 12:
            period = "morning"
        elif hour < 17:
            period = "afternoon"
        elif hour < 21:
            period = "evening"
        else:
            period = "night"

        return f"{day_name}, {date_str}, {period} ({dt.strftime('%H:%M')})"

    def summarize_recent_activity(self, session_id: str, limit: int = 5) -> str:
        """Summarize recent messages as activity context."""
        messages = self.memory.get_last_n_messages(session_id, limit)
        if not messages:
            return ""
        lines = []
        for m in messages:
            role = m["role"].capitalize()
            content = m["content"][:150]
            lines.append(f"{role}: {content}")
        return "[Recent Messages]\n" + "\n".join(lines)
