"""
Project Orchestrator — Self-contained project execution engine (Phase 4).

Each project space has its own orchestration that:
  1. Treats MoiraiCore as a downstream API (via MoiraiCoreClient)
  2. Has its own pipeline definition (build, test, lint, deploy)
  3. Evaluates its own "done" criteria (independent of MoiraiCore's goal system)
  4. Can run standalone (no MoiraiCore daemon required for basic operations)

Usage:
    python3 project_orchestrator.py run --project my-project --pipeline build-and-test
    python3 project_orchestrator.py status --project my-project
    python3 project_orchestrator.py evaluate --project my-project
    python3 project_orchestrator.py init --name my-new-project --template web-app
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional


# ── Paths ──

AGENT_OS_ROOT = Path(os.environ.get("AGENT_OS_ROOT", str(Path(__file__).resolve().parents[1])))
SCRIPTS_DIR = AGENT_OS_ROOT / "scripts"
PROJECTS_DIR = AGENT_OS_ROOT / "projects"
RUNS_DIR = AGENT_OS_ROOT / "config" / "project-runs"


# ── Pipeline Definitions ──

PIPELINES = {
    "build": {
        "name": "Build Project",
        "description": "Compile and build the project",
        "steps": [
            {"name": "install-deps", "command": "pip install -r requirements.txt 2>&1 || echo 'no requirements.txt'", "optional": True},
            {"name": "build", "command": "python3 -m py_compile src/*.py 2>&1 || python3 -c 'import py_compile; [py_compile.compile(f) for f in src/*.py]' 2>&1", "optional": False},
        ],
    },
    "test": {
        "name": "Run Tests",
        "description": "Execute the project test suite",
        "steps": [
            {"name": "unit-tests", "command": "python3 -m pytest tests/ -v --tb=short 2>&1", "optional": False},
        ],
    },
    "lint": {
        "name": "Lint & Type Check",
        "description": "Static analysis and code quality checks",
        "steps": [
            {"name": "syntax-check", "command": "python3 -m py_compile src/*.py 2>&1", "optional": False},
            {"name": "import-check", "command": "python3 -c \"import sys; sys.path.insert(0, 'src'); import importlib; [importlib.import_module(f.stem) for f in Path('src').glob('*.py') if not f.name.startswith('_')]\" 2>&1", "optional": True},
        ],
    },
    "build-and-test": {
        "name": "Build & Test",
        "description": "Full build and test pipeline",
        "steps": [
            {"name": "install-deps", "command": "pip install -r requirements.txt 2>&1 || echo 'no requirements.txt'", "optional": True},
            {"name": "build", "command": "python3 -m py_compile src/*.py 2>&1", "optional": False},
            {"name": "unit-tests", "command": "python3 -m pytest tests/ -v --tb=short 2>&1", "optional": False},
        ],
    },
    "full-ci": {
        "name": "Full CI Pipeline",
        "description": "Lint, build, test, and coverage",
        "steps": [
            {"name": "syntax-check", "command": "python3 -m py_compile src/*.py 2>&1", "optional": False},
            {"name": "unit-tests", "command": "python3 -m pytest tests/ -v --tb=short 2>&1", "optional": False},
            {"name": "integration-check", "command": "python3 -c \"import sys; sys.path.insert(0, 'src'); import importlib; [importlib.import_module(f.stem) for f in Path('src').glob('*.py') if not f.name.startswith('_')]\" 2>&1", "optional": True},
        ],
    },
}


# ── Project Orchestrator ──

class ProjectOrchestrator:
    """
    Self-contained orchestrator for a single project.

    Treats MoiraiCore as a downstream API and manages project-local pipelines
    with independent "done" evaluation.
    """

    def __init__(self, project_name: str):
        self.project_name = project_name
        self.project_path = PROJECTS_DIR / project_name

        if not self.project_path.exists():
            raise FileNotFoundError(f"Project '{project_name}' not found at {self.project_path}")

        self.manifest = self._load_manifest()
        self.state = self._load_state()
        self.runs_dir = RUNS_DIR / project_name
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    def _load_manifest(self) -> dict:
        manifest_path = self.project_path / "project.json"
        if manifest_path.exists():
            return json.loads(manifest_path.read_text())
        return {}

    def _load_state(self) -> dict:
        state_path = self.project_path / ".os_state.json"
        if state_path.exists():
            return json.loads(state_path.read_text())
        return {"pipelines": {}, "evaluations": []}

    def _save_state(self):
        state_path = self.project_path / ".os_state.json"
        state_path.write_text(json.dumps(self.state, indent=2))

    # ── Pipeline Execution ──────────────────────────────────────────────────

    def run_pipeline(self, pipeline_name: str, run_id: str | None = None) -> dict:
        """
        Execute a pipeline for this project.

        Args:
            pipeline_name: Key from PIPELINES dict (build, test, lint, build-and-test, full-ci)
            run_id: Optional run ID (auto-generated if not provided)

        Returns:
            Run record dict with status, steps, and duration
        """
        if pipeline_name not in PIPELINES:
            return {
                "ok": False,
                "error": f"Unknown pipeline: {pipeline_name}. Available: {list(PIPELINES.keys())}",
            }

        pipeline = PIPELINES[pipeline_name]
        if not run_id:
            run_id = f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"

        started_at = datetime.now().isoformat()
        project_space = str(self.project_path)

        print(f"\n🔧 Pipeline: {pipeline['name']} — {self.project_name}")
        print(f"   Steps: {len(pipeline['steps'])}")
        print(f"   Run ID: {run_id}")
        print(f"   Project: {project_space}")
        print()

        step_results = []
        overall_start = time.time()

        for i, step in enumerate(pipeline["steps"], 1):
            step_name = step["name"]
            step_cmd = step["command"]
            optional = step.get("optional", False)

            print(f"   [{i}/{len(pipeline['steps'])}] {step_name}...", end=" ", flush=True)

            start = time.time()
            try:
                result = subprocess.run(
                    step_cmd,
                    shell=True,
                    cwd=project_space,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    env={**os.environ, "PYTHONPATH": project_space},
                )
                duration_ms = int((time.time() - start) * 1000)
                passed = result.returncode == 0
                output = result.stdout.strip()
                error = result.stderr.strip() if not passed else ""

                if passed:
                    print(f"✓ ({duration_ms}ms)")
                elif optional:
                    print(f"⚠ skip ({duration_ms}ms, optional)")
                else:
                    print(f"✗ FAIL ({duration_ms}ms)")
                    if error:
                        print(f"      Error: {error[:100]}")

                step_results.append({
                    "name": step_name,
                    "status": "passed" if passed else "skipped" if optional else "failed",
                    "optional": optional,
                    "duration_ms": duration_ms,
                    "output_preview": output[:200] if output else "",
                    "error_preview": error[:200] if error else "",
                })

                # If a non-optional step fails, stop the pipeline
                if not passed and not optional:
                    break

            except subprocess.TimeoutExpired:
                duration_ms = int((time.time() - start) * 1000)
                print(f"⏰ timeout ({duration_ms}ms)")
                step_results.append({
                    "name": step_name,
                    "status": "timeout",
                    "optional": optional,
                    "duration_ms": duration_ms,
                    "output_preview": "",
                    "error_preview": "Timed out after 120s",
                })
                if not optional:
                    break

            except Exception as e:
                duration_ms = int((time.time() - start) * 1000)
                print(f"✗ error: {e}")
                step_results.append({
                    "name": step_name,
                    "status": "error",
                    "optional": optional,
                    "duration_ms": duration_ms,
                    "output_preview": "",
                    "error_preview": str(e)[:200],
                })
                if not optional:
                    break

        overall_duration_ms = int((time.time() - overall_start) * 1000)

        # Determine overall status
        failed_steps = [s for s in step_results if s["status"] == "failed"]
        if failed_steps:
            status = "failed"
        elif all(s["status"] == "passed" for s in step_results):
            status = "passed"
        else:
            status = "partial"  # Some optional steps skipped/failed

        # Build run record
        run_record = {
            "run_id": run_id,
            "project": self.project_name,
            "pipeline": pipeline_name,
            "status": status,
            "steps_total": len(step_results),
            "steps_passed": sum(1 for s in step_results if s["status"] == "passed"),
            "steps_failed": len(failed_steps),
            "duration_ms": overall_duration_ms,
            "started_at": started_at,
            "finished_at": datetime.now().isoformat(),
            "steps": step_results,
        }

        # Save run record
        run_file = self.runs_dir / f"{run_id}.json"
        run_file.write_text(json.dumps(run_record, indent=2))

        # Update project state
        self.state.setdefault("pipelines", {})[pipeline_name] = {
            "last_run": run_id,
            "last_status": status,
            "last_run_at": run_record["finished_at"],
        }
        self._save_state()

        # Print summary
        icon = "✅" if status == "passed" else "⚠️" if status == "partial" else "❌"
        print(f"\n{icon} Pipeline '{pipeline_name}': {status.upper()}")
        print(f"   {run_record['steps_passed']}/{run_record['steps_total']} steps passed in {overall_duration_ms}ms")
        print(f"   Run saved: {run_file}")

        return run_record

    # ── Done Evaluation ─────────────────────────────────────────────────────

    def evaluate(self, criteria: dict | None = None) -> dict:
        """
        Evaluate whether the project is "done" based on its own criteria.

        This is INDEPENDENT of MoiraiCore's global goal system — the project
        defines what "complete" means for itself.

        Args:
            criteria: Optional dict of evaluation criteria. If not provided,
                      uses the project's manifest description as a heuristic.

        Returns:
            Evaluation record dict
        """
        if criteria is None:
            criteria = self._build_criteria_from_state()

        print(f"\n📊 Evaluating project: {self.project_name}")
        print(f"   Criteria: {len(criteria)} checks")

        results = []
        for check_name, check in criteria.items():
            check_type = check.get("type", "exists")
            expected = check.get("expected", True)
            description = check.get("description", check_name)

            if check_type == "files_exist":
                # Check that required files exist
                files = check.get("files", [])
                missing = [f for f in files if not (self.project_path / f).exists()]
                passed = len(missing) == 0
                detail = f"Missing: {missing}" if missing else "All files present"

            elif check_type == "has_content":
                # Check that a file has non-trivial content
                file_path = check.get("file", "")
                min_lines = check.get("min_lines", 5)
                target = self.project_path / file_path
                if target.exists():
                    lines = len(target.read_text().splitlines())
                    passed = lines >= min_lines
                    detail = f"{lines} lines (min {min_lines})"
                else:
                    passed = False
                    detail = "File not found"

            elif check_type == "tests_pass":
                # Run tests and check they pass
                test_cmd = check.get("command", "python3 -m pytest tests/ -q")
                try:
                    result = subprocess.run(
                        test_cmd, shell=True, cwd=str(self.project_path),
                        capture_output=True, text=True, timeout=120,
                    )
                    passed = result.returncode == 0
                    detail = f"Exit code: {result.returncode}"
                except Exception as e:
                    passed = False
                    detail = str(e)

            elif check_type == "pipeline_passed":
                # Check that a specific pipeline last passed
                pipeline = check.get("pipeline", "build")
                last_status = self.state.get("pipelines", {}).get(pipeline, {}).get("last_status", "never")
                passed = last_status == "passed"
                detail = f"Last run: {last_status}"

            elif check_type == "has_goals":
                # Check that project has goals defined
                goals = self.state.get("goals", []) if hasattr(self, 'state') else []
                min_goals = check.get("min", 1)
                passed = len(goals) >= min_goals
                detail = f"{len(goals)} goals defined"

            elif check_type == "custom":
                # Run a custom command
                cmd = check.get("command", "echo no-check")
                try:
                    result = subprocess.run(
                        cmd, shell=True, cwd=str(self.project_path),
                        capture_output=True, text=True, timeout=60,
                    )
                    passed = result.returncode == 0
                    detail = result.stdout.strip()[:100]
                except Exception as e:
                    passed = False
                    detail = str(e)

            else:
                passed = False
                detail = f"Unknown check type: {check_type}"

            icon = "✓" if passed else "✗"
            print(f"   {icon} {description}: {detail}")
            results.append({
                "name": check_name,
                "passed": passed,
                "detail": detail,
            })

        # Overall evaluation
        passed_count = sum(1 for r in results if r["passed"])
        total = len(results)
        overall = "done" if passed_count == total else "not_done"

        evaluation = {
            "project": self.project_name,
            "status": overall,
            "passed": passed_count,
            "total": total,
            "checks": results,
            "evaluated_at": datetime.now().isoformat(),
        }

        # Save evaluation to project state
        self.state.setdefault("evaluations", []).append(evaluation)
        self._save_state()

        icon = "✅" if overall == "done" else "❌"
        print(f"\n{icon} Evaluation: {overall.upper()} ({passed_count}/{total} checks passed)")

        return evaluation

    def _build_criteria_from_state(self) -> dict:
        """Build evaluation criteria from the project's current state."""
        criteria = {
            "has_source": {
                "type": "files_exist",
                "files": ["src/"],
                "description": "Has source directory",
            },
            "has_tests": {
                "type": "files_exist",
                "files": ["tests/"],
                "description": "Has test directory",
            },
        }

        # Add pipeline criteria if pipelines have been run
        pipelines = self.state.get("pipelines", {})
        if pipelines:
            for pipeline_name in pipelines:
                criteria[f"pipeline_{pipeline_name}"] = {
                    "type": "pipeline_passed",
                    "pipeline": pipeline_name,
                    "description": f"Pipeline '{pipeline_name}' passes",
                }

        return criteria

    # ── Status & History ────────────────────────────────────────────────────

    def get_status(self) -> dict:
        """Get the current status of this project."""
        pipelines = self.state.get("pipelines", {})
        evaluations = self.state.get("evaluations", [])

        return {
            "project": self.project_name,
            "path": str(self.project_path),
            "manifest": {
                "name": self.manifest.get("name", self.project_name),
                "description": self.manifest.get("description", ""),
                "category": self.manifest.get("category", "other"),
                "status": self.manifest.get("status", "unknown"),
            },
            "pipelines": pipelines,
            "last_evaluation": evaluations[-1] if evaluations else None,
            "runs_available": len(list(self.runs_dir.glob("*.json"))),
        }

    def list_runs(self, pipeline: str = None, limit: int = 10) -> list[dict]:
        """List recent pipeline runs."""
        runs = []
        for f in sorted(self.runs_dir.glob("*.json"), reverse=True):
            if len(runs) >= limit:
                break
            try:
                data = json.loads(f.read_text())
                if pipeline and data.get("pipeline") != pipeline:
                    continue
                runs.append({
                    "run_id": data.get("run_id", ""),
                    "pipeline": data.get("pipeline", ""),
                    "status": data.get("status", ""),
                    "duration_ms": data.get("duration_ms", 0),
                    "started_at": data.get("started_at", ""),
                })
            except Exception:
                pass
        return runs


