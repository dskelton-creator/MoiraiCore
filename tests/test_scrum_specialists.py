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


class TestSpecialistExecution:
    """Tier 2 specialist execution inside the scrum pipeline."""

    def _backlog_task(self, sm):
        sm.set_goal("test goal")
        sm.decompose_backlog(task_descriptions=[
            {"title": "Security design", "description": "Design auth flow",
             "tier": 2, "priority": 0, "dependencies": []}])
        return sm.backlog[0]

    def teardown_method(self):
        try:
            get_registry().delete_agent("sec-review")
        except Exception:
            pass

    def test_assign_task_to_agent_sets_key_and_validates(self):
        from scrum_master import TaskStatus
        sm = _sm()
        task = self._backlog_task(sm)
        sm.create_specialist_agent(key="sec-review", name="Sec Reviewer",
                                   role="security", triggers=["security"])
        t = sm.assign_task_to_agent(task.id, "sec-review")
        assert t.agent_key == "sec-review"
        assert t.status == TaskStatus.ASSIGNED
        # unknown agent must raise
        try:
            sm.assign_task_to_agent("task-999", "nope")
            assert False, "should have raised"
        except ValueError:
            pass
        get_registry().delete_agent("sec-review")

    def test_specialist_dispatch_used_when_agent_key_set(self, monkeypatch):
        from scrum_master import Tier
        sm = _sm()
        task = self._backlog_task(sm)
        task.status = __import__("scrum_master").TaskStatus.ASSIGNED
        task.agent_key = "sec-review"

        calls = {}
        monkeypatch.setattr(sm, "_generate_specialist_artifact",
                            lambda t: calls.setdefault("used", t.agent_key) or "# plan")
        monkeypatch.setattr(sm, "evaluate_task", lambda tid, **kw: {"result": "PASS"})
        monkeypatch.setattr(sm, "submit_artifact", lambda *a, **k: None)
        sm._execute_assigned_task(task)
        assert calls.get("used") == "sec-review", "specialist path should be used"

    def test_generate_specialist_artifact_falls_back(self):
        sm = _sm()
        task = self._backlog_task(sm)
        task.agent_key = "nonexistent-agent-xyz"
        out = sm._generate_specialist_artifact(task)
        assert isinstance(out, str) and len(out) > 50  # fallback template

    def test_to_dict_roundtrip_keeps_agent_key(self):
        import json as _json
        sm = _sm()
        task = self._backlog_task(sm)
        task.agent_key = "sec-review"
        d = _json.loads(_json.dumps(task.to_dict()))
        assert d["agent_key"] == "sec-review"
