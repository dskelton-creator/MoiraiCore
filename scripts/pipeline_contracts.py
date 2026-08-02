#!/usr/bin/env python3
"""
MoiraiCore — Structured Pipeline Contracts (Feature 7)
===================================================

The multi-agent pipeline (Goal Mode) moves data between discrete STAGES:

    goal ──▶ decomposition ──▶ task_card ──▶ task_output ──▶ verification ──▶ merge_request

Each handoff has a STRUCTURAL contract: the shape of data the next stage is
allowed to receive. This module enforces those contracts as hard, auditable
gates — NOT suggestions, NOT LLM-judged quality (that's goal_verifier's job).

Why structural (not quality):
  - A task_output can be high quality but structurally malformed (empty string,
    missing session_id) and would crash the next stage. Catching that early is
    real enforcement.
  - These checks are deterministic, instant, offline, and log every breach to
    the audit trail so the gate is visible and accountable.

Design:
  - A tiny schema engine (no external deps) supports: required fields, type
    checks, enum membership, min/max length, min items, custom predicates.
  - `validate(stage, data)` → (ok: bool, errors: list[str]).
  - `enforce(stage, data, context=None)` → raises ContractViolation on failure
    AND records the breach to the audit log (real, durable evidence).
  - Violations are also written to config/pipeline_violations.jsonl so the
    dashboard can show a live "Pipeline integrity" panel.

Known agent set is imported best-effort from agent_registry; falls back to a
built-in set so this module never fails to import.

Usage:
    from pipeline_contracts import enforce, validate, ContractViolation
    enforce("decomposition", decomposition_dict, context={"goal_id": gid})
"""

from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

AGENT_OS_ROOT = Path(os.environ.get("AGENT_OS_ROOT", str(Path(__file__).resolve().parents[1])))
CONFIG_DIR = AGENT_OS_ROOT / "config"
SCRIPTS_DIR = AGENT_OS_ROOT / "scripts"
VIOLATIONS_FILE = CONFIG_DIR / "pipeline_violations.jsonl"

sys.path.insert(0, str(SCRIPTS_DIR))

# Best-effort imports — never break import if a peer module is missing.
try:
    from activity_log import log_activity as _log_activity
except Exception:
    _log_activity = None

# ── Webhooks / Event Bus (Feature 8) ──
_emit_event = None
try:
    from webhooks import emit as _emit_event
except ImportError:
    _emit_event = None

def _fire(event: str, payload: dict) -> None:
    """Best-effort event emit. Never raises into the pipeline."""
    if _emit_event is not None:
        try:
            _emit_event(event, payload)
        except Exception:
            pass



_KNOWN_AGENTS = {"hermes", "researcher", "writer", "developer", "seo",
                 "threat", "antigravity", "codex", "orchestrator", "guardian"}
try:
    from agent_registry import merged_definitions
    _defs = merged_definitions()
    if isinstance(_defs, dict) and _defs:
        _KNOWN_AGENTS |= set(_defs.keys())
except Exception:
    pass

VALID_TASK_STATUSES = {"backlog", "progress", "review", "done"}
VALID_PRIORITIES = {"p0", "p1", "p2", "p3"}
VALID_TIERS = {2, 3}


class ContractViolation(Exception):
    """Raised when a pipeline stage handoff violates its structural contract."""
    def __init__(self, stage: str, errors: list[str], context: Optional[dict] = None):
        self.stage = stage
        self.errors = errors
        self.context = context or {}
        msg = f"Contract violation at stage '{stage}': " + "; ".join(errors)
        super().__init__(msg)


# ── Schema primitives ──

def _is_type(v: Any, t: str) -> bool:
    if t == "str":
        return isinstance(v, str)
    if t == "int":
        return isinstance(v, int) and not isinstance(v, bool)
    if t == "float":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if t == "bool":
        return isinstance(v, bool)
    if t == "dict":
        return isinstance(v, dict)
    if t == "list":
        return isinstance(v, list)
    return False


