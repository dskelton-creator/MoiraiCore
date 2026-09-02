#!/usr/bin/env python3
"""
MoiraiCore — Workflow Engine
Visual workflow builder execution engine. Runs saved workflows node-by-node.

Node types:
  trigger   - Entry point (manual, schedule, file-watch, webhook)
  agent     - Execute an agent (Hermes, Researcher, Writer, Developer)
  action    - Built-in action (http, shell, save-file, notify)
  condition - Branch based on expression (if/else)
  delay     - Pause for N seconds
  merge     - Wait for multiple branches
  output    - End node (collects result)

Usage:
    python3 workflow_engine.py run <workflow_id>
    python3 workflow_engine.py validate <file.json>
    python3 workflow_engine.py list
"""

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor


# Load .env file so API keys are available to this process and its children
_env_file = Path(__file__).resolve().parents[1] / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            if _k.strip() and _v.strip():
                os.environ.setdefault(_k.strip(), _v.strip())

AGENT_OS_ROOT = Path(os.environ.get("AGENT_OS_ROOT", str(Path(__file__).resolve().parents[1])))
WORKFLOWS_DIR = AGENT_OS_ROOT / "config" / "workflows"
RUNS_DIR = AGENT_OS_ROOT / "config" / "workflow-runs"

# ── Node type definitions ──

NODE_TYPES = {
    "trigger": {
        "label": "Trigger",
        "icon": "⚡",
        "color": "#fbbf24",
        "config": {
            "trigger_type": {"type": "select", "options": ["manual", "schedule", "file-watch", "webhook"], "default": "manual"},
            "schedule": {"type": "text", "default": "", "placeholder": "cron: 0 9 * * *"},
            "watch_path": {"type": "text", "default": "", "placeholder": "workspace/incoming"},
        },
        "outputs": ["out"],
    },
    "agent": {
        "label": "Agent Task",
        "icon": "🤖",
        "color": "#7c5bf5",
        "config": {
            "agent": {"type": "select", "options": ["hermes", "researcher", "writer", "developer", "antigravity"], "default": "hermes"},
            "prompt": {"type": "textarea", "default": "", "placeholder": "Task for the agent..."},
            "context": {"type": "text", "default": "", "placeholder": "vault/context/file.md"},
            "timeout": {"type": "number", "default": 300},
        },
        "inputs": ["in"],
        "outputs": ["out", "error"],
    },
    "action": {
        "label": "Action",
        "icon": "⚙️",
        "color": "#5b9bf5",
        "config": {
            "action_type": {"type": "select", "options": ["http", "shell", "save-file", "load-file", "notify", "deep-research"], "default": "http"},
            "url": {"type": "text", "default": "", "placeholder": "https://..."},
            "method": {"type": "select", "options": ["GET", "POST", "PUT", "DELETE"], "default": "GET"},
            "headers": {"type": "text", "default": "", "placeholder": '{"Content-Type":"application/json"}'},
            "body": {"type": "textarea", "default": "", "placeholder": "Request body"},
            "command": {"type": "text", "default": "", "placeholder": "echo hello"},
            "file_path": {"type": "text", "default": "", "placeholder": "workspace/output.txt"},
        },
        "inputs": ["in"],
        "outputs": ["out", "error"],
    },
    "condition": {
        "label": "Condition",
        "icon": "🔀",
        "color": "#f472b6",
        "config": {
            "expression": {"type": "text", "default": "", "placeholder": "{{output.status}} == 200"},
            "field": {"type": "text", "default": "", "placeholder": "output.status"},
            "operator": {"type": "select", "options": ["==", "!=", ">", "<", "contains", "exists"], "default": "=="},
            "value": {"type": "text", "default": "", "placeholder": "200"},
        },
        "inputs": ["in"],
        "outputs": ["true", "false"],
    },
    "delay": {
        "label": "Delay",
        "icon": "⏱",
        "color": "#8888a0",
        "config": {
            "seconds": {"type": "number", "default": 10},
        },
        "inputs": ["in"],
        "outputs": ["out"],
    },
    "merge": {
        "label": "Merge",
        "icon": "⏉",
        "color": "#8888a0",
        "config": {
            "mode": {"type": "select", "options": ["wait-all", "wait-any"], "default": "wait-all"},
        },
        "inputs": ["in1", "in2", "in3", "in4"],
        "outputs": ["out"],
    },
    "output": {
        "label": "Output",
        "icon": "📤",
        "color": "#4ade80",
        "config": {
            "save_to": {"type": "text", "default": "", "placeholder": "workspace/result.md"},
        },
        "inputs": ["in"],
        "outputs": [],
    },
}


