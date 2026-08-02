"""
Antigravity Worker — Tier 2 Architect Engine (Google Antigravity IDE).

Tier 2 architect option that delegates complex coding tasks to Antigravity IDE's
multi-agent orchestration system (Gemini Pro backed).

Selection logic:
  - Used when project sandbox.json has tier_2_architect.engine = "antigravity"
  - Used when task triggers include: parallel, multi-agent, heavy coding, autonomous
  - Falls back to gemini-worker if Antigravity is not available

Since Antigravity is an Electron app (no standalone CLI), this worker:
  1. Prepares the project context
  2. Generates the Antigravity command/script
  3. Returns the command for execution via osascript or manual trigger
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class AntigravityConfig:
    """Configuration for Antigravity Tier 2 worker."""
    enabled: bool = True
    # Antigravity doesn't have a CLI; we use osascript for control
    app_path: str = "/Applications/Antigravity.app"
    use_applescript: bool = True
    # If a CLI becomes available in the future
    cli_path: str = ""
    # Gemini model to use within Antigravity
    model: str = "gemini-2.5-pro"
    # Timeout for Antigravity operations (seconds)
    timeout: int = 300
    # Maximum parallel agents within Antigravity
    max_agents: int = 4

    @classmethod
    def from_env(cls) -> "AntigravityConfig":
        """Load config from environment or defaults."""
        return cls(
            enabled=os.environ.get("ANTIGRAVITY_ENABLED", "1") != "0",
            app_path=os.environ.get("ANTIGRAVITY_APP", "/Applications/Antigravity.app"),
            cli_path=os.environ.get("ANTIGRAVITY_CLI", ""),
            model=os.environ.get("ANTIGRAVITY_MODEL", "gemini-2.5-pro"),
            timeout=int(os.environ.get("ANTIGRAVITY_TIMEOUT", "300")),
        )


@dataclass
class AntigravityTask:
    """A task to be executed by Antigravity."""
    project_space: str
    description: str
    task_type: str = "code_generation"  # code_generation, code_refactor, architecture, research
    agent: str = "auto"  # auto, coder, researcher, writer, or specific agent name
    parallel: bool = False
    context: str = ""
    requirements: list[str] = field(default_factory=list)


@dataclass
class AntigravityResult:
    """Result from Antigravity execution."""
    task: AntigravityTask
    success: bool
    method: str = ""  # "applescript", "cli", "manual"
    command: str = ""
    output: str = ""
    error: str = ""
    duration_ms: int = 0
    artifacts_generated: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "method": self.method,
            "command": self.command,
            "output_preview": self.output[:500] if self.output else "",
            "error": self.error,
            "duration_ms": self.duration_ms,
            "artifacts_generated": self.artifacts_generated,
        }


def is_antigravity_available(config: AntigravityConfig = None) -> bool:
    """Check if Antigravity is installed and available."""
    if config is None:
        config = AntigravityConfig.from_env()

    if not config.enabled:
        return False

    # Check if the app exists
    app_path = Path(config.app_path)
    return app_path.exists() and app_path.is_dir()


def _build_applescript(task: AntigravityTask, config: AntigravityConfig) -> str:
    """Build an AppleScript command to control Antigravity."""
    project_path = Path(task.project_space).resolve()

    if config.cli_path:
        # Future: use CLI if available
        cmd = [
            config.cli_path,
            "run",
            "--project", str(project_path),
            "--task", task.description,
            "--model", config.model,
        ]
        if task.agent != "auto":
            cmd += ["--agent", task.agent]
        if task.parallel:
            cmd.append("--parallel")
        return " ".join(cmd)

    # Current: use AppleScript to control the Electron app
    # Antigravity's AppleScript dictionary is limited, so we use
    # open command + clipboard for task injection
    script_parts = [
        f'tell application "System Events"',
        f'  if not (exists (processes whose name is "Antigravity")) then',
        f'    tell application "{config.app_path}" to activate',
        f'    delay 3',
        f'  end if',
        f'end tell',
        f'',
        f'open "{project_path}"',
    ]

    return "\n".join(script_parts)


def _build_context_injection(task: AntigravityTask, config: AntigravityConfig) -> str:
    """Build the context to inject into Antigravity before task execution."""
    parts = []

    # Project context header
    project_name = Path(task.project_space).name
    parts.append(f"# Project: {project_name}")
    parts.append(f"# Task Type: {task.task_type}")
    parts.append("")

    # Requirements
    if task.requirements:
        parts.append("## Requirements")
        for req in task.requirements:
            parts.append(f"- {req}")
        parts.append("")

    # Context from MoiraiCore
    if task.context:
        parts.append("## Context")
        parts.append(task.context)
        parts.append("")

    # Task description
    parts.append("## Task")
    parts.append(task.description)
    parts.append("")

    # Agent instructions
    if task.agent != "auto":
        parts.append(f"## Agent: {task.agent}")
    else:
        parts.append("## Mode: Auto (select best agent)")

    if task.parallel:
        parts.append("## Execution: Parallel (multi-agent)")

    return "\n".join(parts)


def execute_tier2_antigravity(task: AntigravityTask,
                                config: AntigravityConfig = None) -> AntigravityResult:
    """
    Execute a Tier 2 architect task via Antigravity IDE.

    Since Antigravity is an Electron app without a standalone CLI,
    this returns the command/script for execution rather than running
    it directly. The caller (Hermes Bridge) should:
    1. Save the context injection to the project
    2. Present the command to the user or execute via osascript
    3. Monitor Antigravity's output via project state polling

    Args:
        task: The architecture/coding task
        config: Antigravity configuration

    Returns:
        AntigravityResult with command and instructions
    """
    if config is None:
        config = AntigravityConfig.from_env()

    start_time = time.time()

    # Check availability
    if not is_antigravity_available(config):
        return AntigravityResult(
            task=task,
            success=False,
            error="Antigravity not available. Install from https://antigravity.google.com",
            duration_ms=int((time.time() - start_time) * 1000),
        )

    # Build context injection
    context_text = _build_context_injection(task, config)

    # Save context to project for Antigravity to pick up
    context_file = Path(task.project_space) / "orchestration" / "context_injection.md"
    context_file.parent.mkdir(parents=True, exist_ok=True)
    context_file.write_text(context_text, encoding="utf-8")

    # Build the execution command
    if config.cli_path:
        # CLI mode (future)
        method = "cli"
        command = _build_applescript(task, config)
    elif config.use_applescript:
        # AppleScript mode (current)
        method = "applescript"
        applescript = _build_applescript(task, config)
        command = f"""# Execute this AppleScript to trigger Antigravity:
