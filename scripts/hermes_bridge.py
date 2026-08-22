#!/usr/bin/env python3
"""
Hermes Bridge — CLI wrapper for dashboard ↔ Hermes communication.

Now with Multi-Agent Channel routing:
  - Each query is routed to the best agent (researcher, writer, developer, hermes)
  - Agent-specific memory is loaded and prepended as context
  - Every action is logged to the activity database
  - Dynamic toolset loading based on agent type for optimal token usage

Usage:
    python3 hermes_bridge.py ask "What is 2+2?"
    python3 hermes_bridge.py ask "Research ASX trends" --route
    python3 hermes_bridge.py ask "Write a blog post" --route --log
    python3 hermes_bridge.py route "write a blog post about cybersecurity"
    python3 hermes_bridge.py orchestrate "Build a complete SEO analysis for my site"
    python3 hermes_bridge.py ask "Summarise this" --context "long text here"
    python3 hermes_bridge.py resume <session_id> "Follow-up question"
    python3 hermes_bridge.py history <session_id>
    python3 hermes_bridge.py sessions
    python3 hermes_bridge.py status
"""

import json
import subprocess
import sys
import os
import re
import time
import threading
from pathlib import Path
from datetime import datetime
from typing import Optional

HERMES_CLI = os.environ.get("HERMES_CLI", "")
if not HERMES_CLI:
    # Auto-detect: check common locations before falling back to PATH
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
AGENT_OS_ROOT = Path(os.environ.get("AGENT_OS_ROOT", str(Path(__file__).resolve().parents[1])))
CONVERSATIONS_DIR = AGENT_OS_ROOT / "config" / "hermes-conversations"
SCRIPTS_DIR = AGENT_OS_ROOT / "scripts"

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

def get_ice():
    global _ice
    if _ice is None:
        sys.path.insert(0, str(SCRIPTS_DIR))
        from infinite_context import InfiniteContext
        _ice = InfiniteContext()
        _ice.build_index()
    return _ice

def ensure_dirs():
    CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)


def _hermes_state_db() -> Path:
    """Locate Hermes' SQLite state database (holds session messages)."""
    home = os.environ.get("HERMES_HOME") or os.path.join(os.path.expanduser("~"), ".hermes")
    return Path(home) / "state.db"


def _read_clean_response(session_id: str) -> Optional[str]:
    """Read the final assistant message for a Hermes session from state.db.

    The CLI's --quiet mode still prints inline tool diffs ("┊ review diff" …)
    to stdout alongside the final response. Reading the session's last
    assistant message from the state DB gives us the clean, human-readable
    answer without that noise.
    """
    if not session_id:
        return None
    try:
        import sqlite3
        db = _hermes_state_db()
        if not db.exists():
            return None
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT content FROM messages WHERE session_id = ? "
                "AND role = 'assistant' AND content IS NOT NULL AND content != '' "
                "ORDER BY id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            if row and row[0]:
                return row[0].strip()
            return None
        finally:
            conn.close()
    except Exception:
        return None


