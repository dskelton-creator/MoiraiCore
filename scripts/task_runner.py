#!/usr/bin/env python3
"""
MoiraiCore — Task Runner Daemon
Polls tasks.json for queued tasks, executes them via agent_orchestrator.run_subagent(),
and updates task status. Runs as a background daemon.

Usage:
    python3 task_runner.py          # Run in foreground
    python3 task_runner.py --daemon # Run as background daemon
    python3 task_runner.py --once   # Single pass, then exit
    python3 task_runner.py --stop   # Stop running daemon
    python3 task_runner.py --status # Show daemon status
"""

import json
import os
import signal
import subprocess
import sys
import time
import fcntl
from datetime import datetime
from pathlib import Path

# ── Loop Guardrails ──
_loop_guards_imported = False
_is_loop_allowed = None
try:
    from loop_guards import is_loop_allowed
    _is_loop_allowed = is_loop_allowed
    _loop_guards_imported = True
except ImportError:
    pass

# ── Loop Checkpoints ──
_checkpoint_imported = False
_save_checkpoint = None
try:
    from loop_checkpoint import save_checkpoint
    _save_checkpoint = save_checkpoint
    _checkpoint_imported = True
except ImportError:
    pass

AGENT_OS_ROOT = Path(os.environ.get("AGENT_OS_ROOT", str(Path(__file__).resolve().parents[1])))
SCRIPTS_DIR = AGENT_OS_ROOT / "scripts"
TASKS_FILE = AGENT_OS_ROOT / "config" / "tasks.json"
GOALS_FILE = AGENT_OS_ROOT / "config" / "goals.json"
RUNS_DIR = AGENT_OS_ROOT / "config" / "orchestration-runs"
PID_FILE = AGENT_OS_ROOT / "config" / "task_runner.pid"
LOG_FILE = AGENT_OS_ROOT / "config" / "task_runner.log"

BRIDGE_CLI = str(AGENT_OS_ROOT / "scripts" / "hermes_bridge.py")

# ── Logging ──

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass

# ── PID file management ──

def write_pid():
    PID_FILE.write_text(str(os.getpid()))

def read_pid():
    if PID_FILE.exists():
        try:
            return int(PID_FILE.read_text().strip())
        except Exception:
            pass
    return None

def clear_pid():
    try:
        PID_FILE.unlink()
    except Exception:
        pass