# ── Project Init ────────────────────────────────────────────────────────────

def init_project(name: str, template: str = "blank", description: str | None = None,
                 category: str | None = None) -> dict:
    """
    Initialize a new project with orchestration scaffolding.

    Args:
        name: Project name
        template: Template key (blank, web-app, api-service, cli-tool)
        description: Project description
        category: Project category

    Returns:
        Project manifest dict
    """
    # Ensure projects dir exists
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

    project_path = PROJECTS_DIR / name
    if project_path.exists():
        raise FileExistsError(f"Project '{name}' already exists")

    # Generate API key
    import uuid as _uuid
    api_key = f"proj_{_uuid.uuid4().hex[:24]}"

    # Create directory structure
    project_path.mkdir(parents=True)
    (project_path / "src").mkdir()
    (project_path / "tests").mkdir()
    (project_path / "docs").mkdir()
    (project_path / "orchestration").mkdir()

    # Copy agent_client.py
    template_client = PROJECTS_DIR / ".templates" / "agent_client.py"
    if template_client.exists():
        import shutil
        shutil.copy2(template_client, project_path / "orchestration" / "agent_client.py")

    # Create sandbox.json from template
    sandbox_template = PROJECTS_DIR / ".templates" / "sandbox.json"
    if sandbox_template.exists():
        import shutil
        shutil.copy2(sandbox_template, project_path / "sandbox.json")

    # Create manifest
    manifest = {
        "version": "1.0",
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
            "pipelines": {},
            "evaluations": [],
        },
    }

    (project_path / "project.json").write_text(json.dumps(manifest, indent=2))

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
        "pipelines": {},
        "evaluations": [],
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
    }
    (project_path / ".os_state.json").write_text(json.dumps(os_state, indent=2))

    # Template-specific scaffolding
    if template == "web-app":
        (project_path / "src" / "app.py").write_text(f'''"""{(description or name)} — Main Application."""

from fastapi import FastAPI

app = FastAPI(title="{name}")


@app.get("/")
def root():
    return {{"message": "Welcome to {name}"}}


@app.get("/health")
def health():
    return {{"status": "ok"}}
''')
        (project_path / "tests" / "test_app.py").write_text('''"""Tests for the application."""

import pytest


def test_placeholder():
    """Placeholder test — replace with real tests."""
    assert True
''')
        (project_path / "requirements.txt").write_text("fastapi>=0.100.0\nuvicorn>=0.23.0\npytest>=7.0.0\n")

    elif template == "api-service":
        (project_path / "src" / "api.py").write_text(f'''"""{(description or name)} — API Service."""

from fastapi import FastAPI

app = FastAPI(title="{name}")


@app.get("/api/v1/status")
def status():
    return {{"service": "{name}", "status": "running"}}


@app.get("/api/v1/health")
def health():
    return {{"healthy": True}}
''')
        (project_path / "tests" / "test_api.py").write_text('''"""Tests for the API service."""

import pytest


def test_placeholder():
    """Placeholder test — replace with real tests."""
    assert True
''')
        (project_path / "requirements.txt").write_text("fastapi>=0.100.0\nuvicorn>=0.23.0\npytest>=7.0.0\nhttpx>=0.24.0\n")

    elif template == "cli-tool":
        (project_path / "src" / "cli.py").write_text(f'''"""{(description or name)} — CLI Tool."""

import argparse
import sys


def main():
    parser = argparse.ArgumentParser(description="{description or name}")
    parser.add_argument("--version", action="version", version="{name} 1.0.0")
    parser.add_argument("input", nargs="?", help="Input file")
    args = parser.parse_args()

    print(f"{{name}}: processing {{args.input or 'stdin'}}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
''')
        (project_path / "tests" / "test_cli.py").write_text('''"""Tests for the CLI tool."""

import subprocess


def test_help():
    """Test that help works."""
    result = subprocess.run(["python3", "src/cli.py", "--help"], capture_output=True, text=True)
    assert result.returncode == 0
''')
        (project_path / "requirements.txt").write_text("pytest>=7.0.0\n")

    else:  # blank
        (project_path / "src" / "main.py").write_text(f'"""{(description or name)} — Main Module."""\n\n\ndef main():\n    print("Hello from {name}!")\n\n\nif __name__ == "__main__":\n    main()\n')
        (project_path / "tests" / "test_main.py").write_text('''"""Tests for the main module."""

import pytest


def test_placeholder():
    """Placeholder test — replace with real tests."""
    assert True
''')

    # Create README
    readme = f"""# {name}

{description}

## Category: {category}

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run
python3 src/main.py

# Test
python3 -m pytest tests/ -v
```

## Orchestration

```bash
# Build
python3 scripts/project_orchestrator.py run --project {name} --pipeline build

# Test
python3 scripts/project_orchestrator.py run --project {name} --pipeline test

# Full CI
python3 scripts/project_orchestrator.py run --project {name} --pipeline full-ci

# Evaluate
python3 scripts/project_orchestrator.py evaluate --project {name}
```

## API Key: `{api_key}`

## Status: draft
"""
    (project_path / "README.md").write_text(readme)

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