class FieldSpec:
    """Lightweight field rule."""
    def __init__(self, required=True, type=None, enum=None, min_len=None,
                 max_len=None, min_items=None, max_items=None,
                 allowed=None, predicate=None, desc=""):
        self.required = required
        self.type = type
        self.enum = enum
        self.min_len = min_len
        self.max_len = max_len
        self.min_items = min_items
        self.max_items = max_items
        self.allowed = allowed          # set of allowed values (for str enums)
        self.predicate = predicate      # Callable[[Any], bool]
        self.desc = desc

    def check(self, value, errors: list, path: str):
        if self.type and not _is_type(value, self.type):
            errors.append(f"{path}: expected type '{self.type}', got '{type(value).__name__}'")
            return
        if self.enum is not None and value not in self.enum:
            errors.append(f"{path}: value {value!r} not in allowed set {self.enum}")
            return
        if self.allowed is not None and value not in self.allowed:
            errors.append(f"{path}: value {value!r} not in allowed set {sorted(self.allowed)}")
            return
        if isinstance(value, str):
            if self.min_len is not None and len(value) < self.min_len:
                errors.append(f"{path}: length {len(value)} < min_len {self.min_len}")
            if self.max_len is not None and len(value) > self.max_len:
                errors.append(f"{path}: length {len(value)} > max_len {self.max_len}")
        if isinstance(value, list):
            if self.min_items is not None and len(value) < self.min_items:
                errors.append(f"{path}: {len(value)} items < min_items {self.min_items}")
            if self.max_items is not None and len(value) > self.max_items:
                errors.append(f"{path}: {len(value)} items > max_items {self.max_items}")
        if self.predicate is not None and not self.predicate(value):
            errors.append(f"{path}: failed custom predicate ({self.desc})")


# ── Contract definitions ──
# Each contract = ordered dict of field -> FieldSpec.

CONTRACTS: dict[str, dict[str, FieldSpec]] = {
    # 1) Goal decomposition produced by decompose_goal_with_llm
    "decomposition": {
        "subtasks": FieldSpec(required=True, type="list", min_items=1, max_items=12,
                              desc="at least one subtask"),
        "reasoning": FieldSpec(required=False, type="str"),
    },
    # 1b) each element inside decomposition["subtasks"]
    "subtask": {
        "title": FieldSpec(required=True, type="str", min_len=3, max_len=200),
        "agent": FieldSpec(required=True, type="str", allowed=_KNOWN_AGENTS,
                           desc="must be a registered agent key"),
        "desc": FieldSpec(required=False, type="str", max_len=2000),
        "priority": FieldSpec(required=False, type="str", allowed=VALID_PRIORITIES),
        "depends_on": FieldSpec(required=False, type="int"),
    },
    # 2) Kanban task card created from a subtask
    "task_card": {
        "id": FieldSpec(required=True, type="str", min_len=3),
        "title": FieldSpec(required=True, type="str", min_len=1, max_len=200),
        "agent": FieldSpec(required=True, type="str", allowed=_KNOWN_AGENTS),
        "goal_id": FieldSpec(required=True, type="str", min_len=1),
        "status": FieldSpec(required=True, type="str", allowed=VALID_TASK_STATUSES),
        "depends_on": FieldSpec(required=False, type="int"),
    },
    # 3) Output returned by _execute_task — gating before verification/merge
    "task_output": {
        "ok": FieldSpec(required=True, type="bool"),
        "output": FieldSpec(required=True, type="str", min_len=1,
                            desc="non-empty output required when ok=true"),
        "session_id": FieldSpec(required=False, type="str"),
        "routed_to": FieldSpec(required=False, type="str"),
    },
    # 4) Verification verdict from goal_verifier
    "verification": {
        "status": FieldSpec(required=True, type="str", allowed={"pass", "partial", "fail"}),
        "score": FieldSpec(required=True, type="number", predicate=lambda v: 0.0 <= v <= 1.0,
                           desc="score must be 0.0-1.0"),
    },
    # 5) Merge request submitted to ScrumGate
    "merge_request": {
        "task_id": FieldSpec(required=True, type="str", min_len=1),
        "file_path": FieldSpec(required=True, type="str", min_len=1),
        "generated_code": FieldSpec(required=True, type="str", min_len=1,
                                    desc="non-empty code to merge"),
        "tier": FieldSpec(required=True, type="int", allowed=VALID_TIERS,
                          desc="must route through Tier 2 or 3"),
        "engine": FieldSpec(required=True, type="str", min_len=1),
    },
}

# Stages that support per-element contract checks (list-typed containers).
_LIST_ITEM_CONTRACTS = {
    "decomposition": ("subtasks", "subtask"),
}


# ── Core API ──

