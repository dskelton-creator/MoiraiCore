#!/usr/bin/env python3
"""
MoiraiCore — Goal Verifier
==========================
Evaluates whether a subtask's output actually fulfills the goal.

Uses LLM-as-judge pattern:
  1. Feed goal description + subtask output to verifier
  2. Get structured verdict: {score, reasoning, suggestions}
  3. If score < threshold → retry with adjusted prompt
  4. If score >= threshold → mark complete, save to checkpoint

Verdict format:
{
  "score": 0.0-1.0,
  "status": "pass" | "partial" | "fail",
  "reasoning": "Why this score",
  "suggestions": ["Specific improvements for retry"],
  "missing_elements": ["What's still needed"]
}

Usage:
    from goal_verifier import GoalVerifier
    verifier = GoalVerifier(quality_threshold=0.6)
    verdict = verifier.verify(goal_title, goal_desc, task_title, task_output)
    if verdict["status"] == "pass":
        print("Task output is good!")
"""

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

AGENT_OS_ROOT = Path(os.environ.get("AGENT_OS_ROOT", str(Path(__file__).resolve().parents[1])))
CONFIG_DIR = AGENT_OS_ROOT / "config"
SCRIPTS_DIR = AGENT_OS_ROOT / "scripts"
VERIFICATIONS_DIR = CONFIG_DIR / "verifications"
LOOP_CONFIG_FILE = CONFIG_DIR / "loop_config.json"

sys.path.insert(0, str(SCRIPTS_DIR))

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


# ── Default Configuration ──

DEFAULT_LOOP_CONFIG = {
    "enabled": True,
    "defaults": {
        "max_iterations": 10,
        "max_tokens_usd": 5.00,
        "max_duration_seconds": 3600,
        "max_retries_per_task": 2,
        "quality_threshold": 0.6,
        "convergence_rounds": 3,
    },
    "auto_resume": True,
    "escalation": {
        "notify_on_guard_trigger": True,
        "notify_on_max_retries": True,
        "notify_on_timeout": True,
    },
}


def load_loop_config() -> dict:
    """Load loop configuration from config/loop_config.json."""
    if LOOP_CONFIG_FILE.exists():
        try:
            data = json.loads(LOOP_CONFIG_FILE.read_text())
            # Merge with defaults for any missing keys
            merged = dict(DEFAULT_LOOP_CONFIG)
            merged.update(data)
            # Deep merge defaults
            if "defaults" in data:
                merged["defaults"] = dict(DEFAULT_LOOP_CONFIG["defaults"])
                merged["defaults"].update(data["defaults"])
            return merged
        except Exception:
            pass
    return dict(DEFAULT_LOOP_CONFIG)


def ensure_loop_config():
    """Create default loop_config.json if it doesn't exist."""
    if not LOOP_CONFIG_FILE.exists():
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        LOOP_CONFIG_FILE.write_text(json.dumps(DEFAULT_LOOP_CONFIG, indent=2))


# ── Verification Verdict ──

class VerificationVerdict:
    """Structured result from goal verification."""

    def __init__(
        self,
        score: float = 0.0,
        status: str = "fail",
        reasoning: str = "",
        suggestions: list = None,
        missing_elements: list = None,
        task_id: str = "",
        task_title: str = "",
    ):
        self.score = max(0.0, min(1.0, score))
        self.status = status  # "pass" | "partial" | "fail"
        self.reasoning = reasoning
        self.suggestions = suggestions or []
        self.missing_elements = missing_elements or []
        self.task_id = task_id
        self.task_title = task_title
        self.verified_at = datetime.now().isoformat()
        self._cache_key = ""

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "status": self.status,
            "reasoning": self.reasoning,
            "suggestions": self.suggestions,
            "missing_elements": self.missing_elements,
            "task_id": self.task_id,
            "task_title": self.task_title,
            "verified_at": self.verified_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "VerificationVerdict":
        v = cls()
        v.score = data.get("score", 0.0)
        v.status = data.get("status", "fail")
        v.reasoning = data.get("reasoning", "")
        v.suggestions = data.get("suggestions", [])
        v.missing_elements = data.get("missing_elements", [])
        v.task_id = data.get("task_id", "")
        v.task_title = data.get("task_title", "")
        v.verified_at = data.get("verified_at", datetime.now().isoformat())
        return v

    def __repr__(self):
        return f"Verdict({self.status}, score={self.score:.2f}, task={self.task_title[:40]})"


# ── Goal Verifier ──

