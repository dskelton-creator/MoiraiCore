"""
Project Space Manager — Creates and manages isolated customer project spaces.

Each project lives in /projects/[NAME]/ with its own:
  - Source code (src/)
  - Tests (tests/)
  - Documentation (docs/)
  - Orchestration (orchestration/)
  - State (.os_state.json)
  - Manifest (README.md)
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── Paths ──

AGENT_OS_ROOT = Path(os.environ.get("AGENT_OS_ROOT", str(Path(__file__).resolve().parents[1])))
PROJECTS_DIR = AGENT_OS_ROOT / "projects"
TEMPLATES_DIR = PROJECTS_DIR / ".templates"

# ── Project Manifest Schema ──

PROJECT_MANIFEST_VERSION = "1.0"

PROJECT_CATEGORIES = [
    "blank",
    "web-app",
    "api-service",
    "cli-tool",
    "data-pipeline",
    "ml-model",
    "automation",
    "integration",
    "other",
]

PROJECT_STATUSES = [
    "draft",
    "active",
    "paused",
    "completed",
    "archived",
]


def ensure_projects_dir():
    """Ensure the projects directory exists."""
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)


def project_exists(name: str) -> bool:
    """Check if a project exists."""
    project_dir = PROJECTS_DIR / name
    return project_dir.exists() and project_dir.is_dir()


def get_project_path(name: str) -> Path:
    """Get the project directory path."""
    # Sanitize name: only allow alphanumeric, hyphens, underscores
    safe_name = "".join(c for c in name if c.isalnum() or c in "-_").strip()
    if not safe_name:
        raise ValueError(f"Invalid project name: {name!r}")
    return PROJECTS_DIR / safe_name


def _scaffold_from_template(project_path: Path, template_dir: Path,
                            name: str, description: str) -> None:
    """Copy template files into the project, replacing {{name}} and {{description}}."""
    for template_file in template_dir.rglob("*"):
        if template_file.is_file() and not template_file.name.startswith("."):
            # Determine target path
            rel = template_file.relative_to(template_dir)
            if rel.name == "README.md":
                target = project_path / "README.md"
            elif rel.name.startswith("test_"):
                target = project_path / "tests" / rel.name
            elif rel.suffix == ".py" and rel.name != "README.md":
                target = project_path / "src" / rel.name
            elif rel.suffix == ".md":
                target = project_path / "docs" / rel.name
            else:
                target = project_path / rel

            target.parent.mkdir(parents=True, exist_ok=True)

            # Read and replace template variables
            try:
                content = template_file.read_text(encoding="utf-8")
                content = content.replace("{{name}}", name)
                content = content.replace("{{description}}", description or name)
                target.write_text(content, encoding="utf-8")
            except Exception:
                # For binary files, just copy
                shutil.copy2(template_file, target)


def create_project(
    name: str,
    description: str = "",
    category: str = "other",
    template: str = "blank",
    api_key: str = None,
) -> dict:
    """
    Create a new isolated project space.

    If template matches a directory in .templates/, scaffold from that template.
    Returns the project manifest as a dict.
    """
    ensure_projects_dir()

    project_path = get_project_path(name)

    if project_path.exists():
        raise FileExistsError(f"Project '{name}' already exists at {project_path}")

    if category not in PROJECT_CATEGORIES:
        raise ValueError(
            f"Invalid category '{category}'. Available: {', '.join(PROJECT_CATEGORIES)}"
        )

    # Generate API key if not provided
    if not api_key:
        api_key = f"proj_{uuid.uuid4().hex[:24]}"

    # Create directory structure
    project_path.mkdir(parents=True)
    (project_path / "src").mkdir()
    (project_path / "tests").mkdir()
    (project_path / "docs").mkdir()
    (project_path / "orchestration").mkdir()

    # Scaffold from template if available
    template_dir = TEMPLATES_DIR / template
    if template_dir.exists() and template != "blank":
        _scaffold_from_template(project_path, template_dir, name, description)

    # Always copy agent_client.py to orchestration/
    agent_client_src = TEMPLATES_DIR / "agent_client.py"
    if agent_client_src.exists():
        shutil.copy2(agent_client_src, project_path / "orchestration" / "agent_client.py")

    # Create manifest
    manifest = {
        "version": PROJECT_MANIFEST_VERSION,
        "name": name,
        "description": description,
        "category": category,
        "status": "draft",
        "template": template,
        "api_key": api_key,
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "os_state": {
            "goals": [],
            "backlog": [],
            "sprints": [],
            "current_sprint": None,
        },
    }

    manifest_path = project_path / "project.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))

    # Create .os_state.json
    os_state = {
        "version": "1.0",
        "project_name": name,
        "goals": [],
        "tasks": [],
        "backlog": [],
        "decisions": [],
        "sprints": [],
        "current_sprint": None,
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
    }

    os_state_path = project_path / ".os_state.json"
    os_state_path.write_text(json.dumps(os_state, indent=2))

    # Create README.md
    readme_content = f"""# {name}

{description}

## Category: {category}

## Getting Started

1. Review the project state: `.os_state.json`
2. Source code: `src/`
3. Tests: `tests/`
4. Documentation: `docs/`
5. Orchestration: `orchestration/`

## MoiraiCore Integration

This project can communicate with the Host OS via:

- API: `http://localhost:7878/api/projects/{name}/state`
- Agent: Use Hermes Bridge with `project_id="{name}"`
- API Key: `{api_key}`

## Status: draft
"""
    (project_path / "README.md").write_text(readme_content)

    # Create .gitignore
    gitignore = """__pycache__/
