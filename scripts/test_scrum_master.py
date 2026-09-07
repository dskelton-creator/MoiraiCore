"""Tests for MoiraiCore ScrumMaster — tier enum coercion and Tier-2 artifact fallback.

These are the regression guards for the two bugs found in the "fix Antigravity"
session: (1) assign_task storing a raw int for tier and crashing _save_state, and
(2) Tier-2 artifact generation producing a hollow static template.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_test_dir = Path(os.environ["AGENT_OS_ROOT"])
os.environ["AGENT_OS_ROOT"] = str(_test_dir)

from scrum_master import ScrumMaster, Tier


def setup_module():
    """Create an isolated project for scrum state."""
    proj = _test_dir / "projects" / "acme"
    (proj / ".antigravity" / "artifacts").mkdir(parents=True, exist_ok=True)
    (proj / "src").mkdir(parents=True, exist_ok=True)


def _sm():
    # project_space points into the isolated temp dir (never the live repo tree)
    return ScrumMaster("acme", project_space=str(_test_dir / "projects" / "acme"))


def _backlog_tier2(sm):
    sm.set_goal("test goal")
    sm.decompose_backlog(task_descriptions=[
        {"title": "Design API", "description": "Design a FastAPI REST API for books",
         "tier": 2, "priority": 0, "dependencies": []},
    ])
    return sm.get_next_task()


def test_decompose_backlog_coerces_tier_to_enum():
    sm = _sm()
    task = _backlog_tier2(sm)
    assert isinstance(task.tier, Tier)
    assert task.tier == Tier.TIER_2_ARCHITECT


def test_assign_task_coerces_int_tier(monkeypatch):
    sm = _sm()
    task = _backlog_tier2(sm)
    # Dashboard/API sends `tier` as a raw int (JSON) — must not crash on save.
    assigned = sm.assign_task(task.id, tier=2)
    assert isinstance(assigned.tier, Tier)
    assert assigned.tier == Tier.TIER_2_ARCHITECT

    # Reload from disk must not crash on the enum-coercion path either.
    sm2 = _sm()
    sm2._load_state()  # must not raise AttributeError ('int' has no .value)


def test_generate_tier2_artifact_valid_merge_gate_output():
    # With a working Gemini key this path returns a real plan dict; without one it
    # falls back to a template string. EITHER output must be merge-gate-valid.
    sm = _sm()
    task = _backlog_tier2(sm)
    artifact = sm._generate_tier2_artifact(task)
    if isinstance(artifact, dict):
        assert "files" in artifact and artifact["files"], "real plan must carry files"
        assert "steps" in artifact and artifact["steps"], "real plan must carry steps"
        assert artifact.get("plan"), "real plan must carry a rendered plan"
    else:
        assert isinstance(artifact, str) and len(artifact) > 100, "fallback must be substantial"


def test_generate_tier2_artifact_fallback_when_no_key(monkeypatch):
    # Deterministically force the no-key path -> static template fallback.
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    sm = _sm()
    task = _backlog_tier2(sm)
    artifact = sm._generate_tier2_artifact(task)
    assert isinstance(artifact, str), "with no API key, fallback template string expected"
    assert len(artifact) > 100