# ── CLI ──────────────────────────────────────────────────────────────────────

def cmd_run(args):
    """Run a pipeline for a project."""
    orch = ProjectOrchestrator(args.project)
    result = orch.run_pipeline(args.pipeline, run_id=args.run_id)
    print(json.dumps(result, indent=2))
    return 0 if result.get("status") == "passed" else 1


def cmd_status(args):
    """Show project status."""
    orch = ProjectOrchestrator(args.project)
    status = orch.get_status()
    print(json.dumps(status, indent=2))
    return 0


def cmd_evaluate(args):
    """Evaluate project done-ness."""
    orch = ProjectOrchestrator(args.project)
    result = orch.evaluate()
    print(json.dumps(result, indent=2))
    return 0 if result.get("status") == "done" else 1


def cmd_runs(args):
    """List recent pipeline runs."""
    orch = ProjectOrchestrator(args.project)
    runs = orch.list_runs(pipeline=args.pipeline, limit=args.limit)
    print(json.dumps({"ok": True, "runs": runs, "count": len(runs)}, indent=2))
    return 0


def cmd_init(args):
    """Initialize a new project with orchestration scaffolding."""
    manifest = init_project(
        name=args.name,
        template=args.template,
        description=args.description or "",
        category=args.category or "other",
    )
    print(json.dumps({"ok": True, "project": manifest}, indent=2))
    return 0