def validate(stage: str, data: Any) -> tuple[bool, list[str]]:
    """
    Validate `data` against the named contract.
    Returns (ok, errors). Never raises.
    """
    if stage not in CONTRACTS:
        return False, [f"Unknown contract stage: '{stage}'"]
    spec = CONTRACTS[stage]
    errors: list[str] = []

    # Top-level data must be a dict for all defined contracts.
    if not isinstance(data, dict):
        return False, [f"stage '{stage}': expected dict, got {type(data).__name__}"]

    for field, fspec in spec.items():
        present = field in data and data[field] is not None
        if not present:
            if fspec.required:
                errors.append(f"missing required field '{field}'")
            continue
        fspec.check(data[field], errors, path=field)

    # Per-element checks (e.g. each subtask in a decomposition)
    if stage in _LIST_ITEM_CONTRACTS:
        list_field, item_stage = _LIST_ITEM_CONTRACTS[stage]
        for i, item in enumerate(data.get(list_field, []) or []):
            ok, item_errors = validate(item_stage, item)
            for e in item_errors:
                errors.append(f"{list_field}[{i}].{e}")

    return (len(errors) == 0), errors


def get_contract_schema(stage: str) -> dict:
    """Return a JSON-serialisable description of a contract (for the dashboard)."""
    if stage not in CONTRACTS:
        return {}
    out = {}
    for field, f in CONTRACTS[stage].items():
        out[field] = {
            "required": f.required,
            "type": f.type,
            "allowed": sorted(f.allowed) if f.allowed is not None else None,
            "enum": sorted(f.enum) if f.enum is not None else None,
            "min_len": f.min_len,
            "max_len": f.max_len,
            "min_items": f.min_items,
            "desc": f.desc or None,
        }
    return out


def list_contracts() -> dict:
    return {stage: get_contract_schema(stage) for stage in CONTRACTS}


# ── Enforcement + audit ──

_viol_log_lock = threading.Lock()


def _record_violation(stage: str, errors: list[str], context: Optional[dict]):
    entry = {
        "ts": datetime.now().isoformat(),
        "stage": stage,
        "errors": errors,
        "context": context or {},
    }
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with _viol_log_lock:
            with open(VIOLATIONS_FILE, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass
    # Mirror to the global audit trail so the breach is visible/accountable.
    if _log_activity is not None:
        try:
            ctx = context or {}
            _log_activity(
                message=f"Pipeline contract violation at '{stage}': {'; '.join(errors)}",
                kind="orchestration",
                agent=ctx.get("actor", "pipeline"),
                goal_id=str(ctx.get("goal_id", "")),
                task_id=str(ctx.get("task_id", "")),
                stage=stage,
                status="blocked",
            )
        except Exception:
            pass
    # ── Feature 8: fire contract.violation ──
    _fire("contract.violation", {
        "stage": stage,
        "errors": errors,
        "context": context or {},
    })
    return entry


def enforce(stage: str, data: Any, context: Optional[dict] = None,
            raise_on_fail: bool = True) -> tuple[bool, list[str]]:
    """
    Validate AND enforce a contract boundary.

    On failure (default): records the breach to the audit trail +
    pipeline_violations.jsonl, then raises ContractViolation (if raise_on_fail).
    If raise_on_fail=False: returns (False, errors) without raising, but STILL
    records the breach (so it's always auditable).

    On success: returns (True, []).
    """
    ok, errors = validate(stage, data)
    if not ok:
        _record_violation(stage, errors, context)
        if raise_on_fail:
            raise ContractViolation(stage, errors, context)
    return ok, errors


def read_violations(limit: int = 100) -> list[dict]:
    """Return recent pipeline contract violations (newest last)."""
    if not VIOLATIONS_FILE.exists():
        return []
    try:
        lines = VIOLATIONS_FILE.read_text(encoding="utf-8").splitlines()
        out = [json.loads(l) for l in lines if l.strip()]
        return out[-limit:]
    except Exception:
        return []


if __name__ == "__main__":
    # Quick self-test when run directly.
    bad_decomp = {"subtasks": [], "reasoning": "x"}
    ok, errs = validate("decomposition", bad_decomp)
    print("decomposition(empty subtasks):", ok, errs)

    good_sub = {"title": "Research competitors", "agent": "researcher", "priority": "p1"}
    ok, errs = validate("subtask", good_sub)
    print("subtask(good):", ok, errs)

    bad_sub = {"title": "x", "agent": "not_a_real_agent"}
    ok, errs = validate("subtask", bad_sub)
    print("subtask(bad):", ok, errs)

    good_out = {"ok": True, "output": "Here is the report..."}
    ok, errs = validate("task_output", good_out)
    print("task_output(good):", ok, errs)

    bad_out = {"ok": True, "output": ""}
    ok, errs = validate("task_output", bad_out)
    print("task_output(empty when ok):", ok, errs)

    print("\nContracts defined:", list(CONTRACTS.keys()))
