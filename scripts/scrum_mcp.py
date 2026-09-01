#!/usr/bin/env python3
"""
MoiraiCore — Scrum Pipeline MCP Server (improvement #7)

Exposes the spec-anchored SDD scrum pipeline as MCP tools so any AI agent
(Claude, Hermes, Cursor, etc.) can drive it directly — same stdio JSON-RPC
transport as vault-mcp.py.

Tools:
- scrum_status:        project overview (goal, backlog states, counts)
- scrum_set_goal:      set/replace the project goal
- scrum_decompose:     decompose the goal into a backlog (operator task list
                       optional; contracts auto-derived when absent)
- scrum_assign:        assign a task (optionally override tier)
- scrum_execute:       run the next ready task (or a specific one)
- scrum_submit:        submit an artifact for a task
- scrum_evaluate:      run the merge-gate evaluation for a task
- scrum_review:        run the reviewer contract-compliance scan
- scrum_specs:         list task spec files + the project contracts

State lives in the project space exactly like the HTTP pipeline — the MCP
server and the REST API operate on the same ScrumMaster state.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("AGENT_OS_ROOT", str(Path(__file__).resolve().parents[1])))
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

DEFAULT_PROJECT = os.environ.get("MCP_SCRUM_PROJECT", "smoke-test")

_SM: dict = {}   # project_name -> ScrumMaster (cached in-process)


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def _get_sm(project: str | None = None):
    name = (project or DEFAULT_PROJECT).strip().strip("/")
    if name not in _SM:
        from scrum_master import ScrumMaster
        env_space = os.environ.get("MOIRAICORE_PROJECT_SPACE")
        sm = ScrumMaster(name, project_space=env_space)
        try:
            sm._load_state()
        except Exception as e:
            log(f"state load skipped for {name}: {e}")
        _SM[name] = sm
    return _SM[name]


def _simplify_task(t) -> dict:
    return {
        "id": t.id,
        "title": t.title,
        "tier": t.tier.value if t.tier else None,
        "status": t.status.value,
        "priority": t.priority,
        "dependencies": t.dependencies,
        "iterations": f"{t.current_iteration}/{t.max_iterations}",
        "agent_key": t.agent_key,
        "task_role": (t.spec or {}).get("task_role"),
    }


# ── tool implementations ────────────────────────────────────────────────

def tool_scrum_status(args: dict) -> dict:
    sm = _get_sm(args.get("project"))
    return {
        "project": sm.project_name,
        "goal": sm.goal,
        "project_space": sm.project_space,
        "backlog": [_simplify_task(t) for t in sm.backlog],
        "completed": [_simplify_task(t) for t in sm.completed_tasks],
        "failed": [_simplify_task(t) for t in sm.failed_tasks],
        "counts": {
            "backlog": len(sm.backlog),
            "completed": len(sm.completed_tasks),
            "failed": len(sm.failed_tasks),
        },
    }


def tool_scrum_set_goal(args: dict) -> dict:
    sm = _get_sm(args.get("project"))
    sm.set_goal(str(args.get("goal", "")).strip())
    return {"ok": True, "goal": sm.goal}


def tool_scrum_decompose(args: dict) -> dict:
    sm = _get_sm(args.get("project"))
    if args.get("goal"):
        sm.set_goal(str(args["goal"]).strip())
    tasks = sm.decompose_backlog(args.get("tasks"))
    return {
        "ok": True,
        "backlog": [_simplify_task(t) for t in tasks],
        "contracts_file": str(Path(sm.project_space) / "specs" / "contracts.md"),
        "note": ("shared contracts auto-derived (operator may override in "
                 "specs/contracts.md)"),
    }


def tool_scrum_assign(args: dict) -> dict:
    from scrum_master import Tier
    sm = _get_sm(args.get("project"))
    tier = args.get("tier")
    t = sm.assign_task(str(args["task_id"]),
                       Tier(tier) if tier else None)
    return {"ok": True, "task": _simplify_task(t)}


def tool_scrum_execute(args: dict) -> dict:
    sm = _get_sm(args.get("project"))
    task_id = args.get("task_id")
    if task_id:
        t = sm._find_task(str(task_id))
        if not t:
            raise ValueError(f"task {task_id} not found")
        if t.status.name == "BACKLOG":
            sm.assign_task(t.id)
        sm._execute_assigned_task(t)
        return {"ok": True, "task": _simplify_task(t)}
    nxt = sm.get_next_task()
    if not nxt:
        return {"ok": True, "note": "no ready tasks (all done or blocked)"}
    sm.assign_task(nxt.id)
    sm._execute_assigned_task(nxt)
    return {"ok": True, "task": _simplify_task(nxt)}


def tool_scrum_submit(args: dict) -> dict:
    sm = _get_sm(args.get("project"))
    a = sm.submit_artifact(str(args["task_id"]),
                           str(args.get("artifact_type", "implementation_plan")),
                           args.get("content", ""))
    return {"ok": True, "artifact": a.to_dict()}


def tool_scrum_evaluate(args: dict) -> dict:
    sm = _get_sm(args.get("project"))
    return sm.evaluate_task(str(args["task_id"]))


def tool_scrum_review(args: dict) -> dict:
    sm = _get_sm(args.get("project"))
    findings = sm.review_contract_compliance()
    gate = sm._evaluate_review(findings=findings)
    return {"passed": gate["passed"], "findings": findings,
            "notes": gate["notes"]}


def tool_scrum_specs(args: dict) -> dict:
    sm = _get_sm(args.get("project"))
    specs_dir = Path(sm.project_space) / "specs"
    files = sorted(p.name for p in specs_dir.glob("*.md")) if specs_dir.exists() else []
    return {"project": sm.project_name, "spec_files": files,
            "contracts_file": "specs/contracts.md" in files or
                              (specs_dir / "contracts.md").exists()}


# ── MCP plumbing ─────────────────────────────────────────────────────────

TOOLS = [
    ("scrum_status", "Project overview: goal, backlog/completed/failed tasks",
     {"project": {"type": "string", "description": "project name (default MCP_SCRUM_PROJECT)"}},
     tool_scrum_status),
    ("scrum_set_goal", "Set the project goal",
     {"project": {"type": "string"}, "goal": {"type": "string"}},
     tool_scrum_set_goal),
    ("scrum_decompose", "Decompose the goal into a backlog; contracts auto-derived",
     {"project": {"type": "string"},
      "goal": {"type": "string", "description": "overrides stored goal"},
      "tasks": {"type": "array", "description": "optional operator task list "
                "[{title, description, tier, priority, dependencies, spec}]"}},
     tool_scrum_decompose),
    ("scrum_assign", "Assign a task (moves BACKLOG → ASSIGNED)",
     {"project": {"type": "string"}, "task_id": {"type": "string"},
      "tier": {"type": "integer", "description": "2 or 3 (optional override)"}},
     tool_scrum_assign),
    ("scrum_execute", "Execute the next ready task or a specific one "
     "(real model calls — Tier 3 codegen writes files)",
     {"project": {"type": "string"},
      "task_id": {"type": "string", "description": "optional; default = next ready"}},
     tool_scrum_execute),
    ("scrum_submit", "Submit an artifact for evaluation",
     {"project": {"type": "string"}, "task_id": {"type": "string"},
      "artifact_type": {"type": "string",
                        "description": "implementation_plan|code_diff|design_decision|"
                                       "test_run_log|plan_checkoff"},
      "content": {"type": "string"}},
     tool_scrum_submit),
    ("scrum_evaluate", "Run the merge-gate evaluation for a task",
     {"project": {"type": "string"}, "task_id": {"type": "string"}},
     tool_scrum_evaluate),
    ("scrum_review", "Reviewer scan: check every generated file against shared "
     "contracts (fail-closed)",
     {"project": {"type": "string"}}, tool_scrum_review),
    ("scrum_specs", "List task spec files and project contracts",
     {"project": {"type": "string"}}, tool_scrum_specs),
]


def handle_initialize(req):
    return {
        "protocolVersion": "2024-11-05",
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "moiraicore-scrum-mcp", "version": "1.0.0"},
    }


def handle_tools_list(req):
    return {"tools": [
        {"name": name, "description": desc, "inputSchema": {
            "type": "object", "properties": schema, "additionalProperties": True}}
        for name, desc, schema, _fn in TOOLS
    ]}


def handle_tool_call(params: dict) -> dict:
    name = params.get("name", "")
    args = params.get("arguments") or {}
    for tname, _d, _s, fn in TOOLS:
        if tname == name:
            try:
                result = fn(args)
                return {"content": [
                    {"type": "text", "text": json.dumps(result, indent=2, default=str)
                     if not isinstance(result, str) else result}],
                    "isError": False}
            except Exception as e:
                return {"content": [{"type": "text", "text": f"error: {e}"}],
                        "isError": True}
    return {"content": [{"type": "text", "text": f"unknown tool: {name}"}],
            "isError": True}


def main():
    log(f"MoiraiCore Scrum Pipeline MCP — project: {DEFAULT_PROJECT} (root: {ROOT})")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = req.get("method", "")
        req_id = req.get("id")
        if method == "initialize":
            resp = {"jsonrpc": "2.0", "id": req_id, "result": handle_initialize(req)}
        elif method == "tools/list":
            resp = {"jsonrpc": "2.0", "id": req_id, "result": handle_tools_list(req)}
        elif method == "tools/call":
            resp = {"jsonrpc": "2.0", "id": req_id,
                    "result": handle_tool_call(req.get("params", {}))}
        elif method == "notifications/initialized":
            continue
        elif method == "ping":
            resp = {"jsonrpc": "2.0", "id": req_id, "result": {}}
        else:
            resp = {"jsonrpc": "2.0", "id": req_id,
                    "error": {"code": -32601, "message": f"Unknown method: {method}"}}
        print(json.dumps(resp), flush=True)


if __name__ == "__main__":
    main()