def is_running(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False

# ── Task persistence ──

def load_tasks():
    if TASKS_FILE.exists():
        try:
            return json.loads(TASKS_FILE.read_text())
        except Exception:
            pass
    return []

def save_tasks(tasks):
    TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    TASKS_FILE.write_text(json.dumps(tasks, indent=2))

def load_goals():
    if GOALS_FILE.exists():
        try:
            return json.loads(GOALS_FILE.read_text())
        except Exception:
            pass
    return []

def save_goals(goals):
    GOALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    GOALS_FILE.write_text(json.dumps(goals, indent=2))

# ── Kanban sync ──

LANE_MAP = {"queued": "backlog", "running": "progress", "completed": "done", "failed": "backlog", "in_progress": "progress"}

def sync_kanban(task):
    """Push a task status change to the server's Kanban board via API."""
    card_id = "task-" + task["id"]
    lane = LANE_MAP.get(task.get("status", "queued"), "backlog")
    try:
        import urllib.request, json as _json
        payload = _json.dumps({
            "id": task["id"],
            "status": task.get("status", "queued"),
            "title": task.get("title", ""),
            "desc": task.get("desc", ""),
            "agent": task.get("agent", ""),
            "priority": task.get("priority", "p2"),
            "goal_id": task.get("goal_id", ""),
            "matched_triggers": task.get("matched_triggers", []),
        }).encode()
        req = urllib.request.Request(
            "http://localhost:7878/api/tasks/update",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass  # Never let Kanban sync failures break task execution

# ── Task execution ──

def execute_task(task: dict) -> dict:
    """
    Execute a single task via Hermes CLI.
    Returns updated task dict with status, output, error fields.
    """
    agent_key = task.get("agent", "hermes")
    if agent_key == "any":
        sys.path.insert(0, str(SCRIPTS_DIR))
        try:
            from agent_registry import AgentRegistry
            reg = AgentRegistry()
            best, confidence, triggers = reg.find_agent_for_task(task.get("title", ""))
            if confidence != "default":
                agent_key = best
        except Exception:
            agent_key = "hermes"

    title = task.get("title", "")
    desc = task.get("desc", "")
    # Build the query from title + description
    query = f"Task: {title}\n\n{desc}" if desc and desc != title else title
    run_id = f"task-{task['id']}"
    RUNS_DIR.mkdir(parents=True, exist_ok=True)

    # Execute via Hermes Bridge (handles routing, memory, activity logging)
    log(f"  → Routing via bridge → {agent_key}: {title[:80]}...")
    start = time.time()

    # Resolve skills: task-level override > agent defaults > none
    task_skills = task.get("skills", [])
    if not task_skills:
        try:
            sys.path.insert(0, str(SCRIPTS_DIR))
            from agent_registry import AgentRegistry
            reg = AgentRegistry()
            agent_def = reg.get_agent(agent_key)
            task_skills = agent_def.get("skills", []) if agent_def else []
        except Exception:
            task_skills = []

    # Only auto-route if agent is "any" (not pre-assigned by orchestrator)
    agent_key = task.get("agent", "any")
    do_route = agent_key in ("any", "hermes")
    bridge_cmd = [sys.executable, BRIDGE_CLI, "ask", query, "--log"]
    if do_route:
        bridge_cmd.append("--route")
    if task_skills:
        bridge_cmd += ["--skills", ",".join(task_skills)]

    try:
        result = subprocess.run(
            bridge_cmd,
            capture_output=True,
            text=True,
            timeout=600,
            env={**os.environ},
            cwd=str(AGENT_OS_ROOT),
        )
        duration_ms = int((time.time() - start) * 1000)
        stderr = result.stderr.strip()
        stdout = result.stdout.strip()

        # Bridge returns JSON on stdout
        try:
            bridge_result = json.loads(stdout)
        except Exception:
            bridge_result = {"ok": result.returncode == 0, "response": stdout, "error": stderr or "Failed to parse bridge output"}

        response_text = bridge_result.get("response", stdout) or ""
        session_id = bridge_result.get("session_id")
        routed_to = bridge_result.get("routed_to", agent_key)
        agent_name = bridge_result.get("agent_name", agent_key)
        matched_triggers = bridge_result.get("matched_triggers", [])

        # Save output
        output_file = RUNS_DIR / f"{run_id}_output.md"
        output_file.write_text(f"# {agent_name} Output\n\nTask: {title}\n\nRouted to: {routed_to}\nTriggers: {matched_triggers}\n\n{response_text}")

        bridge_ok = bridge_result.get("ok", result.returncode == 0)
        task["status"] = "completed" if bridge_ok else "failed"
        task["output"] = response_text[:5000]  # Cap stored output
        task["output_path"] = str(output_file)
        task["session_id"] = session_id
        task["duration_ms"] = duration_ms
        task["routed_to"] = routed_to
        task["agent_name"] = agent_name
        task["matched_triggers"] = matched_triggers
        task["error"] = bridge_result.get("error") if not bridge_ok else None

        status_icon = "✅" if result.returncode == 0 else "❌"
        log(f"  {status_icon} {agent_name} (via bridge, routed={routed_to}) — {task['status']} ({duration_ms}ms)")

    except subprocess.TimeoutExpired:
        duration_ms = int((time.time() - start) * 1000)
        task["status"] = "failed"
        task["error"] = "Timed out after 300s"
        task["duration_ms"] = duration_ms
        log(f"  ⏰ {agent_name} — timeout ({duration_ms}ms)")

    except Exception as e:
        duration_ms = int((time.time() - start) * 1000)
        task["status"] = "failed"
        task["error"] = str(e)
        task["duration_ms"] = duration_ms
        log(f"  ❌ {agent_name} — error: {e}")

    task["updated"] = datetime.now().isoformat()
    return task

# ── Goal progress tracking ──

def update_goal_progress(goal_id):
    """Update goal status based on linked task statuses."""
    goals = load_goals()
    goal = next((g for g in goals if g["id"] == goal_id), None)
    if not goal:
        return

    tasks = load_tasks()
    goal_tasks = [t for t in tasks if t.get("goal_id") == goal_id]
    if not goal_tasks:
        return

    total = len(goal_tasks)
    completed = sum(1 for t in goal_tasks if t["status"] == "completed")
    failed = sum(1 for t in goal_tasks if t["status"] == "failed")

    if completed == total:
        goal["status"] = "completed"
    elif failed == total:
        goal["status"] = "failed"
    elif completed > 0 or failed > 0:
        goal["status"] = "in_progress"
    else:
        goal["status"] = "decomposed"

    goal["updated"] = datetime.now().isoformat()
    goal["tasks_completed"] = completed
    goal["tasks_total"] = total

    save_goals(goals)
    log(f"  📊 Goal '{goal['title'][:50]}' — {completed}/{total} tasks done ({goal['status']})")

# ── Main loop ──

def run_once():
    """Process all queued tasks once. Returns count of tasks processed."""
    tasks = load_tasks()
    queued = [t for t in tasks if t.get("status") == "queued"]

    if not queued:
        return 0

    log(f"📋 Found {len(queued)} queued task(s)")

    # Group by priority (p1 first)
    queued.sort(key=lambda t: 0 if t.get("priority") == "p1" else 1)

    processed = 0
    for task in queued:
        # Skip if another task from same goal is already running
        goal_id = task.get("goal_id")
        if goal_id:
            running = [t for t in tasks if t.get("goal_id") == goal_id and t.get("status") == "running"]
            if running:
                log(f"  ⏳ Skipping (goal {goal_id} has active task): {task['title'][:60]}")
                continue

        # ── Loop Guard Check ──
        if _loop_guards_imported and _is_loop_allowed is not None and goal_id:
            try:
                goals = load_goals()
                goal = next((g for g in goals if g["id"] == goal_id), None)
                if goal:
                    allowed, reason = _is_loop_allowed(goal_id, goal)
                    if not allowed:
                        log(f"  🛑 Guard triggered for goal {goal_id}: {reason}")
                        # Don't execute this task — goal is paused
                        continue
            except Exception as e:
                log(f"  ⚠️ Guard check failed (proceeding): {e}")

        task["status"] = "running"
        task["updated"] = datetime.now().isoformat()
        save_tasks(tasks)
        sync_kanban(task)

        log(f"🔄 Running task: {task['title'][:80]}")
        execute_task(task)
        save_tasks(tasks)
        sync_kanban(task)

        # Update goal progress
        if goal_id:
            update_goal_progress(goal_id)

        # ── Save checkpoint after each task ──
        if _checkpoint_imported and _save_checkpoint is not None and goal_id:
            try:
                goals_local = load_goals()
                goal_local = next((g for g in goals_local if g["id"] == goal_id), None)
                if goal_local:
                    tasks_local = [t for t in load_tasks() if t.get("goal_id") == goal_id]
                    _save_checkpoint(goal_id, goal_local, tasks_local)
            except Exception:
                pass  # Never let checkpoint failures break execution

        processed += 1

    return processed

def run_daemon():
    """Main daemon loop."""
    log("🚀 Task Runner Daemon started")
    log(f"   Tasks file: {TASKS_FILE}")
    log(f"   Poll interval: 30s")

    while True:
        try:
            processed = run_once()
            if processed:
                log(f"✅ Processed {processed} task(s)")
        except Exception as e:
            log(f"❌ Error in main loop: {e}")

        time.sleep(30)

# ── CLI ──

def cmd_status():
    pid = read_pid()
    if pid and is_running(pid):
        log(f"Task runner is running (PID {pid})")
    else:
        log("Task runner is not running")
        clear_pid()

    # Show task summary
    tasks = load_tasks()
    by_status = {}
    for t in tasks:
        s = t.get("status", "unknown")
        by_status[s] = by_status.get(s, 0) + 1
    log(f"Tasks: {by_status}")

def cmd_stop():
    pid = read_pid()
    if pid and is_running(pid):
        os.kill(pid, signal.SIGTERM)
        log(f"Stopped task runner (PID {pid})")
        clear_pid()
    else:
        log("Task runner is not running")
        clear_pid()

def main():
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "--daemon":
            # Fork to background
            pid = os.fork()
            if pid > 0:
                print(f"Task runner daemon started (PID {pid})")
                sys.exit(0)
            # Child process
            os.setsid()
            write_pid()
            run_daemon()
        elif cmd == "--once":
            processed = run_once()
            print(f"Processed {processed} task(s)")
        elif cmd == "--stop":
            cmd_stop()
        elif cmd == "--status":
            cmd_status()
        else:
            print(f"Unknown option: {cmd}")
            print("Usage: task_runner.py [--daemon|--once|--stop|--status]")
    else:
        # Foreground mode
        write_pid()
        run_daemon()

if __name__ == "__main__":
    main()