def run_hermes(args: list[str], timeout: int = 600, workdir: str = None) -> dict:
    """Run a Hermes CLI command and return parsed result. Timer starts before call.

    workdir: optional working directory. When set, both the subprocess cwd and
    the deprecated TERMINAL_CWD env var are pointed there so the agent's
    terminal backend follows the requested directory (the --in flag alone only
    scopes the session, not the terminal backend).
    """
    cmd = [HERMES_CLI] + args
    start = time.time()
    env = {**os.environ}
    if workdir:
        env["TERMINAL_CWD"] = workdir
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=workdir or None,
        )
        duration_ms = int((time.time() - start) * 1000)
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()

        session_id = None
        m = re.search(r"session_id:\s*(\S+)", stdout)
        if not m and stderr:
            m = re.search(r"session_id:\s*(\S+)", stderr)
        if m:
            session_id = m.group(1)

        response_text = re.sub(r"session_id:\s*\S+\n?", "", stdout).strip()

        # Prefer the clean final assistant message from the session DB; fall
        # back to raw stdout when that's unavailable (e.g. session not yet
        # committed, or the model replied directly without tool noise).
        if result.returncode == 0 and session_id:
            clean = _read_clean_response(session_id)
            if clean:
                response_text = clean

        # The CLI exited 0 but produced no answer (model transient failure /
        # empty reply). Surface a clear error so the caller never shows a
        # misleading blank "No response".
        if result.returncode == 0 and not response_text:
            return {
                "ok": False,
                "session_id": session_id,
                "response": None,
                "error": "Hermes returned an empty response (model may have stalled — try again or reduce the message size)",
                "exit_code": result.returncode,
                "duration_ms": duration_ms,
            }

        return {
            "ok": result.returncode == 0,
            "session_id": session_id,
            "response": response_text,
            "error": stderr if result.returncode != 0 else None,
            "exit_code": result.returncode,
            "duration_ms": duration_ms,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "session_id": None, "response": None,
                "error": "Hermes timed out", "exit_code": -1, "duration_ms": int((time.time() - start) * 1000)}
    except FileNotFoundError:
        return {"ok": False, "session_id": None, "response": None,
                "error": "Hermes CLI not found", "exit_code": -1, "duration_ms": 0}
    except Exception as e:
        return {"ok": False, "session_id": None, "response": None,
                "error": str(e), "exit_code": -1, "duration_ms": int((time.time() - start) * 1000)}

def load_agent_memory(agent_key: str) -> str:
    """Load an agent's memory.md content to prepend as context."""
    try:
        reg = get_registry()
        content = reg.read_memory(agent_key)
        if content:
            return f"[Agent Memory — {agent_key}]\n{content}"
    except Exception:
        pass
    return None

def get_toolsets_for_agent(agent_key: str) -> str:
    """
    Return appropriate Hermes toolsets for a given agent type.
    This enables dynamic tool loading to minimize token usage.
    """
    # Define toolset mappings for each agent type
    agent_toolsets = {
        "hermes": "terminal,file,session_search,clarify,memory,skills",  # General purpose
        "researcher": "web,search,session_memory,clarify",  # Web research focused
        "writer": "file,session_memory,clarify,skills",  # Document creation focused
        "developer": "terminal,file,code_execution,session_memory,clarify",  # Coding focused
        "antigravity": "terminal,file,code_execution,session_memory,clarify",  # Advanced coding
        "codex": "terminal,file,code_execution,session_memory,clarify",  # Code generation
        "pm": "file,session_memory,clarify,delegation",  # Project management
    }
    
    # Return the toolset for the agent, or default to hermes if not found
    return agent_toolsets.get(agent_key, agent_toolsets["hermes"])

def log_activity(agent: str, action: str, status: str = "completed",
                 duration_ms: int = 0, model: str = "", details: str = ""):
    """Log an agent activity entry to the SQLite database."""
    try:
        v = get_vault_index()
        v.log_activity(
            agent=agent,
            action=action[:200],
            task="",
            status=status,
            duration_ms=duration_ms,
            model=model[:100] if model else "",
            details=details[:500] if details else "",
        )
    except Exception:
        pass  # Never let logging failures break the main flow

