"""Tests for improvements #3 (git-backed spaces), #4 (spec-drift),
#5 (parallel execution), #6 (scratch isolation)."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from sdd_extras import DriftChecker, GitSpace, ParallelExecutor
from scrum_master import ScrumMaster, Task, Tier, TaskStatus


def _sm(tmp_path, goal="test project"):
    sm = ScrumMaster(goal, project_space=str(tmp_path))
    return sm


# ── #3 GitSpace ──────────────────────────────────────────────────────────

def test_gitspace_inits_repo_and_initial_commit(tmp_path):
    g = GitSpace(tmp_path / "proj")
    assert (tmp_path / "proj" / ".git").exists()
    log = g.log()
    assert log[-1]["message"] == "init project space"


def test_snapshot_on_accept_creates_named_commit(tmp_path):
    g = GitSpace(tmp_path / "proj")
    (tmp_path / "proj" / "server.js").write_text("console.log(1)\n")
    sha = g.snapshot_on_accept("task-001")
    entries = g.log()
    assert entries[0]["message"] == "SDD accept: task-001"
    assert entries[0]["sha"] == sha[:12]


def test_restore_recovers_previous_content(tmp_path):
    g = GitSpace(tmp_path / "proj")
    f = tmp_path / "proj" / "file.txt"
    f.write_text("v1\n")
    sha1 = g.commit("v1")
    f.write_text("v2 broken\n")
    g.commit("v2")
    g.restore(sha1)
    assert f.read_text() == "v1\n"


def test_diff_since_reports_changes(tmp_path):
    g = GitSpace(tmp_path / "proj")
    (tmp_path / "proj" / "a.txt").write_text("a\n")
    sha = g.commit("first")
    (tmp_path / "proj" / "b.txt").write_text("b\n")
    g.commit("second")
    assert "b.txt" in g.diff_since(sha)


def test_gitignore_written(tmp_path):
    GitSpace(tmp_path / "proj")
    gi = tmp_path / "proj" / ".gitignore"
    assert gi.exists() and ".antigravity/" in gi.read_text()


# ── #4 spec-drift ────────────────────────────────────────────────────────

def _sm_with_impl(tmp_path, file_content, contracts=None):
    sm = _sm(tmp_path)
    t = Task(id="task-001", title="Roster API", description="impl", tier=Tier(3),
             status=TaskStatus.DONE,
             spec={"intent": "roster api", "target_file": "server/api.js",
                   "acceptance_criteria": ["implements the tasks endpoint"],
                   "contracts": contracts or {
                       "endpoints": {"tasks": "/api/projects/{id}/tasks"}}})
    sm.backlog = [t]
    f = tmp_path / "server"
    f.mkdir(parents=True, exist_ok=True)
    (f / "api.js").write_text(file_content)
    return sm, t


def test_no_drift_when_file_honours_contracts(tmp_path):
    sm, t = _sm_with_impl(
        tmp_path,
        "// implements the tasks endpoint at /api/projects/{id}/tasks\n")
    res = DriftChecker(sm).check_task(t)
    assert res["drifted"] is False


def test_drift_detected_when_file_edited_to_break_contract(tmp_path):
    sm, t = _sm_with_impl(
        tmp_path,
        "// was fine, then someone changed it to /projects/task-list\n")
    res = DriftChecker(sm).check_task(t)
    assert res["drifted"] is True
    assert any("contract" in r for r in res["reasons"])


def test_drift_detected_when_accepted_file_missing(tmp_path):
    sm = _sm(tmp_path)
    t = Task(id="task-001", title="Roster API", description="", tier=Tier(3),
             status=TaskStatus.DONE,
             spec={"intent": "i", "target_file": "server/gone.js",
                   "acceptance_criteria": ["x"], "contracts": {}})
    sm.backlog = [t]
    res = DriftChecker(sm).check_task(t)
    assert res["drifted"] and "missing" in res["reasons"][0]


def test_check_all_returns_only_drifted(tmp_path):
    sm, t = _sm_with_impl(
        tmp_path,
        "// someone hand-edited to /projects/task-list — contract broken\n")
    clean = Task(id="task-002", title="Clean", description="", tier=Tier(3),
                 status=TaskStatus.DONE,
                 spec={"intent": "i", "acceptance_criteria": ["a"], "contracts": {}})
    sm.backlog.append(clean)
    drifted = DriftChecker(sm).check_all()
    assert [d["task_id"] for d in drifted] == ["task-001"]


def test_drift_skips_reviewer_tasks(tmp_path):
    sm = _sm(tmp_path)
    rev = Task(id="task-009", title="review", description="", tier=Tier(2),
               status=TaskStatus.DONE,
               spec={"intent": "i", "task_role": "reviewer",
                     "acceptance_criteria": ["x"], "contracts": {}})
    sm.backlog = [rev]
    assert DriftChecker(sm).check_all() == []


# ── #5 parallel execution + #6 isolation ─────────────────────────────────

def test_ready_tasks_respects_dependencies_and_skips_reviewers(tmp_path):
    sm = _sm(tmp_path)
    a = Task(id="task-001", title="A", description="", tier=Tier(3),
             spec={"intent": "a"})
    b = Task(id="task-002", title="B", description="", tier=Tier(3),
             dependencies=["task-001"], spec={"intent": "b"})
    rev = Task(id="task-003", title="review", description="", tier=Tier(2),
               spec={"intent": "r", "task_role": "reviewer"})
    sm.backlog = [a, b, rev]
    ex = ParallelExecutor(sm)
    ready = [t.id for t in ex.ready_tasks()]
    assert ready == ["task-001"]            # B blocked, reviewer excluded


def test_parallel_run_uses_scratch_dirs(tmp_path):
    sm = _sm(tmp_path)
    a = Task(id="task-001", title="A", description="", tier=Tier(3),
             spec={"intent": "a"})
    b = Task(id="task-002", title="B", description="", tier=Tier(3),
             spec={"intent": "b"})
    sm.backlog = [a, b]
    ex = ParallelExecutor(sm, max_workers=2)
    executed = []

    def fake_executor(task, scratch):
        executed.append((task.id, Path(scratch).name))
        scratch.joinpath("out.txt").write_text(f"output of {task.id}")
        return f"done {task.id}"

    results = ex.run_parallel(executor_fn=fake_executor)
    assert {r["task_id"] for r in results} == {"task-001", "task-002"}
    # each task got its OWN scratch dir named after it (#6 isolation)
    assert {s for _, s in executed} == {"task-001", "task-002"}
    assert (tmp_path / ".work" / "task-001" / "out.txt").exists()
    assert (tmp_path / ".work" / "task-002" / "out.txt").exists()


def test_parallel_errors_isolated_per_task(tmp_path):
    sm = _sm(tmp_path)
    bad = Task(id="task-001", title="bad", description="", tier=Tier(3),
               spec={"intent": "x"})
    good = Task(id="task-002", title="good", description="", tier=Tier(3),
                spec={"intent": "y"})
    sm.backlog = [bad, good]

    def executor(task, scratch):
        if task.id == "task-001":
            raise RuntimeError("codegen exploded")
        return "ok"

    results = ParallelExecutor(sm, max_workers=2).run_parallel(executor_fn=executor)
    by_id = {r["task_id"]: r for r in results}
    assert by_id["task-001"]["status"] == "error"
    assert "exploded" in by_id["task-001"]["error"]
    # good task completed without its error affecting it (isolation)
    assert by_id["task-002"]["status"] != "error"
    assert (tmp_path / ".work" / "task-002").exists()


def test_max_workers_clamped():
    from scrum_master import ScrumMaster
    sm = ScrumMaster("clamp", project_space="/tmp/clamp-test")
    assert ParallelExecutor(sm, max_workers=50).max_workers <= 4
    assert ParallelExecutor(sm, max_workers=0).max_workers == 1
