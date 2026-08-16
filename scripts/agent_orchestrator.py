#!/usr/bin/env python3
"""
MoiraiCore — Multi-Agent Orchestrator
Decomposes complex tasks into parallel sub-agent runs, collects results, synthesizes.

How it works:
  1. Analyze task → determine required agent roles
  2. Decompose into parallel subtasks (one per agent)
  3. Spawn each subtask as an isolated Hermes CLI call
  4. Collect outputs from all sub-agents
  5. Synthesize final result
  6. Log orchestration run to activity database

Usage:
    python3 agent_orchestrator.py run "Build a complete SEO analysis for my website"
    python3 agent_orchestrator.py run "Research ASX competitors and write a report" --agents researcher,writer
    python3 agent_orchestrator.py status <run_id>
    python3 agent_orchestrator.py history

Exit codes:
    0 = success
    1 = partial failure (some sub-agents failed)
    2 = complete failure
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

AGENT_OS_ROOT = Path(os.environ.get("AGENT_OS_ROOT", str(Path(__file__).resolve().parents[1])))
SCRIPTS_DIR = AGENT_OS_ROOT / "scripts"
RUNS_DIR = AGENT_OS_ROOT / "config" / "orchestration-runs"
HERMES_CLI = os.environ.get("HERMES_CLI", "")
if not HERMES_CLI:
    _candidates = [
        os.path.join(os.path.expanduser("~"), ".hermes", "hermes-agent", "venv", "bin", "hermes"),
        os.path.join(os.path.expanduser("~"), ".local", "bin", "hermes"),
    ]
    for _c in _candidates:
        if os.path.isfile(_c):
            HERMES_CLI = _c
            break
    else:
        HERMES_CLI = "hermes"

# ── Lazy-loaded modules ──
_agent_registry = None
_vault_index = None

def get_registry():
    global _agent_registry
    if _agent_registry is None:
        sys.path.insert(0, str(SCRIPTS_DIR))
        from agent_registry import AgentRegistry
        _agent_registry = AgentRegistry()
    return _agent_registry

def get_vault_index():
    global _vault_index
    if _vault_index is None:
        sys.path.insert(0, str(SCRIPTS_DIR))
        from vault_index import VaultIndex
        _vault_index = VaultIndex()
    return _vault_index


# ── Task Decomposition ──

def decompose_task(task: str, preferred_agents: list[str] = None) -> list[dict]:
    """
    Analyze a task and decompose it into parallel subtasks.
    Returns a list of {agent, subtask, context} dicts.
    """
    reg = get_registry()
    task_lower = task.lower()

    # Determine which agents are needed
    agents_needed = []

    if preferred_agents:
        # User explicitly specified agents
        for a in preferred_agents:
            agent_def = reg.get_agent(a.strip())
            if agent_def and agent_def["status"] == "active":
                agents_needed.append(a.strip())
    else:
        # Auto-detect: check each active agent's triggers
        for agent_def in reg.list_agents(status_filter="active"):
            key = agent_def["key"]
            if key == "hermes":
                continue
            triggers = agent_def.get("triggers", [])
            matched = [t for t in triggers if t in task_lower]
            if matched:
                agents_needed.append(key)

    # If no agents matched, use Hermes as a single agent
    if not agents_needed:
        hermes_def = reg.get_agent("hermes")
        return [{
            "agent": "hermes",
            "agent_name": "Hermes",
            "subtask": task,
            "context": "",
            "triggers": [],
            "role": "General-Purpose Agent",
            "reason": "No specialized agents matched — using Hermes general-purpose.",
        }]

    # Build subtasks per agent
    subtasks = []
    for agent_key in agents_needed:
        agent_def = reg.get_agent(agent_key)
        if not agent_def:
            continue

        # Load agent memory for context
        memory = reg.read_memory(agent_key) or ""

        # Build agent-specific subtask prompt
        subtask = _build_subtask(task, agent_key, agent_def)

        subtasks.append({
            "agent": agent_key,
            "agent_name": agent_def["name"],
            "subtask": subtask,
            "context": memory[:2000],  # Cap memory context
            "triggers": agent_def.get("triggers", []),
            "role": agent_def["role"],
            "skills": agent_def.get("skills", []),
        })

    return subtasks


def _build_subtask(full_task: str, agent_key: str, agent_def: dict) -> str:
    """Build a specialized subtask prompt for a specific agent role."""
    role = agent_def["role"]

    # Read the agent's orchestration template if it exists
    template_path = AGENT_OS_ROOT / "memory-vault" / "agents" / "orchestrations" / f"templates/{agent_key}.md"
    template = ""
    if template_path.exists():
        template = template_path.read_text(errors="ignore")

    # Cap full task to 200 chars per subagent to keep prompts lean
    task_snippet = full_task[:200] + ("…" if len(full_task) > 200 else "")

    parts = [f"You are the {agent_def['name']} ({role}). Be concise and actionable.\n"]

    if template:
        parts.append(f"## Output Format\n{template}\n")

    parts.append(f"## Task\n{task_snippet}\n")
    parts.append("Provide your output now. Use vault tools to write findings to files if the response is long.")

    return "\n".join(parts)


# ── Sub-Agent Execution ──

def run_subagent(subtask: dict, run_id: str, timeout: int = 300) -> dict:
    """
    Execute a subtask via Hermes CLI as an isolated sub-agent.
    Returns {agent, status, output, duration_ms, error}.
    """
    agent_key = subtask["agent"]
    agent_name = subtask["agent_name"]
    prompt = subtask["subtask"]
    context = subtask.get("context", "")

    start = time.time()

    # Build the full prompt with context
    if context:
        full_prompt = f"## Agent Memory ({agent_name})\n\n{context}\n\n{prompt}"
    else:
        full_prompt = prompt

    # Save prompt to a temp file for clean CLI passing
    prompt_file = RUNS_DIR / f"{run_id}_{agent_key}_prompt.txt"
    prompt_file.parent.mkdir(parents=True, exist_ok=True)
    prompt_file.write_text(full_prompt)

    # Build Hermes args — use -q for query mode (stdin not supported by hermes CLI)
    hermes_args = [HERMES_CLI, "chat", "-q", full_prompt, "--quiet"]
    for sk in subtask.get("skills", []):
        hermes_args += ["-s", sk]

    try:
        result = subprocess.run(
            hermes_args,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ},
            cwd=str(AGENT_OS_ROOT),
        )

        duration_ms = int((time.time() - start) * 1000)
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()

        # Extract session ID (Hermes writes it to stderr)
        session_id = None
        for text in [stdout, stderr]:
            m = re.search(r"session_id:\s*(\S+)", text)
            if m:
                session_id = m.group(1)
                break

        # Clean response (remove session_id line if it ended up in stdout)
        response_text = re.sub(r"session_id:\s*\S+\n?", "", stdout).strip()

        # Save output
        output_file = RUNS_DIR / f"{run_id}_{agent_key}_output.md"
        output_file.write_text(f"# {agent_name} Output\n\n{response_text}")

        return {
            "agent": agent_key,
            "agent_name": agent_name,
            "status": "completed" if result.returncode == 0 else "failed",
            "output": response_text,
            "output_path": str(output_file),
            "session_id": session_id,
            "duration_ms": duration_ms,
            "error": stderr if result.returncode != 0 else None,
        }

    except subprocess.TimeoutExpired:
        duration_ms = int((time.time() - start) * 1000)
        return {
            "agent": agent_key,
            "agent_name": agent_name,
            "status": "timeout",
            "output": None,
            "output_path": None,
            "session_id": None,
            "duration_ms": duration_ms,
            "error": f"Timed out after {timeout}s",
        }
    except Exception as e:
        duration_ms = int((time.time() - start) * 1000)
        return {
            "agent": agent_key,
            "agent_name": agent_name,
            "status": "error",
            "output": None,
            "output_path": None,
            "session_id": None,
            "duration_ms": duration_ms,
            "error": str(e),
        }


# ── Orchestration Run ──

def run_orchestration(task: str, preferred_agents: list[str] = None,
                      parallel: bool = True, timeout: int = 900,
                      run_id: str = None) -> dict:
    """
    Full orchestration: decompose → spawn → collect → log.
    Returns the complete run record.
    """
    if not run_id:
        run_id = f"orch-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    started_at = datetime.now().isoformat()

    # Step 1: Decompose
    subtasks = decompose_task(task, preferred_agents)
    print(f"📋 Orchestration {run_id}: {len(subtasks)} sub-agent(s)")
    for st in subtasks:
        print(f"   → {st['agent_name']}: {st['subtask'][:80]}...")

    # Step 2: Execute sub-agents
    results = []
    overall_start = time.time()

    if parallel and len(subtasks) > 1:
        # Parallel execution
        print(f"\n⚡ Running {len(subtasks)} sub-agents in parallel…")
        with ThreadPoolExecutor(max_workers=min(len(subtasks), 4)) as executor:
            futures = {
                executor.submit(run_subagent, st, run_id, timeout): st
                for st in subtasks
            }
            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                status_icon = "✅" if result["status"] == "completed" else "❌"
                print(f"   {status_icon} {result['agent_name']} — {result['status']} ({result['duration_ms']}ms)")
    else:
        # Sequential execution
        print(f"\n🔄 Running {len(subtasks)} sub-agents sequentially…")
        for st in subtasks:
            result = run_subagent(st, run_id, timeout)
            results.append(result)
            status_icon = "✅" if result["status"] == "completed" else "❌"
            print(f"   {status_icon} {result['agent_name']} — {result['status']} ({result['duration_ms']}ms)")

    overall_duration_ms = int((time.time() - overall_start) * 1000)

    # Step 3: Build run record
    completed = sum(1 for r in results if r["status"] == "completed")
    failed = sum(1 for r in results if r["status"] != "completed")

    run_record = {
        "run_id": run_id,
        "task": task,
        "status": "completed" if failed == 0 else "partial" if completed > 0 else "failed",
        "parallel": parallel,
        "subtasks_total": len(subtasks),
        "subtasks_completed": completed,
        "subtasks_failed": failed,
        "duration_ms": overall_duration_ms,
        "started_at": started_at,
        "finished_at": datetime.now().isoformat(),
        "results": results,
    }

    # Step 4: Save run record
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    run_file = RUNS_DIR / f"{run_id}.json"
    run_file.write_text(json.dumps(run_record, indent=2))

    # Step 5: Log to activity database
    try:
        v = get_vault_index()
        v.log_activity(
            agent="hermes",
            action=f"Orchestration run: {task[:100]}",
            task=run_id,
            status=run_record["status"],
            duration_ms=overall_duration_ms,
            model="orchestrator",
            details=f"{completed}/{len(subtasks)} sub-agents completed",
        )
        for r in results:
            v.log_activity(
                agent=r["agent"],
                action=f"Sub-agent: {task[:80]}",
                task=run_id,
                status=r["status"],
                duration_ms=r["duration_ms"],
                model="hermes-sub-agent",
                details=r.get("error", ""),
            )
    except Exception:
        pass

    # Print summary
    print(f"\n📊 Orchestration {run_id}: {run_record['status'].upper()}")
    print(f"   {completed}/{len(subtasks)} completed in {overall_duration_ms}ms")
    for r in results:
        icon = "✅" if r["status"] == "completed" else "❌"
        print(f"   {icon} {r['agent_name']:12s} {r['status']:10s} {r['duration_ms']}ms")

    return run_record


def get_run(run_id: str):
    """Retrieve a saved run record. Returns dict or None."""
    run_file = (RUNS_DIR / f"{run_id}.json").resolve()
    if not run_file.is_relative_to(RUNS_DIR.resolve()):
        return None
    if run_file.exists():
        return json.loads(run_file.read_text())
    return None


def list_recent_runs(limit: int = 20) -> list[dict]:
    """List recent orchestration runs."""
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    runs = []
    for f in sorted(RUNS_DIR.glob("orch-*.json"), reverse=True)[:limit]:
        try:
            data = json.loads(f.read_text())
            runs.append({
                "run_id": data["run_id"],
                "task": data["task"][:100],
                "status": data["status"],
                "subtasks_total": data["subtasks_total"],
                "subtasks_completed": data["subtasks_completed"],
                "duration_ms": data["duration_ms"],
                "started_at": data["started_at"],
            })
        except Exception:
            pass
    return runs


# ── CLI ──

def cmd_run(args):
    """Run an orchestration."""
    result = run_orchestration(
        task=args.task,
        run_id=args.run_id,
        preferred_agents=args.agents.split(",") if args.agents else None,
        parallel=not args.sequential,
        timeout=args.timeout,
    )
    # Print summary
    print(json.dumps(result, indent=2, default=str))
    if result["status"] == "completed":
        return 0
    elif result["status"] == "partial":
        return 1
    return 2


def cmd_status(args):
    """Show a specific run's status."""
    run = get_run(args.run_id)
    if not run:
        print(json.dumps({"ok": False, "error": f"Run '{args.run_id}' not found"}))
        return 1
    print(json.dumps(run, indent=2, default=str))
    return 0


