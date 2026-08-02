#!/usr/bin/env python3
"""
MoiraiCore — Loop Checkpoint Manager
====================================
Saves and restores loop state for long-running goals.

A checkpoint captures everything needed to resume a goal after
interruption (server restart, network failure, guard trigger, etc.):

  - Goal definition + current status
  - All subtask outputs so far
  - Iteration count + guard status
  - Loop history (convergence tracking)
  - Timestamp

Stored in: config/checkpoints/{goal_id}.json
Auto-saved after every iteration.
Loaded on server startup (auto-resume in-progress goals).

Usage:
    from loop_checkpoint import save_checkpoint, load_checkpoint, resume_goal

    # Save after each iteration
    save_checkpoint(goal_id, goal_state)

    # Load to inspect
    cp = load_checkpoint(goal_id)

    # Resume from checkpoint
    result = resume_goal(goal_id)
"""

import json
import os
import shutil
from datetime import datetime
from pathlib import Path

AGENT_OS_ROOT = Path(os.environ.get("AGENT_OS_ROOT", str(Path(__file__).resolve().parents[1])))
CONFIG_DIR = AGENT_OS_ROOT / "config"
CHECKPOINTS_DIR = CONFIG_DIR / "checkpoints"
MAX_CHECKPOINTS = 50  # Keep last N checkpoints per goal to limit disk usage


def _ensure_dir():
    """Create checkpoints directory if it doesn't exist."""
    CHECKPOINTS_DIR.mkdir(parents=True, exist_ok=True)


def _checkpoint_path(goal_id: str) -> Path:
    """Get the file path for a goal's latest checkpoint."""
    return CHECKPOINTS_DIR / f"{goal_id}.json"


def _checkpoint_history_path(goal_id: str, timestamp: str) -> Path:
    """Get the file path for a historical checkpoint snapshot."""
    return CHECKPOINTS_DIR / f"{goal_id}_{timestamp}.json"


# ── Save ──

def save_checkpoint(goal_id: str, goal: dict, tasks: list = None) -> dict:
    """
    Save the current loop state as a checkpoint.

    Args:
        goal_id: The goal ID
        goal: The goal dict (from goals.json)
        tasks: Optional list of task dicts (from tasks.json)

    Returns:
        The checkpoint dict that was saved
    """
    _ensure_dir()

    now = datetime.now().isoformat()

    checkpoint = {
        "goal_id": goal_id,
        "saved_at": now,
        "goal": {
            "id": goal.get("id", goal_id),
            "title": goal.get("title", ""),
            "desc": goal.get("desc", ""),
            "status": goal.get("status", ""),
            "priority": goal.get("priority", "p2"),
            "tasks_completed": goal.get("tasks_completed", 0),
            "tasks_failed": goal.get("tasks_failed", 0),
            "tasks_total": goal.get("tasks_total", 0),
            "loop_iterations": goal.get("loop_iterations", 0),
            "loop_history": goal.get("loop_history", []),
            "guard_triggered": goal.get("guard_triggered"),
            "created": goal.get("created", now),
            "updated": goal.get("updated", now),
        },
        "tasks": [],
    }

    # Include task outputs (these are the expensive-to-reproduce bits)
    if tasks is None:
        tasks = []
        # Load tasks from tasks.json if not provided
        tasks_file = CONFIG_DIR / "tasks.json"
        if tasks_file.exists():
            try:
                all_tasks = json.loads(tasks_file.read_text())
                tasks = [t for t in all_tasks if t.get("goal_id") == goal_id]
            except Exception:
                pass

    for task in tasks:
        checkpoint["tasks"].append({
            "id": task.get("id", ""),
            "task_id": task.get("task_id", ""),
            "title": task.get("title", ""),
            "desc": task.get("desc", ""),
            "agent": task.get("agent", "hermes"),
            "status": task.get("status", "queued"),
            "output": task.get("output", "")[:2000],  # Cap stored output
            "output_path": task.get("output_path", ""),
            "error": task.get("error"),
            "retry_count": task.get("retry_count", 0),
            "duration_ms": task.get("duration_ms", 0),
        })

    # Save as latest checkpoint
    cp_path = _checkpoint_path(goal_id)
    cp_path.write_text(json.dumps(checkpoint, indent=2))

    # Also save a timestamped snapshot (for history)
    ts = now.replace(":", "-").replace(".", "_")
    hist_path = _checkpoint_history_path(goal_id, ts)
    hist_path.write_text(json.dumps(checkpoint, indent=2))

    # Prune old snapshots (keep only last MAX_CHECKPOINTS)
    _prune_checkpoints(goal_id)

    return checkpoint


