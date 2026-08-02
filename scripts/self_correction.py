#!/usr/bin/env python3
"""
MoiraiCore — Self-Correction Engine
===================================
When a subtask fails or produces low-quality output, this module
generates an improved retry prompt.

Strategy:
  1. Analyze the failure (error message + output + goal context)
  2. Generate a revised prompt that addresses the failure
  3. Add explicit constraints/instructions to avoid the same issue
  4. Retry with the improved prompt

If max_retries exceeded:
  - Escalate to human (dashboard notification)
  - Mark subtask as "needs_human_input"
  - Continue with other subtasks (don't block the whole goal)

Usage:
    from self_correction import SelfCorrectionEngine

    engine = SelfCorrectionEngine(max_retries=2)
    result = engine.correct(
        goal_title="Research ASX competitors",
        task_title="Find top 5 competitors",
        original_prompt="Find top 5 ASX competitors",
        failure_reason="Output was too short",
        verification_verdict=verdict_dict,
    )
    if result["action"] == "retry":
        new_prompt = result["corrected_prompt"]
    elif result["action"] == "escalate":
        print("Needs human input:", result["reason"])
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

AGENT_OS_ROOT = Path(os.environ.get("AGENT_OS_ROOT", str(Path(__file__).resolve().parents[1])))
CONFIG_DIR = AGENT_OS_ROOT / "config"
SCRIPTS_DIR = AGENT_OS_ROOT / "scripts"
CORRECTIONS_DIR = CONFIG_DIR / "corrections"

sys.path.insert(0, str(SCRIPTS_DIR))


# ── Correction Record ──

class CorrectionRecord:
    """Record of a single self-correction attempt."""

    def __init__(
        self,
        goal_id: str = "",
        task_id: str = "",
        task_title: str = "",
        attempt: int = 0,
        action: str = "retry",  # "retry" | "escalate" | "skip"
        original_prompt: str = "",
        corrected_prompt: str = "",
        failure_reason: str = "",
        verification_score: float = 0.0,
        verification_status: str = "",
        suggestions: list = None,
        reasoning: str = "",
    ):
        self.goal_id = goal_id
        self.task_id = task_id
        self.task_title = task_title
        self.attempt = attempt
        self.action = action
        self.original_prompt = original_prompt
        self.corrected_prompt = corrected_prompt
        self.failure_reason = failure_reason
        self.verification_score = verification_score
        self.verification_status = verification_status
        self.suggestions = suggestions or []
        self.reasoning = reasoning
        self.created_at = datetime.now().isoformat()

    def to_dict(self) -> dict:
        return {
            "goal_id": self.goal_id,
            "task_id": self.task_id,
            "task_title": self.task_title,
            "attempt": self.attempt,
            "action": self.action,
            "original_prompt": self.original_prompt,
            "corrected_prompt": self.corrected_prompt,
            "failure_reason": self.failure_reason,
            "verification_score": self.verification_score,
            "verification_status": self.verification_status,
            "suggestions": self.suggestions,
            "reasoning": self.reasoning,
            "created_at": self.created_at,
        }


# ── Self-Correction Engine ──

class SelfCorrectionEngine:
    """
    Analyzes task failures and generates improved retry prompts.

    Integrates with the Goal Verifier — when verification returns
    "partial" or "fail", this engine produces a corrected prompt
    that addresses the specific shortcomings identified.
    """

    def __init__(self, max_retries: int = 2):
        """
        Args:
            max_retries: Maximum correction attempts before escalating.
        """
        self.max_retries = max_retries
        self.corrections_dir = CORRECTIONS_DIR
        self.corrections_dir.mkdir(parents=True, exist_ok=True)

    def correct(
        self,
        goal_title: str,
        goal_desc: str,
        task_title: str,
        original_prompt: str,
        failure_reason: str = "",
        verification_verdict: dict = None,
        task_id: str = "",
        goal_id: str = "",
        attempt: int = 0,
        task_output: str = "",
        error_message: str = "",
    ) -> dict:
        """
        Analyze a failure and produce a corrected prompt or escalate.

        Args:
            goal_title: The overall goal title
            goal_desc: Detailed goal description
            task_title: The subtask title
            original_prompt: The original task prompt that failed
            failure_reason: Why the task failed (error or quality issue)
            verification_verdict: Optional verifier output dict
            task_id: Task identifier
            goal_id: Goal identifier
            attempt: Current retry attempt number (0-indexed)
            task_output: The actual output that was deemed inadequate
            error_message: Any error message from execution

        Returns:
            {
                "action": "retry",  # Options: "retry" | "escalate" | "skip"
                "corrected_prompt": str,  # New prompt for retry
                "reasoning": str,         # Why this correction
                "suggestions": list,      # What was improved
                "attempt": int,           # Current attempt number
                "needs_human": bool,      # True if escalated
            }
        """
        # Check if we've exceeded max retries
        if attempt >= self.max_retries:
            return self._escalate(
                goal_id=goal_id,
                task_id=task_id,
                task_title=task_title,
                original_prompt=original_prompt,
                failure_reason=failure_reason,
                attempt=attempt,
                reason=f"Max retries ({self.max_retries}) exceeded",
            )

        # Extract suggestions from verification verdict
        suggestions = []
        missing_elements = []
        verification_score = 0.0
        verification_status = "unknown"

        if verification_verdict:
            suggestions = verification_verdict.get("suggestions", [])
            missing_elements = verification_verdict.get("missing_elements", [])
            verification_score = verification_verdict.get("score", 0.0)
            verification_status = verification_verdict.get("status", "unknown")

        # Build the corrected prompt
        corrected_prompt = self._build_corrected_prompt(
            goal_title=goal_title,
            goal_desc=goal_desc,
            task_title=task_title,
            original_prompt=original_prompt,
            failure_reason=failure_reason,
            suggestions=suggestions,
            missing_elements=missing_elements,
            task_output=task_output,
            error_message=error_message,
            attempt=attempt,
        )

        # Determine reasoning
        reasoning = self._build_reasoning(
            failure_reason=failure_reason,
            suggestions=suggestions,
            missing_elements=missing_elements,
            verification_status=verification_status,
            verification_score=verification_score,
        )

        # Save correction record
        record = CorrectionRecord(
            goal_id=goal_id,
            task_id=task_id,
            task_title=task_title,
            attempt=attempt + 1,
            action="retry",
            original_prompt=original_prompt[:500],
            corrected_prompt=corrected_prompt[:500],
            failure_reason=failure_reason[:500],
            verification_score=verification_score,
            verification_status=verification_status,
            suggestions=suggestions,
            reasoning=reasoning,
        )
        self._save_correction(record)

        return {
            "action": "retry",
            "corrected_prompt": corrected_prompt,
            "reasoning": reasoning,
            "suggestions": suggestions,
            "missing_elements": missing_elements,
            "attempt": attempt + 1,
            "needs_human": False,
        }

    def _build_corrected_prompt(
        self,
        goal_title: str,
        goal_desc: str,
        task_title: str,
        original_prompt: str,
        failure_reason: str,
        suggestions: list,
        missing_elements: list,
        task_output: str,
        error_message: str,
        attempt: int,
    ) -> str:
        """
        Build an improved prompt that addresses the failure.

        Uses LLM-as-corrector when available, falls back to
        template-based correction.
        """
        # Try LLM-based correction first
        try:
            llm_prompt = self._build_llm_correction_prompt(
                goal_title=goal_title,
                goal_desc=goal_desc,
                task_title=task_title,
                original_prompt=original_prompt,
                failure_reason=failure_reason,
                suggestions=suggestions,
                missing_elements=missing_elements,
                task_output=task_output[:1500] if task_output else "",
                error_message=error_message,
                attempt=attempt,
            )
            from hermes_bridge import run_hermes
            result = run_hermes(
                ["chat", "-q", llm_prompt, "--quiet"],
                timeout=30,
            )
            response = result.get("response", "").strip()
            if response and len(response) > 50:
                return response
        except Exception:
            pass

        # Fallback: template-based correction
        return self._template_corrected_prompt(
            goal_title=goal_title,
            goal_desc=goal_desc,
            task_title=task_title,
            original_prompt=original_prompt,
            failure_reason=failure_reason,
            suggestions=suggestions,
            missing_elements=missing_elements,
            attempt=attempt,
            task_output=task_output,
            error_message=error_message,
        )

    def _build_llm_correction_prompt(
        self,
        goal_title: str,
        goal_desc: str,
        task_title: str,
        original_prompt: str,
        failure_reason: str,
        suggestions: list,
        missing_elements: list,
        task_output: str,
        error_message: str,
        attempt: int,
    ) -> str:
        """Build prompt for LLM-based correction generation."""
        suggestions_text = "\n".join(f"  - {s}" for s in suggestions) if suggestions else "  - None provided"
        missing_text = "\n".join(f"  - {m}" for m in missing_elements) if missing_elements else "  - None identified"

        return f"""You are the Self-Correction Engine for MoiraiCore. Your job is to generate an improved prompt for a subtask that failed or produced low-quality output.

