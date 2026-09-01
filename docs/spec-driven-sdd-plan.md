# Spec-Anchored SDD for MoiraiCore — Implementation Sketch

Status: proposal (sketch, not yet implemented). Method: spec-anchored SDD — specs
are per-task artifacts that gate evaluation; NOT spec-as-source, NOT Spec Kit CLI.

## What changes

Three touch points, no new dependencies:

1. `Task` gains a spec field + `write_spec()` (scrum_master.py)
2. Generation prompts embed the spec (scrum_master.py, ~3 lines each)
3. Evaluation checks artifact against spec acceptance criteria
   (scrum_master.py `_evaluate_artifact` + scrum_gate.py merge-queue check)

## 1. Constitution file — `config/constitution.md` (new)

One source of truth replacing the hardcoded preamble split across
`agent_working_method.py` and prompt strings. Fallback: existing
`WORKING_METHOD_PREAMBLE` when the file is absent.

```markdown
# Project Constitution (non-negotiable)
1. Evidence over vibes — outputs cite verified results.
2. Challenge ambiguity — state assumptions, never invent requirements.
3. Record decisions — WHY alongside WHAT (Decisions & Lessons note).
4. Specs are contracts — every artifact must satisfy its task spec's
   acceptance criteria verbatim; deviations are gate failures.
```

Loader in scrum_master.py:

```python
def _load_constitution() -> str:
    p = Path(__file__).resolve().parents[1] / "config" / "constitution.md"
    try:
        text = p.read_text().strip()
        if len(text) >= 100:
            return text
    except Exception:
        pass
    return _WORKING_METHOD  # existing agent_working_method fallback
```

## 2. Spec as a first-class Task field — scrum_master.py

```python
@dataclass
class Task:
    ...  # existing fields
    spec: dict = field(default_factory=dict)
    # spec = {"intent": str, "constraints": [str],
    #         "acceptance_criteria": [str], "out_of_scope": [str]}
```

a) In `decompose_backlog()`, accept optional `spec` from each task dict
   (operator-supplied specs win).

b) For auto-decomposed tasks, derive a minimal spec — deterministic,
   no extra model call:

```python
def _derive_spec(self, task: Task) -> dict:
    """Spec-anchored SDD: every task carries a minimal, checkable spec."""
    return {
        "intent": task.description.strip() or task.title,
        "constraints": [
            "Must pass the ScrumGate merge checks (non-empty, compiles, no dangerous patterns)",
        ],
        "acceptance_criteria": [
            f"Artifact addresses the stated intent: '{task.title}'",
            "Artifact is concrete (references real file paths / function names where applicable)",
        ],
        "out_of_scope": [],
    }
```

Call it inside `decompose_backlog()` when `td.get("spec")` is absent, and in
`Task.__post_init__`-safe paths (or wherever tasks are constructed) so older
saved state without `spec` still gets a default via `to_dict`/loader.

c) Persist `spec` in `Task.to_dict()` and the state loader.

d) Emit specs as files in the project space (indexable by vault):

```python
def write_spec_file(self, task: Task) -> str:
    specs_dir = Path(self.project_space) / "specs"
    specs_dir.mkdir(parents=True, exist_ok=True)
    p = specs_dir / f"{task.id}-spec.md"
    if not p.exists():
        s = task.spec or {}
        lines = [f"# Spec — {task.title}", "", f"## Intent", s.get("intent", "")]
        for key in ("constraints", "acceptance_criteria", "out_of_scope"):
            items = s.get(key) or []
            lines += [f"## {key.replace('_', ' ').title()}"] + [f"- {i}" for i in items]
        p.write_text("\n".join(lines) + "\n")
    return str(p)
```

## 3. Embed spec in generation prompts — scrum_master.py

In `_generate_specialist_artifact`, `_generate_tier2_artifact`, and
`_generate_tier3_artifact`, replace the description assembly with:

```python
def _task_brief(self, task: Task) -> str:
    """Spec-anchored brief: constitution + spec + task + retry feedback."""
    s = task.spec or self._derive_spec(task)
    spec_md = (
        "## Task Spec (the contract — your artifact MUST satisfy these)\n"
        f"**Intent:** {s.get('intent','')}\n"
        f"**Constraints:**\n" + "".join(f"- {c}\n" for c in s.get("constraints", [])) +
        f"**Acceptance criteria (checked at the gate):**\n" +
        "".join(f"- {a}\n" for a in s.get("acceptance_criteria", [])) +
        (f"**Out of scope:**\n" + "".join(f"- {o}\n" for o in s.get("out_of_scope", []))
         if s.get("out_of_scope") else "")
    )
    brief = f"{self._load_constitution()}\n\n{spec_md}\n\n{task.title}\n\n{task.description}"
    if task.current_iteration > 0 and task.evaluation_notes:
        brief += f"\n\n=== PREVIOUS ATTEMPT FAILED — FIX THESE ISSUES ===\n{task.evaluation_notes}"
    return brief
```

