"""
ScrumGate — Hard enforcement layer for the multi-agent pipeline.

RULES:
1. NO file writes to src/ without a ScrumMaster DONE evaluation
2. ALL code generation goes through Tier 2 (Gemini) or Tier 3 (Ollama)
3. ALL generated code is written to a merge queue, NOT directly to src/
4. ScrumMaster evaluation is required before merge queue → src/
5. The LLM (Hermes) can only create tasks and submit artifacts — never write src/ directly

This module is the SINGLE POINT OF ENFORCEMENT.
All agent operations flow through it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional


# ── Webhooks / Event Bus (Feature 8) ──
_emit_event = None
try:
    from webhooks import emit as _emit_event
except ImportError:
    _emit_event = None

def _fire(event: str, payload: dict) -> None:
    """Best-effort event emit. Never raises into the pipeline."""
    if _emit_event is not None:
        try:
            _emit_event(event, payload)
        except Exception:
            pass


# ── Agent Working Method (10x discipline) ──
_WORKING_METHOD = ""
try:
    from agent_working_method import WORKING_METHOD_PREAMBLE as _WORKING_METHOD
except ImportError:
    pass


# ── Paths ──

PROJECT_SPACE = Path(os.environ.get("PROJECT_SPACE", str(Path(__file__).resolve().parents[1] / "projects")))
SRC_DIR = PROJECT_SPACE / "src"
MERGE_QUEUE = PROJECT_SPACE / ".antigravity" / "merge_queue"
ARTIFACTS_DIR = PROJECT_SPACE / ".antigravity" / "artifacts"
REJECTED_DIR = PROJECT_SPACE / ".antigravity" / "rejected"


class GateStatus(Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    MERGED = "merged"


@dataclass
class MergeRequest:
    """A request to merge generated code into src/."""
    task_id: str
    file_path: str  # Relative to src/
    generated_code: str
    tier: int  # 2 or 3
    engine: str  # "gemini" or "ollama"
    artifact_path: str = ""
    status: GateStatus = GateStatus.PENDING
    eval_notes: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    evaluated_at: str = ""
    merged_at: str = ""

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "file_path": self.file_path,
            "tier": self.tier,
            "engine": self.engine,
            "artifact_path": self.artifact_path,
            "status": self.status.value,
            "eval_notes": self.eval_notes,
            "created_at": self.created_at,
            "evaluated_at": self.evaluated_at,
            "merged_at": self.merged_at,
        }


class Violation(Exception):
    """Raised when someone tries to bypass the gate."""
    pass


# ── Enforcement ──

def enforce_no_direct_write(file_path: str, project_space: str | None = None) -> None:
    """
    BLOCK direct file writes to src/.

    Raises Violation if someone tries to write outside the merge pipeline.
    This is called before any file write operation.
    """
    p = Path(file_path).resolve()
    src = (Path(project_space) / "src").resolve() if project_space else SRC_DIR.resolve()

    # Block writes to src/ that don't come through merge
    if str(p).startswith(str(src)):
        # Check if this is coming from the merge function
        import inspect
        stack = inspect.stack()
        caller_names = [f.function for f in stack]

        # Only approve if called from merge_approved_code
        if "merge_approved_code" not in caller_names:
            raise Violation(
                f"BLOCKED: Direct write to '{file_path}' is not allowed.\n"
                f"ALL code must go through ScrumGate.generate_code() → evaluate → merge.\n"
                f"Use ScrumGate.submit_to_merge_queue() instead."
            )


# ── Agent Execution Layer ──

def generate_code_via_gemini(task_description: str, file_path: str,
                               current_content: str = "",
                               context: str = "") -> tuple[str, str]:
    """
    Generate code via Gemini Pro API (Tier 2).

    Returns (generated_code, error_message).
    """
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(PROJECT_SPACE) / ".env")

        from gemini_worker import GeminiConfig, generate_code_block, is_gemini_available

        config = GeminiConfig.from_env()
        if not is_gemini_available(config):
            return "", "Gemini API not available"

        full_spec = f"{_WORKING_METHOD}\n\n## Task\n{task_description}\n\n## Target File\n{file_path}\n"
        if current_content:
            full_spec += f"\n## Current Content\n```\n{current_content}\n```\n"
        if context:
            full_spec += f"\n## Context\n{context}\n"

        result = generate_code_block(
            project_space=str(PROJECT_SPACE),
            spec=full_spec,
            file_path=file_path,
            config=config,
        )

        if result and not result.startswith("ERROR:"):
            return result, ""
        return "", result

    except Exception as e:
        return "", f"Gemini error: {e}"


def generate_code_via_ollama(task_description: str, file_path: str,
                              current_content: str = "",
                              test_command: str = "echo no-test",
                              context: str = "",
                              max_iterations: int = 3) -> tuple[str, str]:
    """
    Generate code via Tier 3 (Ollama iterative loop, or the Pi agent harness).

    Backend selected by HAGENT_TIER3_BACKEND (default 'ollama'; set 'pi' to
    route through the Pi harness with a native tool-calling model such as
    qwen3:8b). docs/harness_worker.py documents the Pi backend.

    Returns (generated_code, error_message).
    """
    try:
        from ollama_worker import ExecutionTask
        from harness_worker import _is_placeholder, discover_test_command

        # Real test-verified gate: when the caller didn't supply a test command,
        # discover a real one (pytest file, else import smoke) instead of the
        # silent 'echo no-test' placeholder. Applies to BOTH backends.
        if _is_placeholder(test_command):
            test_command = discover_test_command(str(PROJECT_SPACE), file_path)

        task = ExecutionTask(
            file_path=file_path,
            task_description=task_description,
            test_command=test_command,
            project_space=str(PROJECT_SPACE),
            context=context,
        )

        backend = os.environ.get("HAGENT_TIER3_BACKEND", "ollama").strip().lower()
        if backend == "pi":
            import harness_worker as hw

            config = hw.PiConfig.from_env()
            config.max_retries = max_iterations
            result = hw.execute_tier3(task, config)
        else:
            from ollama_worker import execute_tier3, OllamaConfig

            config = OllamaConfig()
            config.per_iteration_timeout = 180
            config.max_retries = max_iterations
            result = execute_tier3(task, config)

        if result.success:
            code = (
                (PROJECT_SPACE / file_path).read_text()
                if (PROJECT_SPACE / file_path).exists()
                else ""
            )
            return code, ""
        return "", result.terminated_reason

    except Exception as e:
        return "", f"Tier3 error: {e}"


# ── Merge Queue Pipeline ──

def submit_to_merge_queue(task_id: str, file_path: str, generated_code: str,
                           tier: int, engine: str) -> MergeRequest:
    """
    Submit generated code to the merge queue.

    Code sits here until ScrumMaster evaluates and approves it.
    """
    MERGE_QUEUE.mkdir(parents=True, exist_ok=True)

    # ── Feature 7: enforce merge_request contract before queueing ──
    try:
        from pipeline_contracts import enforce as _enf, ContractViolation as _CV
        _enf("merge_request", {
            "task_id": task_id, "file_path": file_path,
            "generated_code": generated_code, "tier": tier, "engine": engine,
        }, context={"task_id": task_id, "actor": "scrum_gate"})
    except ImportError:
        pass
    except Exception as cv:
        raise cv  # ContractViolation propagates — code is NOT queued.

    mr = MergeRequest(
        task_id=task_id,
        file_path=file_path,
        generated_code=generated_code,
        tier=tier,
        engine=engine,
    )

    # Save generated code to merge queue
    ts = int(time.time())
    code_file = MERGE_QUEUE / f"{task_id}_{ts}_{file_path.replace('/', '_')}.py"
    code_file.write_text(generated_code, encoding="utf-8")

    # Save manifest
    manifest_file = code_file.with_suffix(".json")
    manifest_file.write_text(json.dumps(mr.to_dict(), indent=2), encoding="utf-8")

    mr.artifact_path = str(code_file)
    _fire("merge.queued", {
        "task_id": task_id, "file_path": file_path, "tier": tier,
        "engine": engine, "artifact_path": str(code_file),
    })
    return mr


def evaluate_merge_request(mr: MergeRequest, force_pass: bool = False) -> bool:
    """
    Evaluate a merge request.

    Checks:
    1. Code is non-empty
    2. No syntax errors (Python files)
    3. No obvious security issues
    4. Contains expected patterns (if specified)

    Returns True if approved, False if rejected.
    """
    code = mr.generated_code.strip()

    # Check 1: Non-empty
    if not code or len(code) < 10:
        mr.status = GateStatus.REJECTED
        mr.eval_notes = "Code is empty or too short"
        mr.evaluated_at = datetime.now().isoformat()
        _fire("merge.rejected", {"task_id": mr.task_id, "file_path": mr.file_path, "reason": "empty_or_short"})
        return False

    # Check 2: Python syntax (for .py files)
    if mr.file_path.endswith(".py"):
        try:
            compile(code, mr.file_path, "exec")
        except SyntaxError as e:
            mr.status = GateStatus.REJECTED
            mr.eval_notes = f"Syntax error: {e}"
            mr.evaluated_at = datetime.now().isoformat()
            _fire("merge.rejected", {"task_id": mr.task_id, "file_path": mr.file_path, "reason": "syntax_error"})
            return False

    # Check 3: Security scan (basic)
    dangerous_patterns = [
        r"os\.system\s*\(",
        r"subprocess\.call\s*\(",
        r"eval\s*\(",
        r"exec\s*\(",
        r"__import__\s*\(",
    ]
    for pattern in dangerous_patterns:
        if re.search(pattern, code):
            mr.status = GateStatus.REJECTED
            mr.eval_notes = f"Security: dangerous pattern '{pattern}' found"
            mr.evaluated_at = datetime.now().isoformat()
            _fire("merge.rejected", {"task_id": mr.task_id, "file_path": mr.file_path, "reason": "security_pattern"})
            return False

    # Check 4: Force pass (for testing)
    if force_pass:
        mr.status = GateStatus.APPROVED
        mr.eval_notes = "Force passed"
        mr.evaluated_at = datetime.now().isoformat()
        return True

    # All checks passed
    mr.status = GateStatus.APPROVED
    mr.eval_notes = f"Approved: syntax OK, no security issues, {len(code)} chars"
    mr.evaluated_at = datetime.now().isoformat()
    return True


def merge_approved_code(mr: MergeRequest) -> bool:
    """
    Merge an approved merge request into src/.

    This is the ONLY function that writes to src/.
    It is called exclusively by the ScrumMaster after evaluation.
    """
    if mr.status != GateStatus.APPROVED:
        raise Violation(f"Cannot merge: status is {mr.status.value}, not APPROVED")

    target = SRC_DIR / mr.file_path
    target.parent.mkdir(parents=True, exist_ok=True)

    # Backup existing file
    if target.exists():
        backup = target.with_suffix(f".backup_{int(time.time())}.py")
        shutil.copy2(target, backup)

    # Write the approved code
    target.write_text(mr.generated_code, encoding="utf-8")

    mr.status = GateStatus.MERGED
    mr.merged_at = datetime.now().isoformat()

    # ── Feature 8: fire merge.merged ──
    _fire("merge.merged", {
        "task_id": mr.task_id, "file_path": mr.file_path,
        "tier": mr.tier, "artifact_path": mr.artifact_path,
    })

    # Update manifest
    if mr.artifact_path:
        manifest_path = Path(mr.artifact_path).with_suffix(".json")
        manifest_path.write_text(json.dumps(mr.to_dict(), indent=2), encoding="utf-8")

    return True


# ── Full Pipeline ──

def execute_task(task_id: str, task_title: str, task_description: str,
                 file_path: str, tier: int = 3, engine: str = "ollama",
                 test_command: str = "echo no-test",
                 context: str = "", force_eval_pass: bool = False,
                 design_spec: Optional[dict] = None) -> dict:
    """
    Execute a task through the full enforced pipeline.

    Flow:
    0. (Optional) Load or generate design spec for UI tasks
    1. Generate code via specified tier/engine (with design context)
    2. Submit to merge queue
    3. Evaluate (syntax + security)
    4. Merge if approved

    Returns result dict.
    """
    start_time = time.time()

    # Read current file content
    current_file = SRC_DIR / file_path
    current_content = current_file.read_text() if current_file.exists() else ""

    # Step 0: Load design spec for UI/frontend tasks
    if design_spec is None and ("frontend" in task_description.lower() or
                                 "html" in task_description.lower() or
                                 "page" in task_description.lower() or
                                 "ui" in task_description.lower() or
                                 "design" in task_description.lower()):
        from design_agent import DesignAgent
        agent = DesignAgent(str(PROJECT_SPACE))
        spec = agent.load_spec()
        if spec:
            design_spec = spec.to_dict()

    # Enhance task description with design context
    design_context = ""
    if design_spec:
        design_context = f"""

