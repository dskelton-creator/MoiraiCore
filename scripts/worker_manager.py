"""MoiraiCore Worker Manager — Process Isolation for Projects (Feature 11, July 2026).

Each project gets its own subprocess worker with an isolated Python environment.
Workers communicate with the server via JSON-line protocol over stdin/stdout.

Key design:
- Per-project virtual environment (auto-created on first use)
- Worker subprocess lifecycle managed by the server
- JSON-line protocol for commands and responses
- Graceful degradation: falls back to in-process execution if worker fails
- Auto-cleanup of dead workers via periodic health checks

Usage:
    from worker_manager import WorkerManager
    wm = WorkerManager()
    wm.start_worker("my-project")
    wm.run_in_worker("my-project", "task", {"cmd": "generate", "file": "main.py"})
    wm.stop_worker("my-project")
"""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


# ── Paths ──
AGENT_OS_ROOT = Path(os.environ.get("AGENT_OS_ROOT", str(Path(__file__).resolve().parents[1])))


# ── Worker Protocol ──
# Workers communicate via JSON lines on stdout:
#   {"id": "msg-001", "type": "result", "status": "ok", "data": {...}}
#   {"id": "msg-002", "type": "heartbeat", "timestamp": "..."}
# Commands are sent to the worker via stdin:
#   {"id": "cmd-001", "type": "cmd", "command": "run", "payload": {...}}
#   {"id": "cmd-002", "type": "cmd", "command": "shutdown"}


class WorkerError(Exception):
    """Raised when a worker operation fails."""


class WorkerTimeout(Exception):
    """Raised when a worker command times out."""


class ProjectWorker:
    """A single project worker subprocess.

    Each worker runs in its own subprocess, in the project's directory,
    with its own virtual environment (if available).
    """

    def __init__(self, project_name: str, project_path: str | Path):
        self.project_name = project_name
        self.project_path = Path(project_path)
        self.process: Optional[subprocess.Popen] = None
        self.venv_path = self.project_path / ".venv"
        self._lock = threading.Lock()
        self._msg_id = 0
        self._pending: dict[str, threading.Event] = {}
        self._results: dict[str, Any] = {}
        self._reader_thread: Optional[threading.Thread] = None
        self._running = False
        self._started_at: Optional[str] = None
        self._last_heartbeat: Optional[float] = None

    # ── Lifecycle ──

    def ensure_venv(self) -> bool:
        """Create the project's virtual environment if it doesn't exist.

        Returns True if venv is available, False if it should fall back.
        """
        venv_python = self.venv_path / "bin" / "python3"
        if venv_python.exists():
            return True
        try:
            self.project_path.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [sys.executable, "-m", "venv", str(self.venv_path)],
                capture_output=True, text=True, timeout=120,
            )
            # Upgrade pip in the new venv
            subprocess.run(
                [str(venv_python), "-m", "pip", "install", "--upgrade", "pip"],
                capture_output=True, timeout=60,
            )
            return venv_python.exists()
        except Exception:
            return False

    def start(self, timeout: float = 30) -> bool:
        """Start the worker subprocess.

        Returns True if the worker started successfully.
        """
        with self._lock:
            if self._running:
                return True

            # Determine which Python to use
            venv_python = self.venv_path / "bin" / "python3"
            worker_script = AGENT_OS_ROOT / "scripts" / "_worker_loop.py"

            if not worker_script.exists():
                # Worker script not found — can't start isolated worker
                return False

            python = str(venv_python) if venv_python.exists() else sys.executable

            try:
                self.process = subprocess.Popen(
                    [python, str(worker_script), self.project_name, str(self.project_path)],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=str(self.project_path),
                    text=True,
                    bufsize=1,  # Line-buffered
                )
                self._running = True
                self._started_at = datetime.utcnow().isoformat()

                # Start reader thread for stdout
                self._reader_thread = threading.Thread(
                    target=self._read_loop,
                    daemon=True,
                )
                self._reader_thread.start()

                # Wait for initial heartbeat
                deadline = time.time() + timeout
                while time.time() < deadline:
                    if self._last_heartbeat is not None:
                        return True
                    time.sleep(0.1)

                # Worker didn't send heartbeat — stop and return False
                self.stop()
                return False

            except Exception:
                self._running = False
                return False

    def stop(self, timeout: float = 5) -> None:
        """Stop the worker subprocess gracefully."""
        with self._lock:
            if not self._running or self.process is None:
                return
            try:
                # Send shutdown command
                self._send_command("shutdown", {})
                self.process.wait(timeout=timeout)
            except Exception:
                # Force kill
                try:
                    self.process.kill()
                except Exception:
                    pass
            finally:
                self._running = False
                self.process = None
                self._last_heartbeat = None

    def is_alive(self) -> bool:
        """Check if the worker is still alive."""
        if not self._running or self.process is None:
            return False
        if self.process.poll() is not None:
            self._running = False
            return False
        # Check for recent heartbeat (within last 30 seconds)
        if self._last_heartbeat and time.time() - self._last_heartbeat < 30:
            return True
        return False

    # ── Command Execution ──

    def run_command(self, command: str, payload: dict = None,
                    timeout: float = 120) -> dict:
        """Send a command to the worker and wait for the response.

        Args:
            command: The command to execute (e.g., "run_python", "execute_task")
            payload: Command payload data
            timeout: Max seconds to wait for response

        Returns:
            The worker's response dict.

        Raises:
            WorkerError: If the worker is not running
            WorkerTimeout: If the command times out
        """
        if not self.is_alive():
            raise WorkerError(f"Worker for '{self.project_name}' is not running")

        msg_id = self._send_command(command, payload or {})

        # Wait for response
        event = threading.Event()
        self._pending[msg_id] = event

        try:
            if not event.wait(timeout=timeout):
                raise WorkerTimeout(
                    f"Command '{command}' timed out after {timeout}s"
                )
            result = self._results.pop(msg_id, {})
            return result
        finally:
            self._pending.pop(msg_id, None)

    # ── Internal ──

    def _send_command(self, command: str, payload: dict) -> str:
        """Send a JSON command to the worker's stdin.

        Returns the message ID.
        """
        self._msg_id += 1
        msg_id = f"msg-{self._msg_id:04d}"
        msg = {
            "id": msg_id,
            "type": "cmd",
            "command": command,
            "payload": payload,
        }
        if self.process and self.process.stdin:
            line = json.dumps(msg) + "\n"
            self.process.stdin.write(line)
            self.process.stdin.flush()
        return msg_id

    def _read_loop(self):
        """Read JSON lines from worker stdout in a background thread."""
        try:
            while self._running and self.process and self.process.stdout:
                line = self.process.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line.strip())
                    self._handle_message(msg)
                except json.JSONDecodeError:
                    continue
        except Exception:
            pass
        finally:
            self._running = False

    def _handle_message(self, msg: dict):
        """Handle an incoming message from the worker."""
        msg_type = msg.get("type", "")
        msg_id = msg.get("id", "")

        if msg_type == "heartbeat":
            self._last_heartbeat = time.time()

        elif msg_type == "result":
            # Signal the waiting command
            self._results[msg_id] = msg.get("data", {})
            event = self._pending.get(msg_id)
            if event:
                event.set()

        elif msg_type == "error":
            # Error response
            self._results[msg_id] = {"error": msg.get("data", "Unknown error")}
            event = self._pending.get(msg_id)
            if event:
                event.set()

        elif msg_type == "log":
            # Log message from worker (just print for now)
            print(f"  [worker:{self.project_name}] {msg.get('data', '')}")


