#!/usr/bin/env python3
"""
MoiraiCore — Goal Engine
========================
Goal Mode: auto-decompose → agent assignment → execution → monitoring → report.

This is the "killer feature" that separates an operating system from a chatbot.
You set a goal, and the system:
  1. Analyzes the goal
  2. Decomposes it into intelligent subtasks (LLM-powered)
  3. Assigns each subtask to the best agent
  4. Executes autonomously (parallel where possible)
  5. Monitors progress and adapts
  6. Synthesizes a final report

Usage:
    from goal_engine import GoalEngine
from project_chat import emit_agent_thinking, emit_tool_usage, emit_artifact_created
    engine = GoalEngine()
    result = engine.create_goal("Research ASX competitors and write a report")
    # Returns goal_id, decomposition, and starts autonomous execution

    engine.run_goal(goal_id)           # Execute all pending subtasks
    engine.get_goal_status(goal_id)    # Check progress
    engine.synthesize_goal(goal_id)    # Generate final report
"""

import json
import os
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

# ── Loop Guardrails ──
_loop_guards_imported = False
_is_loop_allowed = None
_load_guard_config = None
_LoopGuards = None
try:
    from loop_guards import LoopGuards, is_loop_allowed, load_guard_config
    _is_loop_allowed = is_loop_allowed
    _load_guard_config = load_guard_config
    _LoopGuards = LoopGuards
    _loop_guards_imported = True
except ImportError:
    pass

# ── Loop Checkpoints ──
_checkpoint_imported = False
_save_checkpoint = None
_load_checkpoint = None
_resume_goal = None
try:
    from loop_checkpoint import save_checkpoint, load_checkpoint, resume_goal
    _save_checkpoint = save_checkpoint
    _load_checkpoint = load_checkpoint
    _resume_goal = resume_goal
    _checkpoint_imported = True
except ImportError:
    pass

# ── Goal Verifier ──
_verifier_imported = False
_verify_task_output = None
_GoalVerifier = None
try:
    from goal_verifier import GoalVerifier, verify_task as _verify_task_fn
    _verify_task_output = _verify_task_fn
    _GoalVerifier = GoalVerifier
    _verifier_imported = True
except ImportError:
    pass

# ── Self-Correction Engine ──
_self_correction_imported = False
_SelfCorrectionEngine = None
try:
    from self_correction import SelfCorrectionEngine
    _SelfCorrectionEngine = SelfCorrectionEngine
    _self_correction_imported = True
except ImportError:
    pass

# ── Pipeline Contracts (Feature 7) ──
_contracts_imported = False
_enforce_contract = None
_validate_contract = None
_ContractViolation = Exception
try:
    from pipeline_contracts import enforce as _enforce_contract, validate as _validate_contract, ContractViolation as _ContractViolation
    _contracts_imported = True
except ImportError:
    pass

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

AGENT_OS_ROOT = Path(os.environ.get("AGENT_OS_ROOT", str(Path(__file__).resolve().parents[1])))
SCRIPTS_DIR = AGENT_OS_ROOT / "scripts"
CONFIG_DIR = AGENT_OS_ROOT / "config"
GOALS_FILE = CONFIG_DIR / "goals.json"
KANBAN_FILE = CONFIG_DIR / "kanban.json"
RUNS_DIR = CONFIG_DIR / "orchestration-runs"
REPORTS_DIR = AGENT_OS_ROOT / "workspace" / "goal-reports"

sys.path.insert(0, str(SCRIPTS_DIR))


# ── Goal persistence ───

def load_goals() -> list:
    if GOALS_FILE.exists():
        try:
            return json.loads(GOALS_FILE.read_text())
        except Exception:
            pass
    return []

def save_goals(goals: list):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    GOALS_FILE.write_text(json.dumps(goals, indent=2))

def load_kanban() -> dict:
    if KANBAN_FILE.exists():
        try:
            return json.loads(KANBAN_FILE.read_text())
        except Exception:
            pass
    return {"backlog": [], "progress": [], "review": [], "done": []}