Each generator then becomes e.g.
`description = self._task_brief(task)` (replacing the current
`f"{_WORKING_METHOD}\n\n{task.title}\n\n{task.description}"` blocks and the
three duplicated retry-feedback blocks).

## 4. Gate checks artifacts against the spec — the high-ROI change

a) New helper (deterministic, model-free first pass):

```python
def _check_spec(self, artifact: Artifact, task: Task) -> dict:
    """Spec-anchored check: acceptance criteria coverage. Cheap heuristics
    only; anything subjective is left to the existing evaluator flow."""
    s = task.spec or {}
    content = artifact.content if isinstance(artifact.content, str) else json.dumps(artifact.content)
    text = content.lower()
    failures = []
    for i, ac in enumerate(s.get("acceptance_criteria", []), 1):
        # Keyword-coverage check: extract salient tokens from the criterion
        # (>3 chars, not stopwords) and require most to appear in the artifact.
        toks = [t for t in re.findall(r"[a-z_]{4,}", ac.lower())
                if t not in {"must", "should", "artifact", "real", "file", "where", "with", "that"}]
        if toks:
            hit = sum(1 for t in toks if t in text)
            if hit / len(toks) < 0.5:
                failures.append(f"AC{i} not addressed: '{ac[:80]}'")
    return {"passed": not failures, "notes": "; ".join(failures) or "All acceptance criteria addressed"}
```

b) Wire into `_evaluate_artifact()` (scrum_master.py ~line 1089) — run the
spec check for IMPLEMENTATION_PLAN artifacts before the existing checks:

```python
if artifact.artifact_type == ArtifactType.IMPLEMENTATION_PLAN:
    task = self._find_task(artifact.task_id)
    if task:
        spec_res = self._check_spec(artifact, task)
        if not spec_res["passed"]:
            return spec_res   # feeds the existing PREVIOUS ATTEMPT FAILED loop
    # ...existing structure checks continue unchanged
```

c) Same idea in scrum_gate.py's merge-queue `evaluate()` (line ~295): if a
spec file `specs/{task_id}-spec.md` exists, require the merged code to hit
the same keyword-coverage gate before the syntax/security checks approve it.
Optional first pass — the scrum_master check is the primary gate.

## 5. Retry loop (no change needed — it gets better for free)

The existing critique-feedback loop (line ~1050, `PREVIOUS ATTEMPT FAILED`)
now carries spec-specific failures ("AC2 not addressed: ...") instead of
vague "too short" notes, and `_task_brief` re-injects the unchanged spec, so
retries converge on the contract rather than on length heuristics.

## 6. Rollout

- Order: constitution file → spec field + `_derive_spec` → `_task_brief`
  in the three generators → `_check_spec` in `_evaluate_artifact`
  → (optional) scrum_gate merge-queue check.
- Trial on the QuongTea project's next Tier 2 feature task only; compare
  retry counts and gate rejections before/after.
- No Spec Kit / Specify CLI — the pipeline is headless Hermes-driven and the
  methodology above delivers the same checkpoints without IDE tooling.
- Keep specs lightweight for ad-hoc Tier 3 micro-fixes (auto-derived spec is
  fine); operator-written specs only for features.

---

## Update — Aug 2026: shared contracts implemented (improvements #1, #9, #2)

The cross-file divergence weakness identified above is now closed in code:

- **#1 Shared contracts** (commit d1efb71): task specs carry `contracts` dicts; project-wide agreements live in `specs/contracts.md`. `effective_contracts()` merges (task wins). `_task_brief()` renders them; `_check_spec()` token-checks contract values — violations fail the gate.
- **#9 Auto-derivation** (commit cabf5eb): when no project contracts exist at decompose time, `derive_and_apply_contracts()` drafts them (`scripts/auto_contracts.py`) — heuristic route/unit/field extraction + optional Tier-2 JSON drafting. Toggle: `sm.auto_contracts_use_model = False`.
- **#2 Reviewer tasks** (commit d1784ea): `spec.task_role: "reviewer"` tasks scan each DONE implementation task's **generated file** (not artifact text) against effective contracts and fail closed. `_review_brief()` assembles contracts + intents + file contents + mechanical findings for the model.

Lesson from the validation run against the original wfm-wmo artifacts: keep contracts tight and file-scoped — broad sets applied across unrelated files produce noise findings. Genuinely shared items (status values, block units) are the right granularity.

Tests: `tests/test_contracts.py`, `tests/test_reviewer_tasks.py`, `tests/test_auto_contracts.py` (38 combined). Suite: 72 passing.