def cmd_ask(args) -> dict:
    """Send a single query to Hermes. Optionally route to agent + load memory + log."""
    query = args.query
    context = getattr(args, "context", None)
    model = getattr(args, "model", None)
    skills = getattr(args, "skills", None)
    do_route = getattr(args, "route", False)
    do_log = getattr(args, "log", False)
    project_id = getattr(args, "project_id", None)
    project_space = getattr(args, "project_space", None)
    # Optional forced agent (e.g. from a project-chat @mention). When set,
    # routing targets that agent directly instead of auto-detecting.
    force_agent = getattr(args, "force_agent", None) or getattr(args, "agent", None)

    # ── Project context loading (IPSP: project docs override global vault) ──
    project_context = ""
    project_output_dir = None
    sandbox = None
    if project_id:
        try:
            sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
            from sandbox import ProjectSandbox
            sandbox = ProjectSandbox(project_id)
            project_output_dir = sandbox.project_path / "src"
            # Load project docs as context (isolated from global vault)
            project_context = sandbox.get_context()
            # Also include source code context
            source_ctx = sandbox.get_source_context()
            if source_ctx:
                project_context += "\n\n" + source_ctx
        except FileNotFoundError:
            project_context = f"[Project: {project_id} — NOT FOUND]"
        except Exception:
            pass  # Never let project loading break the main flow

    # Determine agent
    routed_to = "hermes"
    agent_name = "Hermes"
    agent_memory = None
    matched_triggers = []

    if do_route or force_agent:
        try:
            reg = get_registry()
            if force_agent:
                routed_to = force_agent
                confidence = "forced"
                matched_triggers = []
            else:
                routed_to, confidence, matched_triggers = reg.find_agent_for_task(query)
            agent_def = reg.get_agent(routed_to)
            agent_name = agent_def["name"] if agent_def else routed_to
            # Load agent memory to prepend as context
            agent_memory = load_agent_memory(routed_to)
        except Exception:
            routed_to = "hermes"
            agent_name = "Hermes"

    # ── Infinite Context Engine: auto-inject relevant vault context ──
    ice_context = None
    ice_block = ""
    # Allow disabling ICE via env var (e.g., for lightweight search calls)
    if os.environ.get("ICE_DISABLED", "0") != "1":
        try:
            ice = get_ice()
            max_ice_tokens = int(os.environ.get("ICE_MAX_TOKENS", "2000"))
            ice_context = ice.get_context(
                query=query,
                agent_key=routed_to if routed_to != "hermes" else None,
                max_tokens=max_ice_tokens,
                session_id=None,  # Could pass session_id for repeat-injection avoidance
            )
            if ice_context["chunks"]:
                ice_block = ice.format_context_block(ice_context)
        except Exception:
            pass  # Never let context injection break the main flow

    # ── Pre-search: inject web results for researcher agent ──
    search_block = ""
    if routed_to == "researcher" and os.environ.get("SEARCH_DISABLED", "0") != "1":
        try:
            search_script = SCRIPTS_DIR / "search_tool.py"
            if search_script.exists():
                search_result = subprocess.run(
                    [sys.executable, str(search_script), query,
                     "--engine", "both", "--limit", "5", "--context"],
                    capture_output=True, text=True, timeout=30,
                    env={**os.environ},
                )
                if search_result.returncode == 0 and search_result.stdout.strip():
                    search_block = search_result.stdout.strip()
        except Exception:
            pass  # Never let search failures break the main flow

    # Build the prompt
    parts = []
    if project_context:
        parts.append(project_context)
    if agent_memory:
        parts.append(agent_memory)
    if search_block:
        parts.append(search_block)
    if ice_block:
        parts.append(ice_block)
    if context:
        parts.append(f"## Context\n\n{context}")
    parts.append(f"## Query\n\n{query}")
    prompt = "\n\n".join(parts)

    # Build Hermes args
    hermes_args = ["chat", "-q", prompt, "--quiet"]
    if model:
        hermes_args += ["-m", model]
    if skills:
        for s in skills.split(","):
            hermes_args += ["-s", s.strip()]
    
    # ADD DYNAMIC TOOLSET SELECTION BASED ON AGENT TYPE
    # Only add toolsets if not explicitly overridden by user
    if not skills:  # Only auto-add toolsets if user didn't specify skills
        toolsets = get_toolsets_for_agent(routed_to)
        if toolsets:
            hermes_args += ["-t", toolsets]

    # Execute
    result = run_hermes(hermes_args)

    # ── Guardian: Scan response for PII / credentials ──
    if result.get("response") and os.environ.get("GUARDIAN_DISABLED", "0") != "1":
        try:
            from guardian import scan_response
            safe_text, scan_report = scan_response(result["response"], agent=agent_name)
            result["response"] = safe_text
            result["guardian"] = scan_report.to_dict()
        except Exception:
            pass  # Never let guardrail break the main flow

    # ── Tier 3: Ollama execution (auto-detect or explicit) ──
    # Auto-detect: if model router classifies this as a Tier 3 task, route to Ollama
    tier3_task_json = getattr(args, "tier3_task", None)
    # If project_space is set but no explicit tier3_task, try model router auto-detect
    if project_space and not tier3_task_json:
        try:
            from model_router import route_task, TaskType
            routing = route_task(query)
            if routing.tier.value == "tier_3_builder":
                # Auto-construct a Tier 3 task from the query
                # Expect query format: "Fix <file_path>: <description>"
                import re as _re
                file_match = _re.search(r'(?:fix|update|repair)\s+(\S+\.\w+)', query, _re.IGNORECASE)
                file_path = file_match.group(1) if file_match else "unknown.py"
                tier3_task_json = json.dumps({
                    "file_path": file_path,
                    "description": query,
                    "test_command": "echo no-test-specified",
                })
        except Exception:
            pass  # Fall through to normal Hermes execution

    if tier3_task_json and project_space:
        try:
            import json as _json
            tier3_task = _json.loads(tier3_task_json) if isinstance(tier3_task_json, str) else tier3_task_json
            from ollama_worker import execute_tier3, ExecutionTask, OllamaConfig
            from sandbox import ProjectSandbox

            # Use sandbox to validate project and get Tier 3 config
            sb = ProjectSandbox(project_space)
            worker_config = sb.get_tier3_config()

            ollama_config = OllamaConfig(
                base_url=worker_config.get("base_url", "http://localhost:11434/v1"),
                model_name=worker_config.get("model_name", "qwen3-coder:30b-t3"),
                max_retries=worker_config.get("max_retries", 5),
                per_iteration_timeout=worker_config.get("per_iteration_timeout", 60),
            )

            task = ExecutionTask(
                file_path=tier3_task.get("file_path", ""),
                task_description=tier3_task.get("description", ""),
                test_command=tier3_task.get("test_command", "echo no-test"),
                project_space=project_space,
                context=tier3_task.get("context", ""),
            )

            tier3_result = execute_tier3(task, ollama_config)
            result["tier3_result"] = tier3_result.to_dict()

            if tier3_result.success:
                result["response"] = f"[Tier 3 ✓] Fixed in {tier3_result.iteration_count} iterations ({tier3_result.total_time_ms}ms)"
            else:
                result["response"] = (
                    f"[Tier 3 ✗] Failed after {tier3_result.iteration_count} iterations. "
                    f"Reason: {tier3_result.terminated_reason}"
                )
        except Exception as e:
            result["tier3_error"] = str(e)

    # ── Tier 1: ScrumMaster gate — MoiraiCore controls task assignment ──
    # All Tier 2/3 work must be assigned by the ScrumMaster
    sm = None
    if project_space:
        try:
            from scrum_master import ScrumMaster
            sm = ScrumMaster(project_space)
            sm.load_state()
            result["scrum_status"] = sm.status_report()
        except Exception:
            pass

    # ── Tier 2: Antigravity workspace execution (ScrumMaster-assigned only) ──
    if project_space and not tier3_task_json and sm:
        try:
            from model_router import route_task
            routing = route_task(query, context={"project_space": project_space})

            if routing.engine == "antigravity" and routing.tier.value == "tier_2_architect":
                # Try Gemini API first (headless, fast) — same backend the Antigravity IDE uses
                from gemini_worker import GeminiConfig, is_gemini_available
                gemini_config = GeminiConfig.from_env()

                if is_gemini_available(gemini_config):
                    # Actually generate and persist a real artifact (was returning a hint)
                    from gemini_worker import generate_architecture
                    from datetime import datetime as _dt
                    import uuid as _uuid
                    from pathlib import Path as _Path
                    try:
                        plan = generate_architecture(
                            project_space=project_space,
                            description=query,
                            requirements=[query],
                            config=gemini_config,
                        )
                    except Exception as ge:
                        result["tier2_error"] = f"gemini gen: {ge}"
                        plan = None
                    if plan is not None:
                        # Persist the plan to .antigravity/artifacts/ (isolation protocol)
                        art_dir = _Path(project_space) / ".antigravity" / "artifacts"
                        art_dir.mkdir(parents=True, exist_ok=True)
                        tid = f"t2-{_uuid.uuid4().hex[:10]}"
                        payload = plan.to_dict()
                        payload["task_id"] = tid
                        payload["engine"] = "antigravity"
                        payload["generated_at"] = _dt.now().isoformat()
                        art_path = art_dir / f"implementation_plan_{tid}.json"
                        art_path.write_text(json.dumps(payload, indent=2))
                        result["tier2_method"] = "gemini_api"
                        result["tier2_artifact"] = str(art_path)
                        result["tier2_task_id"] = tid
                        result["response"] = (
                            f"[Tier 2 ✓][{tid}] Architected '{query[:60]}' via Gemini API — "
                            f"{len(payload.get('implementation_order') or [])} files. "
                            f"Plan: {art_path.name}"
                        )
                        result["routed_to"] = "gemini-api"
                        result["agent_name"] = "Gemini Pro API"
                        result["ok"] = True
                    else:
                        result["tier2_error"] = "Gemini returned no architecture"
                else:
                    # Fall back to Antigravity GUI bridge
                    from antigravity_bridge import submit_task_antigravity, AntigravityTask
                    ag_task = AntigravityTask(
                        project_name=os.path.basename(project_space),
                        description=query,
                        context=project_context[:2000] if project_context else "",
                    )
                    ag_result = submit_task_antigravity(ag_task)
                    result["tier2_result"] = {
                        "success": ag_result.success,
                        "output": ag_result.output,
                        "error": ag_result.error,
                        "duration_ms": ag_result.duration_ms,
                    }
                    if ag_result.success:
                        result["response"] = f"[Tier 2 → Antigravity] {ag_result.output}"
                        result["routed_to"] = "antigravity"
                    else:
                        result["tier2_error"] = ag_result.error
        except Exception as e:
            result["tier2_error"] = str(e)

    # Add routing metadata to result
    result["routed_to"] = routed_to
    result["agent_name"] = agent_name
    if project_id:
        result["project_id"] = project_id
    if do_route:
        result["matched_triggers"] = matched_triggers
    if ice_context:
        result["ice_sources"] = ice_context.get("sources", [])
        result["ice_tokens"] = ice_context.get("total_tokens", 0)
        result["ice_chunks"] = len(ice_context.get("chunks", []))

    # Log activity
    if do_route or do_log:
        log_activity(
            agent=routed_to,
            action=query[:200],
            status="completed" if result.get("ok") else "failed",
            duration_ms=result.get("duration_ms", 0),
            model=model or "",
            details=f"Bridge ask — routed_to={routed_to}" if do_route else "Bridge ask",
        )

    # Save conversation record
    ensure_dirs()
    if result.get("session_id"):
        record = {
            "session_id": result["session_id"],
            "timestamp": datetime.now().isoformat(),
            "query": query,
            "routed_to": routed_to,
            "agent_name": agent_name,
            "context_preview": (context[:200] + "...") if context and len(context) > 200 else context,
            "response": result.get("response", ""),
            "ok": result["ok"],
        }
        conv_file = CONVERSATIONS_DIR / f"{result['session_id']}.json"
        conv_file.write_text(json.dumps(record, indent=2))

    return result