def save_kanban(kanban: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    KANBAN_FILE.write_text(json.dumps(kanban, indent=2))


# ── LLM-powered decomposition ───

def decompose_goal_with_llm(goal_title: str, goal_desc: str = "") -> dict:
    """
    Use Hermes to intelligently decompose a goal into subtasks.
    Returns {
        "subtasks": [
            {"title": "...", "agent": "researcher", "desc": "...", "priority": "p1", "depends_on": null},
            ...
        ],
        "reasoning": "...",
        "estimated_rounds": 2,
    }
    """
    # Load agent registry for context
    try:
        from agent_registry import AgentRegistry
        reg = AgentRegistry()
        agents = reg.list_agents(status_filter="active")
        agent_summary = "\n".join(
            f"- {a['key']} ({a['name']}): {a['role']} — Triggers: {', '.join(a['triggers'][:5])}"
            for a in agents if a.get("triggers")
        )
    except Exception:
        agent_summary = "- researcher: Research & Analysis\n- writer: Content Creation\n- developer: Code & Architecture\n- hermes: General Purpose"

    prompt = f"""You are the Goal Orchestrator for MoiraiCore. Your job is to decompose a high-level goal into specific, actionable subtasks and assign each to the best agent.

## Available Agents
{agent_summary}

## Goal
Title: {goal_title}
Description: {goal_desc or goal_title}

## Instructions
Analyze this goal and break it into 2-6 subtasks. For each subtask:
1. Give it a clear, specific title
2. Assign it to the best agent (use the agent keys from the list above)
3. Write a detailed description of what the agent should do
4. Set priority (p1=critical, p2=important, p3=nice-to-have)
5. Note if it depends on another subtask (by index, 1-based)

Output format (JSON only):
{{
  "reasoning": "Brief explanation of your decomposition strategy",
  "estimated_rounds": 1,
  "subtasks": [
    {{
      "title": "Specific subtask title",
      "agent": "researcher",
      "desc": "Detailed description of what to do",
      "priority": "p1",
      "depends_on": null
    }}
  ]
}}

Output ONLY the JSON, no other text."""

    try:
        from hermes_bridge import run_hermes
        result = run_hermes(["chat", "-q", prompt, "--quiet", "--pass-session-id"], timeout=60)
        response = result.get("response", "")

        # Parse JSON from response
        import re
        m = re.search(r"\{[\s\S]*\}", response)
        if m:
            data = json.loads(m.group(0))
            if "subtasks" in data and data["subtasks"]:
                return data
    except Exception as e:
        pass

    # Fallback: simple decomposition based on keyword matching
    return _fallback_decomposition(goal_title, goal_desc)


def _fallback_decomposition(goal_title: str, goal_desc: str) -> dict:
    """Simple rule-based decomposition when LLM is unavailable."""
    text = (goal_title + " " + goal_desc).lower()
    subtasks = []

    # Research component
    if any(w in text for w in ["research", "investigate", "analyse", "analyze", "competitor", "market", "trends", "compare"]):
        subtasks.append({
            "title": f"Research: {goal_title[:60]}",
            "agent": "researcher",
            "desc": f"Conduct thorough research on: {goal_title}. {goal_desc}",
            "priority": "p1",
            "depends_on": None,
        })

    # Writing component
    if any(w in text for w in ["write", "report", "document", "blog", "article", "draft", "content"]):
        subtasks.append({
            "title": f"Write report: {goal_title[:60]}",
            "agent": "writer",
            "desc": f"Write a comprehensive report based on research findings for: {goal_title}",
            "priority": "p1",
            "depends_on": 1 if subtasks else None,
        })

    # Development component
    if any(w in text for w in ["build", "implement", "code", "develop", "create", "deploy", "fix"]):
        subtasks.append({
            "title": f"Implement: {goal_title[:60]}",
            "agent": "developer",
            "desc": f"Implement the solution for: {goal_title}. {goal_desc}",
            "priority": "p1",
            "depends_on": None,
        })

    # SEO component
    if any(w in text for w in ["seo", "search engine", "keyword", "ranking", "google"]):
        subtasks.append({
            "title": f"SEO analysis: {goal_title[:60]}",
            "agent": "seo",
            "desc": f"Perform SEO analysis for: {goal_title}. {goal_desc}",
            "priority": "p2",
            "depends_on": None,
        })

    # Threat component
    if any(w in text for w in ["security", "threat", "risk", "vulnerability", "audit", "compliance"]):
        subtasks.append({
            "title": f"Security assessment: {goal_title[:60]}",
            "agent": "threat",
            "desc": f"Conduct security/threat assessment for: {goal_title}. {goal_desc}",
            "priority": "p1",
            "depends_on": None,
        })

    # If no specific agents matched, use Hermes
    if not subtasks:
        subtasks.append({
            "title": goal_title,
            "agent": "hermes",
            "desc": goal_desc or goal_title,
            "priority": "p2",
            "depends_on": None,
        })

    return {
        "reasoning": "Rule-based decomposition (LLM unavailable)",
        "estimated_rounds": 1,
        "subtasks": subtasks,
    }


# ── Main Goal Engine ───

class GoalEngine:
    """
    Goal Mode orchestrator.
    
    Usage:
        engine = GoalEngine()
        
        # Create a goal (auto-decomposes)
        goal = engine.create_goal("Research ASX competitors and write a report")
        print(f"Goal created: {goal['id']}")
        print(f"Subtasks: {len(goal['subtasks'])}")
        
        # Execute autonomously
        engine.run_goal(goal['id'])
        
        # Check status
        status = engine.get_goal_status(goal['id'])
        print(f"Progress: {status['tasks_completed']}/{status['tasks_total']}")
        
        # Synthesize final report
        report = engine.synthesize_goal(goal['id'])
    """

    def create_goal(self, title: str, desc: str = "", priority: str = "p2",
                    auto_run: bool = True) -> dict:
        """
        Create a new goal, decompose it into subtasks, and optionally start execution.
        Returns the goal dict with subtasks.
        """
        goal_id = f"goal-{int(time.time() * 1000)}"
        now = datetime.now().isoformat()

        # Decompose
        decomposition = decompose_goal_with_llm(title, desc)
        subtasks = decomposition.get("subtasks", [])

        # ── Feature 7: enforce the decomposition contract (structural gate) ──
        if _contracts_imported and _enforce_contract is not None:
            try:
                _enforce_contract("decomposition", decomposition,
                                  context={"goal_id": goal_id, "actor": "goal_engine"})
            except _ContractViolation as cv:
                goal = {
                    "id": goal_id, "title": title, "desc": desc or title,
                    "priority": priority, "status": "contract_error",
                    "created": now, "updated": now, "subtasks": subtasks,
                    "tasks_total": len(subtasks), "tasks_completed": 0,
                    "tasks_failed": 0, "report_path": None,
                    "contract_error": str(cv),
                }
                goals = load_goals(); goals.append(goal); save_goals(goals)
                return {"ok": False, "error": f"Decomposition contract violation: {cv}",
                        "goal_id": goal_id, "contract_error": True}

        # Build goal record
        goal = {
            "id": goal_id,
            "title": title,
            "desc": desc or title,
            "priority": priority,
            "status": "decomposed",
            "created": now,
            "updated": now,
            "subtasks": subtasks,
            "decomposition_reasoning": decomposition.get("reasoning", ""),
            "tasks_total": len(subtasks),
            "tasks_completed": 0,
            "tasks_failed": 0,
            "report_path": None,
        }

        # Save goal
        goals = load_goals()
        goals.append(goal)
        save_goals(goals)

        # Create kanban cards for each subtask
        kanban = load_kanban()
        for i, st in enumerate(subtasks):
            task_card = {
                "id": f"task-{goal_id}-{i}",
                "task_id": f"{goal_id}-{i}",
                "title": st["title"][:120],
                "desc": st.get("desc", "")[:500],
                "agent": st.get("agent", "hermes"),
                "priority": st.get("priority", "p2"),
                "goal_id": goal_id,
                "goal_title": title[:80],
                "depends_on": st.get("depends_on"),
                "status": "backlog",
                "triggers": [],
                "synced": False,
            }
            # ── Feature 7: enforce task_card contract before persisting ──
            if _contracts_imported and _enforce_contract is not None:
                try:
                    _enforce_contract("task_card", task_card,
                                      context={"goal_id": goal_id, "task_id": task_card["id"], "actor": "goal_engine"})
                except _ContractViolation as cv:
                    print(f"[contract] task_card rejected for {goal_id}: {cv}")
                    continue
            kanban["backlog"].append(task_card)
        save_kanban(kanban)

        # ── Feature 8: fire goal.created ──
        _fire("goal.created", {
            "goal_id": goal_id, "title": title, "priority": priority,
            "tasks_total": len(subtasks),
        })

        # Auto-run if requested
        if auto_run:
            self.run_goal(goal_id)

        return goal

    def run_goal(self, goal_id: str) -> dict:
        """
        Execute all pending subtasks for a goal.
        Respects dependency ordering.
        Checks loop guardrails before each iteration.
        """
        goals = load_goals()
        goal = next((g for g in goals if g["id"] == goal_id), None)
        if not goal:
            return {"ok": False, "error": f"Goal not found: {goal_id}"}

        # ── Load checkpoint (restore state if resuming) ──
        if _checkpoint_imported and _load_checkpoint is not None:
            try:
                cp = _load_checkpoint(goal_id)
                if cp:
                    goal_data = cp.get("goal", {})
                    # Restore loop tracking fields from checkpoint
                    for key in ("loop_iterations", "loop_history", "tasks_completed", "tasks_failed"):
                        if key in goal_data and goal_data[key]:
                            goal[key] = goal_data[key]
            except Exception:
                pass  # Never let checkpoint loading break execution

        # ── Loop Guard Check ──
        if _loop_guards_imported and _is_loop_allowed is not None:
            allowed, reason = _is_loop_allowed(goal_id, goal)
            if not allowed:
                goal["status"] = "paused_guard"
                goal["guard_triggered"] = reason
                goal["updated"] = datetime.now().isoformat()
                save_goals(goals)
                return {
                    "ok": False,
                    "error": f"Loop guard triggered: {reason}",
                    "guard_triggered": True,
                    "guard_reason": reason,
                }

        # ── Adaptive Quality Threshold ──
        adaptive_quality = None
        if _loop_guards_imported:
            try:
                from loop_guards import AdaptiveQualityManager
                adaptive_quality = AdaptiveQualityManager(goal_id)
            except Exception:
                pass

        # ── Agent Switcher ──
        agent_switcher = None
        if _loop_guards_imported:
            try:
                from loop_guards import AgentSwitcher
                agent_switcher = AgentSwitcher(goal_id)
            except Exception:
                pass

        # Update status
        goal["status"] = "in_progress"
        goal["updated"] = datetime.now().isoformat()
        save_goals(goals)

        # Load kanban tasks for this goal
        kanban = load_kanban()
        goal_tasks = [t for t in kanban["backlog"] if t.get("goal_id") == goal_id]

        if not goal_tasks:
            # Check if all done
            all_tasks = (
                [t for t in kanban["backlog"] if t.get("goal_id") == goal_id] +
                [t for t in kanban["progress"] if t.get("goal_id") == goal_id] +
                [t for t in kanban["done"] if t.get("goal_id") == goal_id]
            )
            if all_tasks:
                goal["status"] = "completed"
                goal["updated"] = datetime.now().isoformat()
                save_goals(goals)
                return {"ok": True, "status": "already_complete"}
            return {"ok": False, "error": "No tasks found for goal"}

        # Sort by dependency order (tasks with no deps first)
        def sort_key(t):
            dep = t.get("depends_on")
            return (0 if dep is None else 1, dep or 0)

        goal_tasks.sort(key=sort_key)

        # ── Execute tasks sequentially (dependency-ordered) ──
        # Hermes CLI is single-threaded; all LLM calls are serialized naturally.
        # Parallel execution infrastructure is in place for future multi-instance Hermes.
        completed = 0
        failed = 0
        skipped = 0

        # ── Session persistence: track session_id per agent for conversation continuity ──
        # Fix (Option A): start each goal run with a FRESH session map. We deliberately do NOT
        # restore agent_sessions from the persisted goal/checkpoint — reusing a prior run's
        # conversation state makes a re-run "come back with stale attempts" instead of fresh work.
        # Continuity now only exists WITHIN a single run (sessions accumulate as tasks execute).
        agent_sessions = {}  # agent -> session_id (fresh per run)

        for task in goal_tasks:
            # Support both kanban card format (id) and goal engine format (task_id)
            task_id = task.get("task_id") or task.get("id")
            if not task_id:
                continue
            agent = task.get("agent", "hermes")
            title = task["title"]
            desc = task.get("desc", "")
            
            # Emit initial thoughts about the task
            try:
                from project_chat import emit_agent_thinking
                thought = f"Starting work on '{title}': {desc[:100] if desc else 'no description provided'}"
                emit_agent_thinking(goal_id=goal_id, task_id=task_id, agent_name=agent, thought=thought)
            except Exception:
                pass  # Never let telemetry break execution

            # ── Agent Switching: check if current agent should be switched ──
            if agent_switcher and agent_switcher.should_switch(agent):
                alternative = agent_switcher.get_alternative_agent(agent, title)
                if alternative != agent:
                    task["original_agent"] = agent
                    agent = alternative
                    task["agent"] = agent
                    task["agent_switched"] = True
                    task["agent_switch_reason"] = f"Switched from {agent_switcher.get_stats()}"

            # Move to progress
            kanban["backlog"] = [t for t in kanban["backlog"] if t["id"] != task["id"]]
            task["status"] = "progress"
            kanban["progress"].append(task)
            save_kanban(kanban)

            # Emit that we're about to use the Hermes LLM tool
            try:
                from project_chat import emit_tool_usage
                emit_tool_usage(goal_id=goal_id, task_id=task_id, agent_name=agent, tool_name="Hermes LLM")
            except Exception:
                pass  # Best effort
            
            # Execute via task runner (with self-correction retries)
            try:
                retry_count = task.get("retry_count", 0)
                correction_history = task.get("corrections", [])
                current_desc = desc

                while True:
                    # Get session_id for this agent (if any) for conversation continuity
                    current_session = agent_sessions.get(agent)
                    result = self._execute_task(task_id, agent, title, current_desc, session_id=current_session, goal_id=goal_id)

                    # ── Verify task output ──
                    verification = None
                    if result.get("ok") and _verifier_imported and _verify_task_output is not None:
                        try:
                            output_text = result.get("output", "")
                            if output_text:
                                verification = _verify_task_output(
                                    goal_title=goal.get("title", ""),
                                    goal_desc=goal.get("desc", ""),
                                    task_title=title,
                                    task_output=output_text,
                                    task_id=task_id,
                                )
                                task["verification"] = verification
                                if "verification_scores" not in goal:
                                    goal["verification_scores"] = []
                                goal["verification_scores"].append({
                                    "task_id": task_id,
                                    "score": verification.get("score", 0),
                                    "status": verification.get("status", "unknown"),
                                })
                        except Exception:
                            pass

                    # ── Adaptive Quality: adjust threshold based on performance ──
                    if adaptive_quality and verification:
                        try:
                            current_thresh = goal.get("quality_threshold", 0.6)
                            new_thresh = adaptive_quality.adjust_threshold(goal, current_thresh)
                            if new_thresh != current_thresh:
                                goal["quality_threshold"] = new_thresh
                                if "adaptive_threshold_history" not in goal:
                                    goal["adaptive_threshold_history"] = []
                                goal["adaptive_threshold_history"].append({
                                    "iteration": goal.get("loop_iterations", 0),
                                    "old_threshold": current_thresh,
                                    "new_threshold": new_thresh,
                                    "reason": "quality_high" if new_thresh > current_thresh else "quality_low",
                                    "timestamp": datetime.now().isoformat(),
                                })
                        except Exception:
                            pass

                    # ── Self-Correction on failure ──
                    needs_retry = False
                    if _self_correction_imported and _SelfCorrectionEngine is not None:
                        failure_reason = ""
                        if not result.get("ok"):
                            failure_reason = result.get("error", "Execution failed")
                        elif verification and verification.get("status") in ("partial", "fail"):
                            failure_reason = verification.get("reasoning", "Quality below threshold")

                        if failure_reason and retry_count < _SelfCorrectionEngine().max_retries:
                            try:
                                correction = _SelfCorrectionEngine().correct(
                                    goal_title=goal.get("title", ""),
                                    goal_desc=goal.get("desc", ""),
                                    task_title=title,
                                    original_prompt=current_desc,
                                    failure_reason=failure_reason,
                                    verification_verdict=verification or {},
                                    task_id=task_id,
                                    goal_id=goal_id,
                                    attempt=retry_count,
                                    task_output=result.get("output", ""),
                                    error_message=result.get("error", ""),
                                )
                                if correction.get("action") == "retry":
                                    current_desc = correction["corrected_prompt"]
                                    retry_count += 1
                                    needs_retry = True
                                    correction_entry = {
                                        "attempt": retry_count,
                                        "reason": failure_reason[:200],
                                        "corrected": True,
                                        "reasoning": correction.get("reasoning", ""),
                                        "timestamp": datetime.now().isoformat(),
                                    }
                                    correction_history.append(correction_entry)
                                    task["retry_count"] = retry_count
                                    task["corrections"] = correction_history
                                    if "corrections_log" not in goal:
                                        goal["corrections_log"] = []
                                    goal["corrections_log"].append({
                                        "task_id": task_id,
                                        "attempt": retry_count,
                                        "action": "retry",
                                    })
                                elif correction.get("action") == "escalate":
                                    task["needs_human_input"] = True
                                    task["escalation_reason"] = correction.get("reasoning", "Max retries exceeded")
                                    if "corrections_log" not in goal:
                                        goal["corrections_log"] = []
                                    goal["corrections_log"].append({
                                        "task_id": task_id,
                                        "attempt": retry_count,
                                        "action": "escalate",
                                        "reason": correction.get("reasoning", ""),
                                    })
                            except Exception:
                                pass

                    if not needs_retry:
                        break

                # ── Persist session_id for this agent (for conversation continuity) ──
                task_session_id = result.get("session_id")
                if task_session_id and task_session_id != "None":
                    agent_sessions[agent] = task_session_id
                    task["session_id"] = task_session_id
                    goal["agent_sessions"] = dict(agent_sessions)

                if result.get("ok") and (not verification or verification.get("status") != "fail"):
                    kanban["progress"] = [t for t in kanban["progress"] if t["id"] != task["id"]]
                    task["status"] = "done"
                    task["output"] = result.get("output", "")[:1000]
                    if verification:
                        task["quality_score"] = verification.get("score", 0)
                    if correction_history:
                        task["corrections"] = correction_history
                        task["retry_count"] = retry_count
                    kanban["done"].append(task)
                    completed += 1
                    if agent_switcher:
                        agent_switcher.record_success(agent)
                else:
                    kanban["progress"] = [t for t in kanban["progress"] if t["id"] != task["id"]]
                    task["status"] = "backlog"
                    task["error"] = result.get("error", "Unknown error")
                    if verification and verification.get("status") == "fail":
                        task["error"] = f"Verification failed: {verification.get('reasoning', '')[:200]}"
                    if correction_history:
                        task["corrections"] = correction_history
                        task["retry_count"] = retry_count
                    kanban["backlog"].append(task)
                    failed += 1
                    if agent_switcher:
                        agent_switcher.record_failure(agent)
            except Exception as e:
                kanban["progress"] = [t for t in kanban["progress"] if t["id"] != task["id"]]
                task["status"] = "backlog"
                task["error"] = str(e)
                kanban["backlog"].append(task)
                failed += 1

            save_kanban(kanban)

        total_tasks = len(goal_tasks)

        # Update goal status
        goals = load_goals()
        goal = next((g for g in goals if g["id"] == goal_id), None)
        if goal:
            goal["tasks_completed"] = completed
            goal["tasks_failed"] = failed
            goal["updated"] = datetime.now().isoformat()
            if completed == total_tasks:
                goal["status"] = "completed"
                # Record loop pattern for learning
                try:
                    from loop_patterns import record_loop_pattern
                    record_loop_pattern(goal)
                except Exception:
                    pass
            elif failed > 0:
                goal["status"] = "partial"

            # ── Record loop iteration for convergence tracking ──
            if _loop_guards_imported and _LoopGuards is not None:
                try:
                    guards = _LoopGuards(goal_id)
                    goal = guards.record_iteration(goal)
                except Exception:
                    pass  # Never let tracking failures break execution

            # ── Save checkpoint ──
            if _checkpoint_imported and _save_checkpoint is not None:
                try:
                    _save_checkpoint(goal_id, goal)
                except Exception:
                    pass  # Never let checkpoint failures break execution

            save_goals(goals)

        # ── Feature 8: fire goal-level completion events ──
        final_status = goal.get("status") if goal else "unknown"
        if final_status == "completed":
            _fire("goal.completed", {"goal_id": goal_id, "tasks_completed": completed, "tasks_failed": failed})
        elif final_status == "failed":
            _fire("goal.failed", {"goal_id": goal_id, "tasks_completed": completed, "tasks_failed": failed})
        elif final_status == "partial":
            _fire("goal.completed", {"goal_id": goal_id, "tasks_completed": completed, "tasks_failed": failed, "partial": True})

        return {
            "ok": True,
            "goal_id": goal_id,
            "tasks_total": len(goal_tasks),
            "tasks_completed": completed,
            "tasks_failed": failed,
        }

    def _execute_task(self, task_id, agent, title, desc, session_id=None, goal_id=""):
        """
        Execute a single task via the Hermes bridge.
        If session_id is provided, uses --resume to continue that conversation.
        Returns dict with ok, output, session_id, duration_ms.

        Telemetry: each run is recorded to config/telemetry.jsonl (cost + latency)
        via the telemetry module, so the dashboard Economics view can aggregate it.
        """
        # ── Emit transcript: task started (project-scoped) ──
        try:
            from project_chat import emit_task_started
            emit_task_started(goal_id=goal_id, task_id=task_id, agent_name=agent, task_title=title)
        except Exception:
            pass  # Best-effort — never break execution

        try:
            from hermes_bridge import run_hermes, build_resume_args

            prompt = f"You are the {agent} agent.\n\n## Task\n{desc or title}\n\nBe thorough and actionable. Write your output as structured markdown."

            base_args = ["chat", "-q", prompt, "--quiet", "--pass-session-id", "--max-turns", "10"]
            args = build_resume_args(base_args, session_id)

            result = run_hermes(args, timeout=600)

            # Save output
            RUNS_DIR.mkdir(parents=True, exist_ok=True)
            output_file = RUNS_DIR / f"{task_id}_output.md"
            output_text = result.get("response", "")
            output_file.write_text(f"# {title}\n\n{output_text}")

            # Extract session_id from result (new_session for first call, or same session resumed)
            returned_session_id = result.get("session_id")

            routed_to = result.get("routed_to", agent)
            # Map the bridge routing key to (tier, model) for telemetry.
            _TIER_MODEL = {
                "gemini-api": ("2", "gemini-pro"),
                "antigravity": ("2", "antigravity-architect"),
                "ollama": ("3", "ollama"),
                "ornith": ("3", "ornith"),
            }
            tier, model = _TIER_MODEL.get(routed_to, ("1", "hermes"))

            try:
                from telemetry import record_run
                record_run(
                    agent=agent,
                    tier=tier,
                    model=model,
                    duration_ms=result.get("duration_ms", 0),
                    prompt_chars=len(prompt),
                    est_output_chars=len(output_text),
                    goal_id=goal_id,
                    ok=result.get("ok", False),
                )
            except Exception:
                pass

            # ── Feature 7: enforce task_output contract before handing off ──
            out_result = {
                "ok": result.get("ok", False),
                "output": output_text[:2000],
                "output_path": str(output_file),
                "session_id": returned_session_id,
                "routed_to": routed_to,
                "duration_ms": result.get("duration_ms", 0),
            }
            if _contracts_imported and _enforce_contract is not None:
                try:
                    _enforce_contract("task_output", out_result,
                                      context={"goal_id": goal_id, "task_id": task_id, "actor": agent})
                except _ContractViolation as cv:
                    out_result["contract_error"] = str(cv)
                    # Still return the result so callers can decide; breach is logged.
            _fire("task.executed", {
                "task_id": task_id, "goal_id": goal_id, "agent": agent,
                "ok": out_result.get("ok", False), "routed_to": out_result.get("routed_to"),
            })
            # ── Emit transcript: task completed (project-scoped) ──
            try:
                from project_chat import emit_task_completed
                emit_task_completed(
                    goal_id=goal_id, task_id=task_id, agent_name=agent,
                    task_title=title, ok=out_result.get("ok", False),
                )
            except Exception:
                pass  # Best-effort — never break execution
            return out_result
        except Exception as e:
            # ── Emit transcript: task failed (project-scoped) ──
            try:
                from project_chat import emit_task_completed
                emit_task_completed(
                    goal_id=goal_id, task_id=task_id, agent_name=agent,
                    task_title=title, ok=False,
                )
            except Exception:
                pass
            _fire("task.failed", {"task_id": task_id, "goal_id": goal_id, "agent": agent, "error": str(e)[:200]})
            return {"ok": False, "error": str(e)}

    def run_goal_background(self, goal_id):
        """
        Run a goal execution in a background thread immediately and return a task_id
        that can be polled via /api/goals/background/status?task_id=xxx.

        This decouples goal execution from the HTTP request so the caller doesn't
        block for minutes while Hermes processes tasks.
        """
        task_id = f"goal_bg_{goal_id}_{int(time.time() * 1000)}"

        from hermes_bridge import _background_tasks, _background_lock

        with _background_lock:
            _background_tasks[task_id] = {
                "task_id": task_id,
                "type": "goal_execution",
                "goal_id": goal_id,
                "status": "starting",
                "started_at": datetime.now().isoformat(),
                "result": None,
                "error": None,
            }

        def _run_goal_thread():
            try:
                with _background_lock:
                    _background_tasks[task_id]["status"] = "running"
                result = self.run_goal(goal_id)
                with _background_lock:
                    _background_tasks[task_id].update({
                        "status": "completed" if result.get("ok") else "failed",
                        "ok": result.get("ok", False),
                        "result": result,
                        "completed_at": datetime.now().isoformat(),
                    })
            except Exception as e:
                with _background_lock:
                    _background_tasks[task_id].update({
                        "status": "error",
                        "error": str(e),
                        "completed_at": datetime.now().isoformat(),
                    })

        thread = threading.Thread(target=_run_goal_thread, daemon=True)
        thread.start()
        return task_id

    def get_goal_status(self, goal_id):
        """Get current status of a goal."""
        goals = load_goals()
        goal = next((g for g in goals if g["id"] == goal_id), None)
        if not goal:
            return {"ok": False, "error": "Goal not found"}
        return {"ok": True, "goal": goal}

    def synthesize_goal(self, goal_id):
        """
        Synthesize all completed subtask outputs into a final report.
        """
        goals = load_goals()
        goal = next((g for g in goals if g["id"] == goal_id), None)
        if not goal:
            return {"ok": False, "error": "Goal not found"}

        # Collect all outputs
        kanban = load_kanban()
        done_tasks = [t for t in kanban["done"] if t.get("goal_id") == goal_id]

        if not done_tasks:
            return {"ok": False, "error": "No completed tasks to synthesize"}

        # Build synthesis prompt
        prompt_parts = [
            f"# Goal Synthesis",
            f"",
            f"## Original Goal",
            f"**{goal['title']}**",
            f"{goal.get('desc', '')}",
            f"",
            f"## Subtask Outputs ({len(done_tasks)} completed)",
            f"",
        ]

        for i, task in enumerate(done_tasks, 1):
            prompt_parts.append(f"### Subtask {i}: {task['title']}")
            output = task.get("output", task.get("desc", "No output"))
            prompt_parts.append(output[:1500])
            # Include verification score if available
            quality_score = task.get("quality_score")
            verification = task.get("verification")
            if quality_score is not None:
                status = verification.get("status", "unknown") if verification else "unknown"
                prompt_parts.append(f"\n[Quality: {status} ({quality_score:.2f})]")
            prompt_parts.append("")

        # Add verification summary if available
        if goal.get("verification_scores"):
            scores = [s["score"] for s in goal["verification_scores"] if isinstance(s.get("score"), (int, float))]
            if scores:
                avg = sum(scores) / len(scores)
                prompt_parts.extend([
                    f"## Quality Summary",
                    f"Average verification score: {avg:.2f}",
                    f"Tasks verified: {len(scores)}",
                    f"",
                ])

        prompt_parts.extend([
            f"## Task",
            f"Synthesize all the subtask outputs above into a comprehensive final report.",
            f"1. Start with an executive summary (3-5 sentences)",
            f"2. Organize findings into clear sections",
            f"3. Include specific data points and recommendations",
            f"4. Note any gaps or areas needing further work",
            f"Write in markdown format.",
        ])

        prompt = "\n".join(prompt_parts)

        try:
            from hermes_bridge import run_hermes
            result = run_hermes(["chat", "-q", prompt, "--quiet", "--pass-session-id"], timeout=120)
            report_text = result.get("response", "")

            # Save report
            REPORTS_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            safe_name = "".join(c if c.isalnum() else "_" for c in goal["title"][:40])
            report_path = REPORTS_DIR / f"goal_{ts}_{safe_name}.md"
            report_path.write_text(f"# Goal Report: {goal['title']}\n\n{report_text}")

            # Update goal
            goal["report_path"] = str(report_path)
            goal["status"] = "completed"
            goal["updated"] = datetime.now().isoformat()
            save_goals(goals)

            return {
                "ok": True,
                "report_path": str(report_path),
                "report_text": report_text,
                "tasks_synthesized": len(done_tasks),
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def list_goals(self, status_filter: str = "all") -> list:
        """List all goals, optionally filtered by status."""
        goals = load_goals()
        if status_filter != "all":
            goals = [g for g in goals if g.get("status") == status_filter]
        return goals

    def resume_goal(self, goal_id: str) -> dict:
        """
        Resume a goal from its last checkpoint.
        Restores state and starts execution from where it left off.
        """
        if _checkpoint_imported and _resume_goal is not None:
            try:
                result = _resume_goal(goal_id)
                if result.get("ok"):
                    # Now run the goal (it will load the checkpoint internally)
                    run_result = self.run_goal(goal_id)
                    return {
                        "ok": True,
                        "resumed_from": result.get("checkpoint_saved_at", ""),
                        "restored_tasks": result.get("restored_tasks", 0),
                        "run_result": run_result,
                    }
                return result
            except Exception as e:
                return {"ok": False, "error": str(e)}
        return {"ok": False, "error": "Checkpoint module not available"}


# ── CLI ───

def main():
    import argparse

    parser = argparse.ArgumentParser(description="MoiraiCore — Goal Engine")
    sub = parsers = parser.add_subparsers(dest="command")

    # create
    p_create = sub.add_parser("create", help="Create a new goal")
    p_create.add_argument("title", help="Goal title")
    p_create.add_argument("--desc", default="", help="Goal description")
    p_create.add_argument("--priority", default="p2", choices=["p1", "p2", "p3"])
    p_create.add_argument("--no-run", action="store_true", help="Don't auto-execute")

    # run
    p_run = sub.add_parser("run", help="Execute a goal's subtasks")
    p_run.add_argument("goal_id", help="Goal ID")

    # status
    p_status = sub.add_parser("status", help="Show goal status")
    p_status.add_argument("goal_id", help="Goal ID")

    # synthesize
    p_synth = sub.add_parser("synthesize", help="Synthesize final report")
    p_synth.add_argument("goal_id", help="Goal ID")

    # list
    p_list = sub.add_parser("list", help="List goals")
    p_list.add_argument("--status", default="all", help="Filter by status")

    args = parser.parse_args()
    engine = GoalEngine()

    if args.command == "create":
        goal = engine.create_goal(
            title=args.title,
            desc=args.desc,
            priority=args.priority,
            auto_run=not args.no_run,
        )
        print(json.dumps(goal, indent=2, default=str))

    elif args.command == "run":
        result = engine.run_goal(args.goal_id)
        print(json.dumps(result, indent=2, default=str))

    elif args.command == "status":
        result = engine.get_goal_status(args.goal_id)
        print(json.dumps(result, indent=2, default=str))

    elif args.command == "synthesize":
        result = engine.synthesize_goal(args.goal_id)
        print(json.dumps(result, indent=2, default=str))

    elif args.command == "list":
        goals = engine.list_goals(status_filter=args.status)
        print(json.dumps(goals, indent=2, default=str))

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