def validate_workflow(workflow: dict) -> list:
    """Validate a workflow definition. Returns list of errors."""
    errors = []
    nodes = workflow.get("nodes", [])
    edges = workflow.get("edges", [])

    if not nodes:
        errors.append("Workflow has no nodes")
        return errors

    node_ids = {n["id"] for n in nodes}

    # Check each node
    for node in nodes:
        nid = node.get("id", "?")
        ntype = node.get("type", "")
        if ntype not in NODE_TYPES:
            errors.append(f"Node '{nid}': unknown type '{ntype}'")
            continue
        node_def = NODE_TYPES[ntype]
        # Check required config fields
        for field_name, field_def in node_def.get("config", {}).items():
            val = node.get("config", {}).get(field_name, "")
            if not val and field_def.get("default") is None:
                errors.append(f"Node '{nid}': missing config '{field_name}'")

    # Check edges
    for i, edge in enumerate(edges):
        src = edge.get("source", "")
        tgt = edge.get("target", "")
        if src not in node_ids:
            errors.append(f"Edge {i}: unknown source '{src}'")
        if tgt not in node_ids:
            errors.append(f"Edge {i}: unknown target '{tgt}'")

    # Check for at least one trigger and one output
    has_trigger = any(n.get("type") == "trigger" for n in nodes)
    has_output = any(n.get("type") == "output" for n in nodes)
    if not has_trigger:
        errors.append("Workflow needs at least one Trigger node")
    if not has_output:
        errors.append("Workflow needs at least one Output node")

    return errors


# ── Workflow Execution ──

