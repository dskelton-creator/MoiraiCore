"""
Moirai Engine — Orchestrates personality, memory, context, and Hermes
to produce intelligent, personality-driven responses.

This is the main entry point for conversational interactions.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from moirai.personality import Personality
from moirai.memory import ConversationMemory
from moirai.context import ContextEngine


class Moirai:
    """The Moirai conversational intelligence engine."""

    def __init__(
        self,
        personality_preset: str = "professional",
        personality_overrides: dict = None,
        user_id: str = "default",
        auto_remember: bool = True,
    ):
        self.personality = Personality(preset=personality_preset, overrides=personality_overrides)
        self.memory = ConversationMemory()
        self.context = ContextEngine(self.memory)
        self.user_id = user_id
        self.auto_remember = auto_remember
        self._session_id: Optional[str] = None

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    def start_session(self, title: str = None) -> str:
        """Start a new conversation session."""
        self._session_id = self.memory.create_session(
            user_id=self.user_id,
            title=title or "",
        )
        return self._session_id

    def resume_session(self, session_id: str) -> bool:
        """Resume an existing session."""
        session = self.memory.get_session(session_id)
        if session:
            self._session_id = session_id
            return True
        return False

    def send(self, message: str, route: bool = True) -> dict:
        """
        Send a message and get a response.

        Returns dict with: ok, response, session_id, duration_ms, routed_to
        """
        if not self._session_id:
            self.start_session()

        assert self._session_id is not None

        # Store user message
        self.memory.add_message(self._session_id, "user", message)

        # Build context
        system_ctx = self.context.build_system_context(
            self._session_id,
            self.user_id,
            personality_prompt=self.personality.system_prompt_fragment(),
        )

        # Assemble full prompt
        full_prompt = self._assemble_prompt(system_ctx, message)

        # Execute via Hermes bridge
        start = time.time()
        try:
            result = self._call_hermes(full_prompt, route=route)
            duration_ms = int((time.time() - start) * 1000)
        except Exception as e:
            return {
                "ok": False,
                "response": None,
                "error": str(e),
                "session_id": self._session_id,
                "duration_ms": int((time.time() - start) * 1000),
            }

        # Store response
        response = result.get("response", "")
        if response:
            self.memory.add_message(self._session_id, "assistant", response)

            # Auto-remember facts if enabled
            if self.auto_remember:
                self._auto_remember(message, response)

        return {
            "ok": result.get("ok", False),
            "response": response,
            "session_id": self._session_id,
            "routed_to": result.get("routed_to", "hermes"),
            "agent_name": result.get("agent_name", "Hermes"),
            "duration_ms": duration_ms,
            "error": result.get("error"),
        }

    def greet(self, user_name: str = None) -> str:
        """Generate a personality-appropriate greeting."""
        from datetime import datetime
        now = datetime.now()
        return self.personality.greeting(user_name, now.strftime("%H:%M"))

    def _assemble_prompt(self, system_ctx: str, user_message: str) -> str:
        """Build the full prompt for Hermes."""
        parts = []
        if system_ctx:
            parts.append(system_ctx)
        parts.append(f"[User]\n{user_message}")
        return "\n\n".join(parts)

    def _call_hermes(self, prompt: str, route: bool = True) -> dict:
        """Call Hermes bridge."""
        import os
        import sys
        import subprocess

        AGENT_OS_ROOT = Path(os.environ.get("AGENT_OS_ROOT", str(Path(__file__).resolve().parents[1])))
        bridge_path = AGENT_OS_ROOT / "scripts" / "hermes_bridge.py"

        if not bridge_path.exists():
            return {"ok": False, "error": "hermes_bridge.py not found", "response": None}

        args = [sys.executable, str(bridge_path), "ask", prompt, "--log"]
        if not route:
            args.remove("--log")

        # Bridge timeout is 600s; engine timeout must be >= bridge + overhead.
        TIMEOUT = 600

        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=TIMEOUT,
                env={**os.environ},
            )
        except subprocess.TimeoutExpired as te:
            # Hermes took too long — capture any partial output for diagnostics
            partial_stdout = ""
            partial_stderr = ""
            if te.stdout:
                partial_stdout = te.stdout[-2000:]
            if te.stderr:
                partial_stderr = te.stderr[-500:]
            # Try to extract a partial response from the truncated JSON stdout
            response = None
            error = f"Hermes timed out after {TIMEOUT}s"
            if partial_stdout:
                response = partial_stdout
            if partial_stderr:
                error += f" | stderr: {partial_stderr.strip()}"
            return {
                "ok": False,
                "response": response,
                "error": error,
                "routed_to": "hermes",
                "agent_name": "Hermes",
            }

        try:
            return json.loads(result.stdout)
        except Exception:
            return {
                "ok": result.returncode == 0,
                "response": result.stdout.strip()[-2000:] if result.stdout else None,
                "error": result.stderr.strip()[-500:] if result.stderr else None,
            }

    def _auto_remember(self, user_msg: str, assistant_msg: str):
        """Extract and store simple facts from conversation."""
        # Extract preference statements like "I prefer X" or "I like X"
        import re
        pref_patterns = [
            r"i (?:prefer|like|want|need)\s+(.+?)(?:\.|\!|$)",
            r"my favorite\s+(.+?)(?:\.|\!|$)",
            r"i'?m\s+(?:a\s+)?(.+?)(?:\.|\!|$)",
        ]
        for pattern in pref_patterns:
            match = re.search(pattern, user_msg, re.IGNORECASE)
            if match:
                value = match.group(1).strip()
                # Derive a key from the value
                key = value.lower()[:40].replace(" ", "_")
                self.memory.remember(key, value, user_id=self.user_id, source="auto")
                break

    def remember_fact(self, key: str, value: str) -> None:
        """Explicitly store a fact about the user."""
        self.memory.remember(key, value, user_id=self.user_id, source="explicit")

    def recall_fact(self, key: str) -> str:
        """Recall a stored fact."""
        return self.memory.recall(key, user_id=self.user_id)

    def get_session_history(self, limit: int = 10) -> list[dict]:
        """Get recent conversation history."""
        if not self._session_id:
            return []
        return self.memory.get_last_n_messages(self._session_id, limit)

    def end_session(self):
        """End the current session."""
        if self._session_id:
            self.memory.end_session(self._session_id)
            self._session_id = None