def cmd_route(args) -> dict:
    """Route a task to the best agent — standalone command."""
    task = args.task
    try:
        reg = get_registry()
        agent_key, confidence, triggers = reg.find_agent_for_task(task)
        agent_def = reg.get_agent(agent_key)
        return {
            "ok": True,
            "task": task,
            "routed_to": agent_key,
            "agent_name": agent_def["name"] if agent_def else agent_key,
            "agent_role": agent_def.get("role", "") if agent_def else "",
            "confidence": confidence,
            "matched_triggers": triggers,
            "memory_found": reg.read_memory(agent_key) is not None,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

def cmd_orchestrate(args) -> dict:
    """Run multi-agent orchestration — decompose, spawn, collect, synthesize."""
    task = args.task
    preferred = args.agents.split(",") if args.agents else None
    parallel = not args.sequential
    timeout = args.timeout

    try:
        sys.path.insert(0, str(SCRIPTS_DIR))
        from agent_orchestrator import run_orchestration
        result = run_orchestration(
            task=task,
            preferred_agents=preferred,
            parallel=parallel,
            timeout=timeout,
        )
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)}

def cmd_resume(args) -> dict:
    """Resume a previous Hermes session."""
    session_id = args.session_id
    query = args.query
    model = getattr(args, "model", None)

    start = time.time()
    hermes_args = ["chat", "-q", query, "--quiet", "--resume", session_id]
    if model:
        hermes_args += ["-m", model]

    result = run_hermes(hermes_args)
    duration_ms = int((time.time() - start) * 1000)

    # Log the resume action
    log_activity(
        agent="hermes",
        action=f"resume session {session_id}: {query[:100]}",
        status="completed" if result.get("ok") else "failed",
        duration_ms=duration_ms,
    )

    return result

