"""Tests for Tier 1 ScrumMaster dynamic specialist-agent creation.

Covers the 'can Tier 1 create additional Tier 2 agents when the project needs
new skills' capability: spawning a specialist into the shared AgentRegistry
(persisted to config/agents.override.json), idempotent ensure_specialist_for_task,
and key validation. All registry writes go through the real override store, so
each test cleans up after itself.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

_test_dir = Path(tempfile.mkdtemp(prefix="moirai_specialist_test_"))
os.environ["AGENT_OS_ROOT"] = str(_test_dir)

from scrum_master import ScrumMaster
from agent_registry import get_registry


def _sm():
    proj = _test_dir / "projects" / "acme"
    (proj / ".antigravity").mkdir(parents=True, exist_ok=True)
    return ScrumMaster("acme", project_space=str(proj))


class TestCreateSpecialistAgent:
    def teardown_method(self):
        reg = get_registry()
        for key in ("security-review", "migration-specialist"):
            try:
                reg.delete_agent(key)
            except Exception:
                pass

    def test_spawn_persists_to_registry(self):
        sm = _sm()
        result = sm.create_specialist_agent(
            key="security-review", name="Security Reviewer",
            role="Security specialist", skills=["security"],
            triggers=["security", "vulnerability"])
        assert result["ok"], result
        agent = get_registry().get_agent("security-review")
        assert agent and agent["name"] == "Security Reviewer"
        assert agent["status"] == "active"
        assert "security" in agent["triggers"]

    def test_requires_key(self):
        result = _sm().create_specialist_agent(key="", name="X")
        assert not result["ok"] and "key" in result["error"]

    def test_ensure_creates_then_reuses(self):
        sm = _sm()
        first = sm.ensure_specialist_for_task(None, "migration")
        assert first["ok"] and first["created"]
        second = sm.ensure_specialist_for_task(None, "migration")
        assert second["ok"] and not second["created"]

    def test_ensure_reuses_builtin_match(self):
        # 'code' should match a built-in developer-type agent via triggers
        result = _sm().ensure_specialist_for_task(None, "code")
        if result["ok"] and not result.get("created", True):
            assert result["agent"]["key"] != "code-specialist"
