"""Unit tests for scripts/harness_worker.py — Pi-harness Tier 3 executor.

These tests are LLM-free: they monkeypatch harness_worker._run_pi so no real pi /
Ollama call is made. The live end-to-end (real pi + Ollama) is exercised
separately by scripts/run_harness_poc.py.
"""

import os
import sys
import shutil
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))

import harness_worker
from ollama_worker import ExecutionTask


def _mk_project(tmp_path, content):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "app.py").write_text(content)
    return str(proj)


def _task(project_space, test_command):
    return ExecutionTask(
        file_path="app.py",
        task_description="Fix add() so add(2,3) returns 5.",
        test_command=test_command,
        project_space=project_space,
    )


GOOD = "def add(a, b):\n    return a + b\n"
BAD = "def add(a, b):\n    return a - b  # bug\n"
TEST = 'python3 -c "from app import add; assert add(2,3)==5"'


def test_config_defaults_use_tool_capable_model():
    c = harness_worker.PiConfig()
    assert c.provider == "ollama"
    assert c.model == "qwen3:8b"  # tool-capable; qwen2.5-coder:14b cannot drive pi
    assert c.max_retries >= 1


def test_effective_backend_and_from_env(monkeypatch):
    monkeypatch.setattr(os, "environ", {**os.environ, "HAGENT_TIER3_BACKEND": "pi",
                                        "HAGENT_TIER3_MODEL": "qwen3:14b",
                                        "HAGENT_TIER3_TIMEOUT": "300"})
    assert harness_worker.effective_backend() == "pi"
    c = harness_worker.PiConfig.from_env()
    assert c.model == "qwen3:14b"
    assert c.per_iteration_timeout == 300
    # default (no env) stays 'ollama'
    monkeypatch.setattr(os, "environ", {k: v for k, v in os.environ.items()
                                        if k != "HAGENT_TIER3_BACKEND"})
    assert harness_worker.effective_backend() == "ollama"


