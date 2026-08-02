"""
Antigravity Workspace Engine — Primary execution surface for project isolation.

Implements the Google Antigravity IDE Extension Protocol:
  1. Project creation via Antigravity workspace engine (not manual directories)
  2. Multi-model execution cascade (Tier 2: Gemini Pro Artifacts, Tier 3: Ornith 9B terminal)
  3. Artifact-based completion evaluation (.antigravity/artifacts/)
  4. Strict isolation — Antigravity artifacts never leak into Hermes core config

Antigravity manages customer projects as isolated workspace clients:
  /projects/[project-name]/
  ├── .antigravity/
  │   ├── workspace.json       ← Antigravity workspace config (isolated from MoiraiCore)
  │   ├── artifacts/           ← Verifiable task outputs
  │   │   ├── test_run_*.log
  │   │   ├── plan_checkoff_*.json
  │   │   └── headless_recording_*.webm
  │   └── memory/              ← Model memory context (isolated)
  ├── src/                    ← Application code
  ├── tests/                  ← Test suite
  ├── orchestration/          ← MoiraiCore orchestration (NOT Antigravity artifacts)
  ├── .os_state.json          ← MoiraiCore project state (NOT Antigravity config)
  └── project.json            ← MoiraiCore manifest (NOT Antigravity workspace)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional


# ── Paths ──

ANTIGRAVITY_ROOT = Path(os.environ.get("ANTIGRAVITY_ROOT", str(Path(__file__).resolve().parents[1] / "projects")))
ANTIGRAVITY_ARTIFACTS_DIR = ".antigravity/artifacts"
ANTIGRAVITY_WORKSPACE_FILE = ".antigravity/workspace.json"


# ── Isolation Guard ──

# These paths are ALWAYS off-limits to Antigravity artifact reads/writes.
HERMES_CORE_PATHS = [
    "config/",
    "dashboard/",
    "jarvis/",
    "memory-vault/",
    "scripts/",
    "workspace/",
    ".env",
]


class AntigravityIsolationError(Exception):
    """Raised when an operation would violate Antigravity↔Hermes isolation."""
    pass


def _assert_within_project(file_path: str, project_space: str) -> Path:
    """
    Assert that a file path is within the project boundary.
    Raises AntigravityIsolationError if the path escapes.
    """
    resolved = Path(file_path).resolve() if os.path.isabs(file_path) else (Path(project_space) / file_path).resolve()
    project_resolved = Path(project_space).resolve()

    try:
        resolved.relative_to(project_resolved)
    except ValueError:
        raise AntigravityIsolationError(
            f"Isolation violation: '{file_path}' resolves outside project '{project_space}'"
        )

    # Check against Hermes core paths
    rel = str(resolved.relative_to(project_resolved))
    for core_path in HERMES_CORE_PATHS:
        if rel.startswith(core_path):
            raise AntigravityIsolationError(
                f"Isolation violation: '{rel}' is within Hermes core path '{core_path}'. "
                f"Antigravity artifacts must never write to Hermes core."
            )

    return resolved


# ── Antigravity Workspace Engine ──

class AntigravityWorkspace:
    """
    Manages an Antigravity workspace for a single customer project.

    The workspace is the boundary between Antigravity's world and Hermes's world.
    All Antigravity operations flow through this class.
    """

    def __init__(self, project_name: str, base_path: str = None):
        self.project_name = self._sanitize_name(project_name)
        self.base_path = Path(base_path) if base_path else ANTIGRAVITY_ROOT
        self.project_path = self.base_path / self.project_name
        self.antigravity_dir = self.project_path / ".antigravity"
        self.artifacts_dir = self.antigravity_dir / "artifacts"
        self.memory_dir = self.antigravity_dir / "memory"
        self.workspace_config_path = self.antigravity_dir / "workspace.json"

    @staticmethod
    def _sanitize_name(name: str) -> str:
        """Sanitize project name: only alphanumeric, hyphens, underscores."""
        safe = "".join(c for c in name if c.isalnum() or c in "-_").strip()
        if not safe:
            raise ValueError(f"Invalid project name: {name!r}")
        return safe

    def exists(self) -> bool:
        """Check if this workspace exists."""
        return self.project_path.exists()

    def is_initialized(self) -> bool:
        """Check if Antigravity workspace is properly initialized."""
        return self.workspace_config_path.exists()

    # ── Section 1: Environment Initialization ──────────────────────────────

    def create_project(self, description: str | None = None, template: str | None = None) -> dict:
        """
        Provision a new isolated project workspace via Antigravity engine.

        This replaces manual directory creation. The Antigravity workspace
        engine provisions the scoped project boundary.
        """
        if self.project_path.exists():
            raise FileExistsError(f"Project '{self.project_name}' already exists")

        # Create the Antigravity workspace structure
        self.antigravity_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.memory_dir.mkdir(parents=True, exist_ok=True)

        # Create standard project subdirectories
        (self.project_path / "src").mkdir(exist_ok=True)
        (self.project_path / "tests").mkdir(exist_ok=True)
        (self.project_path / "orchestration").mkdir(exist_ok=True)

        # Create the Antigravity workspace config (isolated from MoiraiCore config)
        workspace_config = {
            "version": "2.0",
            "name": self.project_name,
            "description": description,
            "template": template,
            "created_at": datetime.now().isoformat(),
            "isolated": True,
            "antigravity_engine": "gemini-3.1-pro",
            "tier2": {
                "engine": "antigravity",
                "model": "gemini-3.1-pro",
                "artifact_types": ["task_list", "implementation_plan", "code_diff", "test_run_log"],
            },
            "tier3": {
                "engine": "ollama_worker",
                "model": "hf.co/deepreinforce-ai/Ornith-1.0-9B-GGUF:Q4_K_M",
                "max_iterations": 5,
                "loop_detection": True,
            },
            "guardrails": {
                "strict_isolation": True,
                "no_hermes_core_write": True,
                "no_global_state_pollution": True,
            },
        }

        self.workspace_config_path.write_text(json.dumps(workspace_config, indent=2))

        # Create the MoiraiCore project manifest (separate from Antigravity workspace)
        manifest = {
            "version": "1.0",
            "name": self.project_name,
            "description": description,
            "category": template,
            "status": "draft",
            "template": template,
            "api_key": f"proj_{uuid.uuid4().hex[:24]}",
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "os_state": {
                "goals": [],
                "tasks": [],
                "backlog": [],
                "decisions": [],
            },
            "antigravity_initialized": True,
        }

        (self.project_path / "project.json").write_text(json.dumps(manifest, indent=2))

        # Create the MoiraiCore .os_state.json
        os_state = {
            "version": "1.0",
            "project_name": self.project_name,
            "goals": [],
            "tasks": [],
            "backlog": [],
            "decisions": [],
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
        }
        (self.project_path / ".os_state.json").write_text(json.dumps(os_state, indent=2))

        # Template-specific scaffolding
        self._scaffold_template(template or "blank", description or "")

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
.antigravity/memory/
"""
        (self.project_path / ".gitignore").write_text(gitignore)

        # Create README
        readme = f"""# {self.project_name}

{description}

## Antigravity Workspace

This project is managed by Antigravity IDE.
- Workspace config: `.antigravity/workspace.json`
- Artifacts: `.antigravity/artifacts/`
- Memory context: `.antigravity/memory/`

## MoiraiCore Integration

- Project manifest: `project.json`
- State: `.os_state.json`
- Source: `src/`
- Tests: `tests/`

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run
python3 src/main.py

# Test
python3 -m pytest tests/ -v
```
"""
        (self.project_path / "README.md").write_text(readme)

        return workspace_config

    def _scaffold_template(self, template: str, description: str) -> None:
        """Scaffold project files based on template."""
        if template == "web-app":
            (self.project_path / "src" / "app.py").write_text(
                f'"""{description or self.project_name}"""\n\n'
                'from fastapi import FastAPI\n\n\n'
                f'app = FastAPI(title="{self.project_name}")\n\n\n'
                '@app.get("/")\n'
                'def root():\n'
                '    return {{"message": "Hello from {self.project_name}"}}\n\n\n'
                '@app.get("/health")\n'
                'def health():\n'
                '    return {{"status": "ok"}}\n\n\n'
                '@app.get("/ready")\n'
                'def readiness():\n'
                '    return {{"ready": True, "checks": []}}\n'
            )
            (self.project_path / "tests" / "test_app.py").write_text(
                '"""Tests for the application."""\n\n'
                'from fastapi.testclient import TestClient\n\n\n'
                'def test_placeholder():\n'
                '    """Placeholder test."""\n'
                '    assert True\n'
            )
            (self.project_path / "requirements.txt").write_text(
                "fastapi>=0.100.0\nuvicorn>=0.23.0\npytest>=7.0.0\nhttpx>=0.24.0\n"
            )
        elif template == "api-service":
            (self.project_path / "src" / "api.py").write_text(
                f'"""{description or self.project_name}"""\n\n'
                'from fastapi import FastAPI\n\n\n'
                f'app = FastAPI(title="{self.project_name}")\n\n\n'
                '@app.get("/api/v1/status")\n'
                'def status():\n'
                f'    return {{"service": "{self.project_name}", "status": "running"}}\n\n\n'
                '@app.get("/api/v1/health")\n'
                'def health():\n'
                '    return {{"healthy": True}}\n'
            )
            (self.project_path / "tests" / "test_api.py").write_text(
                '"""Tests for the API."""\n\n'
                'def test_placeholder():\n'
                '    assert True\n'
            )
            (self.project_path / "requirements.txt").write_text(
                "fastapi>=0.100.0\nuvicorn>=0.23.0\npytest>=7.0.0\nhttpx>=0.24.0\n"
            )
        else:  # blank
            (self.project_path / "src" / "main.py").write_text(
                f'"""{description or self.project_name}"""\n\n\n'
                'def main():\n'
                f'    print("Hello from {self.project_name}!")\n\n\n'
                'if __name__ == "__main__":\n'
                '    main()\n'
            )
            (self.project_path / "tests" / "test_main.py").write_text(
                '"""Tests."""\n\n'
                'def test_placeholder():\n'
                '    assert True\n'
            )

    # ── Section 2: Multi-Model Execution Cascade ────────────────────────────

    def execute_tier2(self, task_description: str, artifact_type: str | None = None) -> dict:
        """
        Execute a Tier 2 Complex Architecture task via Antigravity Gemini Pro engine.

        Returns the instruction set for Antigravity to generate Artifacts.
        """
        if not self.is_initialized():
            return {"ok": False, "error": "Antigravity workspace not initialized"}

        task_id = f"t2-{uuid.uuid4().hex[:10]}"
        timestamp = datetime.now().isoformat()

        # Build the Antigravity execution command
        artifact_type = artifact_type or "implementation_plan"
        execution = {
            "task_id": task_id,
            "tier": 2,
            "engine": "antigravity",
            "model": "gemini-3.1-pro",
            "command": f'antigravity project create --name "{self.project_name}" --path "{self.project_path}"',
            "artifact_type": artifact_type,
            "artifact_path": str(self.artifacts_dir / f"{artifact_type}_{task_id}.json"),
            "timestamp": timestamp,
            "status": "pending",
            "description": task_description,
        }

        # Save the execution request as an artifact
        request_file = self.artifacts_dir / f"task_request_{task_id}.json"
        request_file.write_text(json.dumps(execution, indent=2))

        return {
            "ok": True,
            "task_id": task_id,
            "execution": execution,
            "instructions": (
                f"[Tier 2 → Antigravity Gemini Pro]\n"
                f"Project: {self.project_name}\n"
                f"Task: {task_description}\n"
                f"Artifact type: {artifact_type}\n"
                f"Output path: {execution['artifact_path']}\n"
                f"\n"
                f"Execute via Antigravity IDE:\n"
                f"1. Open: {self.project_path}\n"
                f"2. Submit task to Gemini Pro agent\n"
                f"3. Generate formal Artifact ({artifact_type})\n"
                f"4. Save to: {execution['artifact_path']}"
            ),
        }

    def execute_tier3(self, file_path: str, task_description: str, test_command: str) -> dict:
        """
        Execute a Tier 3 Micro-Fix via Antigravity integrated terminal workspace.

        Delegates to the local Ornith 9B execution node through Antigravity's terminal.
        """
        if not self.is_initialized():
            return {"ok": False, "error": "Antigravity workspace not initialized"}

        # Verify file is within project boundary
        try:
            resolved = _assert_within_project(file_path, str(self.project_path))
        except AntigravityIsolationError as e:
            return {"ok": False, "error": str(e)}

        task_id = f"t3-{uuid.uuid4().hex[:10]}"
        timestamp = datetime.now().isoformat()

        execution = {
            "task_id": task_id,
            "tier": 3,
            "engine": "ollama_worker",
            "model": "hf.co/deepreinforce-ai/Ornith-1.0-9B-GGUF:Q4_K_M",
            "target_file": file_path,
            "task_description": task_description,
            "test_command": test_command,
            "max_iterations": 5,
            "loop_detection": True,
            "timestamp": timestamp,
            "status": "pending",
        }

        # Save the execution request
        request_file = self.artifacts_dir / f"task_request_{task_id}.json"
        request_file.write_text(json.dumps(execution, indent=2))

        return {
            "ok": True,
            "task_id": task_id,
            "execution": execution,
            "instructions": (
                f"[Tier 3 → Antigravity Terminal → Ornith 9B]\n"
                f"Project: {self.project_name}\n"
                f"File: {file_path}\n"
                f"Task: {task_description}\n"
                f"Test: {test_command}\n"
                f"Max iterations: 5\n"
                f"\n"
                f"Execute via Antigravity integrated terminal:\n"
                f"1. Open terminal in: {self.project_path}\n"
                f"2. Run diagnostic repair loop on: {file_path}\n"
                f"3. Verify with: {test_command}\n"
                f"4. Log artifact: {self.artifacts_dir}/test_run_{task_id}.log"
            ),
        }

    # ── Section 3: Artifact Auditing ────────────────────────────────────────

    def list_artifacts(self) -> list[dict]:
        """
        List all Antigravity artifacts for this project.

        Artifacts live in .antigravity/artifacts/ — NOT in Hermes core.
        """
        if not self.artifacts_dir.exists():
            return []

        artifacts = []
        for f in sorted(self.artifacts_dir.iterdir()):
            if f.is_file():
                artifact = {
                    "path": str(f),
                    "name": f.name,
                    "size": f.stat().st_size,
                    "mtime": f.stat().st_mtime,
                    "type": self._classify_artifact(f.name),
                }
                # Parse metadata if JSON
                if f.suffix == ".json":
                    try:
                        data = json.loads(f.read_text())
                        artifact["task_id"] = data.get("task_id", "")
                        artifact["tier"] = data.get("tier", 0)
                        artifact["status"] = data.get("status", "")
                    except Exception:
                        pass
                artifacts.append(artifact)

        return artifacts

    def read_artifact(self, artifact_name: str) -> dict:
        """
        Read a specific artifact for completion evaluation.

        Only reads from .antigravity/artifacts/ — never from Hermes core.
        """
        # Ensure the artifact path is within the project
        try:
            artifact_path = _assert_within_project(artifact_name, str(self.artifacts_dir))
        except AntigravityIsolationError as e:
            return {"ok": False, "error": str(e)}

        if not artifact_path.exists():
            return {"ok": False, "error": f"Artifact not found: {artifact_name}"}

        content = artifact_path.read_text(encoding="utf-8", errors="ignore")

        if artifact_path.suffix == ".json":
            try:
                data = json.loads(content)
                data["_raw"] = content
                return {"ok": True, "artifact": data}
            except json.JSONDecodeError:
                pass

        return {"ok": True, "artifact": {"name": artifact_name, "content": content}}

    def evaluate_completion(self, task_id: str = None) -> dict:
        """
        Evaluate whether a task is 'Complete' by reading its Artifacts.

        Checks:
          1. Test run logs show passing tests
          2. Plan check-offs are all approved
          3. No loop detected in execution
          4. Artifact exists and is valid
        """
        artifacts = self.list_artifacts()

        if task_id:
            artifacts = [a for a in artifacts if a.get("task_id") == task_id]

        if not artifacts:
            return {
                "complete": False,
                "reason": "No artifacts found",
                "artifacts_checked": 0,
            }

        # Evaluate based on artifact types
        test_runs = [a for a in artifacts if a["type"] == "test_run"]
        plans = [a for a in artifacts if a["type"] == "plan_checkoff"]
        task_requests = [a for a in artifacts if a["type"] == "task_request"]

        results = {
            "total_artifacts": len(artifacts),
            "test_runs": len(test_runs),
            "plans": len(plans),
            "task_requests": len(task_requests),
            "artifacts_checked": len(artifacts),
            "checks": [],
        }

        # Check test runs
        all_tests_pass = True
        for tr in test_runs:
            try:
                tr_path = _assert_within_project(tr["path"], str(self.artifacts_dir))
                tr_content = tr_path.read_text()
                # Look for failure indicators
                if "FAILED" in tr_content or "ERROR" in tr_content or "failed" in tr_content.lower():
                    all_tests_pass = False
                    results["checks"].append({
                        "name": tr["name"],
                        "passed": False,
                        "detail": "Test failures detected in log",
                    })
                else:
                    results["checks"].append({
                        "name": tr["name"],
                        "passed": True,
                        "detail": "Test log looks clean",
                    })
            except Exception as e:
                all_tests_pass = False
                results["checks"].append({
                    "name": tr["name"],
                    "passed": False,
                    "detail": str(e),
                })

        # Check plans
        for plan in plans:
            try:
                plan_path = _assert_within_project(plan["path"], str(self.artifacts_dir))
                plan_data = json.loads(plan_path.read_text())
                approved = plan_data.get("approved", False)
                if not approved:
                    all_tests_pass = False
                results["checks"].append({
                    "name": plan["name"],
                    "passed": approved,
                    "detail": "Plan approved" if approved else "Plan not yet approved",
                })
            except Exception as e:
                all_tests_pass = False
                results["checks"].append({
                    "name": plan["name"],
                    "passed": False,
                    "detail": str(e),
                })

        # Check task requests for loop detection
        for req in task_requests:
            try:
                req_path = _assert_within_project(req["path"], str(self.artifacts_dir))
                req_data = json.loads(req_path.read_text())
                if req_data.get("status") == "loop_detected":
                    all_tests_pass = False
                    results["checks"].append({
                        "name": req["name"],
                        "passed": False,
                        "detail": "Loop detected in execution",
                    })
            except Exception:
                pass

        results["complete"] = all_tests_pass and len(results["checks"]) > 0
        results["evaluated_at"] = datetime.now().isoformat()

        return results

    def write_artifact(self, artifact_name: str, content: str, metadata: dict | None = None) -> dict:
        """
        Write an artifact to the .antigravity/artifacts/ directory.

        This is the ONLY way Antigravity should write files in the project.
        Strict isolation is enforced.
        """
        # Verify path is within the artifacts directory
        # Resolve the artifact name relative to artifacts_dir and check it stays inside
        artifacts_dir_resolved = self.artifacts_dir.resolve()
        artifact_path = (self.artifacts_dir / artifact_name).resolve()

        try:
            artifact_path.relative_to(artifacts_dir_resolved)
        except ValueError:
            return {"ok": False, "error": f"Isolation violation: '{artifact_name}' escapes artifacts directory"}

        # Write the artifact
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(content, encoding="utf-8")

        # Write metadata if provided
        if metadata:
            meta_path = artifact_path.with_suffix(".meta.json")
            meta_path.write_text(json.dumps(metadata, indent=2))

        return {"ok": True, "path": str(artifact_path)}

    # ── Workspace Config Access ─────────────────────────────────────────────

    def get_workspace_config(self) -> dict:
        """Get the Antigravity workspace configuration."""
        if not self.workspace_config_path.exists():
            return {}
        return json.loads(self.workspace_config_path.read_text())

    def get_tier2_config(self) -> dict:
        """Get Tier 2 (Antigravity) configuration."""
        return self.get_workspace_config().get("tier2", {})

    def get_tier3_config(self) -> dict:
        """Get Tier 3 (Ornith) configuration."""
        return self.get_workspace_config().get("tier3", {})

    # ── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _classify_artifact(name: str) -> str:
        """Classify an artifact by its filename."""
        if "test_run" in name:
            return "test_run"
        if "plan_checkoff" in name:
            return "plan_checkoff"
        if "task_request" in name:
            return "task_request"
        if "code_diff" in name:
            return "code_diff"
        if "implementation_plan" in name:
            return "implementation_plan"
        if "headless" in name or "recording" in name:
            return "headless_recording"
        return "unknown"


# ── Project Create Command (replaces manual directory creation) ─────────────

def create_project_via_antigravity(name: str, description: str = "",
                                     template: str = "blank",
                                     base_path: str = None) -> dict:
    """
    Primary entry point: Create a project via Antigravity workspace engine.

    This is the ONLY way new customer projects should be created.
    Do NOT create raw local directories manually.
    """
    workspace = AntigravityWorkspace(name, base_path)
    return workspace.create_project(description, template)