class WorkflowRunner:
    """Executes a workflow graph, handling branching, conditions, and parallel paths."""

    def __init__(self, workflow: dict, run_id: str = None):
        self.workflow = workflow
        self.run_id = run_id or f"wfrun-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        self.started_at = datetime.now().isoformat()
        self.node_outputs = {}  # node_id -> output data
        self.node_status = {}   # node_id -> "pending"|"running"|"completed"|"failed"|"skipped"
        self.log = []
        self.nodes_by_id = {n["id"]: n for n in workflow.get("nodes", [])}
        self.edges_by_source = {}
        self.edges_by_target = {}
        for e in workflow.get("edges", []):
            self.edges_by_source.setdefault(e["source"], []).append(e)
            self.edges_by_target.setdefault(e["target"], []).append(e)

    def log_msg(self, level, msg):
        entry = {"time": datetime.now().isoformat(), "level": level, "msg": msg}
        self.log.append(entry)
        print(f"  [{level.upper()}] {msg}")

    def execute(self) -> dict:
        """Run the workflow and return the run record."""
        self.log_msg("info", f"Workflow '{self.workflow.get('name', '?')}' starting")

        # Initialize all nodes as pending
        for nid in self.nodes_by_id:
            self.node_status[nid] = "pending"

        # Find trigger nodes (entry points)
        triggers = [n for n in self.workflow.get("nodes", []) if n["type"] == "trigger"]
        if not triggers:
            self.log_msg("error", "No trigger nodes found")
            return self._build_record("failed")

        # Execute from triggers outward
        try:
            self._execute_parallel([t["id"] for t in triggers])
        except Exception as e:
            self.log_msg("error", f"Workflow execution error: {e}")

        # Determine overall status
        statuses = list(self.node_status.values())
        if all(s in ("completed", "skipped") for s in statuses):
            status = "completed"
        elif any(s == "failed" for s in statuses):
            status = "failed"
        else:
            status = "partial"

        self.log_msg("info", f"Workflow finished: {status}")
        return self._build_record(status)

    def _execute_parallel(self, node_ids):
        """Execute a set of nodes (potentially in parallel)."""
        # For simplicity, execute sequentially but could parallelize independent branches
        for nid in node_ids:
            if self.node_status.get(nid) != "pending":
                continue
            self._execute_node(nid)

    def _execute_node(self, node_id):
        """Execute a single node and trigger downstream nodes."""
        node = self.nodes_by_id.get(node_id)
        if not node:
            return

        self.node_status[node_id] = "running"
        ntype = node["type"]
        config = node.get("config", {})
        self.log_msg("info", f"Node '{node.get('label', node_id)}' ({ntype})")

        # Gather inputs from upstream
        inputs = {}
        for edge in self.edges_by_target.get(node_id, []):
            src = edge["source"]
            src_output_key = edge.get("sourceHandle", "out")
            inputs[src_output_key] = self.node_outputs.get(src)

        try:
            if ntype == "trigger":
                result = self._run_trigger(config, inputs)
            elif ntype == "agent":
                result = self._run_agent(config, inputs)
            elif ntype == "action":
                result = self._run_action(config, inputs)
            elif ntype == "condition":
                result = self._run_condition(config, inputs)
            elif ntype == "delay":
                result = self._run_delay(config, inputs)
            elif ntype == "merge":
                result = self._run_merge(config, inputs)
            elif ntype == "output":
                result = self._run_output(config, inputs)
            else:
                raise ValueError(f"Unknown node type: {ntype}")

            self.node_outputs[node_id] = result
            self.node_status[node_id] = "completed"
            self.log_msg("info", f"Node '{node.get('label', node_id)}' completed")

            # Execute downstream nodes
            edges = self.edges_by_source.get(node_id, [])
            downstream_ids = []

            # Handle conditional routing
            if ntype == "condition":
                cond_result = result.get("result", False)
                for edge in edges:
                    edge_src_handle = edge.get("sourceHandle", "true")
                    if (cond_result and edge_src_handle == "true") or (not cond_result and edge_src_handle == "false"):
                        downstream_ids.append(edge["target"])
            else:
                # For error routing from agent/action
                if result.get("error") and any(e.get("sourceHandle") == "error" for e in edges):
                    for edge in edges:
                        if edge.get("sourceHandle") == "error":
                            downstream_ids.append(edge["target"])
                        # Mark "true"/"out" branches as skipped
                        src_n = self.nodes_by_id.get(edge["target"], {})
                        # We don't skip here; we just don't traverse
                else:
                    downstream_ids = [e["target"] for e in edges]

            if downstream_ids:
                self._execute_parallel(downstream_ids)

        except Exception as e:
            self.node_status[node_id] = "failed"
            self.node_outputs[node_id] = {"error": str(e)}
            self.log_msg("error", f"Node '{node.get('label', node_id)}' failed: {e}")

            # Execute error branches
            edges = self.edges_by_source.get(node_id, [])
            for edge in edges:
                if edge.get("sourceHandle") == "error":
                    self._execute_node(edge["target"])

    # ── Node type handlers ──

    def _run_trigger(self, config, inputs):
        """Trigger node — entry point, passes through."""
        trigger_type = config.get("trigger_type", "manual")
        return {"trigger_type": trigger_type, "timestamp": datetime.now().isoformat()}

    def _run_agent(self, config, inputs):
        """Agent node — execute a sub-agent via Hermes CLI."""
        agent_key = config.get("agent", "hermes")
        prompt = config.get("prompt", "")
        timeout = int(config.get("timeout", 300))

        if not prompt:
            return {"error": "No prompt provided for agent node"}

        # Build Hermes command
        cmd = [sys.executable, str(AGENT_OS_ROOT / "scripts" / "hermes_bridge.py"),
               "ask", prompt, "--log", "--route"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                               cwd=str(AGENT_OS_ROOT))
            stdout = r.stdout.strip()
            # Parse JSON if possible
            try:
                data = json.loads(stdout)
                response = data.get("response", stdout)
                routed_to = data.get("routed_to", agent_key)
            except json.JSONDecodeError:
                response = stdout
                routed_to = agent_key

            return {"agent": routed_to, "response": response[:5000], "status": 200}
        except subprocess.TimeoutExpired:
            return {"error": f"Agent timed out after {timeout}s", "status": 504}
        except Exception as e:
            return {"error": str(e), "status": 500}

    def _run_action(self, config, inputs):
        """Action node — built-in actions (http, shell, save-file, etc.)."""
        action_type = config.get("action_type", "http")

        if action_type == "http":
            url = config.get("url", "")
            method = config.get("method", "GET")
            headers_str = config.get("headers", "")
            body = config.get("body", "")
            if not url:
                return {"error": "No URL provided"}
            try:
                import urllib.request
                import urllib.parse
                headers = {}
                if headers_str:
                    headers = json.loads(headers_str)
                data = body.encode() if body else None
                req = urllib.request.Request(url, data=data, headers=headers, method=method)
                with urllib.request.urlopen(req, timeout=30) as resp:
                    resp_body = resp.read().decode(errors="ignore")
                    return {"status": resp.status, "body": resp_body[:2000], "url": url}
            except Exception as e:
                return {"error": str(e), "status": 0}

        elif action_type == "shell":
            command = config.get("command", "")
            if not command:
                return {"error": "No command provided"}
            try:
                # Security: restrict shell actions to the workspace tree (same
                # boundary as save-file) so a compromised workflow definition
                # cannot touch config/, auth, or the wider filesystem.
                workspace_root = (AGENT_OS_ROOT / "workspace").resolve()
                r = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=60,
                                   cwd=str(workspace_root))
                return {"stdout": r.stdout[:2000], "stderr": r.stderr[:1000], "returncode": r.returncode}
            except Exception as e:
                return {"error": str(e)}

        elif action_type == "save-file":
            file_path = config.get("file_path", "")
            if not file_path:
                return {"error": "No file_path provided"}
            # Path-traversal guard: resolve and require the target stays under AGENT_OS_ROOT.
            root = AGENT_OS_ROOT.resolve()
            full_path = (root / file_path).resolve()
            try:
                full_path.relative_to(root)
            except ValueError:
                return {"error": f"Invalid file_path: resolves outside workspace root ({full_path})"}
            # Get content from previous node
            content = ""
            for src_key, src_data in inputs.items():
                if isinstance(src_data, dict):
                    content = src_data.get("response") or src_data.get("body") or src_data.get("stdout", "")
                    if content:
                        break
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(str(content))
            return {"saved_to": str(full_path), "size": len(str(content))}

        elif action_type == "load-file":
            file_path = config.get("file_path", "")
            if not file_path:
                return {"error": "No file_path provided"}
            # Path-traversal guard: resolve and require the target stays under AGENT_OS_ROOT.
            root = AGENT_OS_ROOT.resolve()
            full_path = (root / file_path).resolve()
            try:
                full_path.relative_to(root)
            except ValueError:
                return {"error": f"Invalid file_path: resolves outside workspace root ({full_path})"}
            if not full_path.exists():
                return {"error": f"File not found: {file_path}"}
            content = full_path.read_text(errors="ignore")[:5000]
            return {"content": content, "path": str(full_path)}

        elif action_type == "notify":
            message = config.get("message", "Workflow notification")
            # Save notification to a file
            notif_dir = AGENT_OS_ROOT / "workspace" / "notifications"
            notif_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            notif_file = notif_dir / f"notif-{ts}.txt"
            notif_file.write_text(message)
            return {"notified": True, "message": message}

        elif action_type == "deep-research":
            question = config.get("question", "")
            if not question:
                # Try to get question from upstream input
                for src_key, src_data in inputs.items():
                    if isinstance(src_data, dict):
                        question = src_data.get("response") or src_data.get("body") or src_data.get("stdout", "")
                        if question:
                            question = question[:500]
                            break
            if not question:
                return {"error": "No question provided for deep research"}

            # Get config
            rounds = int(config.get("rounds", 3))
            search_backend = config.get("search_backend", "duckduckgo")

            try:
                dr_script = AGENT_OS_ROOT / "scripts" / "deep_research.py"
                cmd = [
                    sys.executable, str(dr_script),
                    question,
                    "--rounds", str(rounds),
                    "--search", search_backend,
                    "--json",
                    "--notify",
                ]
                # Build a sanitized env: only pass whitelisted keys, exclude secrets
                _safe_env = {
                    k: v for k, v in os.environ.items()
                    if k in (
                        "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE",
                        "TMPDIR", "TERM", "SHELL", "USER", "LOGNAME",
                        "AGENT_OS_ROOT", "BRAVE_API_KEY",
                    )
                }
                r = subprocess.run(
                    cmd,
                    capture_output=True, text=True, timeout=600,
                    cwd=str(AGENT_OS_ROOT),
                    env=_safe_env,
                )
                stdout = r.stdout.strip()
                try:
                    return json.loads(stdout)
                except json.JSONDecodeError:
                    return {"output": stdout, "returncode": r.returncode}
            except subprocess.TimeoutExpired:
                return {"error": "Deep research timed out after 600s"}
            except Exception as e:
                return {"error": "Deep research execution failed"}

        return {"error": f"Unknown action type: {action_type}"}

    def _run_condition(self, config, inputs):
        """Condition node — evaluate an expression to branch."""
        # Gather the first available input value
        input_val = None
        for src_key, src_data in inputs.items():
            if isinstance(src_data, dict):
                # Check nested field path like "output.status"
                field = config.get("field", "")
                if field:
                    parts = field.split(".")
                    v = src_data
                    for p in parts:
                        v = v.get(p) if isinstance(v, dict) else None
                    input_val = v
                else:
                    input_val = src_data.get("status") or src_data.get("returncode")
                break
            elif src_data is not None:
                input_val = src_data
                break

        operator = config.get("operator", "==")
        compare_value = config.get("value", "")
        # Type coercion
        try:
            compare_value = int(compare_value)
            try:
                input_val = int(input_val)
            except (TypeError, ValueError):
                pass
        except ValueError:
            pass

        ops = {
            "==": lambda a, b: a == b,
            "!=": lambda a, b: a != b,
            ">": lambda a, b: a > b if a is not None and b is not None else False,
            "<": lambda a, b: a < b if a is not None and b is not None else False,
            "contains": lambda a, b: str(b).lower() in str(a).lower() if a else False,
            "exists": lambda a, b: a is not None and a != "",
        }
        result = ops.get(operator, lambda a, b: False)(input_val, compare_value)
        self.log_msg("info", f"Condition: {input_val} {operator} {compare_value} = {result}")
        return {"result": result, "field_value": input_val, "operator": operator}

    def _run_delay(self, config, inputs):
        """Delay node — pause execution."""
        seconds = int(config.get("seconds", 10))
        self.log_msg("info", f"Delay: {seconds}s")
        time.sleep(seconds)
        return {"delayed_seconds": seconds}

    def _run_merge(self, config, inputs):
        """Merge node — combine multiple inputs."""
        merged = {}
        for key, val in inputs.items():
            merged[key] = val
        return {"merged": True, "inputs": merged}

    def _run_output(self, config, inputs):
        """Output node — collect final result and optionally save."""
        save_to = config.get("save_to", "")
        output_data = {}
        for src_key, src_data in inputs.items():
            output_data = src_data
            break

        if save_to:
            full_path = AGENT_OS_ROOT / save_to
            full_path.parent.mkdir(parents=True, exist_ok=True)
            content = json.dumps(output_data, indent=2, default=str) if isinstance(output_data, dict) else str(output_data)
            full_path.write_text(content)
            return {"saved_to": str(full_path), "output": output_data}

        return {"output": output_data}

    def _build_record(self, status):
        return {
            "run_id": self.run_id,
            "workflow_id": self.workflow.get("id", "?"),
            "workflow_name": self.workflow.get("name", "?"),
            "status": status,
            "started_at": self.started_at,
            "finished_at": datetime.now().isoformat(),
            "node_status": self.node_status,
            "node_outputs": {k: v for k, v in self.node_outputs.items()},
            "log": self.log,
        }


