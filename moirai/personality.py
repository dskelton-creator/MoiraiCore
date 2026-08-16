"""
Personality Layer — Configurable voice persona for Moirai.

Controls how Moirai speaks, not what it says.
Supports presets and custom overrides.
"""

from moirai import PERSONALITIES


class Personality:
    """Configurable personality for conversational responses."""

    def __init__(self, preset: str = "professional", overrides: dict = None):
        if preset not in PERSONALITIES:
            raise ValueError(
                f"Unknown personality '{preset}'. "
                f"Available: {', '.join(PERSONALITIES.keys())}"
            )
        self._config = {**PERSONALITIES[preset]}
        if overrides:
            self._config.update(overrides)

    @property
    def name(self) -> str:
        return self._config["name"]

    @property
    def tone(self) -> str:
        return self._config["tone"]

    @property
    def formality(self) -> str:
        return self._config["formality"]

    @property
    def humor(self) -> str:
        return self._config["humor"]

    @property
    def greeting_style(self) -> str:
        return self._config["greeting_style"]

    @property
    def response_length(self) -> str:
        return self._config["response_length"]

    def system_prompt_fragment(self) -> str:
        """Return a system prompt fragment describing this personality."""
        lines = [
            f"You are {self._config['name']}, an AI assistant.",
            f"Tone: {self._config['tone']}.",
            f"Formality: {self._config['formality']}.",
            f"Humor: {self._config['humor']}.",
            f"Response length: {self._config['response_length']}.",
        ]
        if self._config["greeting_style"] == "formal":
            lines.append("Begin interactions with a polite greeting.")
        elif self._config["greeting_style"] == "casual":
            lines.append("Begin interactions naturally, as if continuing a conversation.")
        elif self._config["greeting_style"] == "warm":
            lines.append("Greet people warmly and make them feel welcome.")
        elif self._config["greeting_style"] == "playful":
            lines.append("Start with something light or engaging.")
        return "\n".join(lines)

    def greeting(self, user_name: str = None, time_of_day: str = None) -> str:
        """Generate an appropriate greeting based on personality and context."""
        parts = []
        if time_of_day:
            hour = int(time_of_day.split(":")[0]) if ":" in time_of_day else 12
            if hour < 12:
                parts.append("Good morning")
            elif hour < 18:
                parts.append("Good afternoon")
            else:
                parts.append("Good evening")
        else:
            parts.append("Hello")

        if user_name:
            parts.append(f", {user_name}")
        parts.append(".")

        if self._config["greeting_style"] == "playful":
            parts.append(" What can I help you with today?")
        elif self._config["greeting_style"] == "warm":
            parts.append(" How can I assist you?")
        elif self._config["greeting_style"] == "formal":
            parts.append(" How may I be of service?")

        return "".join(parts)

    def transition_phrase(self, topic: str) -> str:
        """Generate a smooth transition phrase when changing topics."""
        if self._config["humor"] == "frequent":
            transitions = [
                f"Switching gears to {topic} — fun one!",
                f"Alright, let's talk {topic}!",
                f"On the topic of {topic} — great choice!",
            ]
        elif self._config["humor"] == "light":
            transitions = [
                f"Regarding {topic}...",
                f"Let me address your {topic} question.",
                f"Speaking of {topic}...",
            ]
        else:
            transitions = [
                f"Regarding {topic}.",
                f"Let me address {topic}.",
                f"I'll help with {topic}.",
            ]
        import random
        return random.choice(transitions)

    def __repr__(self):
        return f"Personality(preset=custom, name={self.name!r})"
