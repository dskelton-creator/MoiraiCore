#!/usr/bin/env python3
"""Test script for goal engine parallel execution."""
import time
import sys
import os
from pathlib import Path

os.environ.setdefault('AGENT_OS_ROOT', str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

from goal_engine import GoalEngine, load_kanban, save_kanban

engine = GoalEngine()
goal = engine.create_goal(title="Parallel Test", desc="Test 2 tasks", auto_run=False)
goal_id = goal["id"]
print(f"Goal: {goal_id}", flush=True)

kanban = load_kanban()
for i in range(2):
    kanban["backlog"].append({
        "id": f"task-parallel-{goal_id}-{i}",
        "task_id": f"{goal_id}-p{i}",
        "title": f"Parallel task {i+1}",
        "desc": f"Say hello from task {i+1}",
        "agent": "hermes",
        "priority": "p3",
        "goal_id": goal_id,
        "goal_title": goal["title"],
        "depends_on": None,
        "status": "backlog",
        "triggers": [],
        "synced": False,
    })
save_kanban(kanban)

tasks = [t for t in kanban["backlog"] if t.get("goal_id") == goal_id]
print(f"Tasks to execute: {len(tasks)}", flush=True)

start = time.time()
result = engine.run_goal(goal_id)
elapsed = time.time() - start
print(f"Completed: {result.get('tasks_completed', 0)}", flush=True)
print(f"Failed: {result.get('tasks_failed', 0)}", flush=True)
print(f"Time: {elapsed:.1f}s", flush=True)