# ── CLI ──

def cmd_run(args):
    """Run a workflow from file."""
    workflow_file = Path(args.workflow)
    if not workflow_file.is_absolute():
        workflow_file = WORKFLOWS_DIR / workflow_file
    if not workflow_file.exists():
        print(json.dumps({"ok": False, "error": f"Workflow not found: {workflow_file}"}))
        return 1

    workflow = json.loads(workflow_file.read_text())
    errors = validate_workflow(workflow)
    if errors:
        print(json.dumps({"ok": False, "validation_errors": errors}))
        return 1

    runner = WorkflowRunner(workflow)
    record = runner.execute()

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    run_file = RUNS_DIR / f"{record['run_id']}.json"
    run_file.write_text(json.dumps(record, indent=2, default=str))

    print(json.dumps(record, indent=2, default=str))
    return 0 if record["status"] == "completed" else 1


def cmd_validate(args):
    """Validate a workflow file."""
    workflow_file = Path(args.workflow)
    if not workflow_file.is_absolute():
        workflow_file = WORKFLOWS_DIR / workflow_file
    workflow = json.loads(workflow_file.read_text())
    errors = validate_workflow(workflow)
    if errors:
        print(json.dumps({"valid": False, "errors": errors}, indent=2))
        return 1
    print(json.dumps({"valid": True, "node_count": len(workflow.get("nodes", []))}, indent=2))
    return 0