def cmd_history(args) -> dict:
    """Get previous conversation history."""
    session_id = args.session_id
    conv_file = CONVERSATIONS_DIR / f"{session_id}.json"
    if conv_file.exists():
        data = json.loads(conv_file.read_text())
        return {"ok": True, "conversation": data}
    return {"ok": False, "error": f"No conversation found for session {session_id}"}

def cmd_sessions(args) -> dict:
    """List recent conversations."""
    ensure_dirs()
    sessions = []
    for f in sorted(CONVERSATIONS_DIR.glob("*.json"), reverse=True)[:20]:
        try:
            data = json.loads(f.read_text())
            sessions.append({
                "session_id": data.get("session_id", f.stem),
                "timestamp": data.get("timestamp", ""),
                "query_preview": (data.get("query", "")[:80] + "...") if len(data.get("query", "")) > 80 else data.get("query", ""),
                "routed_to": data.get("routed_to", "hermes"),
                "response_preview": (data.get("response", "")[:120] + "...") if len(data.get("response", "")) > 120 else data.get("response", ""),
                "ok": data.get("ok", True),
            })
        except Exception:
            pass
    return {"ok": True, "sessions": sessions}

def cmd_status(args) -> dict:
    """Check if Hermes is available and responding."""
    start = time.time()
    result = run_hermes(["chat", "-q", "Reply with exactly: PONG", "--quiet"], timeout=15)
    duration_ms = int((time.time() - start) * 1000)
    return {
        "ok": result["ok"] and "PONG" in (result.get("response") or ""),
        "response": result.get("response"),
        "error": result.get("error"),
        "duration_ms": duration_ms,
    }

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Hermes Bridge — Dashboard ↔ Hermes communication with Multi-Agent routing")
    sub = parser.add_subparsers(dest="command")

    # ask
    p_ask = sub.add_parser("ask", help="Send a query to Hermes")
    p_ask.add_argument("query", help="The question or prompt")
    p_ask.add_argument("--context", default=None, help="Optional context to prepend")
    p_ask.add_argument("--model", default=None, help="Model override")
    p_ask.add_argument("--skills", default=None, help="Comma-separated skills to load")
    p_ask.add_argument("--route", action="store_true", help="Route to best agent + load agent memory")
    p_ask.add_argument("--agent", default=None, help="Force routing to a specific agent key (e.g. researcher) — overrides auto-detection")
    p_ask.add_argument("--log", action="store_true", help="Log this action to activity database")
    p_ask.add_argument("--project_id", default=None, help="Project ID for isolated project space (IPSP)")
    p_ask.add_argument("--project_space", default=None, help="Project space path (e.g. /projects/my-app)")
    p_ask.add_argument("--tier3_task", default=None, help="JSON string: '{\"file_path\":\"...\",\"description\":\"...\",\"test_command\":\"...\"}' for Tier 3 execution")
    p_ask.set_defaults(func=cmd_ask)

    # route
    p_route = sub.add_parser("route", help="Route a task to the best agent (no execution)")
    p_route.add_argument("task", help="Task description to route")
    p_route.set_defaults(func=cmd_route)

    # orchestrate
    p_orch = sub.add_parser("orchestrate", help="Run multi-agent orchestration")
    p_orch.add_argument("task", help="The complex task to orchestrate")
    p_orch.add_argument("--agents", default=None, help="Comma-separated agent keys (auto-detect if omitted)")
    p_orch.add_argument("--sequential", action="store_true", help="Run sub-agents sequentially")
    p_orch.add_argument("--timeout", type=int, default=300, help="Per-sub-agent timeout in seconds")
    p_orch.set_defaults(func=cmd_orchestrate)

    # resume
    p_resume = sub.add_parser("resume", help="Resume a previous session")
    p_resume.add_argument("session_id", help="Session ID to resume")
    p_resume.add_argument("query", help="Follow-up query")
    p_resume.add_argument("--model", default=None, help="Model override")
    p_resume.set_defaults(func=cmd_resume)

    # history
    p_hist = sub.add_parser("history", help="Get conversation history")
    p_hist.add_argument("session_id", help="Session ID")
    p_hist.set_defaults(func=cmd_history)

    # sessions
    p_sessions = sub.add_parser("sessions", help="List recent conversations")
    p_sessions.set_defaults(func=cmd_sessions)

    # status
    p_status = sub.add_parser("status", help="Check Hermes availability")
    p_status.set_defaults(func=cmd_status)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    result = args.func(args)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("ok") else 1)