=== DESIGN SPEC (MUST FOLLOW) ===
Colors: primary={design_spec['colors']['primary']}, secondary={design_spec['colors']['secondary']}, accent={design_spec['colors']['accent']}
Background: {design_spec['colors']['background']}, Surface: {design_spec['colors']['surface']}
Fonts: body={design_spec['typography']['font_family']}, headings={design_spec['typography']['heading_family']}
Border radius: sm={design_spec['border_radius']['sm']}, md={design_spec['border_radius']['md']}, lg={design_spec['border_radius']['lg']}
Max width: {design_spec['max_width']}
Card: padding={design_spec['components']['card_padding']}, radius={design_spec['border_radius']['lg']}, shadow={design_spec['components']['card_shadow']}
Button: padding={design_spec['components']['button_padding']}, radius={design_spec['components']['button_radius']}
Use CSS class names: .card, .btn, .btn-primary, .btn-secondary, .badge, .container
DO NOT use inline styles. Use the class names above.
================================="""

    enhanced_description = task_description + design_context

    # Step 1: Generate code — with automatic tier degradation.
    #
    # Previously this rigidly bound engine->tier and ERRORED if the requested
    # engine wasn't the expected one, so a Gemini(T2) outage failed the task
    # instead of degrading to Ollama(T3). Now the caller's engine/tier is a
    # PREFERENCE: tier_fallback probes availability and degrades down the
    # configured chain (default gemini -> ollama). Set MOIRAI_TIER_FALLBACK
    # or sandbox.json execution_workers.tier_fallback to override.
    degraded_from = None
    try:
        from tier_fallback import resolve_tier
        _sandbox_cfg = None
        try:
            from sandbox import ProjectSandbox
            _sandbox_cfg = ProjectSandbox(str(PROJECT_SPACE)).config
        except Exception:
            _sandbox_cfg = None

        choice = resolve_tier(preferred_tier=tier, sandbox_config=_sandbox_cfg)
        if choice.available and (choice.engine != engine or choice.tier != tier):
            degraded_from = {"engine": engine, "tier": tier}
            _fire("tier.degraded", {
                "task_id": task_id, "from": degraded_from,
                "to": {"engine": choice.engine, "tier": choice.tier},
                "reason": choice.reason,
            })
            engine, tier = choice.engine, choice.tier
        elif not choice.available:
            return {"ok": False, "error": choice.reason, "stage": "tier_resolution",
                    "probed": list(choice.probed)}
    except ImportError:
        pass  # tier_fallback not present — fall through to legacy binding below.

    if engine == "gemini" and tier == 2:
        generated_code, error = generate_code_via_gemini(
            enhanced_description, file_path, current_content, context
        )
    elif engine == "ollama" and tier == 3:
        generated_code, error = generate_code_via_ollama(
            enhanced_description, file_path, current_content, test_command, context
        )
    else:
        return {"ok": False, "error": f"Unknown engine/tier: {engine}/{tier}"}

    if error:
        return {"ok": False, "error": error, "stage": "generation"}

    if not generated_code.strip():
        return {"ok": False, "error": "Generated code is empty", "stage": "generation"}


    # Step 2: Submit to merge queue
    mr = submit_to_merge_queue(task_id, file_path, generated_code, tier, engine)

    # Step 3: Evaluate
    approved = evaluate_merge_request(mr, force_pass=force_eval_pass)

    if not approved:
        # Save to rejected
        REJECTED_DIR.mkdir(parents=True, exist_ok=True)
        rejected_file = REJECTED_DIR / f"{task_id}_{int(time.time())}_{file_path.replace('/', '_')}.py"
        rejected_file.write_text(generated_code, encoding="utf-8")
        rejected_manifest = rejected_file.with_suffix(".json")
        rejected_manifest.write_text(json.dumps(mr.to_dict(), indent=2), encoding="utf-8")

        return {
            "ok": False,
            "error": f"Evaluation failed: {mr.eval_notes}",
            "stage": "evaluation",
            "merge_request": mr.to_dict(),
        }

    # Step 4: Merge
    try:
        merge_approved_code(mr)
    except Violation as e:
        return {"ok": False, "error": str(e), "stage": "merge"}

    elapsed = time.time() - start_time

    # ── Telemetry: record the cost/latency of this gate execution ──
    try:
        from telemetry import record_run
        _gate_model = f"{engine or 'unknown'}"
        record_run(
            agent="scrumgate",
            tier=str(tier),
            model=_gate_model,
            duration_ms=int(elapsed * 1000),
            prompt_chars=len(generated_code),
            est_output_chars=0,
            goal_id="",
            ok=approved,
        )
    except Exception:
        pass

    return {
        "ok": True,
        "task_id": task_id,
        "file_path": file_path,
        "tier": tier,
        "engine": engine,
        "code_size": len(generated_code),
        "time_s": round(elapsed, 1),
        "eval_notes": mr.eval_notes,
        "merge_request": mr.to_dict(),
    }
