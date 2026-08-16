"""
Moirai — Conversational Intelligence Layer for MoiraiCore

A general-purpose "Moirai-style" brain that provides:
  - Configurable personality (tone, formality, humor)
  - Conversation memory across sessions (SQLite-backed)
  - Proactive context awareness (time, activity, goals)
  - Seamless integration with Hermes Bridge for LLM execution

This module is hotel-agnostic. Hotel-specific features layer on top.
"""

from pathlib import Path

MOIRAI_DIR = Path(__file__).parent
CONVERSATIONS_DB = MOIRAI_DIR / "moirai_conversations.db"

# ── Personality Presets ──

PERSONALITIES = {
    "professional": {
        "name": "Moirai",
        "tone": "professional and concise",
        "formality": "high",
        "humor": "none",
        "greeting_style": "formal",
        "response_length": "brief",
    },
    "friendly": {
        "name": "Moirai",
        "tone": "warm and friendly",
        "formality": "medium",
        "humor": "light",
        "greeting_style": "casual",
        "response_length": "conversational",
    },
    "concierge": {
        "name": "Moirai",
        "tone": "attentive and helpful",
        "formality": "medium-high",
        "humor": "subtle",
        "greeting_style": "warm",
        "response_length": "detailed",
    },
    "witty": {
        "name": "Moirai",
        "tone": "clever and engaging",
        "formality": "medium",
        "humor": "frequent",
        "greeting_style": "playful",
        "response_length": "conversational",
    },
}

# ── Knowledge Base Paths ──

KNOWLEDGE_BASE_PATHS = {
    "faq": MOIRAI_DIR / "knowledge" / "faq.md",
    "policies": MOIRAI_DIR / "knowledge" / "policies.md",
    "local": MOIRAI_DIR / "knowledge" / "local_attractions.md",
    "services": MOIRAI_DIR / "knowledge" / "hotel_services.md",
}