class GoalVerifier:
    """
    LLM-as-judge verifier for subtask outputs.

    Evaluates whether a subtask's output actually contributes to fulfilling
    the overall goal. Returns a structured verdict with score, reasoning,
    and suggestions for improvement.

    Features:
    - Response caching (avoid re-verifying identical output)
    - Markdown code fence stripping
    - Configurable timeout (default 60s for production LLM calls)
    """

    def __init__(self, quality_threshold: float = None, cache_enabled: bool = True):
        """
        Initialize the verifier.

        Args:
            quality_threshold: Minimum score to consider a task "pass".
                             Loaded from loop_config.json if not specified.
            cache_enabled: Enable response caching (default True).
        """
        config = load_loop_config()
        if quality_threshold is not None:
            self.quality_threshold = quality_threshold
        else:
            self.quality_threshold = config.get("defaults", {}).get(
                "quality_threshold", 0.6
            )
        self.verifications_dir = VERIFICATIONS_DIR
        self.verifications_dir.mkdir(parents=True, exist_ok=True)
        self._cache_enabled = cache_enabled
        self._response_cache = {}  # task_id -> VerificationVerdict

    def verify(
        self,
        goal_title: str,
        goal_desc: str,
        task_title: str,
        task_output: str,
        task_id: str = "",
    ) -> VerificationVerdict:
        """
        Verify a subtask's output against the goal.

        Args:
            goal_title: The overall goal title
            goal_desc: Detailed goal description
            task_title: The subtask title
            task_output: The subtask's output text
            task_id: Optional task identifier

        Returns:
            VerificationVerdict with score, status, reasoning, suggestions
        """
        # Check cache first
        if self._cache_enabled and task_id and task_id in self._response_cache:
            cached = self._response_cache[task_id]
            # Only cache if output hasn't changed (use task_id + output hash)
            cache_key = f"{task_id}:{hash(task_output[:200])}"
            if hasattr(cached, '_cache_key') and cached._cache_key == cache_key:
                return cached

        # Build the verification prompt
        prompt = self._build_verification_prompt(
            goal_title, goal_desc, task_title, task_output
        )

        try:
            from hermes_bridge import run_hermes

            result = run_hermes(
                ["chat", "-q", prompt, "--quiet"],
                timeout=60,
            )
            response = result.get("response", "")

            # If LLM returned no usable response, fall through to heuristic
            if not response or not result.get("ok"):
                raise ValueError(f"LLM returned empty/error: {result.get('error', 'unknown')}")

            # Strip markdown code fences if present
            response = response.strip()
            if response.startswith("```"):
                # Remove ```json ... ``` wrapper
                lines = response.split("\n")
                # Remove first line (```json) and last line (```)
                if lines[-1].strip() == "```":
                    lines = lines[1:-1]
                else:
                    lines = lines[1:]
                response = "\n".join(lines).strip()

            # Parse the structured verdict from the response
            verdict = self._parse_verdict(response, task_id, task_title)

        except Exception as e:
            # Fallback: heuristic-based verification
            verdict = self._heuristic_verify(
                goal_title, goal_desc, task_title, task_output, task_id
            )
            verdict.reasoning = f"Heuristic verification (LLM unavailable: {e}). {verdict.reasoning}"

        # Save verification record
        self._save_verification(verdict)

        # ── Feature 7: enforce verification contract before returning ──
        try:
            from pipeline_contracts import enforce as _enf, ContractViolation as _CV
            _enf("verification", {
                "status": getattr(verdict, "status", None),
                "score": getattr(verdict, "score", None),
            }, context={"task_id": task_id, "actor": "goal_verifier"})
        except ImportError:
            pass
        except Exception as _cv_exc:
            # Only act if this is a ContractViolation; otherwise ignore.
            try:
                from pipeline_contracts import ContractViolation as _CV2
                is_cv = isinstance(_cv_exc, _CV2)
            except ImportError:
                is_cv = False
            if is_cv:
                # Verdict structurally invalid (e.g. score out of range). Demote to
                # 'fail' so the pipeline does not advance on a malformed judgement.
                try:
                    verdict.status = "fail"
                    verdict.reasoning = (getattr(verdict, "reasoning", "") or "") + " [contract: invalid verdict demoted to fail]"
                except Exception:
                    pass

        # Cache the result
        if self._cache_enabled and task_id:
            cache_key = f"{task_id}:{hash(task_output[:200])}"
            verdict._cache_key = cache_key
            self._response_cache[task_id] = verdict

        # ── Feature 8: fire verification.completed ──
        _fire("verification.completed", {
            "task_id": getattr(verdict, "task_id", task_id),
            "goal_id": getattr(verdict, "goal_id", ""),
            "status": getattr(verdict, "status", None),
            "score": getattr(verdict, "score", None),
        })

        return verdict

    def clear_cache(self):
        """Clear the response cache."""
        self._response_cache.clear()

    def verify_batch(
        self,
        goal_title: str,
        goal_desc: str,
        tasks: list,
    ) -> list:
        """
        Verify multiple subtask outputs at once.

        Args:
            goal_title: The overall goal title
            goal_desc: Detailed goal description
            tasks: List of dicts with {task_id, title, output}

        Returns:
            List of VerificationVerdict objects
        """
        verdicts = []
        for task in tasks:
            verdict = self.verify(
                goal_title=goal_title,
                goal_desc=goal_desc,
                task_title=task.get("title", ""),
                task_output=task.get("output", ""),
                task_id=task.get("task_id", task.get("id", "")),
            )
            verdicts.append(verdict)
        return verdicts

    def get_verification_history(self, task_id: str = None) -> list:
        """Get verification history, optionally filtered by task_id."""
        history = []
        if not self.verifications_dir.exists():
            return history

        for f in sorted(self.verifications_dir.glob("*.json"), reverse=True):
            try:
                data = json.loads(f.read_text())
                if task_id is None or data.get("task_id") == task_id:
                    history.append(data)
            except Exception:
                continue
        return history

    def get_average_score(self, task_id: str = None) -> float:
        """Get average verification score."""
        history = self.get_verification_history(task_id)
        if not history:
            return 0.0
        scores = [h.get("score", 0.0) for h in history]
        return sum(scores) / len(scores)

    # ── Internal Methods ──

    def _build_verification_prompt(
        self,
        goal_title: str,
        goal_desc: str,
        task_title: str,
        task_output: str,
    ) -> str:
        """Build the LLM-as-judge verification prompt."""
        # Truncate output to avoid token overflow
        truncated_output = task_output[:3000] if task_output else "(no output)"

        return f"""You are the Goal Verifier for MoiraiCore. Your job is to evaluate whether a subtask's output actually contributes to fulfilling the overall goal.

## Overall Goal
**{goal_title}**
{goal_desc or "No additional description"}

## Subtask
**{task_title}**

## Subtask Output
{truncated_output}

## Evaluation Criteria
Rate the output on these dimensions (0.0 to 1.0 each):
1. **Relevance** — Does the output address the subtask goal?
2. **Completeness** — Does it cover the key aspects needed?
3. **Actionability** — Are the findings/outputs concrete and usable?
4. **Quality** — Is the output well-structured and professional?

## Output Format
Respond with ONLY this JSON structure (no other text):
{{
  "score": 0.0-1.0,
  "status": "pass" | "partial" | "fail",
  "reasoning": "Brief explanation of your assessment (2-3 sentences)",
  "suggestions": ["Specific improvement 1", "Specific improvement 2"],
  "missing_elements": ["What's still needed 1", "What's still needed 2"]
}}

Scoring guide:
- 0.8-1.0 = "pass" (excellent, no changes needed)
- 0.5-0.79 = "partial" (good but needs improvement)
- 0.0-0.49 = "fail" (inadequate, significant rework needed)

Output ONLY the JSON, no other text."""

    def _parse_verdict(
        self, response: str, task_id: str, task_title: str
    ) -> VerificationVerdict:
        """Parse the LLM response into a VerificationVerdict."""
        if not response:
            return VerificationVerdict(
                score=0.0,
                status="fail",
                reasoning="Empty response from verifier",
                suggestions=["Retry verification"],
                missing_elements=["Verification output"],
                task_id=task_id,
                task_title=task_title,
            )

        # Try to extract JSON from the response
        try:
            # Look for JSON block
            m = re.search(r"\{[\s\S]*\}", response)
            if m:
                data = json.loads(m.group(0))
                score = float(data.get("score", 0.0))
                status = data.get("status", "fail")
                # Validate status
                if status not in ("pass", "partial", "fail"):
                    # Derive from score
                    if score >= 0.8:
                        status = "pass"
                    elif score >= 0.5:
                        status = "partial"
                    else:
                        status = "fail"

                return VerificationVerdict(
                    score=score,
                    status=status,
                    reasoning=data.get("reasoning", "No reasoning provided"),
                    suggestions=data.get("suggestions", []),
                    missing_elements=data.get("missing_elements", []),
                    task_id=task_id,
                    task_title=task_title,
                )
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

        # Fallback: try to extract a score from the text
        score = self._extract_score_from_text(response)
        status = "pass" if score >= 0.8 else "partial" if score >= 0.5 else "fail"

        return VerificationVerdict(
            score=score,
            status=status,
            reasoning=response[:500],
            suggestions=["Could not parse structured verdict"],
            missing_elements=[],
            task_id=task_id,
            task_title=task_title,
        )

    def _extract_score_from_text(self, text: str) -> float:
        """Try to extract a numeric score from unstructured text."""
        # Look for patterns like "score: 0.7", "0.7/1.0", "70%", etc.
        patterns = [
            r"score[:\s]+(\d+\.?\d*)",
            r"(\d+\.?\d*)\s*/\s*1\.0",
            r"(\d+\.?\d*)\s*/\s*10",
            r"(\d{1,3})%",
        ]
        for pattern in patterns:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                val = float(m.group(1))
                if "%" in pattern:
                    return val / 100.0
                elif "/10" in pattern:
                    return val / 10.0
                elif val > 1.0:
                    return val / 10.0
                return val
        return 0.0

    def _heuristic_verify(
        self,
        goal_title: str,
        goal_desc: str,
        task_title: str,
        task_output: str,
        task_id: str,
    ) -> VerificationVerdict:
        """
        Fallback heuristic verification when LLM is unavailable.
        Uses simple metrics: output length, keyword overlap, structure.
        """
        suggestions = []
        missing = []

        # Check 1: Output exists and has substance
        if not task_output or len(task_output.strip()) < 50:
            return VerificationVerdict(
                score=0.1,
                status="fail",
                reasoning="Output is too short or empty",
                suggestions=["Provide a more detailed output", "Include specific findings"],
                missing_elements=["Substantive content"],
                task_id=task_id,
                task_title=task_title,
            )

        score = 0.5  # Start at partial

        # Check 2: Output length (longer = more effort, up to a point)
        output_len = len(task_output.strip())
        if output_len > 500:
            score += 0.1
        if output_len > 1000:
            score += 0.1
        if output_len > 3000:
            score -= 0.05  # Too long might mean unfocused

        # Check 3: Keyword overlap with goal
        goal_words = set((goal_title + " " + goal_desc).lower().split())
        output_words = set(task_output.lower().split())
        if goal_words:
            overlap = len(goal_words & output_words) / len(goal_words)
            score += overlap * 0.15

        # Check 4: Structure (has headers, lists, etc.)
        has_headers = bool(re.search(r"^#{1,3}\s", task_output, re.MULTILINE))
        has_lists = bool(re.search(r"^[-*]\s|^\d+\.\s", task_output, re.MULTILINE))
        if has_headers:
            score += 0.05
        if has_lists:
            score += 0.05

        # Check 5: Has actionable content
        action_words = ["recommend", "should", "must", "action", "implement",
                        "create", "build", "deploy", "fix", "update", "add"]
        has_actions = any(w in task_output.lower() for w in action_words)
        if has_actions:
            score += 0.05

        # Clamp score
        score = max(0.0, min(1.0, score))

        # Determine status
        if score >= 0.8:
            status = "pass"
        elif score >= 0.5:
            status = "partial"
            suggestions.append("Add more specific details and recommendations")
        else:
            status = "fail"
            suggestions.extend([
                "Expand the output with more detail",
                "Include specific findings and recommendations",
                "Structure the output with headers and bullet points",
            ])

        if not has_headers:
            missing.append("Structured headers")
        if not has_lists:
            missing.append("Bullet points or numbered lists")

        return VerificationVerdict(
            score=round(score, 2),
            status=status,
            reasoning=f"Heuristic score: {score:.2f} (length={output_len}, structure={'yes' if has_headers else 'no'})",
            suggestions=suggestions,
            missing_elements=missing,
            task_id=task_id,
            task_title=task_title,
        )

    def _save_verification(self, verdict: VerificationVerdict):
        """Save verification record to disk."""
        try:
            self.verifications_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            safe_task = "".join(c if c.isalnum() else "_" for c in verdict.task_title[:30])
            filename = f"verify_{ts}_{safe_task}.json"
            filepath = self.verifications_dir / filename
            filepath.write_text(json.dumps(verdict.to_dict(), indent=2))
        except Exception:
            pass  # Never let save failures break verification