def test_file_not_found_returns_clean_failure(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    task = _task(str(proj), TEST)
    task.file_path = "missing.py"
    res = harness_worker.execute_tier3(task)
    assert res.success is False
    assert "File not found" in res.terminated_reason
    assert res.iteration_count == 0


def test_successful_fix_when_agent_edits_staging(tmp_path, monkeypatch):
    proj = _mk_project(tmp_path, BAD)

    def fake_run_pi(prompt, staging_dir, config):
        # Simulate the agent fixing the file inside staging.
        Path(os.path.join(staging_dir, "app.py")).write_text(GOOD)
        return {
            "ok": True, "exit_code": 0, "stderr": "",
            "tools_executed": 2, "tool_errors": 0, "errors": 0,
            "final_text": "Fixed add. DONE",
        }

    monkeypatch.setattr(harness_worker, "_run_pi", fake_run_pi)
    res = harness_worker.execute_tier3(_task(proj, TEST))

    assert res.success is True
    assert res.iteration_count == 1
    assert res.iterations[0].test_passed is True
    assert res.iterations[0].file_changed is True
    assert open(os.path.join(proj, "app.py")).read() == GOOD


def test_no_tool_capability_blocker_stops_early(tmp_path, monkeypatch):
    """qwen2.5-coder-style model emits tool call as text -> no tool executes."""
    proj = _mk_project(tmp_path, BAD)

    def fake_run_pi(prompt, staging_dir, config):
        return {
            "ok": True, "exit_code": 0, "stderr": "",
            "tools_executed": 0, "tool_errors": 0, "errors": 0,
            "final_text": '<tools>\n{\n  "name": "write",\n  "arguments": {}\n}\n</tools>',
        }

    monkeypatch.setattr(harness_worker, "_run_pi", fake_run_pi)
    res = harness_worker.execute_tier3(_task(proj, TEST), harness_worker.PiConfig(max_retries=5))

    assert res.success is False
    assert res.iteration_count == 1  # stops after first, doesn't burn retries
    assert "native tool_calls" in res.terminated_reason
    assert "qwen2.5-coder:14b" in res.terminated_reason


def test_staging_dir_is_cleaned_up(tmp_path, monkeypatch):
    proj = _mk_project(tmp_path, BAD)

    def fake_run_pi(prompt, staging_dir, config):
        Path(os.path.join(staging_dir, "app.py")).write_text(GOOD)
        return {
            "ok": True, "exit_code": 0, "stderr": "",
            "tools_executed": 1, "tool_errors": 0, "errors": 0, "final_text": "DONE",
        }

    monkeypatch.setattr(harness_worker, "_run_pi", fake_run_pi)
    harness_worker.execute_tier3(_task(proj, TEST))
    leftovers = [d for d in os.listdir(proj) if d.startswith(".pi_t3_")]
    assert leftovers == [], f"staging dir leaked: {leftovers}"


def test_pi_timeout_is_surfaced(tmp_path, monkeypatch):
    proj = _mk_project(tmp_path, BAD)

    def fake_run_pi(prompt, staging_dir, config):
        raise NotImplementedError  # not reached; we test the caller path via timeout flag
    # Instead directly test that a timeout-shaped return terminates cleanly.
    def fake_timeout(prompt, staging_dir, config):
        return {
            "ok": False, "exit_code": None, "stderr": "pi TIMEOUT after 240s",
            "tools_executed": 0, "tool_errors": 0, "errors": 1, "final_text": "",
        }
    monkeypatch.setattr(harness_worker, "_run_pi", fake_timeout)
    res = harness_worker.execute_tier3(_task(proj, TEST))
    assert res.success is False
    assert "TIMEOUT" in res.terminated_reason


def _ok_result(task):
    from ollama_worker import ExecutionResult
    r = ExecutionResult(task=task, success=True)
    r.terminated_reason = "Test passed on iteration 1"
    return r


def test_scrum_gate_routes_to_pi_backend_when_env_set(tmp_path, monkeypatch):
    import ollama_worker
    import scrum_gate

    proj = tmp_path / "sproj"
    proj.mkdir()
    (proj / "app.py").write_text(BAD)
    monkeypatch.setattr(scrum_gate, "PROJECT_SPACE", proj)
    monkeypatch.setenv("HAGENT_TIER3_BACKEND", "pi")

    routed = {}

    def fake_hw_exec(task, config):
        routed["backend"] = "pi"
        (proj / "app.py").write_text(GOOD)
        return _ok_result(task)

    def fake_oll_exec(task, config):
        routed["backend"] = "ollama"  # must not be called
        return None

    monkeypatch.setattr(harness_worker, "execute_tier3", fake_hw_exec)
    monkeypatch.setattr(ollama_worker, "execute_tier3", fake_oll_exec)

    code, err = scrum_gate.generate_code_via_ollama("Fix add", "app.py")
    assert routed.get("backend") == "pi"
    assert err == ""
    assert code == GOOD


def test_scrum_gate_routes_to_ollama_backend_by_default(tmp_path, monkeypatch):
    import ollama_worker
    import scrum_gate

    proj = tmp_path / "sproj2"
    proj.mkdir()
    (proj / "app.py").write_text(BAD)
    monkeypatch.setattr(scrum_gate, "PROJECT_SPACE", proj)
    monkeypatch.delenv("HAGENT_TIER3_BACKEND", raising=False)

    routed = {}

    def fake_hw_exec(task, config):
        routed["backend"] = "pi"  # must not be called
        return None

    def fake_oll_exec(task, config):
        routed["backend"] = "ollama"
        (proj / "app.py").write_text(GOOD)
        return _ok_result(task)

    monkeypatch.setattr(harness_worker, "execute_tier3", fake_hw_exec)
    monkeypatch.setattr(ollama_worker, "execute_tier3", fake_oll_exec)

    code, err = scrum_gate.generate_code_via_ollama("Fix add", "app.py")
    assert routed.get("backend") == "ollama"
    assert err == ""
    assert code == GOOD