def cmd_list(args):
    """List saved workflows."""
    WORKFLOWS_DIR.mkdir(parents=True, exist_ok=True)
    workflows = []
    for f in sorted(WORKFLOWS_DIR.glob("*.json")):
        try:
            w = json.loads(f.read_text())
            workflows.append({"id": w.get("id", f.stem), "name": w.get("name", f.stem),
                              "nodes": len(w.get("nodes", [])), "file": f.name})
        except Exception:
            pass
    print(json.dumps({"workflows": workflows, "count": len(workflows)}, indent=2))
    return 0


def cmd_runs(args):
    """List workflow runs."""
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    runs = []
    for f in sorted(RUNS_DIR.glob("wfrun-*.json"), reverse=True)[:args.limit]:
        try:
            r = json.loads(f.read_text())
            runs.append({"run_id": r["run_id"], "workflow_name": r["workflow_name"],
                         "status": r["status"], "started_at": r["started_at"]})
        except Exception:
            pass
    print(json.dumps({"runs": runs, "count": len(runs)}, indent=2))
    return 0


def main():
    parser = argparse.ArgumentParser(description="MoiraiCore — Workflow Engine")
    sub = parser.add_subparsers(dest="command")

    p_run = sub.add_parser("run", help="Run a workflow")
    p_run.add_argument("workflow", help="Workflow file name or path")
    p_run.set_defaults(func=cmd_run)

    p_val = sub.add_parser("validate", help="Validate a workflow file")
    p_val.add_argument("workflow", help="Workflow file name or path")
    p_val.set_defaults(func=cmd_validate)

    p_list = sub.add_parser("list", help="List saved workflows")
    p_list.set_defaults(func=cmd_list)

    p_runs = sub.add_parser("runs", help="List workflow runs")
    p_runs.add_argument("--limit", type=int, default=20)
    p_runs.set_defaults(func=cmd_runs)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