# ── Convenience Functions ──

_verifier_instance = None


def get_verifier(quality_threshold: float = None) -> GoalVerifier:
    """Get or create the global verifier instance."""
    global _verifier_instance
    if _verifier_instance is None:
        _verifier_instance = GoalVerifier(quality_threshold=quality_threshold)
    return _verifier_instance


def verify_task(
    goal_title: str,
    goal_desc: str,
    task_title: str,
    task_output: str,
    task_id: str = "",
    quality_threshold: float = None,
) -> dict:
    """Convenience function to verify a single task."""
    verifier = get_verifier(quality_threshold=quality_threshold)
    verdict = verifier.verify(goal_title, goal_desc, task_title, task_output, task_id)
    return verdict.to_dict()


# ── Self-test ──

if __name__ == "__main__":
    print("=" * 60)
    print("Goal Verifier — Self Test")
    print("=" * 60)

    # Ensure config exists
    ensure_loop_config()
    print(f"✅ Config: {LOOP_CONFIG_FILE}")

    # Test 1: Heuristic verification — good output
    print("\n--- Test 1: Heuristic verify (good output) ---")
    verifier = GoalVerifier(quality_threshold=0.6)
    good_output = """
# Research Findings: ASX Competitors

## Executive Summary
The ASX competitor landscape includes three major players...

## Key Findings
1. Company A has 40% market share
2. Company B is growing at 15% YoY
3. Company C has the strongest brand

## Recommendations
- **Action 1**: Focus on differentiation
- **Action 2**: Invest in digital channels
- **Action 3**: Monitor Company B's growth

## Next Steps
Implement the above recommendations by Q3.
"""
    v1 = verifier.verify(
        goal_title="Research ASX competitors and write a report",
        goal_desc="Analyze the competitive landscape for ASX-listed companies",
        task_title="Research: ASX Competitor Analysis",
        task_output=good_output,
        task_id="task-001",
    )
    print(f"  Score: {v1.score:.2f}")
    print(f"  Status: {v1.status}")
    print(f"  Reasoning: {v1.reasoning[:100]}...")
    assert v1.status in ("pass", "partial"), f"Expected pass/partial, got {v1.status}"
    print("  ✅ PASS")

    # Test 2: Heuristic verification — poor output
    print("\n--- Test 2: Heuristic verify (poor output) ---")
    v2 = verifier.verify(
        goal_title="Research ASX competitors and write a report",
        goal_desc="Analyze the competitive landscape",
        task_title="Research: ASX Competitor Analysis",
        task_output="ASX is good. Many companies.",
        task_id="task-002",
    )
    print(f"  Score: {v2.score:.2f}")
    print(f"  Status: {v2.status}")
    assert v2.status == "fail", f"Expected fail, got {v2.status}"
    print("  ✅ PASS")

    # Test 3: Heuristic verification — empty output
    print("\n--- Test 3: Heuristic verify (empty output) ---")
    v3 = verifier.verify(
        goal_title="Build a website",
        goal_desc="Create a responsive website",
        task_title="Implement homepage",
        task_output="",
        task_id="task-003",
    )
    print(f"  Score: {v3.score:.2f}")
    print(f"  Status: {v3.status}")
    assert v3.status == "fail", f"Expected fail, got {v3.status}"
    print("  ✅ PASS")

    # Test 4: Batch verification
    print("\n--- Test 4: Batch verification ---")
    tasks = [
        {
            "task_id": "t1",
            "title": "Research phase",
            "output": "# Research\n\n## Findings\n1. Data point A\n2. Data point B\n\n## Recommendations\n- Action item 1\n- Action item 2",
        },
        {
            "task_id": "t2",
            "title": "Writing phase",
            "output": "ok done",
        },
    ]
    verdicts = verifier.verify_batch(
        goal_title="Write a research report",
        goal_desc="Research and write a comprehensive report",
        tasks=tasks,
    )
    print(f"  Task 1: {verdicts[0].status} ({verdicts[0].score:.2f})")
    print(f"  Task 2: {verdicts[1].status} ({verdicts[1].score:.2f})")
    assert len(verdicts) == 2
    print("  ✅ PASS")

    # Test 5: Config loading
    print("\n--- Test 5: Config loading ---")
    config = load_loop_config()
    assert "defaults" in config
    assert "quality_threshold" in config["defaults"]
    print(f"  Quality threshold: {config['defaults']['quality_threshold']}")
    print("  ✅ PASS")

    # Test 6: Verdict serialization
    print("\n--- Test 6: Verdict serialization ---")
    verdict_dict = v1.to_dict()
    v1_restored = VerificationVerdict.from_dict(verdict_dict)
    assert v1_restored.score == v1.score
    assert v1_restored.status == v1.status
    print(f"  Round-trip: score={v1_restored.score}, status={v1_restored.status}")
    print("  ✅ PASS")

    # Test 7: Verification history
    print("\n--- Test 7: Verification history ---")
    history = verifier.get_verification_history()
    print(f"  History entries: {len(history)}")
    assert len(history) >= 3  # At least 3 from tests above
    print("  ✅ PASS")

    print("\n" + "=" * 60)
    print("All tests passed! ✅")
    print("=" * 60)