def cmd_pipelines(args):
    """List available pipelines."""
    print(json.dumps({"ok": True, "pipelines": PIPELINES}, indent=2, default=str))
    return 0


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Project Orchestrator — Self-contained project execution (Phase 4)"
    )
    sub = parser.add_subparsers(dest="command")

    # run
    p_run = sub.add_parser("run", help="Run a pipeline for a project")
    p_run.add_argument("--project", required=True, help="Project name")
    p_run.add_argument("--pipeline", default="build-and-test", help="Pipeline name (default: build-and-test)")
    p_run.add_argument("--run-id", default=None, help="Run ID (auto-generated if omitted)")
    p_run.set_defaults(func=cmd_run)

    # status
    p_status = sub.add_parser("status", help="Show project status")
    p_status.add_argument("--project", required=True, help="Project name")
    p_status.set_defaults(func=cmd_status)

    # evaluate
    p_eval = sub.add_parser("evaluate", help="Evaluate project done-ness")
    p_eval.add_argument("--project", required=True, help="Project name")
    p_eval.set_defaults(func=cmd_evaluate)

    # runs
    p_runs = sub.add_parser("runs", help="List recent pipeline runs")
    p_runs.add_argument("--project", required=True, help="Project name")
    p_runs.add_argument("--pipeline", default=None, help="Filter by pipeline name")
    p_runs.add_argument("--limit", type=int, default=10, help="Max runs to show")
    p_runs.set_defaults(func=cmd_runs)

    # init
    p_init = sub.add_parser("init", help="Initialize a new project with orchestration scaffolding")
    p_init.add_argument("--name", required=True, help="Project name")
    p_init.add_argument("--template", default="blank", help="Template: blank, web-app, api-service, cli-tool")
    p_init.add_argument("--description", default="", help="Project description")
    p_init.add_argument("--category", default="other", help="Project category")
    p_init.set_defaults(func=cmd_init)

    # pipelines
    p_pipes = sub.add_parser("pipelines", help="List available pipelines")
    p_pipes.set_defaults(func=cmd_pipelines)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