class WorkerManager:
    """Manages worker subprocesses for all active projects.

    Provides a unified interface for starting/stopping workers and
    executing commands in them, with automatic fallback to in-process
    execution when workers are unavailable.
    """

    def __init__(self):
        self._workers: dict[str, ProjectWorker] = {}
        self._lock = threading.Lock()
        self._health_thread: Optional[threading.Thread] = None
        self._running = False

    # ── Lifecycle ──

    def start(self):
        """Start the worker manager health-check thread."""
        self._running = True
        self._health_thread = threading.Thread(target=self._health_loop, daemon=True)
        self._health_thread.start()

    def stop(self, timeout: float = 5):
        """Stop all workers and the health-check thread."""
        self._running = False
        with self._lock:
            for name, worker in list(self._workers.items()):
                try:
                    worker.stop(timeout=timeout)
                except Exception:
                    pass
            self._workers.clear()

    # ── Worker Management ──

    def start_worker(self, project_name: str,
                     project_path: str | Path = None) -> bool:
        """Start a worker for a project.

        Creates the project's venv if needed, then spawns the worker subprocess.

        Args:
            project_name: The project name
            project_path: Optional override for project path

        Returns:
            True if the worker is running, False if fallback to in-process needed.
        """
        if project_path is None:
            project_path = AGENT_OS_ROOT / "projects" / project_name

        with self._lock:
            # Check if already running
            existing = self._workers.get(project_name)
            if existing and existing.is_alive():
                return True

            # Create and start new worker
            worker = ProjectWorker(project_name, project_path)
            worker.ensure_venv()
            started = worker.start()
            if started:
                self._workers[project_name] = worker
            return started

    def stop_worker(self, project_name: str, timeout: float = 5):
        """Stop a project's worker."""
        with self._lock:
            worker = self._workers.pop(project_name, None)
            if worker:
                worker.stop(timeout=timeout)

    def get_worker(self, project_name: str) -> Optional[ProjectWorker]:
        """Get the worker for a project, or None if not running."""
        with self._lock:
            worker = self._workers.get(project_name)
            if worker and worker.is_alive():
                return worker
            return None

    def is_worker_running(self, project_name: str) -> bool:
        """Check if a project's worker is running."""
        worker = self.get_worker(project_name)
        return worker is not None

    # ── Command Execution ──

    def run_in_worker(self, project_name: str, command: str,
                      payload: dict = None, timeout: float = 120,
                      fallback_in_process: bool = True) -> dict:
        """Run a command in a project's worker.

        Attempts to use the worker subprocess. If the worker isn't running
        or fails, falls back to in-process execution.

        Args:
            project_name: The project name
            command: The command to execute
            payload: Command payload
            timeout: Max seconds to wait
            fallback_in_process: If True, fall back to in-process on failure

        Returns:
            Response dict with at minimum {"status": "ok"} or {"status": "error", "error": "..."}
        """
        worker = self.get_worker(project_name)
        if worker is None:
            if fallback_in_process:
                return self._run_in_process(project_name, command, payload)
            return {"status": "error", "error": "Worker not running"}

        try:
            result = worker.run_command(command, payload, timeout=timeout)
            return {"status": "ok", "data": result, "mode": "worker"}
        except (WorkerError, WorkerTimeout) as e:
            if fallback_in_process:
                return self._run_in_process(project_name, command, payload)
            return {"status": "error", "error": str(e)}
        except Exception as e:
            if fallback_in_process:
                return self._run_in_process(project_name, command, payload)
            return {"status": "error", "error": str(e)}

    # ── Status ──

    def list_workers(self) -> list[dict]:
        """List all running workers with status info."""
        with self._lock:
            result = []
            for name, worker in list(self._workers.items()):
                alive = worker.is_alive()
                result.append({
                    "project_name": name,
                    "alive": alive,
                    "started_at": worker._started_at,
                    "pid": worker.process.pid if worker.process and alive else None,
                    "has_venv": worker.venv_path.exists(),
                })
            return result

    def worker_count(self) -> int:
        """Get the number of alive workers."""
        return len(self.list_workers())

    # ── Internal ──

    def _run_in_process(self, project_name: str, command: str,
                        payload: dict = None) -> dict:
        """Fallback: run the command in-process (no isolation)."""
        project_path = AGENT_OS_ROOT / "projects" / project_name
        old_cwd = os.getcwd()
        try:
            os.chdir(str(project_path))

            if command == "execute_task":
                task = (payload or {}).get("task", "")
                file_path = (payload or {}).get("file_path", "")
                result = self._execute_task_in_process(project_name, task, file_path)
            elif command == "run_python":
                code = (payload or {}).get("code", "")
                result = self._run_python_in_process(code)
            else:
                result = {"status": "ok", "mode": "in_process", "command": command}

            return {"status": "ok", "data": result, "mode": "in_process"}
        except Exception as e:
            return {"status": "error", "error": str(e), "mode": "in_process"}
        finally:
            os.chdir(old_cwd)

    def _execute_task_in_process(self, project_name: str, task: str,
                                  file_path: str) -> dict:
        """Execute a task in-process (fallback path)."""
        try:
            sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
            from scrum_gate import generate_code_via_ollama, generate_code_via_gemini
            # Try Gemini first, fall back to Ollama
            code, error = generate_code_via_gemini(task, file_path)
            if error:
                code, error = generate_code_via_ollama(task, file_path)
            if error:
                return {"error": error}
            return {"code": code, "task": task, "file_path": file_path}
        except Exception as e:
            return {"error": str(e)}

    def _run_python_in_process(self, code: str) -> dict:
        """Run Python code in-process (fallback path)."""
        try:
            compiled = compile(code, "<worker>", "exec")
            ns = {}
            exec(compiled, ns)
            return {"ok": True}
        except Exception as e:
            return {"error": str(e)}

    def _health_loop(self):
        """Periodically check worker health and clean up dead ones."""
        while self._running:
            try:
                time.sleep(15)  # Check every 15 seconds
                with self._lock:
                    dead = []
                    for name, worker in self._workers.items():
                        if not worker.is_alive():
                            dead.append(name)
                    for name in dead:
                        self._workers.pop(name, None)
            except Exception:
                pass