def _prune_checkpoints(goal_id: str):
    """Remove old checkpoint snapshots, keeping only the most recent ones."""
    try:
        pattern = f"{goal_id}_"
        snapshots = sorted(
            [f for f in CHECKPOINTS_DIR.iterdir()
             if f.name.startswith(pattern) and f.suffix == ".json"],
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        for old in snapshots[MAX_CHECKPOINTS:]:
            old.unlink()
    except Exception:
        pass  # Never let cleanup failures break execution


# ── Load ──

def load_checkpoint(goal_id: str) -> dict:
    """
    Load the latest checkpoint for a goal.

    Returns:
        Checkpoint dict, or None if no checkpoint exists.
    """
    cp_path = _checkpoint_path(goal_id)
    if not cp_path.exists():
        return None
    try:
        return json.loads(cp_path.read_text())
    except Exception:
        return None


def list_checkpoints() -> list:
    """
    List all saved checkpoints (latest per goal).

    Returns:
        List of {goal_id, saved_at, status, tasks_completed, tasks_total}
    """
    _ensure_dir()
    results = []
    for f in CHECKPOINTS_DIR.iterdir():
        if f.suffix == ".json" and "_" not in f.stem:
            try:
                cp = json.loads(f.read_text())
                results.append({
                    "goal_id": cp.get("goal_id", f.stem),
                    "title": cp.get("goal", {}).get("title", ""),
                    "saved_at": cp.get("saved_at", ""),
                    "status": cp.get("goal", {}).get("status", ""),
                    "tasks_completed": cp.get("goal", {}).get("tasks_completed", 0),
                    "tasks_total": cp.get("goal", {}).get("tasks_total", 0),
                    "loop_iterations": cp.get("goal", {}).get("loop_iterations", 0),
                })
            except Exception:
                pass
    return sorted(results, key=lambda x: x.get("saved_at", ""), reverse=True)


def list_checkpoint_history(goal_id: str) -> list[dict]:
    """List all historical snapshots for a goal."""
    _ensure_dir()
    pattern = f"{goal_id}_"
    results = []
    for f in CHECKPOINTS_DIR.iterdir():
        if f.name.startswith(pattern) and f.suffix == ".json":
            try:
                cp = json.loads(f.read_text())
                results.append({
                    "saved_at": cp.get("saved_at", ""),
                    "status": cp.get("goal", {}).get("status", ""),
                    "tasks_completed": cp.get("goal", {}).get("tasks_completed", 0),
                })
            except Exception:
                pass
    return sorted(results, key=lambda x: x.get("saved_at", ""), reverse=True)


# ── Resume ──

def resume_goal(goal_id: str) -> dict:
    """
    Resume a goal from its last checkpoint.

    This:
    1. Loads the checkpoint
    2. Restores the goal state in goals.json
    3. Restores completed task outputs in tasks.json
    4. Sets goal status to "in_progress" (ready for next iteration)
    5. Clears any guard trigger

    Returns:
        {ok: bool, goal: dict, restored_tasks: int, error: str}
    """
    cp = load_checkpoint(goal_id)
    if not cp:
        return {"ok": False, "error": f"No checkpoint found for goal {goal_id}"}

    try:
        # Restore goal state
        goal_data = cp.get("goal", {})
        goal_data["status"] = "in_progress"
        goal_data.pop("guard_triggered", None)
        goal_data["updated"] = datetime.now().isoformat()

        # Load current goals and update
        goals_file = CONFIG_DIR / "goals.json"
        if goals_file.exists():
            goals = json.loads(goals_file.read_text())
        else:
            goals = []

        # Find existing goal or add restored one
        existing_idx = next((i for i, g in enumerate(goals) if g["id"] == goal_id), None)
        if existing_idx is not None:
            # Preserve any fields not in checkpoint
            existing = goals[existing_idx]
            for key, value in existing.items():
                if key not in goal_data:
                    goal_data[key] = value
            goals[existing_idx] = goal_data
        else:
            goals.insert(0, goal_data)

        goals_file.write_text(json.dumps(goals, indent=2))

        # Restore task outputs
        restored_tasks = 0
        checkpoint_tasks = cp.get("tasks", [])
        if checkpoint_tasks:
            tasks_file = CONFIG_DIR / "tasks.json"
            if tasks_file.exists():
                all_tasks = json.loads(tasks_file.read_text())
            else:
                all_tasks = []

            # Build index of existing tasks
            task_index = {t.get("task_id", t.get("id", "")): i for i, t in enumerate(all_tasks)}

            for cp_task in checkpoint_tasks:
                tid = cp_task.get("task_id", cp_task.get("id", ""))
                if tid and tid in task_index:
                    # Update existing task with checkpoint data
                    idx = task_index[tid]
                    # Only restore if checkpoint task has output
                    if cp_task.get("output"):
                        all_tasks[idx]["output"] = cp_task["output"]
                        all_tasks[idx]["output_path"] = cp_task.get("output_path", "")
                        all_tasks[idx]["status"] = cp_task.get("status", all_tasks[idx]["status"])
                        restored_tasks += 1
                elif cp_task.get("status") == "completed":
                    # Task exists in checkpoint but not in tasks.json — re-add it
                    all_tasks.append(cp_task)
                    restored_tasks += 1

            tasks_file.write_text(json.dumps(all_tasks, indent=2))

        return {
            "ok": True,
            "goal": goal_data,
            "restored_tasks": restored_tasks,
            "checkpoint_saved_at": cp.get("saved_at", ""),
        }

    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Delete ──

def delete_checkpoint(goal_id: str) -> bool:
    """Delete all checkpoints for a goal (fresh start)."""
    _ensure_dir()
    deleted = False
    for f in CHECKPOINTS_DIR.iterdir():
        if f.stem == goal_id or f.name.startswith(f"{goal_id}_"):
            try:
                f.unlink()
                deleted = True
            except Exception:
                pass
    return deleted


def clear_all_checkpoints() -> int:
    """Delete ALL checkpoints. Returns count deleted."""
    _ensure_dir()
    count = 0
    for f in CHECKPOINTS_DIR.iterdir():
        if f.suffix == ".json":
            try:
                f.unlink()
                count += 1
            except Exception:
                pass
    return count


# ── Auto-resume ──

def get_resumable_goals() -> list[dict]:
    """
    Find goals that should be auto-resumed.
    These are goals with status 'in_progress' or 'paused_guard' that have checkpoints.
    """
    resumable = []
    checkpoints = list_checkpoints()
    for cp in checkpoints:
        goal_status = cp.get("status", "")
        if goal_status in ("in_progress", "paused_guard", "partial"):
            resumable.append(cp)
    return resumable


# ── Self-test ──

if __name__ == "__main__":
    print("=== Loop Checkpoint Self-Test ===\n")

    test_goal_id = "goal-test-checkpoint-123"

    # Test 1: Save checkpoint
    print("Test 1: Save checkpoint")
    mock_goal = {
        "id": test_goal_id,
        "title": "Test goal for checkpoint",
        "desc": "Testing save/load/resume",
        "status": "in_progress",
        "priority": "p1",
        "tasks_completed": 2,
        "tasks_failed": 1,
        "tasks_total": 5,
        "loop_iterations": 3,
        "loop_history": [
            {"iteration": 1, "tasks_completed": 0, "timestamp": "2026-01-01T00:00:00"},
            {"iteration": 2, "tasks_completed": 1, "timestamp": "2026-01-01T00:05:00"},
            {"iteration": 3, "tasks_completed": 2, "timestamp": "2026-01-01T00:10:00"},
        ],
        "guard_triggered": None,
        "created": "2026-01-01T00:00:00",
        "updated": "2026-01-01T00:10:00",
    }
    mock_tasks = [
        {"id": "t1", "task_id": f"{test_goal_id}-0", "title": "Research", "status": "completed",
         "output": "Research output here...", "agent": "researcher", "retry_count": 0},
        {"id": "t2", "task_id": f"{test_goal_id}-1", "title": "Write", "status": "completed",
         "output": "Written report...", "agent": "writer", "retry_count": 0},
        {"id": "t3", "task_id": f"{test_goal_id}-2", "title": "Review", "status": "failed",
         "output": "", "error": "Timeout", "agent": "hermes", "retry_count": 2},
        {"id": "t4", "task_id": f"{test_goal_id}-3", "title": "Publish", "status": "queued",
         "output": "", "agent": "developer", "retry_count": 0},
        {"id": "t5", "task_id": f"{test_goal_id}-4", "title": "Verify", "status": "queued",
         "output": "", "agent": "hermes", "retry_count": 0},
    ]
    cp = save_checkpoint(test_goal_id, mock_goal, mock_tasks)
    print(f"  ✅ Saved: {cp['goal']['title']} ({len(cp['tasks'])} tasks)")
    print(f"  📁 Checkpoint file: {_checkpoint_path(test_goal_id)}")
    print()

    # Test 2: Load checkpoint
    print("Test 2: Load checkpoint")
    loaded = load_checkpoint(test_goal_id)
    if loaded:
        print(f"  ✅ Loaded: {loaded['goal']['title']}")
        print(f"  📊 Status: {loaded['goal']['status']}")
        print(f"  🔄 Iterations: {loaded['goal']['loop_iterations']}")
        print(f"  📋 Tasks: {len(loaded['tasks'])}")
    else:
        print("  ❌ Failed to load checkpoint")
    print()

    # Test 3: List checkpoints
    print("Test 3: List all checkpoints")
    all_cps = list_checkpoints()
    print(f"  ✅ Found {len(all_cps)} checkpoint(s)")
    for c in all_cps:
        print(f"    - {c['goal_id']}: {c['status']} (iter {c.get('loop_iterations', 0)})")
    print()

    # Test 4: Resume goal
    print("Test 4: Resume goal from checkpoint")
    result = resume_goal(test_goal_id)
    if result.get("ok"):
        print(f"  ✅ Resumed: {result['goal']['title']}")
        print(f"  📊 Status: {result['goal']['status']}")
        print(f"  🔄 Restored {result['restored_tasks']} task outputs")
        print(f"  💾 Checkpoint from: {result.get('checkpoint_saved_at', 'unknown')}")
    else:
        print(f"  ❌ Resume failed: {result.get('error')}")
    print()

    # Test 5: Resumable goals
    print("Test 5: Find resumable goals")
    resumable = get_resumable_goals()
    print(f"  ✅ Found {len(resumable)} resumable goal(s)")
    for r in resumable:
        print(f"    - {r['goal_id']}: {r['status']}")
    print()

    # Test 6: Delete checkpoint
    print("Test 6: Delete checkpoint")
    deleted = delete_checkpoint(test_goal_id)
    print(f"  ✅ Deleted: {deleted}")
    loaded_after = load_checkpoint(test_goal_id)
    print(f"  📁 Exists after delete: {loaded_after is not None}")
    print()

    # Cleanup test files
    print("Test 7: Cleanup")
    clear_all_checkpoints()
    remaining = list_checkpoints()
    print(f"  ✅ Remaining checkpoints: {len(remaining)}")
    print()

    print("=== All tests complete ===")
