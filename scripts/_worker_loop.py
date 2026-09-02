#!/usr/bin/env python3
"""Worker Loop — Subprocess entry point for isolated project workers.

This script is spawned by worker_manager.py for each project worker.
It reads JSON commands from stdin and writes JSON responses to stdout.

Protocol:
  stdin:  {"id": "...", "type": "cmd", "command": "run_python", "payload": {...}}
  stdout: {"id": "...", "type": "result", "status": "ok", "data": {...}}
  stdout: {"id": "...", "type": "heartbeat", "timestamp": "..."}

Commands:
  - shutdown: Gracefully exit
  - ping: Respond with pong
  - run_python: Execute Python code (payload: {"code": "..."})
  - execute_task: Execute a task via scrum_gate (payload: {"task": "...", "file_path": "..."})
  - run_command: Run a shell command (payload: {"cmd": "..."})
"""

import json
import os
import sys
import time
import traceback
from pathlib import Path


def send(msg: dict):
    """Send a JSON message to stdout."""
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def send_result(msg_id: str, data: dict):
    """Send a result message."""
    send({"id": msg_id, "type": "result", "data": data})


def send_error(msg_id: str, error: str):
    """Send an error message."""
    send({"id": msg_id, "type": "error", "data": error})


def send_heartbeat():
    """Send a heartbeat message."""
    send({"id": "hb", "type": "heartbeat", "timestamp": time.time()})


def handle_command(cmd_id: str, command: str, payload: dict):
    """Handle a command from the parent process."""
    try:
        if command == "shutdown":
            send_result(cmd_id, {"status": "shutting_down"})
            sys.exit(0)

        elif command == "ping":
            send_result(cmd_id, {"status": "pong", "pid": os.getpid()})

        elif command == "run_python":
            code = payload.get("code", "")
            ns = {"__builtins__": __builtins__}
            try:
                compiled = compile(code, "<worker>", "exec")
                exec(compiled, ns)
                # Collect non-dunder results
                result = {k: v for k, v in ns.items() if not k.startswith("_")}
                send_result(cmd_id, {"ok": True, "result": result})
            except Exception as e:
                send_error(cmd_id, f"Python execution error: {e}\n{traceback.format_exc()}")

        elif command == "execute_task":
            task = payload.get("task", "")
            file_path = payload.get("file_path", "")
            test_command = payload.get("test_command", "")
            project_space = payload.get("project_space", os.getcwd())

            # Add project space to path
            sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

            try:
                from scrum_gate import generate_code_via_ollama, generate_code_via_gemini
                # Try Gemini first, fall back to Ollama
                code, error = generate_code_via_gemini(task, file_path,
                                                        context=project_space)
                if error:
                    code, error = generate_code_via_ollama(
                        task, file_path, test_command=test_command,
                        context=project_space,
                    )
                if error:
                    send_error(cmd_id, f"Task execution error: {error}")
                else:
                    send_result(cmd_id, {"code": code, "task": task, "file_path": file_path})
            except Exception as e:
                send_error(cmd_id, f"Import error: {e}\n{traceback.format_exc()}")

        elif command == "run_command":
            cmd = payload.get("cmd", "")
            # Security: confine worker shell commands to the project space so a
            # task payload cannot touch config/, auth, or the wider filesystem.
            cwd = os.path.abspath(payload.get("project_space") or os.getcwd())
            import subprocess
            try:
                result = subprocess.run(
                    cmd, shell=True, capture_output=True, text=True, timeout=60,
                    cwd=cwd,
                )
                send_result(cmd_id, {
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "returncode": result.returncode,
                })
            except subprocess.TimeoutExpired:
                send_error(cmd_id, "Command timed out")
            except Exception as e:
                send_error(cmd_id, f"Command error: {e}")

        elif command == "list_files":
            project_path = Path(payload.get("project_path", os.getcwd()))
            try:
                files = []
                for f in project_path.rglob("*"):
                    if f.is_file() and not f.name.startswith("."):
                        files.append(str(f.relative_to(project_path)))
                send_result(cmd_id, {"files": files[:500]})
            except Exception as e:
                send_error(cmd_id, f"File listing error: {e}")

        else:
            send_error(cmd_id, f"Unknown command: {command}")

    except Exception as e:
        send_error(cmd_id, f"Unhandled error: {e}\n{traceback.format_exc()}")


def main():
    """Main loop: read commands from stdin, send responses to stdout."""
    # Send initial heartbeat
    send_heartbeat()

    # Set up project context
    if len(sys.argv) > 2:
        project_name = sys.argv[1]
        project_path = sys.argv[2]
        os.chdir(project_path)
        os.environ["MOIRAI_PROJECT_NAME"] = project_name
        os.environ["MOIRAI_PROJECT_PATH"] = project_path

    # Heartbeat timer
    last_heartbeat = time.time()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            msg = json.loads(line)
            msg_type = msg.get("type", "")
            msg_id = msg.get("id", "unknown")
            command = msg.get("command", "")
            payload = msg.get("payload", {})

            if msg_type == "cmd":
                handle_command(msg_id, command, payload)

        except json.JSONDecodeError:
            send({"id": "parse-error", "type": "error", "data": "Invalid JSON"})

        # Send heartbeat every 15 seconds
        if time.time() - last_heartbeat >= 15:
            send_heartbeat()
            last_heartbeat = time.time()


if __name__ == "__main__":
    main()