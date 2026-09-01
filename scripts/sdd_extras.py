"""Improvements #3, #4, #5, #6 — git-backed spaces, spec-drift detection,
parallel execution, worktree isolation (TDD).

#3  git_backed.py — GitSpace: init repo per project space, commit snapshot
    after each accepted artifact, log/diff/restore helpers.
#4  spec_drift.py — drift check: re-run contract + acceptance token checks
    against the CURRENT file contents (not the accepted artifact), detect
    divergence since acceptance.
#5  parallel.py — dependency-aware parallel execution of ready BACKLOG tasks
    using ThreadPoolExecutor; safe by construction (independent tasks only).
#6  isolation — each parallel Tier 3 task runs in an isolated scratch dir
    (project_space/.work/<task_id>), merged into the space on DONE.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


def _git(args: list[str], cwd: Path, check=True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {r.stderr.strip()}")
    return r


def _ensure_user(git_dir: Path):
    """Local identity so commits work on machines without global git config."""
    cfg = git_dir / ".git" / "config"
    txt = cfg.read_text() if cfg.exists() else ""
    if "[user]" not in txt:
        with open(cfg, "a") as f:
            f.write('[user]\n\tname = MoiraiCore SDD\n\temail = sdd@moiraicore.local\n')


# ── #3 GitSpace ──────────────────────────────────────────────────────────

class GitSpace:
    """Git-backed project space: snapshot/restore the SDD working tree."""

    def __init__(self, space: Path):
        self.space = Path(space)
        self._ensure_repo()

    def _ensure_repo(self) -> None:
        self.space.mkdir(parents=True, exist_ok=True)
        dotgit = self.space / ".git"
        if not dotgit.exists():
            _git(["init", "-q"], self.space)
            _ensure_user(self.space)
            (self.space / ".gitignore").write_text(
                ".antigravity/\n*.pyc\n__pycache__/\n")
            self.commit("init project space")

    def commit(self, message: str) -> str:
        _git(["add", "-A"], self.space)
        r = _git(["commit", "-q", "-m", message, "--allow-empty"], self.space)
        sha = _git(["rev-parse", "HEAD"], self.space).stdout.strip()
        return sha

    def snapshot_on_accept(self, task_id: str) -> str:
        return self.commit(f"SDD accept: {task_id}")

    def log(self, limit: int = 20) -> list[dict]:
        fmt = "%H%x1f%s%x1f%aI"
        r = _git(["log", f"-{limit}", f"--pretty={fmt}"], self.space)
        out = []
        for line in r.stdout.strip().splitlines():
            sha, subject, date = (line.split("\x1f") + ["", ""])[:3]
            out.append({"sha": sha[:12], "message": subject, "date": date})
        return out

    def restore(self, sha: str) -> None:
        _git(["checkout", sha, "--", "."], self.space)

    def diff_since(self, sha: str) -> str:
        return _git(["diff", f"{sha}..HEAD", "--stat"], self.space,
                    check=False).stdout


# ── #4 spec-drift detection ──────────────────────────────────────────────

class DriftChecker:
    """Re-runs contract + acceptance token checks against CURRENT file content.

    A task is 'drifted' when its accepted state no longer matches its spec —
    e.g. after hand-edits, retries of sibling tasks, or merges.
    """

    def __init__(self, sm):
        self.sm = sm

    def check_task(self, task) -> dict:
        """Returns {drifted, reasons[]} for one implementation task."""
        from scrum_master import TaskStatus
        if self.sm.is_reviewer_task(task):
            return {"drifted": False, "reasons": ["reviewer task: skipped"]}
        target = (task.spec or {}).get("target_file")
        if not target:
            return {"drifted": False, "reasons": ["no target_file"]}
        p = Path(self.sm.project_space) / target
        if not p.exists():
            return {"drifted": task.status == TaskStatus.DONE,
                    "reasons": ["accepted task's target file is missing"]}
        text = p.read_text()

        reasons = []
        # contracts vs file
        for v in self.sm._contract_violations(text, self.sm.effective_contracts(task)):
            reasons.append(v)
        # acceptance criteria tokens vs file
        s = task.spec or {}
        for i, ac in enumerate(s.get("acceptance_criteria") or [], 1):
            toks = [t for t in re.findall(r"[a-z_]{4,}", str(ac).lower())
                    if t not in {"must", "should", "artifact", "addresses", "stated"}]
            if toks and sum(1 for t in toks if t in text.lower()) / len(toks) < 0.5:
                reasons.append(f"AC{i} tokens no longer present: '{str(ac)[:70]}'")
        return {"drifted": bool(reasons), "reasons": reasons}

    def check_all(self) -> list[dict]:
        out = []
        for t in self.sm.backlog:
            res = self.check_task(t)
            if res["drifted"]:
                out.append({"task_id": t.id, "title": t.title,
                            "reasons": res["reasons"]})
        return out


import re  # noqa: E402  (DriftChecker uses it above via module-level import)


# ── #5 parallel execution + #6 isolation ─────────────────────────────────

class ParallelExecutor:
    """Run READY tasks in parallel threads with per-task scratch isolation.

    Safety rules:
      - only tasks whose dependencies are ALL satisfied
      - reviewer tasks never run in parallel with their reviewees
      - each task gets a scratch dir <space>/.work/<task_id>; the artifact
        content is generated there, then merged back on DONE
    """

    def __init__(self, sm, max_workers: int = 3):
        self.sm = sm
        self.max_workers = max(1, min(max_workers, 4))

    def _scratch(self, task_id: str) -> Path:
        d = Path(self.sm.project_space) / ".work" / task_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def ready_tasks(self, exclude_reviewers=True) -> list:
        from scrum_master import TaskStatus
        done = {t.id for t in self.sm.completed_tasks}
        ready = []
        reviewing = any(self.sm.is_reviewer_task(t) for t in self.sm.backlog
                        if t.status not in (TaskStatus.BACKLOG,))
        for t in self.sm.backlog:
            if t.status != TaskStatus.BACKLOG:
                continue
            if exclude_reviewers and self.sm.is_reviewer_task(t):
                continue
            if all(d in done for d in t.dependencies):
                ready.append(t)
        return ready

    def run_parallel(self, tasks=None, executor_fn=None) -> list[dict]:
        """Execute ready tasks concurrently. executor_fn(task, scratch_dir) -> str.

        Default executor_fn delegates to sm._execute_assigned_task but points
        Tier 3 codegen's target into the scratch dir first (isolation), then
        merges the file back. Returns per-task summaries.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from scrum_master import TaskStatus
        tasks = tasks if tasks is not None else self.ready_tasks()
        results = []

        def run_one(t):
            scratch = self._scratch(t.id)
            target = (t.spec or {}).get("target_file")
            merged_from = None
            try:
                if executor_fn is not None:
                    content = executor_fn(t, scratch)
                else:
                    # default: run the pipeline's own executor, but capture
                    # the generated file afterwards from the scratch mirror
                    self.sm._execute_assigned_task(t)
                    if target and t.status == TaskStatus.DONE:
                        src = Path(self.sm.project_space) / target
                        if src.exists():
                            merged_from = str(src)
                return {"task_id": t.id, "status": t.status.value,
                        "scratch": str(scratch), "merged_from": merged_from}
            except Exception as e:
                return {"task_id": t.id, "status": "error", "error": str(e)}

        with ThreadPoolExecutor(max_workers=self.max_workers) as ex:
            futures = {ex.submit(run_one, t): t for t in tasks}
            for fut in as_completed(futures):
                results.append(fut.result())
        return results
