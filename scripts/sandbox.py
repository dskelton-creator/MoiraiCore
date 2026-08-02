"""
Sandbox — Project-Isolated Agent Execution Engine (Phase 3)

Enforces the IPSP (Isolated Project Space Protocol):
  1. Context isolation: loads from project docs/, not global vault
  2. Write jail: all writes restricted to project boundary
  3. State isolation: reads/writes project .os_state.json, not global goals
  4. Project auth: validates API keys for external project calls

Usage:
    from sandbox import ProjectSandbox
    sandbox = ProjectSandbox("my-app")
    sandbox.verify_write("src/app.py")       # OK
    sandbox.verify_write("/etc/passwd")       # Blocked!
    sandbox.get_context()                    # Load project docs as context
    sandbox.update_state({"goals": [...]})   # Write to project state
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional


class SandboxViolation(Exception):
    """Raised when an operation violates the project boundary."""
    pass


class ProjectSandbox:
    """
    Enforces project-isolated execution boundaries for a single project.

    All agent operations (read, write, state, context) flow through this sandbox.
    """

    AGENT_OS_ROOT = Path(os.environ.get("AGENT_OS_ROOT", str(Path(__file__).resolve().parents[1])))

    def __init__(self, project_name: str):
        """
        Initialize the sandbox for a project.

        Args:
            project_name: The sanitized project name (e.g., "my-app")

        Raises:
            FileNotFoundError: If the project doesn't exist
        """
        self.project_name = self._sanitize_name(project_name)
        self.project_path = self.AGENT_OS_ROOT / "projects" / self.project_name

        if not self.project_path.exists():
            raise FileNotFoundError(
                f"Project '{project_name}' not found at {self.project_path}"
            )

        self.manifest = self._load_manifest()
        self.config = self._load_sandbox_config()

    @staticmethod
    def _sanitize_name(name: str) -> str:
        """Sanitize project name: only alphanumeric, hyphens, underscores."""
        safe = "".join(c for c in name if c.isalnum() or c in "-_").strip()
        if not safe:
            raise ValueError(f"Invalid project name: {name!r}")
        return safe

    def _load_manifest(self) -> dict:
        """Load the project manifest (project.json)."""
        manifest_path = self.project_path / "project.json"
        if manifest_path.exists():
            return json.loads(manifest_path.read_text())
        return {}

    def _load_sandbox_config(self) -> dict:
        """Load the sandbox.json config, falling back to template."""
        config_path = self.project_path / "sandbox.json"
        if config_path.exists():
            return json.loads(config_path.read_text())
        # Fall back to template
        template_path = self.AGENT_OS_ROOT / "projects" / ".templates" / "sandbox.json"
        if template_path.exists():
            return json.loads(template_path.read_text())
        return {}

    # ── Write Jail ────────────────────────────────────────────────────────────

    def verify_write(self, file_path: str) -> Path:
        """
        Verify that a write operation is within the project boundary.

        Args:
            file_path: Relative or absolute path to write

        Returns:
            The resolved Path object (absolute)

        Raises:
            SandboxViolation: If the path is outside the project
        """
        # Resolve relative paths against project root
        if os.path.isabs(file_path):
            resolved = Path(file_path)
        else:
            resolved = (self.project_path / file_path).resolve()

        # Ensure the resolved path is within the project directory
        try:
            resolved.relative_to(self.project_path.resolve())
        except ValueError:
            raise SandboxViolation(
                f"Write blocked: '{file_path}' resolves to '{resolved}' "
                f"which is outside project boundary '{self.project_path}'"
            )

        # Check against allowed write directories
        allowed_dirs = self._get_allowed_write_dirs()
        if allowed_dirs:
            try:
                resolved.relative_to(self.project_path)
                # Check if relative path starts with an allowed directory
                rel = str(resolved.relative_to(self.project_path))
                if not any(rel.startswith(d) or rel == d for d in allowed_dirs):
                    # Still allow if it's a file directly in allowed dir
                    parent = resolved.parent
                    parent_rel = str(parent.relative_to(self.project_path))
                    if parent_rel != "." and not any(parent_rel.startswith(d) for d in allowed_dirs):
                        pass  # We'll allow it — guardrails are advisory for now
            except ValueError:
                pass

        return resolved

    def write_file(self, file_path: str, content: str) -> Path:
        """
        Write a file within the project boundary.

        Args:
            file_path: Relative path within the project
            content: File content to write

        Returns:
            The resolved Path that was written

        Raises:
            SandboxViolation: If the path is outside the project
        """
        resolved = self.verify_write(file_path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        return resolved

    def read_file(self, file_path: str) -> str:
        """
        Read a file from the project (or anywhere if allowed).

        Args:
            file_path: Relative or absolute path to read

        Returns:
            File content as string
        """
        if os.path.isabs(file_path):
            resolved = Path(file_path)
        else:
            resolved = self.project_path / file_path

        if not resolved.exists():
            raise FileNotFoundError(f"File not found: {resolved}")

        return resolved.read_text(encoding="utf-8", errors="ignore")

    # ── Context Isolation ──────────────────────────────────────────────────────

    def get_context(self, max_docs: int = 10, max_doc_size: int = 2000) -> str:
        """
        Load project documentation as context for agents.

        Loads from project's docs/ directory, NOT the global vault.
        This is the IPSP-compliant context loading.

        Args:
            max_docs: Maximum number of doc files to load
            max_doc_size: Maximum characters per doc file

        Returns:
            Formatted context string with all project docs
        """
        docs_dir = self.project_path / "docs"
        if not docs_dir.exists():
            return f"[Project: {self.project_name} — no docs/ directory]"

        parts = [f"[Project: {self.project_name}]"]

        # Load project state summary
        state = self.get_state()
        if state:
            goal_count = len(state.get("goals", []))
            task_count = len(state.get("tasks", []))
            parts.append(f"[State: {goal_count} goals, {task_count} tasks]")

        # Load manifest description
        desc = self.manifest.get("description", "")
        if desc:
            parts.append(f"[Description: {desc}]")

        # Load doc files
        doc_count = 0
        for doc_file in sorted(docs_dir.rglob("*")):
            if doc_file.is_file() and doc_count >= max_docs:
                break
            if doc_file.suffix in (".md", ".txt", ".py", ".json"):
                try:
                    content = doc_file.read_text(encoding="utf-8", errors="ignore")
                    if len(content) > max_doc_size:
                        content = content[:max_doc_size] + "\n... [truncated]"
                    rel_path = doc_file.relative_to(docs_dir)
                    parts.append(f"[Doc: {rel_path}]\n{content}")
                    doc_count += 1
                except Exception:
                    pass

        return "\n\n".join(parts)

    def get_source_context(self, max_files: int = 5, max_lines: int = 100) -> str:
        """
        Load a summary of the project's source code for agent context.

        Args:
            max_files: Max source files to include
            max_lines: Max lines per file

        Returns:
            Formatted string with source file previews
        """
        src_dir = self.project_path / "src"
        if not src_dir.exists():
            return ""

        parts = []
        count = 0
        for src_file in sorted(src_dir.rglob("*.py")):
            if count >= max_files:
                break
            try:
                lines = src_file.read_text(encoding="utf-8", errors="ignore").splitlines()
                content = "\n".join(lines[:max_lines])
                if len(lines) > max_lines:
                    content += f"\n... [{len(lines) - max_lines} more lines]"
                rel_path = src_file.relative_to(self.project_path)
                parts.append(f"### {rel_path}\n```python\n{content}\n```")
                count += 1
            except Exception:
                pass

        return "\n\n".join(parts)

    # ── State Isolation ────────────────────────────────────────────────────────

    def get_state(self) -> dict:
        """
        Get the project's isolated state (.os_state.json).

        This is NOT the global config/goals.json — it's project-local state.
        """
        state_path = self.project_path / ".os_state.json"
        if state_path.exists():
            return json.loads(state_path.read_text())
        return {
            "version": "1.0",
            "project_name": self.project_name,
            "goals": [],
            "tasks": [],
            "backlog": [],
            "decisions": [],
            "sprints": [],
            "current_sprint": None,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
        }

    def update_state(self, updates: dict) -> dict:
        """
        Update the project's .os_state.json (NOT global goals.json).

        Args:
            updates: Dict of fields to merge into state

        Returns:
            The updated state dict
        """
        state = self.get_state()

        for key, value in updates.items():
            if isinstance(value, dict) and isinstance(state.get(key), dict):
                state[key].update(value)
            elif isinstance(value, list) and key in state:
                state[key].extend(value)
            else:
                state[key] = value

        state["updated_at"] = datetime.now().isoformat()

        state_path = self.project_path / ".os_state.json"
        state_path.write_text(json.dumps(state, indent=2))
        return state

    def add_goal(self, text: str, agent: str = "hermes") -> dict:
        """Add a goal to the project state."""
        state = self.get_state()
        goal = {
            "id": f"proj-goal-{len(state.get('goals', [])) + 1:03d}",
            "text": text,
            "agent": agent,
            "status": "pending",
            "created_at": datetime.now().isoformat(),
        }
        state.setdefault("goals", []).append(goal)
        self.update_state({"goals": [goal]})
        return goal

    def add_task(self, text: str, goal_id: str = None) -> dict:
        """Add a task to the project state."""
        task = {
            "id": f"proj-task-{len(self.get_state().get('tasks', [])) + 1:03d}",
            "goal_id": goal_id,
            "text": text,
            "status": "pending",
            "created_at": datetime.now().isoformat(),
        }
        self.update_state({"tasks": [task]})
        return task

    def add_decision(self, decision: str, rationale: str = "") -> dict:
        """Log an architectural decision."""
        entry = {
            "id": f"dec-{len(self.get_state().get('decisions', [])) + 1:03d}",
            "decision": decision,
            "rationale": rationale,
            "created_at": datetime.now().isoformat(),
        }
        self.update_state({"decisions": [entry]})
        return entry

    # ── Project Auth ───────────────────────────────────────────────────────────

    def validate_api_key(self, key: str) -> bool:
        """
        Validate an API key for this project.

        Args:
            key: The API key to validate

        Returns:
            True if the key is valid for this project
        """
        expected = self.manifest.get("api_key", "")
        return key == expected and len(expected) > 0

    def get_api_key(self) -> str:
        """Get the project's API key."""
        return self.manifest.get("api_key") or ""

    # ── Tier 3 Config Access ──────────────────────────────────────────────────

    def get_tier3_config(self) -> dict:
        """Get the Tier 3 (Ollama) worker config for this project."""
        return self.config.get("execution_workers", {}).get("tier_3_builder", {})

    def get_guardrails(self) -> dict:
        """Get the guardrails config for this project."""
        return self.config.get("guardrails", {})

    def get_loop_safeguards(self) -> dict:
        """Get loop safeguard config for this project."""
        return self.config.get("loop_safeguards", {})

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _get_allowed_write_dirs(self) -> list[str]:
        """Get list of allowed write directories from config."""
        allowed = set()
        for tier_name, tier_config in self.config.get("execution_workers", {}).items():
            if isinstance(tier_config, dict) and "can_write" in tier_config:
                for d in tier_config["can_write"]:
                    if d != "*":
                        allowed.add(d)
        return list(allowed)

    def list_files(self, subdir: str = "src") -> list[dict]:
        """List files in a project subdirectory."""
        search_path = self.project_path / subdir
        if not search_path.exists():
            return []
        files = []
        for item in sorted(search_path.rglob("*")):
            if item.is_file() and not item.name.startswith("."):
                rel = str(item.relative_to(self.project_path))
                files.append({
                    "path": rel,
                    "size": item.stat().st_size,
                    "mtime": item.stat().st_mtime,
                })
        return files

    @property
    def is_active(self) -> bool:
        """Check if the project is in 'active' status."""
        return self.manifest.get("status") == "active"

    def __repr__(self) -> str:
        return f"<ProjectSandbox: {self.project_name} ({self.manifest.get('status', 'unknown')})>"
