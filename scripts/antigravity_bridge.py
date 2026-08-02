"""
Antigravity Bridge — Controls Google Antigravity IDE.

Antigravity is an Electron app with no public REST API for task submission.
The language_server binary is internal (LSP only). Gemini Pro access is only
available through the GUI.

This bridge provides:
  1. submit_task_antigravity() — Saves task to file, opens project, sets clipboard
  2. monitor_artifacts() — Watches .antigravity/artifacts/ for results
  3. launch_antigravity() — Ensures Antigravity is running

The user pastes from clipboard into Antigravity's agent input (1 second).
This is the only reliable programmatic path without an API key.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional


# ── Paths ──

ANTIGRAVITY_APP = "/Applications/Antigravity.app"
PROJECTS_ROOT = Path(__file__).resolve().parents[1] / "projects"


# ── AppleScript Commands ──

def _osascript(script: str) -> tuple[bool, str]:
    """
    Execute an AppleScript command.
    Returns (success, output).
    """
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return True, result.stdout.strip()
        return False, result.stderr.strip()
    except subprocess.TimeoutExpired:
        return False, "osascript timed out"
    except FileNotFoundError:
        return False, "osascript not found (not macOS?)"
    except Exception as e:
        return False, str(e)


# ── Antigravity Bridge ──

@dataclass
class AntigravityTask:
    """A task to submit to Antigravity."""
    project_name: str
    description: str
    agent: str = "auto"  # auto, coder, researcher, writer, architect
    context: str = ""
    requirements: list[str] = field(default_factory=list)


@dataclass
class AntigravityResult:
    """Result from Antigravity execution."""
    task: AntigravityTask
    success: bool
    script_executed: str = ""
    output: str = ""
    error: str = ""
    duration_ms: int = 0


def is_antigravity_running() -> bool:
    """Check if Antigravity is currently running."""
    try:
        result = subprocess.run(
            ["pgrep", "-x", "Antigravity"],
            capture_output=True, text=True, timeout=5
        )
        return result.returncode == 0
    except Exception:
        return False


def launch_antigravity(timeout: int = 10) -> bool:
    """Launch Antigravity if not already running."""
    if is_antigravity_running():
        return True

    ok, _ = _osascript(f'tell application "{ANTIGRAVITY_APP}" to activate')
    if not ok:
        return False

    # Wait for it to start
    start = time.time()
    while time.time() - start < timeout:
        if is_antigravity_running():
            time.sleep(2)  # Extra time for window to appear
            return True
        time.sleep(0.5)

    return False


def is_antigravity_available() -> bool:
    """Check if Antigravity app is installed."""
    return Path(ANTIGRAVITY_APP).exists()


def open_project(project_name: str) -> bool:
    """
    Open a project in Antigravity via AppleScript.

    Uses 'open' command which Antigravity registers for its file types,
    or falls back to menu navigation.
    """
    project_path = PROJECTS_ROOT / project_name

    if not project_path.exists():
        return False

    # Try direct open (Antigravity registers as handler for its workspace files)
    ok, _ = _osascript(
        f'do shell script "open {project_path}"'
    )

    if ok:
        time.sleep(3)  # Wait for project to load
        return True

    return False


def _set_clipboard(text: str) -> bool:
    """Set the macOS clipboard to the given text."""
    # Escape the text for shell
    escaped = text.replace('\\', '\\\\').replace('"', '\\"').replace("'", "'\\''")
    ok, _ = _osascript(
        f'set the clipboard to "{escaped}"'
    )
    return ok


def _paste_clipboard() -> bool:
    """Simulate Cmd+V paste."""
    ok, _ = _osascript(
        'tell application "System Events" to keystroke "v" using command down'
    )
    return ok


def submit_task_antigravity(task: AntigravityTask, timeout: int = 5) -> AntigravityResult:
    """
    Submit a task to Antigravity IDE.

    Saves task to .antigravity/pending_tasks/ and opens Antigravity.
    Task can be pasted manually or via AppleScript if focus is reliable.
    """
    start_time = time.time()

    # Step 1: Ensure running
    if not launch_antigravity():
        return AntigravityResult(
            task=task,
            success=False,
            error="Could not launch Antigravity",
            duration_ms=int((time.time() - start_time) * 1000),
        )

    # Step 2: Save task to pending directory
    project_path = PROJECTS_ROOT / task.project_name
    pending_dir = project_path / ".antigravity" / "pending_tasks"
    pending_dir.mkdir(parents=True, exist_ok=True)

    task_file = pending_dir / f"task_{int(time.time())}.md"
    task_file.write_text(_build_task_text(task))

    # Step 3: Also save as JSON manifest
    manifest = {
        "project": task.project_name,
        "agent": task.agent,
        "requirements": task.requirements,
        "description": task.description[:500],
        "submitted_at": datetime.now().isoformat(),
        "status": "pending",
    }
    manifest_file = task_file.with_suffix(".json")
    manifest_file.write_text(json.dumps(manifest, indent=2))

    # Step 4: Open project in Antigravity
    open_project(task.project_name)

    # Step 5: Try clipboard paste (best effort)
    task_text = _build_task_text(task)
    _set_clipboard(task_text)

    duration_ms = int((time.time() - start_time) * 1000)

    return AntigravityResult(
        task=task,
        success=True,
        script_executed="launch → save_task → open_project → clipboard_set",
        output=(
            f"Task saved to {task_file}. "
            f"Antigravity opened with project. "
            f"Task is in clipboard — paste into agent input and press Enter."
        ),
        duration_ms=duration_ms,
    )


def _build_task_text(task: AntigravityTask) -> str:
    """Build the formatted task text for Antigravity input."""
    parts = []

    if task.context:
        parts.append(f"## Context\n{task.context}\n")

    parts.append(f"## Task\n{task.description}\n")

    if task.requirements:
        parts.append("## Requirements")
        for req in task.requirements:
            parts.append(f"- {req}")
        parts.append("")

    if task.agent != "auto":
        parts.append(f"## Agent: {task.agent}")

    return "\n".join(parts)


def submit_task_headless(task: AntigravityTask) -> AntigravityResult:
    """
    Headless submission without AppleScript GUI interaction.

    Instead of pasting into the IDE, saves the task to the project's
    orchestration directory for manual pickup or future automation.

    This is the fallback when AppleScript GUI control isn't reliable.
    """
    start_time = time.time()

    project_path = PROJECTS_ROOT / task.project_name
    if not project_path.exists():
        return AntigravityResult(
            task=task,
            success=False,
            error=f"Project not found: {project_path}",
            duration_ms=int((time.time() - start_time) * 1000),
        )

    # Save task to project
    task_file = project_path / ".antigravity" / "pending_tasks" / f"task_{int(time.time())}.md"
    task_file.parent.mkdir(parents=True, exist_ok=True)
    task_file.write_text(_build_task_text(task), encoding="utf-8")

    # Also create a task manifest
    manifest = {
        "task": task.project_name,
        "description": task.description,
        "agent": task.agent,
        "requirements": task.requirements,
        "submitted_at": datetime.now().isoformat(),
        "status": "pending",
        "submission_method": "headless",
    }
    manifest_file = task_file.with_suffix(".json")
    manifest_file.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    return AntigravityResult(
        task=task,
        success=True,
        script_executed="headless file write",
        output=f"Task saved to {task_file}. Open Antigravity and paste manually, or use submit_task_antigravity() for GUI automation.",
        duration_ms=int((time.time() - start_time) * 1000),
    )


def monitor_artifacts(project_name: str, task_id: str | None = None,
                       timeout: int = 300, polling_interval: int = 5) -> dict:
    """
    Monitor Antigravity artifact output for a project.

    Watches .antigravity/artifacts/ for new or modified files.
    Returns when artifacts are found or timeout is reached.

    Args:
        project_name: Name of the project
        task_id: Optional specific task to monitor for
        timeout: Maximum seconds to wait
        polling_interval: Seconds between checks

    Returns:
        Dict with artifacts found and status
    """
    artifacts_dir = PROJECTS_ROOT / project_name / ".antigravity" / "artifacts"

    if not artifacts_dir.exists():
        return {"status": "no_artifacts_dir", "artifacts": [], "waited_s": 0}

    # Snapshot of existing artifacts
    existing = {f.name: f.stat().st_mtime for f in artifacts_dir.iterdir() if f.is_file()}

    start_time = time.time()
    new_artifacts = []

    while time.time() - start_time < timeout:
        time.sleep(polling_interval)

        current = {f.name: f.stat().st_mtime for f in artifacts_dir.iterdir() if f.is_file()}

        # Find new or modified files
        for name, mtime in current.items():
            if name not in existing or existing[name] < mtime:
                # New or modified
                if task_id is None or task_id in name:
                    new_artifacts.append(name)

        if new_artifacts:
            return {
                "status": "artifacts_found",
                "artifacts": new_artifacts,
                "waited_s": int(time.time() - start_time),
            }

    return {"status": "timeout", "artifacts": [], "waited_s": timeout}


# ── Status ──

def get_antigravity_status() -> dict:
    """Get the current status of Antigravity."""
    running = is_antigravity_running()
    installed = is_antigravity_available()

    return {
        "installed": installed,
        "running": running,
        "app_path": ANTIGRAVITY_APP,
        "ready": installed and running,
    }
