"""
Ollama Worker — Tier 3 Execution Engine (Junior Developer).

Executes tightly scoped, single-file coding tasks via a local Ollama model.
Enforces: iteration cap, loop detection, per-iteration timeout, test-driven success.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class OllamaConfig:
    """Configuration for an Ollama Tier 3 worker."""
    provider: str = "openai_compatible"
    base_url: str = "http://localhost:11434/v1"
    model_name: str = "qwen3-coder:30b-t3"
    max_retries: int = 5
    per_iteration_timeout: int = 60  # seconds (reduced from 120)
    temperature: float = 0.1  # Low temp for deterministic code fixes
    max_tokens: int = 4096  # Full-file generation needs headroom; 1024 truncated mid-statement
    api_key: str = "ollama"  # Ollama doesn't require a real key
    warm_on_start: bool = True  # Warm model to eliminate cold-start


@dataclass
class ExecutionTask:
    """A single-file coding task for Tier 3 execution."""
    file_path: str
    task_description: str
    test_command: str
    project_space: str
    context: str = ""  # Additional context (error messages, requirements)


@dataclass
class IterationResult:
    """Result of a single execution iteration."""
    iteration: int
    file_hash_before: str
    file_hash_after: str
    test_output: str
    test_passed: bool
    file_changed: bool
    error: str = ""


@dataclass
class ExecutionResult:
    """Complete result of the Tier 3 execution loop."""
    task: ExecutionTask
    success: bool
    iterations: list[IterationResult] = field(default_factory=list)
    total_time_ms: int = 0
    terminated_reason: str = ""
    final_file_hash: str = ""

    @property
    def iteration_count(self) -> int:
        return len(self.iterations)

    @property
    def loop_detected(self) -> bool:
        if len(self.iterations) < 3:
            return False
        # Check if last 3 file hashes are the same (no progress)
        last_3 = [i.file_hash_after for i in self.iterations[-3:]]
        return len(set(last_3)) == 1 and not self.iterations[-1].test_passed

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "iteration_count": self.iteration_count,
            "loop_detected": self.loop_detected,
            "total_time_ms": self.total_time_ms,
            "terminated_reason": self.terminated_reason,
            "iterations": [
                {
                    "iteration": i.iteration,
                    "test_passed": i.test_passed,
                    "file_changed": i.file_changed,
                    "error": i.error,
                }
                for i in self.iterations
            ],
        }


def _file_hash(path: str) -> str:
    """Compute SHA256 hash of a file."""
    try:
        content = Path(path).read_bytes()
        return hashlib.sha256(content).hexdigest()[:16]
    except Exception:
        return ""


def warm_ollama(config: OllamaConfig) -> bool:
    """
    Warm the Ollama model to eliminate cold-start latency on first real task.
    
    Sends a trivial prompt to ensure the model is loaded and cached in memory.
    Returns True if warm succeeded, False if cold start will still occur.
    """
    import urllib.request

    url = f"{config.base_url}/chat/completions"
    payload = {
        "model": config.model_name,
        "messages": [
            {
                "role": "system",
                "content": "You are a code engine. Output ONLY code.",
            },
            {"role": "user", "content": "pass"},
        ],
        "temperature": 0,
        "max_tokens": 50,
        "stream": False,
        "options": {
            "num_ctx": 512,
        },
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.api_key}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            content = resp.read().decode("utf-8")
            return len(content) > 100
    except Exception:
        return False


def _call_ollama(config: OllamaConfig, prompt: str, timeout: int = 60) -> str:
    """
    Call Ollama API with performance optimizations:
    - Uses /v1/chat/completions (not /api/generate) for better system prompt support
    - Low temperature (0.1) for deterministic code
    - Reduced num_ctx (2048) to speed up generation
    - Lower max_tokens default (1024) for focused code fixes
    - System prompt tells model to output CODE ONLY (no thinking/explaining)
    """
    import urllib.request

    url = f"{config.base_url}/chat/completions"
    payload = {
        "model": config.model_name,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a code generation engine. Output ONLY the complete fixed file content. "
                    "No explanation. No markdown fences. No comments about your changes. "
                    "Just code. If impossible, output: CANNOT_FIX: reason"
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
        "stream": False,
        "options": {
            "num_ctx": 2048,
        },
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.api_key}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result.get("choices", [{}])[0].get("message", {}).get("content", "")
    except Exception as e:
        return f"OLLAMA_ERROR: {e}"


def _run_test(test_command: str, project_space: str, timeout: int = 60) -> tuple[str, bool]:
    """Run the test command in the project space. Returns (output, passed)."""
    try:
        result = subprocess.run(
            test_command,
            shell=True,
            cwd=project_space,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "PYTHONPATH": project_space},
        )
        output = result.stdout + result.stderr
        passed = result.returncode == 0
        return output, passed
    except subprocess.TimeoutExpired:
        return "TIMEOUT: Test exceeded time limit", False
    except Exception as e:
        return f"TEST_ERROR: {e}", False


def _build_prompt(task: ExecutionTask, current_content: str, previous_error: str = "") -> str:
    """Build the prompt for Ollama."""
    parts = [
        f"## Task\n{task.task_description}",
        f"\n## File to modify\n`{task.file_path}`",
        f"\n## Current file content\n```\n{current_content}\n```",
    ]

    if previous_error:
        parts.append(f"\n## Previous error\n{previous_error}")

    parts.append(
        f"\n## Test command\n`{task.test_command}`"
        f"\n\nOutput the complete fixed file content for `{task.file_path}`:"
    )

    return "\n".join(parts)


def execute_tier3(task: ExecutionTask, config: OllamaConfig = None) -> ExecutionResult:
    """
    Execute a Tier 3 coding task with iteration cap and loop detection.

    Loop safeguards:
    - Max iterations: config.max_retries (default 5)
    - Per-iteration timeout: config.per_iteration_timeout (default 60s)
    - Loop detection: terminate if same file hash repeats 3 times
    - Test-driven: success only when test passes
    """
    if config is None:
        config = OllamaConfig()

    start_time = time.time()
    result = ExecutionResult(task=task, success=False)

    # Verify file exists
    file_full_path = os.path.join(task.project_space, task.file_path)
    if not os.path.exists(file_full_path):
        result.terminated_reason = f"File not found: {file_full_path}"
        return result

    # Warm the model on first execution to eliminate cold-start latency
    if config.warm_on_start:
        try:
            warm_ollama(config)
        except Exception:
            pass  # Non-critical, proceed even if warm fails

    for iteration in range(1, config.max_retries + 1):
        iter_start = time.time()

        # Read current file
        current_content = Path(file_full_path).read_text(encoding="utf-8", errors="ignore")
        hash_before = _file_hash(file_full_path)

        # Get previous error for context (after first iteration)
        previous_error = ""
        if result.iterations and not result.iterations[-1].test_passed:
            previous_error = result.iterations[-1].test_output[-500:]  # Last 500 chars

        # Build prompt
        prompt = _build_prompt(task, current_content, previous_error)

        # Call Ollama
        response = _call_ollama(config, prompt, timeout=config.per_iteration_timeout)

        # Check for errors
        if response.startswith("OLLAMA_ERROR:"):
            result.iterations.append(IterationResult(
                iteration=iteration,
                file_hash_before=hash_before,
                file_hash_after=hash_before,
                test_output="",
                test_passed=False,
                file_changed=False,
                error=response,
            ))
            result.terminated_reason = response
            break

        # Check if model says it can't fix
        if response.startswith("CANNOT_FIX:"):
            result.iterations.append(IterationResult(
                iteration=iteration,
                file_hash_before=hash_before,
                file_hash_after=hash_before,
                test_output="",
                test_passed=False,
                file_changed=False,
                error=response,
            ))
            result.terminated_reason = response
            break

        # Strip markdown code fences from response
        response = response.strip()
        if response.startswith("```"):
            # Remove first line (```python, ```, etc.) and last line (```)
            lines = response.split("\n")
            if lines[-1].strip() == "```":
                lines = lines[1:-1]
            else:
                lines = lines[1:]
            response = "\n".join(lines).strip()

        # Write the fix
        Path(file_full_path).write_text(response, encoding="utf-8")
        hash_after = _file_hash(file_full_path)
        file_changed = hash_before != hash_after

        # Run the test
        test_output, test_passed = _run_test(
            task.test_command, task.project_space, timeout=config.per_iteration_timeout
        )

        iter_result = IterationResult(
            iteration=iteration,
            file_hash_before=hash_before,
            file_hash_after=hash_after,
            test_output=test_output,
            test_passed=test_passed,
            file_changed=file_changed,
        )
        result.iterations.append(iter_result)

        # Success!
        if test_passed:
            result.success = True
            result.terminated_reason = f"Test passed on iteration {iteration}"
            break

        # Loop detection: same file 3 times in a row
        if result.loop_detected:
            result.terminated_reason = f"Loop detected: same file hash repeated 3 times after {iteration} iterations"
            break

    result.total_time_ms = int((time.time() - start_time) * 1000)
    result.final_file_hash = _file_hash(file_full_path)

    if not result.success and not result.terminated_reason:
        result.terminated_reason = f"Max iterations ({config.max_retries}) reached without test passing"

    return result


def get_ollama_models(base_url: str = "http://localhost:11434") -> list[dict]:
    """List available models from Ollama."""
    import urllib.request
    try:
        url = f"{base_url}/api/tags"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("models", [])
    except Exception:
        return []


def is_ollama_available(base_url: str = "http://localhost:11434") -> bool:
    """Check if Ollama is running and accessible."""
    import urllib.request
    try:
        url = f"{base_url}/api/tags"
        with urllib.request.urlopen(url, timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False