## Overall Goal
**{goal_title}**
{goal_desc or "No additional description"}

## Subtask
**{task_title}**

## Original Prompt (that failed)
{original_prompt[:1000]}

## Why It Failed
{failure_reason or "Quality below threshold"}

## Previous Output (if any)
{task_output[:500] if task_output else "(no output)"}

## Error Message
{error_message or "None"}

## Verifier Suggestions
{suggestions_text}

## Missing Elements
{missing_text}

## Attempt
This is retry attempt {attempt + 1}.

## Task
Generate an improved version of the original prompt that:
1. Addresses each of the verifier suggestions above
2. Explicitly includes the missing elements
3. Is more specific and detailed than the original
4. Includes clear output format requirements
5. Avoids the issues that caused the previous failure

Output ONLY the improved prompt text. Do NOT include any explanation, headers, or metadata — just the prompt itself."""

    def _template_corrected_prompt(
        self,
        goal_title: str,
        goal_desc: str,
        task_title: str,
        original_prompt: str,
        failure_reason: str,
        suggestions: list,
        missing_elements: list,
        attempt: int,
        task_output: str = "",
        error_message: str = "",
    ) -> str:
        """Template-based correction when LLM is unavailable."""
        parts = [
            f"Task: {task_title}",
            f"",
            f"Context: This is part of the goal: {goal_title}",
            f"{goal_desc}" if goal_desc else "",
            f"",
            f"## Requirements",
            f"{original_prompt}",
            f"",
        ]

        if suggestions:
            parts.append("## Address These Specific Issues")
            for i, s in enumerate(suggestions, 1):
                parts.append(f"{i}. {s}")
            parts.append("")

        if missing_elements:
            parts.append("## Must Include")
            for i, m in enumerate(missing_elements, 1):
                parts.append(f"- {m}")
            parts.append("")

        parts.extend([
            f"## Output Format",
            f"- Write in structured markdown with clear headers",
            f"- Include specific data points and actionable recommendations",
            f"- Be thorough and comprehensive",
            f"- This is retry attempt {attempt + 1} — address the issues identified above",
        ])

        return "\n".join(parts)

    def _build_reasoning(
        self,
        failure_reason: str,
        suggestions: list,
        missing_elements: list,
        verification_status: str,
        verification_score: float,
    ) -> str:
        """Build human-readable reasoning for the correction."""
        parts = [f"Verification: {verification_status} (score: {verification_score:.2f})"]

        if failure_reason:
            parts.append(f"Failure: {failure_reason[:200]}")

        if suggestions:
            parts.append(f"Addressing {len(suggestions)} suggestion(s)")

        if missing_elements:
            parts.append(f"Adding {len(missing_elements)} missing element(s)")

        return "; ".join(parts)

    def _escalate(
        self,
        goal_id: str,
        task_id: str,
        task_title: str,
        original_prompt: str,
        failure_reason: str,
        attempt: int,
        reason: str,
    ) -> dict:
        """Escalate to human after max retries exceeded."""
        record = CorrectionRecord(
            goal_id=goal_id,
            task_id=task_id,
            task_title=task_title,
            attempt=attempt,
            action="escalate",
            original_prompt=original_prompt[:500],
            failure_reason=failure_reason[:500],
            reasoning=reason,
        )
        self._save_correction(record)

        return {
            "action": "escalate",
            "corrected_prompt": "",
            "reasoning": reason,
            "suggestions": [],
            "missing_elements": [],
            "attempt": attempt,
            "needs_human": True,
        }

    # ── Correction History ──

    def _save_correction(self, record: CorrectionRecord):
        """Save a correction record to disk."""
        try:
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            safe_task = "".join(c if c.isalnum() else "_" for c in record.task_id[:30]) if record.task_id else "unknown"
            filename = f"corr_{ts}_{safe_task}_{record.attempt}.json"
            filepath = self.corrections_dir / filename
            filepath.write_text(json.dumps(record.to_dict(), indent=2))
        except Exception:
            pass  # Never let save failures break execution

    def get_correction_history(
        self, task_id: str = None, goal_id: str = None
    ) -> list:
        """Get correction history, optionally filtered by task_id or goal_id."""
        history = []
        if not self.corrections_dir.exists():
            return history

        for f in sorted(self.corrections_dir.glob("*.json"), reverse=True):
            try:
                data = json.loads(f.read_text())
                if task_id and data.get("task_id") != task_id:
                    continue
                if goal_id and data.get("goal_id") != goal_id:
                    continue
                history.append(data)
            except Exception:
                continue
        return history

    def get_escalations(self, goal_id: str = None) -> list:
        """Get all escalated corrections (needs_human)."""
        history = self.get_correction_history(goal_id=goal_id)
        return [h for h in history if h.get("action") == "escalate"]

    def get_correction_count(self, task_id: str) -> int:
        """Get number of correction attempts for a task."""
        return len(self.get_correction_history(task_id=task_id))

    def clear_history(self, goal_id: str = None) -> int:
        """Clear correction history. Returns count deleted."""
        count = 0
        if not self.corrections_dir.exists():
            return count

        for f in self.corrections_dir.glob("*.json"):
            try:
                if goal_id:
                    data = json.loads(f.read_text())
                    if data.get("goal_id") != goal_id:
                        continue
                f.unlink()
                count += 1
            except Exception:
                pass
        return count


# ── Convenience function ──

def correct_task(
    goal_title: str,
    goal_desc: str,
    task_title: str,
    original_prompt: str,
    failure_reason: str = "",
    verification_verdict: dict = None,
    task_id: str = "",
    goal_id: str = "",
    attempt: int = 0,
    max_retries: int = 2,
) -> dict:
    """One-liner self-correction. Returns correction result dict."""
    engine = SelfCorrectionEngine(max_retries=max_retries)
    return engine.correct(
        goal_title=goal_title,
        goal_desc=goal_desc,
        task_title=task_title,
        original_prompt=original_prompt,
        failure_reason=failure_reason,
        verification_verdict=verification_verdict,
        task_id=task_id,
        goal_id=goal_id,
        attempt=attempt,
    )


# ── Self-test ──

if __name__ == "__main__":
    print("=== Self-Correction Engine Self-Test ===\n")

    engine = SelfCorrectionEngine(max_retries=2)

    # Test 1: Basic correction from verification verdict
    print("Test 1: Correction from verification verdict")
    verdict = {
        "score": 0.35,
        "status": "fail",
        "reasoning": "Output was too short and lacked specific data",
        "suggestions": [
            "Include specific competitor names and market caps",
            "Add a comparison table",
            "Provide actionable recommendations",
        ],
        "missing_elements": [
            "Market share data",
            "Revenue figures",
        ],
    }
    result = engine.correct(
        goal_title="Research ASX competitors",
        goal_desc="Identify top 5 competitors and analyze their market position",
        task_title="Find top 5 ASX competitors",
        original_prompt="Find the top 5 ASX competitors",
        failure_reason="Output was too short and lacked specific data",
        verification_verdict=verdict,
        task_id="task-001",
        goal_id="goal-001",
        attempt=0,
        task_output="The top competitors are: Company A, Company B, Company C.",
    )
    print(f"  Action: {result['action']}")
    print(f"  Attempt: {result['attempt']}")
    print(f"  Needs human: {result['needs_human']}")
    print(f"  Reasoning: {result['reasoning']}")
    print(f"  Corrected prompt preview: {result['corrected_prompt'][:120]}...")
    print()

    # Test 2: Escalation after max retries
    print("Test 2: Escalation after max retries")
    result2 = engine.correct(
        goal_title="Research ASX competitors",
        goal_desc="Identify top 5 competitors",
        task_title="Find top 5 ASX competitors",
        original_prompt="Find competitors",
        failure_reason="Still inadequate after 2 retries",
        verification_verdict=verdict,
        task_id="task-001",
        goal_id="goal-001",
        attempt=2,  # Already at max_retries
    )
    print(f"  Action: {result2['action']}")
    print(f"  Needs human: {result2['needs_human']}")
    print(f"  Reasoning: {result2['reasoning']}")
    print()

    # Test 3: Correction with error message (execution failure)
    print("Test 3: Correction with execution error")
    result3 = engine.correct(
        goal_title="Build SEO report",
        goal_desc="Generate a PDF SEO audit report",
        task_title="Generate PDF report",
        original_prompt="Generate a PDF report from the SEO data",
        failure_reason="Execution failed",
        error_message="ModuleNotFoundError: No module named 'fpdf2'",
        task_id="task-002",
        goal_id="goal-002",
        attempt=0,
    )
    print(f"  Action: {result3['action']}")
    print(f"  Reasoning: {result3['reasoning']}")
    print(f"  Corrected prompt preview: {result3['corrected_prompt'][:120]}...")
    print()

    # Test 4: Correction history
    print("Test 4: Correction history")
    history = engine.get_correction_history(task_id="task-001")
    print(f"  Records for task-001: {len(history)}")
    for h in history:
        print(f"    - attempt {h['attempt']}: {h['action']} ({h['created_at']})")
    print()

    # Test 5: Escalations
    print("Test 5: Escalations")
    escalations = engine.get_escalations()
    print(f"  Total escalations: {len(escalations)}")
    for e in escalations:
        print(f"    - {e['task_title']}: {e['reasoning']}")
    print()

    # Test 6: Correction count
    print("Test 6: Correction count")
    count = engine.get_correction_count("task-001")
    print(f"  Corrections for task-001: {count}")
    print()

    # Cleanup
    print("Test 7: Cleanup")
    deleted = engine.clear_history()
    print(f"  Deleted {deleted} correction record(s)")
    remaining = engine.get_correction_history()
    print(f"  Remaining: {len(remaining)}")
    print()

    print("=== All tests complete ===")