*.pyc
*.pyo
.env
*.db
*.db-shm
*.db-wal
node_modules/
.venv/
"""
    (project_path / ".gitignore").write_text(gitignore)

    return manifest


def list_projects() -> list[dict]:
    """List all projects with their manifests.

    The manifest (project.json) carries a static os_state block baked in at
    creation time. The live goal/task/decision state actually lives in
    .os_state.json (mutated by add_project_goal etc.). Merge the live state
    back into each project's os_state so callers (e.g. the dashboard project
    list) see real counts instead of the stale manifest snapshot.
    """
    ensure_projects_dir()
    projects = []

    for item in sorted(PROJECTS_DIR.iterdir()):
        if item.is_dir() and not item.name.startswith("."):
            manifest_path = item / "project.json"
            if manifest_path.exists():
                try:
                    manifest = json.loads(manifest_path.read_text())
                except Exception:
                    projects.append({
                        "name": item.name,
                        "error": "Failed to read manifest",
                    })
                    continue
                # Overlay live state from .os_state.json
                live = get_project_state(item.name) or {}
                os_state = manifest.get("os_state", {}) or {}
                for key in ("goals", "tasks", "decisions", "backlog"):
                    if key in live:
                        os_state[key] = live[key]
                manifest["os_state"] = os_state
                projects.append(manifest)

    return projects


def get_project(name: str) -> Optional[dict]:
    """Get a project's manifest."""
    project_path = get_project_path(name)
    manifest_path = project_path / "project.json"

    if not manifest_path.exists():
        return None

    return json.loads(manifest_path.read_text())


def get_project_state(name: str) -> Optional[dict]:
    """Get a project's .os_state.json."""
    project_path = get_project_path(name)
    state_path = project_path / ".os_state.json"

    if not state_path.exists():
        return None

    return json.loads(state_path.read_text())


def update_project_state(name: str, updates: dict) -> dict:
    """Update a project's .os_state.json."""
    project_path = get_project_path(name)
    state_path = project_path / ".os_state.json"

    if not state_path.exists():
        raise FileNotFoundError(f"Project '{name}' not found")

    state = json.loads(state_path.read_text())

    # Deep merge updates
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(state.get(key), dict):
            state[key].update(value)
        elif isinstance(value, list) and key in state:
            state[key].extend(value)
        else:
            state[key] = value

    state["updated_at"] = datetime.now().isoformat()

    state_path.write_text(json.dumps(state, indent=2))
    return state


def add_project_goal(name: str, goal_text: str, agent: str = "hermes") -> dict:
    """Add a goal to a project's .os_state.json (NOT global goals.json)."""
    state = get_project_state(name)
    if not state:
        raise FileNotFoundError(f"Project '{name}' not found")

    goal = {
        "id": f"proj-goal-{uuid.uuid4().hex[:12]}",
        "text": goal_text,
        "agent": agent,
        "status": "pending",
        "created_at": datetime.now().isoformat(),
    }
    state["goals"].append(goal)
    state["updated_at"] = datetime.now().isoformat()

    state_path = get_project_path(name) / ".os_state.json"
    state_path.write_text(json.dumps(state, indent=2))
    return goal


def add_project_task(name: str, task_text: str, goal_id: str = None) -> dict:
    """Add a task to a project's .os_state.json."""
    state = get_project_state(name)
    if not state:
        raise FileNotFoundError(f"Project '{name}' not found")

    task = {
        "id": f"proj-task-{uuid.uuid4().hex[:12]}",
        "goal_id": goal_id,
        "text": task_text,
        "status": "pending",
        "created_at": datetime.now().isoformat(),
    }
    state["tasks"].append(task)
    state["updated_at"] = datetime.now().isoformat()

    state_path = get_project_path(name) / ".os_state.json"
    state_path.write_text(json.dumps(state, indent=2))
    return task


def add_project_decision(name: str, decision: str, rationale: str = "") -> dict:
    """Log an architectural decision in the project state."""
    state = get_project_state(name)
    if not state:
        raise FileNotFoundError(f"Project '{name}' not found")

    entry = {
        "id": f"dec-{uuid.uuid4().hex[:8]}",
        "decision": decision,
        "rationale": rationale,
        "created_at": datetime.now().isoformat(),
    }
    state["decisions"].append(entry)
    state["updated_at"] = datetime.now().isoformat()

    state_path = get_project_path(name) / ".os_state.json"
    state_path.write_text(json.dumps(state, indent=2))
    return entry


def update_project(name: str, updates: dict) -> dict:
    """Update a project's manifest."""
    project_path = get_project_path(name)
    manifest_path = project_path / "project.json"

    if not manifest_path.exists():
        raise FileNotFoundError(f"Project '{name}' not found")

    manifest = json.loads(manifest_path.read_text())

    # Only allow updating safe fields
    allowed_fields = {"description", "status"}
    for field in allowed_fields:
        if field in updates:
            manifest[field] = updates[field]

    manifest["updated_at"] = datetime.now().isoformat()
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return manifest


def delete_project(name: str) -> bool:
    """Delete a project. Returns True if successful."""
    project_path = get_project_path(name)

    if not project_path.exists():
        return False

    shutil.rmtree(project_path)
    return True


def get_project_files(name: str, subdir: str = "") -> list[dict]:
    """List all files in a project directory."""
    project_path = get_project_path(name)
    search_path = project_path / subdir if subdir else project_path

    if not search_path.exists():
        return []

    files = []
    for item in sorted(search_path.rglob("*")):
        if item.is_file() and not item.name.startswith("."):
            rel_path = str(item.relative_to(project_path))
            files.append({
                "path": rel_path,
                "size": item.stat().st_size,
                "mtime": item.stat().st_mtime,
            })

    return files
