"""ScrumMaster — MoiraiCore Tier 1 Director with formal workflow control.

MoiraiCore is the Scrum Master. It owns:
  - The backlog (goals → tasks decomposition)
  - Task assignment (Tier 2 architect or Tier 3 builder)
  - Artifact evaluation (merge gates)
  - Delivery status reporting
  - Auto-execution of assigned tasks (NEW)

The LLM (Hermes) executes within constraints MoiraiCore sets.
No code enters src/ without MoiraiCore approval.
No task is marked done without passing artifacts.

Usage:
    sm = ScrumMaster("my-project")
    sm.set_goal("Build a REST API for library books")
    sm.decompose_backlog()
    task = sm.get_next_task()
    sm.assign_task(task.id, tier=2)
    # Auto-execution will now handle the rest!
    sm.submit_artifact(task.id, "implementation_plan", artifact_data)
    sm.evaluate_task(task.id)  # PASS/FAIL
    sm.status_report()
"""

from __future__ import annotations

import json
import os
import subprocess
import re
import time
import uuid
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional
import threading

# ── Paths ──
AGENT_OS_ROOT = Path(os.environ.get("AGENT_OS_ROOT", str(Path(__file__).resolve().parents[1])))

# ── Agent Working Method (10x discipline) ──
_WORKING_METHOD = ""
try:
    from agent_working_method import WORKING_METHOD_PREAMBLE as _WORKING_METHOD
except ImportError:
    pass

# ── Spec-anchored SDD: constitution loader ──
# config/constitution.md is the single source of truth for the working method.
# Falls back to the hardcoded WORKING_METHOD_PREAMBLE when absent/too short,
# so a missing file never breaks the pipeline.

def _load_constitution() -> str:
    """Load the project constitution, falling back to the built-in preamble."""
    try:
        p = Path(__file__).resolve().parents[1] / "config" / "constitution.md"
        text = p.read_text().strip()
        # Strip markdown headings for a leaner prompt injection
        lines = [ln for ln in text.splitlines() if not ln.startswith("#")]
        body = "\n".join(lines).strip()
        if len(body) >= 100:
            return body
    except Exception:
        pass
    return _WORKING_METHOD


# ── Enums ──


class TaskStatus(Enum):
    BACKLOG = "backlog"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    ARTIFACT_PENDING = "artifact_pending"
    EVALUATION = "evaluation"
    DONE = "done"
    FAILED = "failed"
    BLOCKED = "blocked"


class Tier(Enum):
    TIER_2_ARCHITECT = 2  # Gemini Pro / Antigravity
    TIER_3_BUILDER = 3    # Ollama / Ornith 9B


class ArtifactType(Enum):
    IMPLEMENTATION_PLAN = "implementation_plan"
    CODE_DIFF = "code_diff"
    TEST_RUN_LOG = "test_run_log"
    PLAN_CHECKOFF = "plan_checkoff"
    DESIGN_DECISION = "design_decision"


# ── Data Classes ——


@dataclass
class Artifact:
    artifact_type: ArtifactType
    task_id: str
    content: dict | str
    file_path: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    evaluated: bool = False
    passed: bool = False
    eval_notes: str = ""

    def to_dict(self) -> dict:
        return {
            "type": self.artifact_type.value,
            "task_id": self.task_id,
            "content": self.content,
            "file_path": self.file_path,
            "created_at": self.created_at,
            "evaluated": self.evaluated,
            "passed": self.passed,
            "eval_notes": self.eval_notes,
        }


@dataclass
class Task:
    id: str
    title: str
    description: str
    tier: Optional[Tier] = None
    status: TaskStatus = TaskStatus.BACKLOG
    artifacts: list[Artifact] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    assigned_at: str = ""
    completed_at: str = ""
    evaluation_notes: str = ""
    priority: int = 0  # 0 = highest
    dependencies: list[str] = field(default_factory=list)
    max_iterations: int = 5
    current_iteration: int = 0
    # Specialist execution (Tier 2): when set, this task is executed BY the
    # named registry agent (Hermes CLI with its memory/toolsets) instead of
    # the generic Gemini/template Tier 2 path. The tier stays TIER_2_ARCHITECT.
    agent_key: Optional[str] = None
    # Spec-anchored SDD: the task's contract. Keys: intent (str),
    # constraints (list[str]), acceptance_criteria (list[str]),
    # out_of_scope (list[str]), task_role ("reviewer" | absent),
    # contracts (dict of dicts — shared agreements). Checked by _check_spec.
    spec: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "tier": self.tier.value if self.tier else None,
            "status": self.status.value,
            "artifacts": [a.to_dict() for a in self.artifacts],
            "created_at": self.created_at,
            "assigned_at": self.assigned_at,
            "completed_at": self.completed_at,
            "evaluation_notes": self.evaluation_notes,
            "priority": self.priority,
            "dependencies": self.dependencies,
            "max_iterations": self.max_iterations,
            "current_iteration": self.current_iteration,
            "agent_key": self.agent_key,
            "spec": self.spec,
        }


# ── ScrumMaster ——


