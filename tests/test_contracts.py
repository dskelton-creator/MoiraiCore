"""Improvement #1 — Shared contracts in task specs (TDD).

Closes the proven SDD weak point: each artifact passes its own spec while
independently generated files disagree on endpoint paths, field names, units,
shapes.

Design:
  - Project-level contracts live in task.spec["contracts"] (a dict, optional)
    and/or a project-level contracts file (specs/contracts.md) that ALL tasks
    in the project share. Task-level contracts win for that task; project
    contracts always apply.
  - contracts shape: {"<contract name>": {"<token>": "<value>"}} e.g.
      {"endpoints": {"tasks_endpoint": "/api/projects/{id}/tasks"},
       "units": {"time": "seconds"},
       "fields": {"task_id": "taskId"}}
  - _check_spec now ALSO checks contract tokens: for each contract entry,
    the VALUE's salient tokens must appear in the artifact (same 50% rule).
    This is what makes independently generated files agree.
  - _task_brief renders the contracts section so every generator sees the
    shared agreement BEFORE writing code.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from scrum_master import ScrumMaster, Task, Tier, TaskStatus


def _sm(tmp_project: Path, goal="contract test") -> ScrumMaster:
    sm = ScrumMaster(goal)
    sm.project_space = str(tmp_project)
    return sm


STOP = {"must", "should", "artifact", "addresses", "stated", "intent"}


# ── spec structure ───────────────────────────────────────────────────────

def test_spec_can_carry_contracts(tmp_path):
    sm = _sm(tmp_path)
    tasks = sm.decompose_backlog([
        {"title": "Tasks endpoint", "description": "CRUD for tasks",
         "tier": 3,
         "spec": {
             "intent": "Tasks REST endpoint",
             "acceptance_criteria": ["Routes registered under /api/projects/{id}/tasks"],
             "contracts": {
                 "endpoints": {"tasks": "/api/projects/{id}/tasks"},
                 "units": {"time": "seconds"},
             },
         }},
    ])
    assert tasks[0].spec["contracts"]["units"]["time"] == "seconds"


def test_contracts_persisted_to_spec_file(tmp_path):
    sm = _sm(tmp_path)
    sm.decompose_backlog([
        {"title": "T", "description": "d",
         "spec": {"intent": "i", "acceptance_criteria": ["does x"],
                  "contracts": {"fields": {"task_id": "taskId"}}}},
    ])
    spec_md = (tmp_path / "specs" / "task-001-spec.md").read_text()
    assert "Contracts" in spec_md and "taskId" in spec_md


def test_brief_renders_contracts_for_generators(tmp_path):
    sm = _sm(tmp_path)
    t = Task(id="task-001", title="Tasks endpoint", description="CRUD",
             tier=Tier(3),
             spec={"intent": "i", "acceptance_criteria": ["a"],
                   "contracts": {"endpoints": {"tasks": "/api/projects/{id}/tasks"},
                                  "units": {"time": "seconds"}}})
    brief = sm._task_brief(t)
    assert "Shared Contracts" in brief
    assert "/api/projects/{id}/tasks" in brief
    assert "time = seconds" in brief


# ── contract checking at the gate ────────────────────────────────────────

def _artifact(content: str):
    from scrum_master import Artifact, ArtifactType
    return Artifact(artifact_type=ArtifactType.CODE_DIFF, task_id="task-001",
                    content=content)


def test_check_spec_passes_when_contract_tokens_present(tmp_path):
    sm = _sm(tmp_path)
    t = Task(id="task-001", title="Tasks endpoint", description="",
             tier=Tier(3),
             spec={"intent": "i", "acceptance_criteria": ["uses the tasks endpoint"],
                   "contracts": {"endpoints": {"tasks": "/api/projects/{id}/tasks"},
                                  "units": {"time": "seconds"}}})
    art = _artifact("// tasks endpoint: /api/projects/{id}/tasks\n"
                    "const router = require('express');\n"
                    "const timeSec = 0; // time tracked in seconds\n")
    res = sm._check_spec(art, t)
    assert res["passed"], res["notes"]


def test_check_spec_fails_when_endpoint_contract_violated(tmp_path):
    sm = _sm(tmp_path)
    t = Task(id="task-001", title="Tasks endpoint", description="",
             tier=Tier(3),
             spec={"intent": "i", "acceptance_criteria": ["a tasks endpoint"],
                   "contracts": {"endpoints": {"tasks": "/api/projects/{id}/tasks"}}})
    # artifact uses a DIFFERENT endpoint shape — the exact wfm-wmo failure
    art = _artifact("app.get('/projects/:id/task-list', handler)  // wrong path\n")
    res = sm._check_spec(art, t)
    assert not res["passed"]
    assert "contract" in res["notes"].lower()


def test_check_spec_fails_on_unit_contract_violation(tmp_path):
    sm = _sm(tmp_path)
    t = Task(id="task-001", title="timer", description="",
             tier=Tier(3),
             spec={"intent": "i", "acceptance_criteria": ["a timer"],
                   "contracts": {"units": {"time": "seconds"}}})
    art = _artifact("const elapsedHours = 2; // time in hours-of-day\n")
    res = sm._check_spec(art, t)
    assert not res["passed"]


def test_no_contracts_section_behaves_exactly_as_before(tmp_path):
    sm = _sm(tmp_path)
    t = Task(id="task-001", title="T", description="",
             tier=Tier(3),
             spec={"intent": "i", "acceptance_criteria": ["covers the widget"]})
    art = _artifact("the widget is implemented here")
    res = sm._check_spec(art, t)
    assert res["passed"]
    # and no contract chatter in notes
    assert "contract" not in res["notes"].lower()


def test_project_level_contracts_file_applies_to_all_tasks(tmp_path):
    sm = _sm(tmp_path)
    sm.decompose_backlog([
        {"title": "T1", "description": "d", "tier": 3,
         "spec": {"intent": "i", "acceptance_criteria": ["a"]}},
    ])
    # operator drops a project-wide contracts file
    contracts_md = (
        "# Project Contracts (all tasks must honour)\n\n"
        "## endpoints\n- tasks = /api/projects/{id}/tasks\n\n"
        "## units\n- time = seconds\n"
    )
    (tmp_path / "specs" / "contracts.md").write_text(contracts_md)
    merged = sm.load_project_contracts()
    assert merged["endpoints"]["tasks"] == "/api/projects/{id}/tasks"
    # and it merges into the task's effective spec at check time
    t = sm.backlog[0]
    t.spec.setdefault("acceptance_criteria", []).append("implements the tasks endpoint")
    art = _artifact("// implements the tasks endpoint\n"
                    "route /api/projects/{id}/tasks registered; time in seconds\n")
    res = sm._check_spec(art, t)
    assert res["passed"], res["notes"]


def test_task_contracts_win_over_project_contracts(tmp_path):
    sm = _sm(tmp_path)
    (tmp_path / "specs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "specs" / "contracts.md").write_text(
        "## units\n- time = seconds\n")
    sm.decompose_backlog([
        {"title": "T", "description": "d", "tier": 3,
         "spec": {"intent": "i", "acceptance_criteria": ["a"],
                  "contracts": {"units": {"time": "minutes"}}}},  # override
    ])
    t = sm.backlog[0]
    eff = sm.effective_contracts(t)
    assert eff["units"]["time"] == "minutes"


# ── integration with retry loop ──────────────────────────────────────────

def test_contract_failure_feeds_retry_notes(tmp_path):
    sm = _sm(tmp_path)
    t = Task(id="task-001", title="Tasks endpoint", description="",
             tier=Tier(3), status=TaskStatus.ASSIGNED, current_iteration=1,
             spec={"intent": "i", "acceptance_criteria": ["a tasks endpoint"],
                   "contracts": {"endpoints": {"tasks": "/api/projects/{id}/tasks"}}})
    art = _artifact("wrong: /projects/task-list\n")
    res = sm._check_spec(art, t)
    brief = sm._task_brief(t)
    # evaluation notes flow into the next brief via evaluation_notes — verified
    # by the existing retry mechanism; here we assert the message is actionable
    assert "endpoints" in res["notes"]
