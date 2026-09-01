"""Improvement #2 — Integration/reviewer task type (TDD).

Adds a REVIEW task kind that closes the multi-file SDD loop:

  - Task spec may carry `task_role: "reviewer"`. Reviewer tasks run AFTER
    implementation tasks (via normal dependencies) and:
      1. Gather the completed tasks' artifacts and generated files.
      2. Build a review brief: shared contracts + each implementation's
         spec intent + the actual file contents.
      3. Ask the model (Tier 2 path) to verify cross-file consistency,
         producing a review report artifact.
  - Model-free verification ALSO runs: `_review_contracts()` re-checks the
    actual generated files in the project space against effective contracts —
    the mechanical version of the wfm-wmo failure mode.
  - A reviewer task passes only when: its own spec passes AND no contract
    violations exist in any implementation task's generated files.

The review task's artifact is DESIGN_DECISION kind (a report), not CODE_DIFF.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from scrum_master import ScrumMaster, Task, Tier, TaskStatus


def _sm(tmp_project: Path, goal="review test") -> ScrumMaster:
    sm = ScrumMaster(goal)
    sm.project_space = str(tmp_project)
    return sm


def _impl_task(tid="task-001", title="Tasks endpoint"):
    return Task(id=tid, title=title, description="impl", tier=Tier(3),
                spec={"intent": f"{title} implementation",
                      "acceptance_criteria": ["endpoint"],
                      "target_file": f"server/{tid}.js",
                      "contracts": {"endpoints": {"tasks": "/api/projects/{id}/tasks"}}})


# ── reviewer task creation ───────────────────────────────────────────────

def test_reviewer_task_role_round_trips(tmp_path):
    sm = _sm(tmp_path)
    tasks = sm.decompose_backlog([
        {"title": "Implement tasks API", "description": "d", "tier": 3},
        {"title": "Cross-file review", "description": "verify consistency",
         "tier": 2, "dependencies": ["task-001"],
         "spec": {"intent": "verify consistency", "task_role": "reviewer",
                  "acceptance_criteria": ["review report produced"]}},
    ])
    assert tasks[1].spec["task_role"] == "reviewer"


def test_is_reviewer_task(tmp_path):
    sm = _sm(tmp_path)
    t = Task(id="task-001", title="review", description="",
             tier=Tier(2), spec={"intent": "i", "task_role": "reviewer"})
    assert sm.is_reviewer_task(t) is True
    impl = Task(id="task-002", title="impl", description="",
                tier=Tier(3), spec={"intent": "i"})
    assert sm.is_reviewer_task(impl) is False


# ── model-free file-level contract review ────────────────────────────────

def test_review_finds_violation_in_generated_files(tmp_path):
    from scrum_master import TaskStatus
    sm = _sm(tmp_path)
    impl = _impl_task("task-001")
    impl.status = TaskStatus.DONE
    sm.backlog = [impl]
    # simulate the wfm-wmo failure: generated file used the WRONG endpoint
    f = Path(sm.project_space) / "server"
    f.mkdir(parents=True, exist_ok=True)
    (f / "task-001.js").write_text("app.get('/projects/:id/task-list', h)\n")

    findings = sm.review_contract_compliance()
    assert any("task-001" in f_ and "endpoints" in f_ for f_ in findings)


def test_review_passes_when_files_honour_contracts(tmp_path):
    sm = _sm(tmp_path)
    impl = _impl_task("task-001")
    sm.backlog = [impl]
    f = Path(sm.project_space) / "server"
    f.mkdir(parents=True, exist_ok=True)
    (f / "task-001.js").write_text("// tasks endpoint /api/projects/{id}/tasks\n")

    findings = sm.review_contract_compliance()
    assert findings == []


def test_review_skips_tasks_without_target_file(tmp_path):
    sm = _sm(tmp_path)
    impl = _impl_task("task-001")
    impl.spec.pop("target_file")
    sm.backlog = [impl]
    assert sm.review_contract_compliance() == []


def test_review_skips_missing_files(tmp_path):
    sm = _sm(tmp_path)
    sm.backlog = [_impl_task("task-001")]  # file never generated
    assert sm.review_contract_compliance() == []


# ── reviewer evaluation integrates with the gate ─────────────────────────

def test_reviewer_artifact_type_is_design_decision(tmp_path):
    sm = _sm(tmp_path)
    rev = Task(id="task-001", title="review", description="",
               tier=Tier(2), spec={"intent": "i", "task_role": "reviewer"})
    assert sm.reviewer_artifact_type().value == "design_decision"


def test_review_brief_includes_findings_and_specs(tmp_path):
    sm = _sm(tmp_path)
    impl = _impl_task("task-001")
    impl.status = TaskStatus.DONE
    sm.backlog = [impl]
    f = Path(sm.project_space) / "server"
    f.mkdir(parents=True, exist_ok=True)
    (f / "task-001.js").write_text("app.get('/projects/:id/task-list', h)\n")

    rev = Task(id="task-002", title="Cross-file review", description="",
               tier=Tier(2), spec={"intent": "i", "task_role": "reviewer",
                                   "acceptance_criteria": ["review"]})
    brief = sm._review_brief(rev)
    assert "task-001" in brief
    assert "/api/projects/{id}/tasks" in brief           # contract restated
    assert "task-list" in brief or "findings" in brief.lower()


def test_review_gate_fails_when_findings_exist(tmp_path):
    sm = _sm(tmp_path)
    impl = _impl_task("task-001")
    impl.status = TaskStatus.DONE
    sm.backlog = [impl]
    f = Path(sm.project_space) / "server"
    f.mkdir(parents=True, exist_ok=True)
    (f / "task-001.js").write_text("app.get('/projects/:id/task-list', h)\n")

    res = sm._evaluate_review(impl_tasks=[impl], findings=sm.review_contract_compliance())
    assert res["passed"] is False
    assert res["notes"]


def test_review_gate_passes_clean_project(tmp_path):
    sm = _sm(tmp_path)
    res = sm._evaluate_review(findings=[])
    assert res["passed"] is True