# ── Background Execution & Session Persistence ──
_background_tasks: dict[str, dict] = {}
_background_lock = threading.Lock()

def run_hermes_background(args, task_id=None, timeout=900):
    """
    Launch a Hermes command in the background.
    Returns immediately with a task_id that can be polled via poll_background_task().
    Hermes auto-prints session_id in output; it will be captured when the process finishes.
    """
    if task_id is None:
        task_id = f"bg_{int(time.time() * 1000)}_{os.getpid()}"

    cmd = [HERMES_CLI] + args
    start = time.time()

    with _background_lock:
        _background_tasks[task_id] = {
            "task_id": task_id,
            "status": "running",
            "started_at": datetime.now().isoformat(),
            "cmd": " ".join(cmd),
            "result": None,
            "error": None,
        }

    def _run():
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env={**os.environ},
            )
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate()
                with _background_lock:
                    _background_tasks[task_id].update({
                        "status": "timeout",
                        "stdout": stdout.strip(),
                        "stderr": stderr.strip(),
                        "duration_ms": int((time.time() - start) * 1000),
                        "session_id": _extract_session_id(stdout),
                    })
                return

            duration_ms = int((time.time() - start) * 1000)
            session_id = _extract_session_id(stdout)
            response_text = re.sub(r"session_id:\s*\S+\n?", "", stdout).strip()

            with _background_lock:
                _background_tasks[task_id].update({
                    "status": "completed" if proc.returncode == 0 else "failed",
                    "ok": proc.returncode == 0,
                    "session_id": session_id,
                    "response": response_text,
                    "error": stderr.strip() if proc.returncode != 0 else None,
                    "exit_code": proc.returncode,
                    "duration_ms": duration_ms,
                })
        except Exception as e:
            with _background_lock:
                _background_tasks[task_id].update({
                    "status": "error",
                    "ok": False,
                    "error": str(e),
                    "duration_ms": int((time.time() - start) * 1000),
                })

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return task_id

