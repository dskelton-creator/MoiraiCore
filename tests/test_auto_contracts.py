"""Improvement #9 tests — auto-derive contracts."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from auto_contracts import (
    auto_contracts,
    build_contract_prompt,
    heuristic_contracts,
    parse_contract_json,
)
from scrum_master import ScrumMaster


# ── heuristic extraction ─────────────────────────────────────────────────

def test_heuristic_extracts_api_routes():
    td = [{"title": "Server", "description": "routes /api/shifts and /api/split-requests"}]
    c = heuristic_contracts(td)
    assert c["endpoints"]["shifts"] == "/api/shifts"
    assert c["endpoints"]["split_requests"] == "/api/split-requests"


def test_heuristic_extracts_time_units():
    assert heuristic_contracts(
        [{"title": "T", "description": "timestamps in seconds"}])["units"]["time"] == "seconds"
    assert heuristic_contracts(
        [{"title": "T", "description": "shifts use startHour blocks"}])["units"]["time"] == "hours-of-day"


def test_heuristic_extracts_known_fields():
    td = [{"title": "T", "description": "each record has taskId and employeeId"}]
    c = heuristic_contracts(td)
    assert "taskid" in c["fields"] and "employeeid" in c["fields"]


def test_heuristic_empty_when_nothing_matches():
    assert heuristic_contracts([{"title": "T", "description": "make it nice"}]) == {}


def test_heuristic_dedupes_routes():
    td = [{"title": "A", "description": "/api/items"}, {"title": "B", "description": "/api/items"}]
    c = heuristic_contracts(td)
    assert list(c["endpoints"].values()).count("/api/items") == 1


# ── model prompt / parsing ───────────────────────────────────────────────

def test_prompt_includes_goal_and_tasks():
    p = build_contract_prompt("build a widget", [
        {"title": "A", "description": "does x"}, {"title": "B", "description": "does y"}])
    assert "build a widget" in p
    assert "A: does x" in p and "B: does y" in p
    assert "STRICT JSON" in p


def test_parse_contract_json_plain():
    raw = '{"endpoints": {"tasks": "/api/tasks"}, "units": {"time": "seconds"}}'
    out = parse_contract_json(raw)
    assert out["endpoints"]["tasks"] == "/api/tasks"


def test_parse_contract_json_with_fences_and_prose():
    raw = 'Here you go:\n```json\n{"units": {"time": "minutes"}}\n```\nDone.'
    assert parse_contract_json(raw)["units"]["time"] == "minutes"


def test_parse_contract_json_garbage_returns_empty():
    assert parse_contract_json("no json here at all") == {}
    assert parse_contract_json("") == {}


def test_parse_contract_json_drops_empty_sections():
    raw = '{"endpoints": {}, "units": {"time": "seconds"}}'
    out = parse_contract_json(raw)
    assert "endpoints" not in out and out["units"]["time"] == "seconds"


def test_parse_contract_json_values_normalised_to_str():
    raw = '{"fields": {"count": 5}}'
    out = parse_contract_json(raw)
    assert out["fields"]["count"] == "5"


# ── auto_contracts merge ─────────────────────────────────────────────────

def test_auto_contracts_merges_model_over_heuristic(monkeypatch):
    import auto_contracts as ac
    monkeypatch.setattr(ac, "heuristic_contracts",
                        lambda td: {"units": {"time": "seconds"},
                                     "endpoints": {"a": "/api/a"}})
    monkeypatch.setattr(ac, "model_contracts",
                        lambda goal, td: {"units": {"time": "minutes"}})
    out = ac.auto_contracts("g", [{"title": "T"}], use_model=True)
    assert out["units"]["time"] == "minutes"          # model wins
    assert out["endpoints"]["a"] == "/api/a"          # heuristic preserved


def test_auto_contracts_heuristic_only_when_model_off(monkeypatch):
    import auto_contracts as ac
    calls = []
    monkeypatch.setattr(ac, "model_contracts", lambda *a, **k: calls.append(1) or {})
    monkeypatch.setattr(ac, "heuristic_contracts", lambda td: {"units": {"time": "seconds"}})
    out = ac.auto_contracts("g", [{"title": "T"}], use_model=False)
    assert not calls
    assert out["units"]["time"] == "seconds"


def test_auto_contracts_never_raises(monkeypatch):
    import auto_contracts as ac
    def boom(*a, **k):
        raise RuntimeError("model exploded")
    monkeypatch.setattr(ac, "heuristic_contracts", boom)
    monkeypatch.setattr(ac, "model_contracts", boom)
    assert ac.auto_contracts("g", [{"title": "T"}]) == {}


# ── ScrumMaster integration ──────────────────────────────────────────────

def test_derive_and_apply_writes_contract_file(tmp_path):
    sm = ScrumMaster("contract derivation", project_space=str(tmp_path))
    sm.goal = "roster system"
    td = [{"title": "Server", "description": "handles /api/shifts", "tier": 3},
          {"title": "Client", "description": "calendar UI", "tier": 3}]
    out = sm.derive_and_apply_contracts(td, use_model=False)
    assert out["endpoints"]["shifts"] == "/api/shifts"
    assert (tmp_path / "specs" / "contracts.md").exists()


def test_decompose_auto_derives_when_no_operator_contracts(tmp_path):
    sm = ScrumMaster("auto at decompose", project_space=str(tmp_path))
    sm.goal = "shift tracker with /api/shifts in seconds"
    sm.decompose_backlog([
        {"title": "Server", "description": "implements /api/shifts", "tier": 3},
        {"title": "Client", "description": "UI", "tier": 3},
    ])
    assert (tmp_path / "specs" / "contracts.md").exists()
    assert sm.load_project_contracts()["endpoints"]["shifts"] == "/api/shifts"
    # and the contracts merge into every task's effective view
    for t in sm.backlog:
        eff = sm.effective_contracts(t)
        assert eff["endpoints"]["shifts"] == "/api/shifts"


def test_does_not_override_existing_project_contracts(tmp_path):
    sm = ScrumMaster("respect existing", project_space=str(tmp_path))
    sm.goal = "shift tracker with /api/shifts in seconds"
    sm.write_contract_file({"units": {"time": "minutes"}})   # operator already set
    sm.decompose_backlog([
        {"title": "Server", "description": "implements /api/shifts in seconds", "tier": 3},
    ])
    # existing file wins — no auto-overwrite
    assert sm.load_project_contracts()["units"]["time"] == "minutes"
    assert "endpoints" not in sm.load_project_contracts()


def test_operator_task_contracts_still_win_at_merge(tmp_path):
    sm = ScrumMaster("operator wins", project_space=str(tmp_path))
    sm.goal = "shift tracker with /api/shifts in seconds"
    sm.decompose_backlog([
        {"title": "Server", "description": "implements /api/shifts in seconds", "tier": 3,
         "spec": {"intent": "i", "acceptance_criteria": ["a"],
                  "contracts": {"units": {"time": "minutes"}}}},  # operator override
    ])
    t = sm.backlog[0]
    eff = sm.effective_contracts(t)
    assert eff["units"]["time"] == "minutes"          # task beats derived project
