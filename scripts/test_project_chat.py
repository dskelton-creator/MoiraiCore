"""Tests for MoiraiCore Project Chat — @mention parsing, message persistence, team roster."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_test_dir = Path(tempfile.mkdtemp(prefix="moirai_chat_test_"))
os.environ["AGENT_OS_ROOT"] = str(_test_dir)

from project_chat import (
    parse_mention,
    get_team_roster,
    list_agents_used,
    add_message,
    add_agent_transcript,
    get_messages,
)


def setup_module():
    """Create an isolated project chat directory."""
    (_test_dir / "projects" / "acme" / "chat").mkdir(parents=True, exist_ok=True)
    (_test_dir / "projects" / "acme" / ".antigravity").mkdir(parents=True, exist_ok=True)


# ── @mention parsing ──
def test_parse_mention_leading():
    assert parse_mention("@researcher summarise this") == ("researcher", "summarise this")
    assert parse_mention("@Writer do x") == ("writer", "do x")
    assert parse_mention("@my-agent hi") == ("my-agent", "hi")
    assert parse_mention("@writer") == ("writer", "")


def test_parse_mention_negatives():
    assert parse_mention("hello @writer how are you")[0] is None
    assert parse_mention("   ") == (None, "")
    assert parse_mention("") == (None, "")
    assert parse_mention("plain message")[0] is None


# ── message persistence ──
def test_message_roundtrip():
    add_message("acme", "user", "hello", agent_name="user")
    add_message("acme", "assistant", "hi there", agent_name="jarvis")
    add_agent_transcript("acme", "Developer", "working on task-1")
    msgs = get_messages("acme")
    roles = [m["role"] for m in msgs]
    assert roles == ["user", "assistant", "agent_transcript"]
    assert msgs[0]["agent_name"] == "user"
    assert msgs[1]["agent_name"] == "jarvis"
    assert msgs[2]["content"] == "working on task-1"


# ── team roster ──
def test_get_team_roster_structure():
    roster = get_team_roster("acme")
    assert isinstance(roster, list)
    assert len(roster) > 0, "roster should include registry agents"
    keys = [r["key"] for r in roster]
    assert len(keys) == len(set(keys)), "agent keys must be unique"
    for r in roster:
        for field in ("key", "name", "emoji", "role", "status", "is_working"):
            assert field in r, f"roster row missing {field}"


def test_list_agents_used_excludes_user():
    used = list_agents_used("acme")
    assert "user" not in used
    assert "jarvis" in used