def cmd_history(args):
    """List recent orchestration runs."""
    runs = list_recent_runs(limit=args.limit)
    print(json.dumps({"ok": True, "runs": runs, "count": len(runs)}, indent=2, default=str))
    return 0


def main():
    parser = argparse.ArgumentParser(description="MoiraiCore — Multi-Agent Orchestrator")
    sub = parsers = parser.add_subparsers(dest="command")

    # run
    p_run = sub.add_parser("run", help="Run an orchestration")
    p_run.add_argument("task", help="The complex task to orchestrate")
    p_run.add_argument("--run-id", default=None, help="Run ID (generated if omitted)")
    p_run.add_argument("--agents", default=None, help="Comma-separated agent keys (auto-detect if omitted)")
    p_run.add_argument("--sequential", action="store_true", help="Run sub-agents sequentially (default: parallel)")
    p_run.add_argument("--timeout", type=int, default=900, help="Per-sub-agent timeout in seconds (default: 900)")
    p_run.set_defaults(func=cmd_run)

    # status
    p_status = sub.add_parser("status", help="Show a specific run's full record")
    p_status.add_argument("run_id", help="Run ID")
    p_status.set_defaults(func=cmd_status)

    # history
    p_hist = sub.add_parser("history", help="List recent orchestration runs")
    p_hist.add_argument("--limit", type=int, default=20)
    p_hist.set_defaults(func=cmd_history)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