def _extract_session_id(stdout):
    """Extract session_id from Hermes output (printed as 'session_id: xxx')."""
    m = re.search(r"session_id:\s*(\S+)", stdout)
    return m.group(1) if m else None

def poll_background_task(task_id):
    """Get the current status of a background Hermes task."""
    with _background_lock:
        task = _background_tasks.get(task_id)
        if task is None:
            return {"ok": False, "error": f"Unknown task: {task_id}"}
        return dict(task)

def list_background_tasks(status_filter=None):
    """List background tasks, optionally filtered by status."""
    with _background_lock:
        tasks = list(_background_tasks.values())
    if status_filter:
        tasks = [t for t in tasks if t.get("status") == status_filter]
    return [{k: v for k, v in t.items() if k != "stdout" or t.get("status") != "running"} for t in tasks]

def cleanup_background_task(task_id):
    """Remove a completed background task from the registry."""
    with _background_lock:
        if task_id in _background_tasks:
            del _background_tasks[task_id]
            return True
        return False

def build_resume_args(args, session_id):
    """
    Given Hermes CLI args, inject --resume <session_id> if a session_id is provided
    and --resume is not already in args.
    """
    if not session_id or session_id == "None":
        return args
    # Check if --resume or -r already present
    for i, a in enumerate(args):
        if a in ("--resume", "-r"):
            # Replace next arg if it differs
            if i + 1 < len(args) and args[i + 1] != session_id:
                new_args = list(args)
                new_args[i + 1] = session_id
                return new_args
            return args
    return args + ["--resume", session_id]

if __name__ == "__main__":
    main()