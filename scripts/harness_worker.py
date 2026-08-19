"""
Pi Harness — Tier 3 Execution Engine (Junior Developer) via the Pi agent harness.

Drop-in replacement for `ollama_worker.execute_tier3()`. It drives the `pi`
coding-agent CLI (https://github.com/earendil-works/pi) in JSON event-stream
mode against a STAGING copy of the target file, so the agent's tool use
(read / write / edit / bash) is confined to staging and its changes flow back
through the existing generate -> evaluate -> merge path (never direct src/).

Same public contract as ollama_worker so every existing caller
(scrum_gate.generate_code_via_ollama, worker_loop, /api/ollama/run) keeps
working unchanged: swap the import of execute_tier3 to point here.

IMPORTANT — model requirement
------------------------------
pi requires a model that exposes NATIVE `tool_calls` on Ollama's OpenAI-compatible
endpoint. Verified on this machine:
  * qwen3:8b                                         -> native tool_calls: YES (default)
  * hf.co/deepreinforce-ai/Ornith-1.0-9B-GGUF:Q4_K_M -> native tool_calls: YES
  * qwen2.5-coder:14b                                -> native tool_calls: NO
    (it emits its tool invocation as TEXT inside a `<tools>...</tools>` block,
     so pi's openai-completions adapter never sees a tool call and executes
     nothing). Do not use it to drive pi.

Enforces: iteration cap, loop detection, per-iteration timeout, test-driven success.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass

from ollama_worker import (
    ExecutionResult,
    ExecutionTask,
    IterationResult,
    _file_hash,
    _run_test,
)


@dataclass
class PiConfig:
    """Configuration for a Pi-harness Tier 3 worker."""

    pi_bin: str = "pi"
    provider: str = "ollama"
    # Tool-capable local model. qwen2.5-coder:14b does NOT support native
    # tool_calls on Ollama and cannot drive pi. qwen3:8b (and Ornith-1.0-9B)
    # do. qwen3:8b is the default; override via HAGENT_TIER3_MODEL.
    model: str = "qwen3:8b"
    max_retries: int = 3
    per_iteration_timeout: int = 240  # agent loop is slower than a single LLM call
    test_timeout: int = 60
    staging_keep: bool = False  # keep the staging dir for debugging
    extra_args: tuple = ()  # extra pi CLI flags, e.g. ("--thinking", "off")

    @classmethod
    def from_env(cls) -> "PiConfig":
        """Build a PiConfig from env vars (HAGENT_TIER3_PROVIDER/MODEL)."""
        return cls(
            provider=os.environ.get("HAGENT_TIER3_PROVIDER", "ollama"),
            model=os.environ.get("HAGENT_TIER3_MODEL", "qwen3:8b"),
            max_retries=int(os.environ.get("HAGENT_TIER3_MAX_RETRIES", "3")),
            per_iteration_timeout=int(
                os.environ.get("HAGENT_TIER3_TIMEOUT", "240")
            ),
        )


def effective_backend() -> str:
    """Which Tier 3 backend to use: 'ollama' (default) or 'pi'."""
    return os.environ.get("HAGENT_TIER3_BACKEND", "ollama").strip().lower()


def _build_agent_prompt(task: ExecutionTask, previous_error: str = "") -> str:
    """Natural-language instruction for the Pi agent (it uses its own tools)."""
    rel = os.path.normpath(task.file_path)
    prompt = (
        "You are a Tier 3 developer agent working inside a sandboxed directory.\n"
        f"The file `{rel}` exists in the current directory.\n"
        f"Task: {task.task_description}\n\n"
        "Diagnose and fix `{rel}` (relative to the current directory) so this test command passes:\n"
        f"  {task.test_command}\n"
    ).format(rel=rel)

    if task.context:
        prompt += f"\nContext:\n{task.context}\n"
    if previous_error:
        prompt += f"\nPrevious test failure (last 500 chars):\n{previous_error}\n"

    prompt += (
        f"\nRules:\n"
        f"- Use your read tool to inspect `{rel}`, then fix it with your edit/write tools.\n"
        f"- Do NOT read, write, or delete anything outside this directory.\n"
        f"- Do NOT run commands that modify anything outside this directory.\n"
        f"- You may run `{task.test_command}` here to self-check, but the authoritative "
        "test runs externally.\n"
        f"- When you have edited the file and believe the test would pass, reply with a "
        "short summary ending in DONE."
    )
    return prompt


def _run_pi(prompt: str, staging_dir: str, config: PiConfig) -> dict:
    """Run `pi` non-interactively in JSON mode. Returns a parsed summary."""
    model_spec = f"{config.provider}/{config.model}"
    cmd = [
        config.pi_bin,
        "--mode", "json",
        "-p",
        "--provider", config.provider,
        "--model", model_spec,
        "--no-session",   # ephemeral: don't leak session state under ~/.pi
        "--approve",      # no project-trust prompt in a fresh staging dir
    ]
    cmd += list(config.extra_args)
    cmd.append(prompt)

    try:
        proc = subprocess.run(
            cmd,
            cwd=staging_dir,
            capture_output=True,
            text=True,
            timeout=config.per_iteration_timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "exit_code": None,
            "stderr": f"pi TIMEOUT after {config.per_iteration_timeout}s",
            "tools_executed": 0,
            "tool_errors": 0,
            "errors": 1,
            "final_text": exc.stdout or "",
        }

    out, err, code = proc.stdout, proc.stderr, proc.returncode
    tools = tool_errs = errs = 0
    final_text = ""
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        t = event.get("type")
        if t == "tool_execution_end":
            tools += 1
            if event.get("isError"):
                tool_errs += 1
        elif t == "error":
            errs += 1
        elif t == "message_end":
            final_text = "".join(
                b.get("text", "")
                for b in event.get("message", {}).get("content", [])
                if isinstance(b, dict) and b.get("type") == "text"
            )

    return {
        "ok": code == 0,
        "exit_code": code,
        "stderr": (err or "")[-1000:],
        "tools_executed": tools,
        "tool_errors": tool_errs,
        "errors": errs,
        "final_text": final_text,
    }


def _copy_into_staging(task: ExecutionTask, file_full_path: str, staging: str) -> str:
    """Copy the target file into staging preserving its relative path."""
    rel = os.path.normpath(task.file_path)
    staging_file = os.path.join(staging, rel)
    parent = os.path.dirname(staging_file)
    if parent:
        os.makedirs(parent, exist_ok=True)
    shutil.copyfile(file_full_path, staging_file)
    return staging_file


def execute_tier3(task: ExecutionTask, config: PiConfig = None) -> ExecutionResult:
    """
    Execute a Tier 3 coding task via the Pi harness inside a staging sandbox.

    Loop safeguards (mirror ollama_worker): max iterations, per-iteration
    timeout, loop detection (same file hash 3x), test-driven success.
    """
    if config is None:
        config = PiConfig()

    start_time = time.time()
    result = ExecutionResult(task=task, success=False)

    file_full_path = os.path.join(task.project_space, task.file_path)
    if not os.path.exists(file_full_path):
        result.terminated_reason = f"File not found: {file_full_path}"
        return result

    staging = None
    try:
        staging = tempfile.mkdtemp(prefix=".pi_t3_", dir=task.project_space)
        staging_file = _copy_into_staging(task, file_full_path, staging)

        for iteration in range(1, config.max_retries + 1):
            hash_before = _file_hash(file_full_path)

            previous_error = ""
            if result.iterations and not result.iterations[-1].test_passed:
                previous_error = result.iterations[-1].test_output[-500:]

            prompt = _build_agent_prompt(task, previous_error)
            pi = _run_pi(prompt, staging, config)

            # Copy the agent's edited file back into the project space. This is
            # the only project-space write, and it mirrors what ollama_worker
            # does; scrum_gate still governs the merge to src/.
            if os.path.exists(staging_file):
                parent = os.path.dirname(file_full_path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                shutil.copyfile(staging_file, file_full_path)

            hash_after = _file_hash(file_full_path)
            file_changed = hash_before != hash_after

            test_output, test_passed = _run_test(
                task.test_command, task.project_space, timeout=config.test_timeout
            )

            # Diagnose a pi run that made no tool calls: the most common cause
            # is a model that lacks native tool_calls (qwen2.5-coder:14b).
            error = ""
            if not pi["ok"]:
                error = pi["stderr"] or f"pi exited {pi['exit_code']}"
            elif pi["tools_executed"] == 0:
                if "<tools" in pi["final_text"] or pi["final_text"].lstrip().startswith("{"):
                    error = (
                        "No tool executed: the model emitted its tool call as TEXT, not "
                        "native tool_calls. This model cannot drive pi's agent loop "
                        "(qwen2.5-coder:14b is known-affected). Use a tool-capable model "
                        "(Ornith-1.0-9B or a qwen3-family coder)."
                    )
                elif not file_changed:
                    error = "No tool executed and file unchanged."

            result.iterations.append(IterationResult(
                iteration=iteration,
                file_hash_before=hash_before,
                file_hash_after=hash_after,
                test_output=test_output,
                test_passed=test_passed,
                file_changed=file_changed,
                error=error,
            ))

            if test_passed:
                result.success = True
                result.terminated_reason = f"Test passed on iteration {iteration}"
                break

            if result.loop_detected:
                result.terminated_reason = (
                    f"Loop detected: same file hash repeated 3 times after "
                    f"{iteration} iterations"
                )
                break

            # A tool-capability blocker won't fix itself — stop instead of
            # burning remaining iterations.
            if error and pi["tools_executed"] == 0:
                result.terminated_reason = error
                break
    finally:
        if staging and not config.staging_keep:
            shutil.rmtree(staging, ignore_errors=True)

    result.total_time_ms = int((time.time() - start_time) * 1000)
    result.final_file_hash = _file_hash(file_full_path)

    if not result.success and not result.terminated_reason:
        result.terminated_reason = (
            f"Max iterations ({config.max_retries}) reached without test passing"
        )

    return result