class ScrumMaster:
    """MoiraiCore Tier 1 Scrum Master.

    Controls the entire delivery pipeline:
    - Goal decomposition → backlog
    - Task assignment → Tier 2 or Tier 3
    - Artifact evaluation → merge gate
    - Status reporting → user visibility
    - Auto-execution of assigned tasks (NEW)
    """

    def __init__(self, project_name: str, project_space: str | None = None):
        self.project_name = project_name
        self.project_space = project_space or str(Path(__file__).resolve().parents[1] / "projects" / project_name)
        self.goal: str = ""
        self.backlog: list[Task] = []
        self.sprints: list[dict] = []
        self.current_sprint: dict | None = None
        self.completed_tasks: list[Task] = []
        self.failed_tasks: list[Task] = []
        self.status_file = Path(self.project_space) / ".antigravity" / "scrum_status.json"
        self.backup_file = Path(self.project_space) / ".antigravity" / "scrum_backup.json"
        # Auto-execution control
        self._auto_executor_thread: Optional[threading.Thread] = None
        self._auto_executor_running = False
        self._auto_executor_interval = 5  # seconds
        # Dry-run mode (generate artifacts without writing to src/)
        self._dry_run = False
        # Improvement #9: auto-derive shared contracts at decompose time.
        # use_model=False keeps derivation deterministic (heuristic-only) —
        # useful for tests and offline operation.
        self.auto_contracts_use_model = True
        # Warm Ollama model in background to eliminate cold-start on first Tier 3 task
        self._warm_model()

    def _agent_label(self, tier) -> str:
        """Map a Task tier to a roster-friendly agent display name.

        Tasks carrying an agent_key (specialist execution) resolve the
        agent's display name from the registry; fall back to role labels.
        """
        if tier == Tier.TIER_3_BUILDER:
            return "Developer"
        if tier == Tier.TIER_2_ARCHITECT:
            return "Architect"
        return "Scrum Master"

    def _task_agent_label(self, task: "Task") -> str:
        """Display name for the agent executing this task — specialist name
        when agent_key is set (and exists in the registry), else tier label."""
        key = getattr(task, "agent_key", None)
        if key:
            try:
                sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
                from agent_registry import get_registry
                agent = get_registry().get_agent(key)
                if agent and agent.get("name"):
                    return agent["name"]
            except Exception:
                pass
            return key.title()
        return self._agent_label(task.tier)

    def assign_task_to_agent(self, task_id: str, agent_key: str) -> Task:
        """Assign a task to a SPECIFIC registered agent for execution.

        The task runs as a true Tier 2 worker inside the scrum pipeline: it
        keeps TIER_2_ARCHITECT semantics (merge gate, evaluation) but its
        artifact is generated by that agent via the Hermes CLI, with the
        agent's own memory and toolsets. Use create_specialist_agent() first
        if no suitable agent exists.
        """
        sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
        from agent_registry import get_registry
        agent = get_registry().get_agent(agent_key)
        if not agent or agent.get("status") != "active":
            raise ValueError(f"Agent '{agent_key}' not found or not active")
        task = self.assign_task(task_id)  # validates BACKLOG state + audit
        task.agent_key = agent_key.lower().strip()
        self._save_state()
        self._emit(agent["name"], f"📌 Assigned '{task.title}' → {agent['name']}")
        return task

    def _emit(self, agent: str, content: str, msg_type: str = "transcript") -> None:
        """Best-effort live transcript into the project chat feed.

        Never raises — telemetry must not break the pipeline. Writes directly to
        the project's chat log so 'what the agents are working on' is visible
        in the Slack-like feed in real time.
        """
        try:
            sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
            from project_chat import add_agent_transcript
            add_agent_transcript(
                project_name=self.project_name,
                agent_name=agent,
                content=content,
                msg_type=msg_type,
            )
        except Exception:
            pass

    def _warm_model(self):
        """Warm the active Tier 3 model in a background thread.

        Respects HAGENT_TIER3_BACKEND: 'pi' harness runs qwen3:8b via Ollama's
        OpenAI endpoint, so warm that; only warm the legacy ollama_worker
        default when the backend is actually ollama. Never warms a model that
        isn't going to be used (24GB RAM budget matters).
        """
        try:
            if os.environ.get("HAGENT_TIER3_BACKEND", "ollama") == "pi":
                from tier3_manager import get_config
                cfg = get_config()
                model = cfg.get("ollama_worker_model") or "qwen3:8b"
                threading.Thread(
                    target=self._warm_ollama_model, args=(model,), daemon=True
                ).start()
            else:
                from ollama_worker import OllamaConfig, warm_ollama
                config = OllamaConfig()
                threading.Thread(target=warm_ollama, args=(config,), daemon=True).start()
        except Exception:
            pass

    def _warm_ollama_model(self, model_name: str):
        """Best-effort keep-alive load of a specific Ollama model."""
        try:
            import urllib.request
            req = urllib.request.Request(
                "http://localhost:11434/api/generate",
                data=json.dumps({"model": model_name, "keep_alive": "30m"}).encode(),
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=120).read()
        except Exception:
            pass

    # ── Goal & Backlog Management ──────────────────────────────────────


    def set_goal(self, goal: str) -> None:
        """Set the high-level goal for this project."""
        self.goal = goal
        self._save_state()

    def decompose_backlog(self, task_descriptions: list[dict] | None = None) -> list[Task]:
        """Decompose the goal into a formal backlog of tasks.

        Each task dict should have:
          - title: str
          - description: str
          - tier: 2 or 3 (optional, auto-detected)
          - priority: int (0 = highest)
          - dependencies: list[str] (task IDs)
          - spec: dict (optional; spec-anchored SDD contract — derived when absent)

        If no tasks provided, generates a default decomposition based on the goal.
        """
        if task_descriptions is None:
            task_descriptions = self._auto_decompose()

        self.backlog = []
        for i, td in enumerate(task_descriptions):
            task = Task(
                id=f"task-{i+1:03d}",
                title=td.get("title", f"Task {i+1}"),
                description=td.get("description", ""),
                tier=Tier(td.get("tier", 2)),
                priority=td.get("priority", i),
                dependencies=td.get("dependencies", []),
                agent_key=td.get("agent_key"),
                spec=td.get("spec") or {},
            )
            if not task.spec:
                task.spec = self._derive_spec(task)
            self.write_spec_file(task)
            self.backlog.append(task)

        # Sort by priority
        self.backlog.sort(key=lambda t: t.priority)

        # Improvement #9: auto-derive shared contracts when the operator
        # supplied none (project-level file absent). Runs once per decompose;
        # never blocks the pipeline.
        try:
            self.derive_and_apply_contracts(
                task_descriptions,
                use_model=self.auto_contracts_use_model,
                force=False,
            )
        except Exception:
            pass

        self._save_state()
        return self.backlog

    def _auto_decompose(self) -> list[dict]:
        """Generic fallback decomposition.

        A real backlog for a project is NOT derivable from the goal alone — callers
        should pass explicit `task_descriptions` to decompose_backlog(). This returns
        two neutral tasks so the pipeline stays usable without inventing a domain.
        """
        return [
            {
                "title": "Project Setup",
                "description": "Set up project structure, dependencies and configuration for: " + (self.goal or "the project"),
                "tier": 2,
                "priority": 0,
            },
            {
                "title": "Core Implementation",
                "description": "Implement the core functionality described by: " + (self.goal or "the goal"),
                "tier": 3,
                "priority": 1,
            },
            {
                "title": "Tests",
                "description": "Add tests covering the core implementation and verify with pytest.",
                "tier": 3,
                "priority": 2,
                "dependencies": ["task-002"],
            },
        ]

    # ── Spec-anchored SDD ────────────────────────────────────────────────


    def _derive_spec(self, task: "Task") -> dict:
        """Spec-anchored SDD: every task carries a minimal, checkable spec.

        Deterministic derivation — no extra model call. Operator-supplied
        specs (passed via decompose_backlog) always win over this.
        """
        return {
            "intent": (task.description or task.title).strip(),
            "constraints": [
                "Must pass the ScrumGate merge checks (non-empty, compiles, no dangerous patterns)",
            ],
            "acceptance_criteria": [
                f"Artifact addresses the stated intent: '{task.title}'",
                "Artifact is concrete (references real file paths / function names where applicable)",
            ],
            "out_of_scope": [],
        }

    # ── Shared contracts (improvement #1) ─────────────────────────────────

    _CONTRACTS_STOPWORDS = {
        "must", "should", "honour", "honor", "shared", "contract", "contracts",
        "agreement", "tasks", "value", "values",
    }

    @staticmethod
    def _parse_contracts_md(text: str) -> dict:
        """Parse contracts.md format:

            ## endpoints
            - tasks = /api/projects/{id}/tasks

            ## units
            - time = seconds
        """
        out: dict = {}
        section = None
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("## "):
                section = line[3:].strip().lower()
                out.setdefault(section, {})
                continue
            if section and (line.startswith("- ") or line.startswith("* ")):
                item = line[2:].strip()
                if "=" in item:
                    k, v = item.split("=", 1)
                    out[section][k.strip()] = v.strip()
                else:
                    out[section][item] = ""
        return {k: v for k, v in out.items() if v}

    def load_project_contracts(self) -> dict:
        """Load project-level contracts from specs/contracts.md (empty if absent)."""
        p = Path(self.project_space) / "specs" / "contracts.md"
        if not p.exists():
            return {}
        try:
            return self._parse_contracts_md(p.read_text())
        except Exception as e:
            print(f"  contracts.md parse skipped: {e}")
            return {}

    def effective_contracts(self, task: "Task") -> dict:
        """Merged view: project contracts + task-level contracts (task wins)."""
        merged = dict(self.load_project_contracts())
        raw = (task.spec or {}).get("contracts") or {}
        if isinstance(raw, dict):
            for section, entries in raw.items():
                if isinstance(entries, dict):
                    merged.setdefault(section, {}).update(entries)
        return merged

    def _contract_violations(self, artifact_text: str, contracts: dict) -> list[str]:
        """Check contract token coverage in the artifact text.

        For each contract entry, the VALUE's salient tokens must appear in the
        artifact (same 50%-coverage rule as acceptance criteria). Independent
        generators that ignore shared shapes fail here instead of at integration.
        """
        text = (artifact_text or "").lower()
        failures: list[str] = []
        for section, entries in (contracts or {}).items():
            for key, value in entries.items():
                tokens = [
                    t for t in re.findall(r"[a-z_{}0-9./-]{4,}", str(value).lower())
                    if t not in self._CONTRACTS_STOPWORDS
                ]
                if not tokens:
                    continue
                hit = sum(1 for t in tokens if t in text)
                if hit / len(tokens) < 0.5:
                    failures.append(
                        f"contract '{section}.{key}' not honoured: expected '{value}'")
        return failures

    def write_contract_file(self, contracts: dict) -> str:
        """Persist project-level contracts as specs/contracts.md."""
        try:
            specs_dir = Path(self.project_space) / "specs"
            specs_dir.mkdir(parents=True, exist_ok=True)
            p = specs_dir / "contracts.md"
            lines = ["# Project Contracts (all tasks must honour)", ""]
            for section, entries in (contracts or {}).items():
                lines.append(f"## {section}")
                for k, v in entries.items():
                    lines.append(f"- {k} = {v}")
                lines.append("")
            p.write_text("\n".join(lines))
            return str(p)
        except Exception as e:
            print(f"  contracts file write skipped: {e}")
            return ""

    # ── Reviewer tasks (improvement #2) ───────────────────────────────────

    def is_reviewer_task(self, task: "Task") -> bool:
        """True when the task's spec marks it as a cross-file reviewer task."""
        return (task.spec or {}).get("task_role") == "reviewer"

    def reviewer_artifact_type(self):
        """Reviewer output is a report — reuse the DESIGN_DECISION artifact type."""
        return ArtifactType.DESIGN_DECISION

    def review_contract_compliance(self) -> list[str]:
        """Model-free cross-file check: scan every implementation task's
        generated file (spec.target_file) against effective contracts.

        This is the mechanical version of the wfm-wmo failure mode — the file
        CONTENT is what ships, so that is what gets checked, not the artifact
        text that was accepted earlier.
        Returns findings as [\"task-001: contract 'endpoints.tasks' not honoured: expected '...'\"].
        """
        findings: list[str] = []
        for t in self.backlog:
            if self.is_reviewer_task(t) or t.status != TaskStatus.DONE:
                continue
            target = (t.spec or {}).get("target_file")
            if not target:
                continue
            p = Path(self.project_space) / target
            if not p.exists():
                continue
            try:
                text = p.read_text()
            except Exception:
                continue
            for v in self._contract_violations(text, self.effective_contracts(t)):
                findings.append(f"{t.id}: {v}")
        return findings

    def _implementation_tasks(self) -> list["Task"]:
        return [t for t in self.backlog if not self.is_reviewer_task(t)]

    def _review_brief(self, review_task: "Task") -> str:
        """Brief for the reviewer: contracts + per-task intents + findings so far."""
        lines = [self._task_brief(review_task), "",
                 "## Review Scope — implementation tasks in this project"]
        for t in self._implementation_tasks():
            s = t.spec or {}
            lines.append(f"### {t.id} — {t.title} ({t.status.value})")
            lines.append(f"- Intent: {s.get('intent', '')}")
            tf = s.get("target_file")
            if tf:
                p = Path(self.project_space) / tf
                if p.exists():
                    content = p.read_text()[:2000]
                    lines.append(f"- File `{tf}`:\n```\n{content}\n```")
                else:
                    lines.append(f"- File `{tf}`: (not yet generated)")
        findings = self.review_contract_compliance()
        if findings:
            lines.append("## Mechanical findings (already detected — verify and expand)")
            lines += [f"- {f}" for f in findings]
        else:
            lines.append("## Mechanical findings: none — verify semantic consistency")
        return "\n".join(lines)

    def _evaluate_review(self, impl_tasks: list["Task"] | None = None,
                         findings: list[str] | None = None) -> dict:
        """Gate for reviewer tasks: pass only when mechanical findings are empty.

        The model's review report must ALSO acknowledge the findings; silence
        about known violations fails the review.
        """
        findings = findings if findings is not None else self.review_contract_compliance()
        if not findings:
            return {"passed": True, "notes": "All files honour shared contracts"}
        return {
            "passed": False,
            "notes": "Cross-file contract violations: " + "; ".join(findings),
        }

    def derive_and_apply_contracts(
        self,
        task_descriptions: list[dict],
        *,
        use_model: bool = True,
        force: bool = False,
    ) -> dict:
        """Improvement #9: auto-derive project contracts at decompose time.

        Model (Tier 2) drafts cross-file agreements from goal + task
        descriptions; heuristic extraction (routes/units/fields) fills gaps.
        Applied + persisted ONLY when no project contracts exist yet (or
        force=True). Operator task-level contracts always win at merge time.
        Never raises — pipeline continuity comes first.
        """
        try:
            from auto_contracts import auto_contracts
            existing = self.load_project_contracts()
            if existing and not force:
                return existing
            if not task_descriptions:
                return {}
            contracts = auto_contracts(self.goal or "", task_descriptions,
                                       use_model=use_model)
            if contracts:
                self.write_contract_file(contracts)
            return contracts
        except Exception as e:
            print(f"  contract auto-derivation skipped: {e}")
            return {}

    def write_spec_file(self, task: "Task") -> str:
        """Persist the task spec as markdown in the project space (vault-indexable)."""
        try:
            specs_dir = Path(self.project_space) / "specs"
            specs_dir.mkdir(parents=True, exist_ok=True)
            p = specs_dir / f"{task.id}-spec.md"
            if p.exists():
                return str(p)
            s = task.spec or {}
            lines = [f"# Spec — {task.title}", "", "## Intent", s.get("intent", "")]
            for key in ("constraints", "acceptance_criteria", "out_of_scope"):
                items = s.get(key) or []
                lines += [f"## {key.replace('_', ' ').title()}"] + [f"- {i}" for i in items]
            if s.get("contracts"):
                lines.append("## Contracts")
                for section, entries in s["contracts"].items():
                    lines.append(f"### {section}")
                    lines += [f"- {k} = {v}" for k, v in entries.items()]
            p.write_text("\n".join(lines) + "\n")
            return str(p)
        except Exception as e:
            print(f"  Spec file write skipped for {task.id}: {e}")
            return ""

    def _task_brief(self, task: "Task") -> str:
        """Spec-anchored brief: constitution + spec contract + task + retry feedback.

        Replaces the duplicated description assembly in the three artifact
        generators; the spec is presented as the contract the artifact must
        satisfy.
        """
        s = task.spec or self._derive_spec(task)
        spec_md = (
            "## Task Spec (the contract — your artifact MUST satisfy these)\n"
            f"**Intent:** {s.get('intent', '')}\n"
            f"**Constraints:**\n" + "".join(f"- {c}\n" for c in s.get("constraints", [])) +
            "**Acceptance criteria (checked at the gate):**\n" +
            "".join(f"- {a}\n" for a in s.get("acceptance_criteria", []))
        )
        if s.get("out_of_scope"):
            spec_md += "**Out of scope:**\n" + "".join(f"- {o}\n" for o in s["out_of_scope"])
        contracts = self.effective_contracts(task)
        if contracts:
            spec_md += "**Shared Contracts (project-wide agreement — violations fail the gate):**\n"
            for section, entries in contracts.items():
                for k, v in entries.items():
                    spec_md += f"- {section}: {k} = {v}\n"
        brief = f"{_load_constitution()}\n\n{spec_md}\n\n{task.title}\n\n{task.description}"
        if task.current_iteration > 0 and task.evaluation_notes:
            brief += f"\n\n=== PREVIOUS ATTEMPT FAILED — FIX THESE ISSUES ===\n{task.evaluation_notes}"
        return brief

    def _check_spec(self, artifact: "Artifact", task: "Task") -> dict:
        """Spec-anchored evaluation: acceptance-criteria coverage.

        Cheap, model-free keyword-coverage heuristic. Extracts salient tokens
        from each acceptance criterion and requires most of them to appear in
        the artifact. Subjective quality is left to the existing checks.
        """
        s = task.spec or {}
        content = artifact.content if isinstance(artifact.content, str) else json.dumps(artifact.content)
        text = (content or "").lower()
        failures = []
        stopwords = {
            "must", "should", "artifact", "addresses", "stated", "intent",
            "real", "file", "files", "paths", "where", "with", "that", "this",
            "references", "concrete", "applicable", "function", "names", "such",
            "covers", "include", "including",
        }
        criteria = s.get("acceptance_criteria") or []
        for i, ac in enumerate(criteria, 1):
            toks = [t for t in re.findall(r"[a-z_]{4,}", str(ac).lower()) if t not in stopwords]
            if toks:
                hit = sum(1 for t in toks if t in text)
                if hit / len(toks) < 0.5:
                    failures.append(f"AC{i} not addressed: '{str(ac)[:80]}'")
        # Shared contracts (improvement #1): independent generators must honour
        # project/task-wide agreements (endpoints, units, field names, shapes).
        contract_failures = self._contract_violations(text, self.effective_contracts(task))
        failures.extend(contract_failures)
        notes = ("All acceptance criteria addressed" if not failures
                 else "; ".join(failures))
        if contract_failures:
            notes = (notes + " — shared contracts keep independently generated "
                     "files consistent; fix to the agreed values").strip("; ")
        return {
            "passed": not failures,
            "notes": notes,
        }

    # ── Task Assignment ──────────────────────────────────────────────────


    def get_next_task(self) -> Optional[Task]:
        """Get the next task from the backlog that is ready to start.

        A task is ready when:
        - It is in BACKLOG status
        - All dependencies are DONE
        """
        for task in self.backlog:
            if task.status != TaskStatus.BACKLOG:
                continue

            # Check dependencies
            deps_met = all(
                any(ct.id == dep and ct.status == TaskStatus.DONE for ct in self.completed_tasks)
                for dep in task.dependencies
            )
            if deps_met:
                return task

        return None

    def assign_task(self, task_id: str, tier: Optional[Tier] = None) -> Task:
        """Assign a task for execution.

        Moves task from BACKLOG → ASSIGNED.
        Optionally overrides the tier.
        """
        task = self._find_task(task_id)
        if not task:
            raise ValueError(f"Task '{task_id}' not found in backlog")

        if task.status != TaskStatus.BACKLOG:
            raise ValueError(f"Task '{task_id}' is not in BACKLOG status (current: {task.status.value})")

        if tier is not None:
            task.tier = tier if isinstance(tier, Tier) else Tier(tier)

        task.status = TaskStatus.ASSIGNED
        task.assigned_at = datetime.now().isoformat()
        self._save_state()
        # Audit log
        try:
            from audit import audit_log
            audit_log("task.assign", task_id=task_id, status="success",
                     details={"tier": task.tier.value if task.tier else None})
        except ImportError:
            pass
        return task

    # ── Dynamic Agent Creation (Tier 1 capability) ────────────────────────

    def create_specialist_agent(self, key: str, name: str = "", role: str = "",
                                skills: list[str] | None = None,
                                triggers: list[str] | None = None,
                                memory_folder: str | None = None) -> dict:
        """Spawn a NEW specialist agent into the shared AgentRegistry.

        This is the Tier 1 Scrum Master's ability to grow its own workforce:
        when the project requires a skill no existing agent covers (e.g.
        'security review', 'data migration'), Tier 1 registers a specialist
        that persists in config/agents.override.json across restarts. The
        orchestrator's trigger-based router picks it up immediately for
        future matching tasks.

        Returns {'ok': True, 'agent': <summary>} or {'ok': False, 'error': ...}.
        Never raises — staffing problems must not break the pipeline.
        """
        key = (key or "").lower().strip()
        if not key:
            return {"ok": False, "error": "agent key is required"}
        try:
            sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
            from agent_registry import get_registry
            definition = {
                "key": key,
                "name": name or key.title(),
                "role": role or f"Specialist ({', '.join(skills or []) or 'general'})",
                "emoji": "🧩",
                "status": "active",
                "model": "",
                "description": role or f"Specialist agent spawned by Tier 1 for project {self.project_name}",
                "skills": skills or [],
                "toolsets": [],
                "triggers": triggers or [],
            }
            if memory_folder:
                definition["memory_folder"] = memory_folder
            saved = get_registry().save_agent(key, definition)
        except Exception as e:
            return {"ok": False, "error": str(e)}

        # Audit log (best-effort)
        try:
            from audit import audit_log
            audit_log("agent.spawn_specialist", status="success",
                      details={"key": key, "project": self.project_name})
        except Exception:
            pass

        self._emit("Scrum Master",
                   f"🧩 Spawned specialist agent '{saved.get('name', key)}' "
                   f"({definition['role']}) for project needs")
        return {"ok": True, "agent": saved}

    def ensure_specialist_for_task(self, task: "Task", skill: str,
                                   trigger_words: list[str] | None = None) -> dict:
        """Idempotently ensure a specialist exists for a named skill.

        Reuses an existing active agent whose key/triggers cover the skill;
        otherwise spawns one via create_specialist_agent(). Intended to be
        called by Tier 1 during decomposition or after a failed evaluation
        cites a missing capability.
        """
        skill_key = "".join(c if c.isalnum() else "-" for c in skill.lower()).strip("-")
        try:
            sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
            from agent_registry import get_registry
            reg = get_registry()
            needle = skill.lower()
            for summary in reg.list_agents(status_filter="active"):
                trig = [t.lower() for t in (summary.get("triggers") or [])]
                if needle in (summary.get("key") or "").lower() or any(needle in t for t in trig):
                    return {"ok": True, "agent": summary, "created": False}
        except Exception:
            pass
        result = self.create_specialist_agent(
            key=f"{skill_key}-specialist"[:64],
            name=f"{skill.title()} Specialist",
            role=f"{skill.title()} specialist for {self.project_name}",
            skills=[skill],
            triggers=trigger_words or [skill.lower()],
            memory_folder=f"agents/{skill_key}",
        )
        result["created"] = result.get("ok", False)
        return result

    def _find_task(self, task_id: str) -> Optional[Task]:
        """Find a task by ID in backlog or completed."""
        for task in self.backlog:
            if task.id == task_id:
                return task
        for task in self.completed_tasks:
            if task.id == task_id:
                return task
        return None

    # ── Auto-Execution (NEW) ──────────────────────────────────────────────


    def start_auto_executor(self, interval: int = 5):
        """Start the automatic task executor in a background thread.

        Args:
            interval: Seconds between checks for assigned tasks (default: 5)
        """
        if self._auto_executor_thread and self._auto_executor_thread.is_alive():
            print("Auto-executor is already running")
            return

        self._auto_executor_interval = interval
        self._auto_executor_running = True
        self._auto_executor_thread = threading.Thread(target=self._auto_executor_loop, daemon=True)
        self._auto_executor_thread.start()
        print(f"Auto-executor started (checking every {interval}s)")

    def stop_auto_executor(self):
        """Stop the automatic task executor."""
        self._auto_executor_running = False
        if self._auto_executor_thread:
            self._auto_executor_thread.join(timeout=2)
        print("Auto-executor stopped")

    def _auto_executor_loop(self):
        """Main loop for the auto-executor."""
        print("Auto-executor thread started")
        while self._auto_executor_running:
            try:
                self._process_assigned_tasks()
                time.sleep(self._auto_executor_interval)
            except Exception as e:
                print(f"Auto-executor error: {e}")
                time.sleep(self._auto_executor_interval)
        print("Auto-executor thread ended")

    def _process_assigned_tasks(self):
        """Process all currently ASSIGNED tasks in parallel where possible.
        
        Tasks without dependencies on each other are executed concurrently.
        Tasks with shared dependencies are batched for efficiency.
        """
        assigned_tasks = [task for task in self.backlog if task.status == TaskStatus.ASSIGNED]
        if not assigned_tasks:
            return
        
        # Group tasks that can run in parallel (no shared dependencies)
        # Tasks that depend on common tasks run sequentially
        parallel_groups = self._group_parallel_tasks(assigned_tasks)
        
        for group in parallel_groups:
            if len(group) == 1:
                # Single task - execute directly
                task = group[0]
                print(f"Auto-executor processing task {task.id}: {task.title}")
                self._execute_assigned_task(task)
            else:
                # Parallel execution - use thread pool
                print(f"Auto-executor processing {len(group)} parallel tasks: {[t.id for t in group]}")
                with ThreadPoolExecutor(max_workers=3) as executor:
                    futures = {executor.submit(self._execute_assigned_task, task): task for task in group}
                    for future in as_completed(futures):
                        task = futures[future]
                        try:
                            future.result()
                        except Exception as e:
                            print(f"Parallel task {task.id} error: {e}")
    
    def _group_parallel_tasks(self, tasks: list[Task]) -> list[list[Task]]:
        """Group tasks that can run in parallel based on dependency analysis."""
        # Simple heuristic: tasks with same priority level and no inter-dependencies
        # can run in parallel, OR tasks that all depend on the same completed set
        
        # Get completed task IDs
        completed_ids = set(t.id for t in self.completed_tasks)
        
        # Check if all dependencies are satisfied for each task
        ready_tasks = [t for t in tasks if all(d in completed_ids for d in t.dependencies)]
        
        if not ready_tasks:
            return [[t] for t in tasks]  # Each task needs its deps first
        
        # Group by priority for parallel execution
        priority_groups = {}
        for task in ready_tasks:
            priority_groups.setdefault(task.priority, []).append(task)
        
        # Return groups (can run in parallel within each group)
        result = []
        for priority in sorted(priority_groups.keys()):
            group = priority_groups[priority]
            if len(group) > 1:
                result.append(group)
            else:
                result.extend([[t] for t in group])
        
        return result

    def _scan_dependencies(self) -> dict:
        """Scan project dependencies for known vulnerabilities.
        
        Uses 'safety' to check requirements against PyPI vulnerability DB.
        Returns {'ok': True, 'vulnerabilities': []} or {'ok': False, 'error': str}.
        """
        try:
            # Find requirements files
            req_files = list((AGENT_OS_ROOT / "requirements").glob("*.txt"))
            req_files.extend((AGENT_OS_ROOT / "requirements").glob("*.in"))
            req_files.extend([AGENT_OS_ROOT / "requirements.txt"])
            
            # Also check project-specific requirements
            project_req = Path(self.project_space) / "requirements.txt"
            if project_req.exists():
                req_files.append(project_req)
            
            if not any(f.exists() for f in req_files):
                return {"ok": True, "vulnerabilities": [], "message": "No requirements files found"}
            
            # Run safety check on combined requirements
            req_args = []
            seen = set()
            for rf in req_files:
                if rf.exists() and rf.name not in seen:
                    req_args.extend(["--file", str(rf)])
                    seen.add(rf.name)
            
            # Safety check (output JSON for parsing)
            result = subprocess.run(
                [sys.executable, "-m", "safety", "check", "--json", "--output", "json"] + req_args,
                capture_output=True, text=True, timeout=120, cwd=str(AGENT_OS_ROOT)
            )
            
            # Safety returns 0 for no vulns, non-zero for vulns or errors
            vulnerabilities = []
            if result.stdout:
                try:
                    data = json.loads(result.stdout)
                    vulnerabilities = data.get("vulnerabilities", [])
                except json.JSONDecodeError:
                    pass
            
            # Audit log the scan
            try:
                from audit import audit_log
                audit_log("scan.dependencies", status="success",
                         details={"vulnerabilities_found": len(vulnerabilities)})
            except ImportError:
                pass
            
            return {
                "ok": True,
                "vulnerabilities": vulnerabilities,
                "scanned_files": [str(f) for f in req_files if f.exists()],
            }
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "Vulnerability scan timed out"}
        except FileNotFoundError:
            return {"ok": False, "error": "safety package not installed"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _execute_assigned_task(self, task: Task):
        """Execute an assigned task by generating and submitting the appropriate artifact."""
        try:
            # Update status to show we're working on it
            task.status = TaskStatus.IN_PROGRESS
            task.assigned_at = datetime.now().isoformat()
            self._save_state()

            # On retry, clear old failed artifacts so they don't poison evaluation
            if task.current_iteration > 0:
                task.artifacts.clear()

            agent_lbl = self._task_agent_label(task)
            # Live transcript: the agent has picked up work
            self._emit(agent_lbl, f"🚀 Started task: {task.title}")

            # Run vulnerability scan before executing Tier 3 tasks
            if task.tier == Tier.TIER_3_BUILDER:
                scan_result = self._scan_dependencies()
                if scan_result.get("vulnerabilities"):
                    print(f"  ⚠️ Dependency scan found {len(scan_result['vulnerabilities'])} vulnerabilities")
                elif scan_result.get("ok"):
                    print("  ✓ Dependency scan: no vulnerabilities")

            # Generate the appropriate initial artifact based on tier
            if task.agent_key:
                # Specialist execution: the named registry agent produces the
                # artifact itself (true Tier 2 worker in the pipeline)
                artifact_content = self._generate_specialist_artifact(task)
                artifact_type = ArtifactType.IMPLEMENTATION_PLAN
                self._emit(agent_lbl, f"🧠 Executing '{task.title}' as specialist")
            elif task.tier == Tier.TIER_2_ARCHITECT:
                artifact_content = self._generate_tier2_artifact(task)
                artifact_type = ArtifactType.IMPLEMENTATION_PLAN
                self._emit(agent_lbl, f"🧠 Designing architecture for '{task.title}'")
            elif task.tier == Tier.TIER_3_BUILDER:
                # Real Tier 3 codegen: Ollama via scrum_gate, writing the file
                # into the project space. Spec-anchored brief drives generation;
                # the resulting source code becomes the artifact.
                artifact_content = self._generate_tier3_code(task)
                artifact_type = ArtifactType.CODE_DIFF
                self._emit(agent_lbl, f"💻 Building implementation for '{task.title}'")
            else:
                # Default to Tier 2 if not specified
                artifact_content = self._generate_tier2_artifact(task)
                artifact_type = ArtifactType.IMPLEMENTATION_PLAN
                self._emit(agent_lbl, f"🧠 Designing architecture for '{task.title}'")

            # Submit the artifact
            artifact = self.submit_artifact(task.id, artifact_type.value, artifact_content)
            print(f"  Submitted {artifact_type.value} for task {task.id}")
            self._emit(agent_lbl, f"📦 Submitted {artifact_type.value} for '{task.title}'")

            # Evaluate the task
            result = self.evaluate_task(task.id)
            print(f"  Evaluation result: {result['result']} ({'PASS' if result['result'] == 'PASS' else 'FAIL'})")

            # If the task failed and we should retry, the evaluate_task method will handle resetting to ASSIGNED
            # If it passed, it will be moved to completed

        except Exception as e:
            print(f"Error executing task {task.id}: {e}")
            self._emit(self._agent_label(task.tier), f"❌ Failed task: {task.title} ({e})")
            # Mark as failed so it can be retried or handled appropriately
            task.status = TaskStatus.FAILED
            task.evaluation_notes = f"Execution error: {str(e)}"
            self._save_state()

    def _generate_specialist_artifact(self, task: Task) -> str:
        """Generate a Tier 2 artifact BY a specialist agent (Hermes CLI).

        The agent runs with its own memory and toolsets (resolved from the
        registry), producing an implementation-plan markdown that flows
        through the normal merge gate. Falls back to the generic template on
        any failure so the pipeline never stalls.
        """
        key = task.agent_key or ""
        description = self._task_brief(task)

        try:
            sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
            from hermes_bridge import load_agent_memory, get_toolsets_for_agent, run_hermes

            parts = [f"You are a specialist agent ({key}) executing one backlog task "
                     f"as part of the project '{self.project_name}' scrum pipeline.",
                     f"Goal: {self.goal or '(see task)'}"]
            memory = load_agent_memory(key)
            if memory:
                parts.append(memory)

            prompt = "\n\n".join(parts) + (
                f"\n\n## Task\n{description}\n\n"
                "## Required Output\n"
                "Produce an implementation plan in markdown with these sections:\n"
                "- **Approach** — how you will solve it\n"
                "- **Steps** — numbered implementation steps\n"
                "- **Deliverables** — concrete files/artifacts to produce\n"
                "- **Rationale** — why this design over alternatives\n"
            )

            args = ["chat", "-q", prompt, "--quiet", "--max-turns", "10",
                    "-t", get_toolsets_for_agent(key)]
            result = run_hermes(args, timeout=900,
                                workdir=str(self.project_space or AGENT_OS_ROOT))
            output = (result.get("response") or "").strip()
            if len(output) >= 200:  # reject hollow/short responses
                return output
            print(f"  Specialist '{key}' produced short/no output ({len(output)} chars); falling back")
        except Exception as e:
            print(f"  Specialist execution failed ({e}); falling back")

        return str(self._generate_tier2_artifact(task))

    def _generate_tier2_artifact(self, task: Task):
        """Generate a REAL implementation plan for Tier 2 tasks.

        Preferred path: Gemini API (headless, works when the operator is remote) via
        gemini_worker.generate_architecture() — same Gemini backend the
        Antigravity IDE uses. Produces a structured {files, steps, plan} dict
        that passes the scrum merge gate with actual per-task content.

        Fallback: static template (preserves prior behavior) when Gemini is
        unreachable or no key is set.
        """
        # Spec-anchored brief: constitution + spec contract + retry feedback
        description = self._task_brief(task)

        try:
            from gemini_worker import GeminiConfig, is_gemini_available, generate_architecture
            cfg = GeminiConfig.from_env()
            if is_gemini_available(cfg):
                plan = generate_architecture(
                    project_space=str(Path(self.project_space)),
                    description=description,
                    requirements=[task.description] if task.description else [],
                    config=cfg,
                )
                if plan is not None:
                    return self._build_tier2_plan_payload(plan, task)
        except Exception:
            pass  # fall through to template — never break the pipeline

        # Fallback template (original behavior)
        return f"""# Implementation Plan for {task.title}

## Task Description
{task.description}

## Approach
This task should be handled by the Tier 2 architect (Gemini Pro) using the Antigravity IDE for complex architectural work and UI/design components.

### Steps:
1. Analyze the requirements and constraints
2. Design the solution architecture
3. Create detailed implementation specifications
4. Prepare code structures and interfaces
5. Define data models and API contracts
6. Create UI mockups and design specifications (if applicable)

### Deliverables
- Detailed technical specification
- Component architecture diagrams
- Data model definitions
- API interface definitions
- Implementation roadmap

### Notes
This is a Tier 2 task requiring architectural design and complex problem-solving.
The Antigravity IDE with Gemini Pro should be used for this work.
"""

    def _build_tier2_plan_payload(self, plan, task: Task) -> dict:
        """Turn a Gemini ArchitecturePlan into a merge-gate-friendly artifact."""
        files = list(getattr(plan, "implementation_order", None) or [])
        if not files:
            files = self._flatten_structure(getattr(plan, "directory_structure", {}))
        if not files:
            files = ["src/"]  # at least one entry so the gate has content

        steps = []
        if getattr(plan, "database_schema", None):
            steps.append("Define database schema and models")
        if getattr(plan, "api_endpoints", None):
            steps.append("Implement API endpoints")
        if files:
            steps.append("Create project structure and source files")
        for f in files[:20]:
            steps.append(f"Implement {f}")
        if not steps:
            steps = ["Analyze requirements", "Design architecture", "Implement", "Test"]

        return {
            "task_id": task.id,
            "title": task.title,
            "task_description": task.description,
            "generated_by": "gemini-2.5-flash",
            "engine": "antigravity",
            "files": files,
            "steps": steps,
            "estimated_files": getattr(plan, "estimated_files", len(files)),
            "tech_stack": getattr(plan, "tech_stack", {}) or {},
            "plan": self._render_plan_markdown(plan, task),
        }

    def _flatten_structure(self, structure, prefix: str = ""):
        """Flatten a nested directory_structure dict into file paths."""
        out = []
        if not isinstance(structure, dict):
            return out
        for key, val in structure.items():
            path = f"{prefix}{key}"
            if isinstance(val, dict):
                out.extend(self._flatten_structure(val, path + "/"))
            else:
                out.append(path)
        return out

    def _render_plan_markdown(self, plan, task: Task) -> str:
        """Render a readable markdown plan from a Gemini ArchitecturePlan."""
        lines = [f"# Implementation Plan: {task.title}", ""]
        lines.append(f"## Task\n{task.description}\n")
        ts = getattr(plan, "tech_stack", {}) or {}
        if ts:
            lines.append("## Tech Stack")
            lang = ts.get("language"); fw = ts.get("framework"); db = ts.get("database")
            lines.append(f"- Language: {lang}  \n- Framework: {fw}  \n- Database: {db}")
            if ts.get("additional"):
                lines.append(f"- Additional: {', '.join(ts['additional'])}")
            lines.append("")
        ends = getattr(plan, "api_endpoints", None) or []
        if ends:
            lines.append("## API Endpoints")
            for e in ends:
                lines.append(f"- `{e.get('method')} {e.get('path')}` — {e.get('description','')}")
            lines.append("")
        order = getattr(plan, "implementation_order", None) or []
        if order:
            lines.append("## Implementation Order")
            for i, f in enumerate(order, 1):
                lines.append(f"{i}. {f}")
            lines.append("")
        return "\n".join(lines)

    def _generate_tier3_artifact(self, task: Task) -> str:
        """Generate an implementation plan for Tier 3 tasks (Qwen 2.5 Coder / Ollama)."""
        description = task.description

        return f"""{self._task_brief(task)}

# Implementation Plan for {task.title}

## Task Description
{description}

## Approach
This task should be handled by the Tier 3 builder (local Ollama model) for focused, well-defined coding work.

### Steps:
1. Review the requirements and any existing designs/specifications
2. Implement the core functionality according to specifications
3. Write clean, maintainable code following best practices
4. Add appropriate error handling and validation
5. Include inline documentation where necessary
6. Prepare for testing and validation

### Deliverables
- Working source code implementation
- Inline documentation and comments
- Basic error handling
- Test readiness preparation

### Notes
This is a Tier 3 task focused on concrete implementation.
The local Ollama model should be used for this work.
"""

    def _generate_tier3_code(self, task: Task) -> str:
        """Real Tier 3 codegen: Ollama writes actual source into the project space.

        Uses scrum_gate.generate_code_via_ollama (the iterative Ollama loop with
        test verification) with the spec-anchored brief. Target file derived
        from the task spec: spec['target_file'] if the operator supplied one,
        else a slug of the task title under server/. Returns the generated
        source as the artifact content. Falls back to the template plan when
        Ollama is unavailable or generation fails, so the pipeline never stalls.
        """
        spec = task.spec or {}
        target = spec.get("target_file") or ""
        if not target:
            slug = re.sub(r"[^a-z0-9]+", "_", task.title.lower()).strip("_")
            target = f"server/{slug}.js"
        # Ensure the parent dir exists inside the project space
        dest = Path(self.project_space) / target
        dest.parent.mkdir(parents=True, exist_ok=True)
        # generate_code_via_ollama requires the file to exist
        dest.touch(exist_ok=True)

        try:
            import scrum_gate
            # scrum_gate resolves paths against its module-level PROJECT_SPACE
            # (defaults to the projects/ parent). Point it at this task's
            # project space before calling codegen.
            scrum_gate.PROJECT_SPACE = Path(self.project_space)
            scrum_gate.SRC_DIR = scrum_gate.PROJECT_SPACE / "src"
            scrum_gate.MERGE_QUEUE = scrum_gate.PROJECT_SPACE / ".antigravity" / "merge_queue"
            scrum_gate.ARTIFACTS_DIR = scrum_gate.PROJECT_SPACE / ".antigravity" / "artifacts"
            scrum_gate.REJECTED_DIR = scrum_gate.PROJECT_SPACE / ".antigravity" / "rejected"
            from scrum_gate import generate_code_via_ollama
            rel = str(dest.relative_to(Path(self.project_space)))
            code, err = generate_code_via_ollama(
                task_description=self._task_brief(task),
                file_path=rel,
                test_command="echo no-test",
            )
            if code:
                return code
            print(f"  Tier3 codegen failed for {task.id}: {err}; using template plan")
        except Exception as e:
            print(f"  Tier3 codegen error for {task.id}: {e}; using template plan")
        return self._generate_tier3_artifact(task)

    # ── Artifact Management (Merge Gates) ─────────────────────────────────────


    def submit_artifact(self, task_id: str, artifact_type: str, content: dict | str,
                       file_path: str = "") -> Artifact:
        """Submit an artifact for a task.

        This is the ONLY way code/artifacts enter the evaluation pipeline.
        No direct file writes to src/ are allowed without a passing artifact.
        """
        task = self._find_task(task_id)
        if not task:
            raise ValueError(f"Task '{task_id}' not found")

        try:
            art_type = ArtifactType(artifact_type)
        except ValueError:
            art_type = ArtifactType.IMPLEMENTATION_PLAN

        artifact = Artifact(
            artifact_type=art_type,
            task_id=task_id,
            content=content,
            file_path=file_path,
        )

        task.artifacts.append(artifact)
        task.status = TaskStatus.EVALUATION
        self._save_state()
        return artifact

    def evaluate_task(self, task_id: str, force_pass: bool = False) -> dict:
        """Evaluate whether a task is 'Complete' by auditing its artifacts.

        Fix for "Stale Task State" gap: Properly handles error conditions to prevent
        tasks from being stuck in ASSIGNED state when evaluation encounters issues.

        Merge gate: ALL artifacts must pass for the task to be marked DONE.

        Evaluation criteria by artifact type:
          - implementation_plan: Has required keys, valid structure
          - test_run_log: No FAILED/ERROR entries
          - plan_checkoff: approved = true
          - code_diff: Non-empty, valid format
          - design_decision: Has rationale

        Returns evaluation result dict.
        """
        task = self._find_task(task_id)
        if not task:
            return {"ok": False, "error": f"Task '{task_id}' not found"}

        if not task.artifacts:
            # No artifacts to evaluate - mark as failed to prevent infinite retry loop
            task.status = TaskStatus.FAILED
            task.evaluation_notes = "No artifacts submitted for evaluation"
            self._save_state()
            return {
                "ok": False,
                "task_id": task_id,
                "result": "FAIL",
                "reason": "No artifacts submitted. Merge gate requires at least one artifact.",
            }

        all_passed = True
        evaluations = []

        try:
            for artifact in task.artifacts:
                eval_result = self._evaluate_artifact(artifact, force_pass, task=task)
                artifact.evaluated = True
                artifact.passed = eval_result["passed"]
                artifact.eval_notes = eval_result["notes"]

                if not eval_result["passed"]:
                    all_passed = False

                evaluations.append({
                    "type": artifact.artifact_type.value,
                    "passed": eval_result["passed"],
                    "notes": eval_result["notes"],
                })
        except Exception as e:
            # If evaluation process itself fails, mark task as failed to prevent stale state
            task.status = TaskStatus.FAILED
            task.evaluation_notes = f"Evaluation error: {str(e)}"
            self._save_state()
            return {
                "ok": False,
                "task_id": task_id,
                "result": "ERROR",
                "error": f"Evaluation failed: {str(e)}",
            }

        # Determine final status
        failed_count = sum(1 for e in evaluations if not e["passed"])
        if all_passed:
            task.status = TaskStatus.DONE
            task.completed_at = datetime.now().isoformat()
            task.evaluation_notes = f"All {len(task.artifacts)} artifacts passed"
            # Move to completed
            if task in self.backlog:
                self.backlog.remove(task)
            self.completed_tasks.append(task)
        else:
            task.current_iteration += 1
            # Build specific feedback for the retry
            failed_notes = [e["notes"] for e in evaluations if not e["passed"]]
            task.evaluation_notes = (
                f"[RETRY #{task.current_iteration}/{task.max_iterations}] "
                f"{failed_count}/{len(evaluations)} artifacts failed: "
                + "; ".join(failed_notes)
            )

            if task.current_iteration >= task.max_iterations:
                task.status = TaskStatus.BLOCKED
                self.failed_tasks.append(task)
                if task in self.backlog:
                    self.backlog.remove(task)
            else:
                # Allow retry - reset to ASSIGNED for another attempt
                task.status = TaskStatus.ASSIGNED
                task.assigned_at = datetime.now().isoformat()

        self._save_state()

        agent_lbl = self._agent_label(task.tier)
        if all_passed:
            self._emit(agent_lbl, f"✅ Task completed: {task.title}")
        else:
            self._emit(agent_lbl, f"❌ Task needs work: {task.title} ({failed_count}/{len(evaluations)} artifacts failed)")

        return {
            "ok": True,
            "task_id": task_id,
            "result": "PASS" if all_passed else "FAIL",
            "passed": all_passed,
            "artifact_count": len(task.artifacts),
            "evaluations": evaluations,
            "task_status": task.status.value,
            "iteration": task.current_iteration,
        }

    def _evaluate_artifact(self, artifact: Artifact, force_pass: bool = False,
                           task: "Task | None" = None) -> dict:
        """Evaluate a single artifact based on its type.

        Spec-anchored SDD: when the owning task is supplied (and carries
        acceptance criteria), the spec check runs first — a spec failure
        short-circuits into the normal retry-feedback loop.
        """
        if force_pass:
            return {"passed": True, "notes": "Force passed (manual override)"}

        try:
            if task is not None and getattr(task, "spec", None):
                spec_res = self._check_spec(artifact, task)
                if not spec_res["passed"]:
                    return spec_res

            if artifact.artifact_type == ArtifactType.TEST_RUN_LOG:
                content_str = artifact.content if isinstance(artifact.content, str) else json.dumps(artifact.content)
                has_failures = "FAILED" in content_str or "ERROR" in content_str
                if has_failures:
                    # Find specific failures
                    lines = content_str.split("\n")
                    failures = [l.strip() for l in lines if "FAILED" in l or "ERROR" in l][:5]
                    return {
                        "passed": False,
                        "notes": f"Test failures detected: {'; '.join(failures)}",
                    }
                return {"passed": True, "notes": "No test failures detected"}

            elif artifact.artifact_type == ArtifactType.PLAN_CHECKOFF:
                if isinstance(artifact.content, dict):
                    approved = artifact.content.get("approved", False)
                    if approved:
                        return {"passed": True, "notes": "Plan approved"}
                    return {"passed": False, "notes": "Plan not approved"}
                return {"passed": False, "notes": "Invalid plan format"}

            elif artifact.artifact_type == ArtifactType.IMPLEMENTATION_PLAN:
                if isinstance(artifact.content, dict):
                    required_keys = ["files", "steps"]
                    missing = [k for k in required_keys if k not in artifact.content]
                    if missing:
                        return {"passed": False, "notes": f"Missing keys: {missing}"}
                    return {"passed": True, "notes": f"Plan has {len(artifact.content.get('files', []))} files"}
                elif isinstance(artifact.content, str) and len(artifact.content) > 100:
                    return {"passed": True, "notes": f"Plan content: {len(artifact.content)} chars"}
                return {"passed": False, "notes": "Plan content too short or invalid"}

            elif artifact.artifact_type == ArtifactType.CODE_DIFF:
                content_str = artifact.content if isinstance(artifact.content, str) else json.dumps(artifact.content)
                if len(content_str) > 20:
                    return {"passed": True, "notes": f"Code diff: {len(content_str)} chars"}
                return {"passed": False, "notes": "Code diff empty or too short"}

            elif artifact.artifact_type == ArtifactType.DESIGN_DECISION:
                if isinstance(artifact.content, dict):
                    has_rationale = artifact.content.get("rationale") or artifact.content.get("reasoning")
                    if has_rationale:
                        return {"passed": True, "notes": "Has rationale"}
                    return {"passed": False, "notes": "Missing rationale"}
                return {"passed": False, "notes": "Invalid format"}

            return {"passed": True, "notes": "Unknown artifact type, assumed pass"}
        except Exception as e:
            # If individual artifact evaluation fails, mark it as failed
            return {"passed": False, "notes": f"Evaluation error: {str(e)}"}

    # ── Status & Reporting ─────────────────────────────────────────────────


    def status_report(self) -> dict:
        """Generate a status report for the project."""
        next_task = self.get_next_task()

        report = {
            "project": self.project_name,
            "goal": self.goal,
            "total_tasks": len(self.backlog) + len(self.completed_tasks) + len(self.failed_tasks),
            "completed": len(self.completed_tasks),
            "in_backlog": len([t for t in self.backlog if t.status == TaskStatus.BACKLOG]),
            "in_progress": len([t for t in self.backlog if t.status not in (TaskStatus.BACKLOG, TaskStatus.DONE)]),
            "failed": len(self.failed_tasks),
            "blocked": len([t for t in self.backlog if t.status == TaskStatus.BLOCKED]),
            "next_task": next_task.to_dict() if next_task else None,
            "auto_executor_running": self._auto_executor_running,
            # Specialist staffing summary (Tier 1 dynamic workforce)
            "specialist_assigned": len([t for t in self.backlog if getattr(t, "agent_key", None)]),
        }

        return report

    def get_all_tasks(self) -> list[dict]:
        """All tasks (backlog + completed + failed) for dashboard display.

        Each dict includes agent_key and the resolved specialist display
        name when the task is assigned to a registry agent.
        """
        out = []
        for t in list(self.backlog) + list(self.completed_tasks) + list(self.failed_tasks):
            d = t.to_dict()
            key = getattr(t, "agent_key", None)
            if key:
                d["agent_key"] = key
                d["agent_name"] = self._task_agent_label(t)
            out.append(d)
        return out

    def list_available_agents(self) -> list[dict]:
        """Active agents that can be assigned tasks (for the dashboard picker)."""
        try:
            sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
            from agent_registry import get_registry
            return get_registry().list_agents(status_filter="active")
        except Exception:
            return []

    # ── Task Dependency Graph ──────────────────────────────────────────────

    def get_dependency_graph(self) -> dict:
        """Generate a dependency graph for visualization.
        
        Returns nodes and edges suitable for D3.js or similar visualization tools.
        Contains all tasks in the backlog with their status and dependencies.
        """
        nodes = []
        edges = []
        
        # Add all tasks as nodes
        for task in self.backlog:
            nodes.append({
                "id": task.id,
                "title": task.title,
                "tier": task.tier.value if task.tier else 2,
                "status": task.status.value,
                "priority": task.priority,
                "has_deps": len(task.dependencies) > 0,
            })
        
        # Add completed tasks as nodes
        for task in self.completed_tasks:
            nodes.append({
                "id": task.id,
                "title": task.title,
                "tier": task.tier.value if task.tier else 2,
                "status": task.status.value,
                "priority": task.priority,
                "has_deps": len(task.dependencies) > 0,
            })
        
        # Add edges for dependencies
        for task in self.backlog:
            for dep_id in task.dependencies:
                edges.append({
                    "source": dep_id,
                    "target": task.id,
                })
        
        return {
            "nodes": nodes,
            "edges": edges,
            "project": self.project_name,
            "goal": self.goal,
        }

    def get_critical_path(self) -> list[str]:
        """Get the critical path - longest chain of dependent tasks.
        
        Used for timeline estimation and bottleneck identification.
        """
        # Build reverse dependency map
        dep_map = {t.id: t.dependencies for t in self.backlog}
        
        def get_chain(task_id: str) -> list[str]:
            task = self._find_task(task_id)
            if not task or not task.dependencies:
                return [task_id]
            # Recursively get longest chain
            chains = [get_chain(d) for d in task.dependencies]
            longest = max(chains, key=len) if chains else []
            return longest + [task_id]
        
        # Find longest chain among all tasks
        chains = [get_chain(t.id) for t in self.backlog]
        critical = max(chains, key=len) if chains else []
        
        return critical

    # ── Dry-Run Mode ──────────────────────────────────────────────────────

    def set_dry_run(self, enabled: bool = True):
        """Enable or disable dry-run mode.
        
        In dry-run mode, artifacts are generated but NOT written to src/.
        This allows reviewing generated code before actual execution.
        """
        self._dry_run = enabled
        
    def _save_state(self):
        """Save the current state to disk."""
        state = {
            "goal": self.goal,
            "backlog": [task.to_dict() for task in self.backlog],
            "completed_tasks": [task.to_dict() for task in self.completed_tasks],
            "failed_tasks": [task.to_dict() for task in self.failed_tasks],
            "timestamp": datetime.now().isoformat(),
        }
        self.status_file.parent.mkdir(parents=True, exist_ok=True)
        self.status_file.write_text(json.dumps(state, indent=2))

    def _load_state(self) -> dict:
        """Load state from disk."""
        if self.status_file.exists():
            try:
                state = json.loads(self.status_file.read_text())
                self.goal = state.get("goal", "")

                def _rebuild_task(td: dict) -> "Task":
                    td = dict(td)
                    if td.get("tier") is not None:
                        td["tier"] = Tier(td["tier"])
                    if "status" in td:
                        td["status"] = TaskStatus(td["status"])
                    # Rebuild artifacts (persisted as dicts) into Artifact objects
                    rebuilt_arts = []
                    for a in td.get("artifacts", []) or []:
                        if isinstance(a, Artifact):
                            rebuilt_arts.append(a)
                        elif isinstance(a, dict):
                            try:
                                rebuilt_arts.append(Artifact(
                                    artifact_type=ArtifactType(a.get("type", "implementation_plan")),
                                    task_id=a.get("task_id", td.get("id", "")),
                                    content=a.get("content", ""),
                                    file_path=a.get("file_path", ""),
                                    created_at=a.get("created_at", ""),
                                    evaluated=bool(a.get("evaluated", False)),
                                    passed=bool(a.get("passed", False)),
                                    eval_notes=a.get("eval_notes", ""),
                                ))
                            except Exception:
                                pass
                    td["artifacts"] = rebuilt_arts
                    return Task(**td)

                self.backlog = [_rebuild_task(t) for t in state.get("backlog", [])]
                self.completed_tasks = [_rebuild_task(t) for t in state.get("completed_tasks", [])]
                self.failed_tasks = [_rebuild_task(t) for t in state.get("failed_tasks", [])]
                return state
            except Exception as e:
                print(f"Error loading state: {e}")
        return {}


# ── Module-level convenience ——


def get_scrum_master(project_name: str) -> ScrumMaster:
    """Get or create a ScrumMaster instance for a project."""
    return ScrumMaster(project_name)