#
# 1. Open Script Editor (Applications > Utilities > Script Editor)
# 2. Paste the following:
#
# --- AppleScript ---
# {applescript}
# --------------------
#
# 3. Run the script
# 4. Antigravity will open the project and await task input
# 5. Paste the task description from: {context_file}
#
# Or run from terminal:
osascript -e '<escaped_applescript>'
"""
    else:
        method = "manual"
        command = f"""# Manual execution:
# 1. Open Antigravity: open {config.app_path}
# 2. Open project: {task.project_space}
# 3. Paste context from: {context_file}
# 4. Submit task: {task.description}
"""

    duration_ms = int((time.time() - start_time) * 1000)

    return AntigravityResult(
        task=task,
        success=True,
        method=method,
        command=command,
        output=f"Context saved to {context_file}. Ready for Antigravity execution.",
        duration_ms=duration_ms,
        artifacts_generated=[str(context_file)],
    )


def get_antigravity_models() -> list[dict]:
    """Get available models in Antigravity."""
    return [
        {"name": "gemini-2.5-pro", "provider": "google", "type": "reasoning"},
        {"name": "gemini-2.5-flash", "provider": "google", "type": "fast"},
        {"name": "gemini-2.0-flash", "provider": "google", "type": "fast"},
    ]


def get_antigravity_agents() -> list[dict]:
    """Get available agents in Antigravity."""
    return [
        {"key": "auto", "name": "Auto-Select", "description": "Let Antigravity choose the best agent"},
        {"key": "coder", "name": "Coder", "description": "Code generation and implementation"},
        {"key": "researcher", "name": "Researcher", "description": "Research and analysis"},
        {"key": "writer", "name": "Writer", "description": "Documentation and content generation"},
        {"key": "architect", "name": "Architect", "description": "System design and architecture"},
    ]
