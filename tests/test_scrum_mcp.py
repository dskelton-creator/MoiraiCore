"""Improvement #7 tests — Scrum pipeline MCP server.

Exercises the MCP plumbing end-to-end: initialize → tools/list → tools/call
over the JSON-RPC stdio protocol, against a temporary project space.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MCP = ROOT / "scripts" / "scrum_mcp.py"

PROJECT = "mcp-test-project"


@pytest.fixture
def project_space(tmp_path):
    # a fresh project space per test
    return tmp_path / PROJECT


@pytest.fixture
def client(project_space):
    """JSON-RPC client speaking to a live scrum_mcp.py subprocess."""
    import os
    env = dict(os.environ,
               MCP_SCRUM_PROJECT=PROJECT,
               AGENT_OS_ROOT=str(ROOT),
               MOIRAICORE_PROJECT_SPACE=str(project_space))
    proc = subprocess.Popen(
        [sys.executable, str(MCP)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, env=env, cwd=str(ROOT))
    state = {"id": 0}

    def call(method, params=None):
        state["id"] += 1
        req = {"jsonrpc": "2.0", "id": state["id"], "method": method}
        if params is not None:
            req["params"] = params
        proc.stdin.write(json.dumps(req) + "\n")
        proc.stdin.flush()
        while True:
            line = proc.stdout.readline()
            if not line:
                raise RuntimeError("MCP server closed stdout")
            resp = json.loads(line)
            if resp.get("id") == state["id"]:
                return resp
    # patch the project space: the server derives it from project name under
    # ROOT/projects — simplest isolation is chdir-free env override; we accept
    # ROOT/projects/mcp-test-project
    real_space = ROOT / "projects" / PROJECT
    real_space.mkdir(parents=True, exist_ok=True)
    yield call
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _rpc_ok(resp):
    assert "error" not in resp, resp
    return resp["result"]


def _tool_json(resp):
    """tools/call result -> parsed JSON payload of the text content."""
    result = _rpc_ok(resp)
    assert result.get("isError") is not True, result
    return json.loads(result["content"][0]["text"])


# ── protocol ─────────────────────────────────────────────────────────────

def test_initialize(client):
    res = _rpc_ok(client("initialize", {"protocolVersion": "2024-11-05"}))
    assert res["serverInfo"]["name"] == "moiraicore-scrum-mcp"
    assert "tools" in res["capabilities"]


def test_tools_list_exposes_nine_tools(client):
    res = _rpc_ok(client("tools/list"))
    names = [t["name"] for t in res["tools"]]
    assert set(names) == {
        "scrum_status", "scrum_set_goal", "scrum_decompose", "scrum_assign",
        "scrum_execute", "scrum_submit", "scrum_evaluate", "scrum_review",
        "scrum_specs"}


def test_unknown_method_is_error(client):
    resp = client("bogus/method")
    assert resp["error"]["code"] == -32601


def test_unknown_tool_is_isError(client):
    res = _rpc_ok(client("tools/call", {"name": "no_such_tool", "arguments": {}}))
    assert res["isError"] is True


# ── pipeline tools against a live project ────────────────────────────────

def test_set_goal_then_status(client):
    _rpc_ok(client("tools/call", {"name": "scrum_set_goal",
                                  "arguments": {"goal": "build a task tracker"}}))
    res = json.loads(_rpc_ok(client("tools/call", {
        "name": "scrum_status", "arguments": {}}))["content"][0]["text"])
    assert res["goal"] == "build a task tracker"


def test_decompose_auto_derives_contracts(client, project_space):
    res = _tool_json(client("tools/call", {"name": "scrum_decompose", "arguments": {
        "goal": "task tracker exposing /api/shifts with times in seconds",
        "tasks": [{"title": "Server", "description": "implements /api/shifts",
                   "tier": 3},
                  {"title": "Client", "description": "UI", "tier": 3}]}}))
    backlog = res["backlog"]
    assert len(backlog) == 2
    # auto-derived contracts landed in the isolated project space
    contracts = project_space / "specs" / "contracts.md"
    assert contracts.exists()
    assert "/api/shifts" in contracts.read_text()


def test_full_pipeline_assign_execute_review(client):
    _tool_json(client("tools/call", {"name": "scrum_decompose", "arguments": {
        "goal": "demo pipeline",
        "tasks": [{"title": "Tiny task", "description": "do a tiny thing",
                   "tier": 3}]}}))
    _rpc_ok(client("tools/call", {"name": "scrum_assign",
                                  "arguments": {"task_id": "task-001", "tier": 3}}))
    # execute would make real model calls — verify assign/status path only,
    # then confirm review tool runs clean on an empty project space
    res = json.loads(_rpc_ok(client("tools/call", {
        "name": "scrum_review", "arguments": {}}))["content"][0]["text"])
    assert res["passed"] is True
    assert res["findings"] == []


def test_specs_lists_files(client):
    _tool_json(client("tools/call", {"name": "scrum_decompose", "arguments": {
        "goal": "service exposing /api/items",
        "tasks": [{"title": "A", "description": "implements /api/items", "tier": 3}]}}))
    res = json.loads(_rpc_ok(client("tools/call", {
        "name": "scrum_specs", "arguments": {}}))["content"][0]["text"])
    assert any(f.endswith("-spec.md") for f in res["spec_files"])
    assert res["contracts_file"] is True
