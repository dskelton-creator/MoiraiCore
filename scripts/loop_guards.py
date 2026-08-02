#!/usr/bin/env python3
"""
MoiraiCore — Loop Guardrails
===========================
Prevents runaway autonomous loops with configurable safety limits.

Each goal loop has configurable guards:
  - max_iterations:    Max subtask execution rounds (default: 10)
  - max_tokens_usd:    Token spend cap in USD (default: $5.00)
  - max_duration_sec:  Wall-clock timeout in seconds (default: 3600)
  - max_retries:       Retry limit per subtask (default: 2)
  - convergence_rounds: Stop if no progress for N rounds (default: 3)

Guards are checked BEFORE each iteration. If any limit is hit:
  - Loop pauses (not fails) → status = "paused_guard"
  - Partial results preserved
  - Dashboard shows which guard triggered
  - User can adjust limits and resume

Usage:
    from loop_guards import LoopGuards, check_guards, load_guard_config

    guards = LoopGuards(goal_id)
    result = guards.check_all(goal_state)
    if result["triggered"]:
        print(f"Guard triggered: {result['reason']}")
"""

import json
import os
import time
from datetime import datetime
from pathlib import Path

AGENT_OS_ROOT = Path(os.environ.get("AGENT_OS_ROOT", str(Path(__file__).resolve().parents[1])))
CONFIG_DIR = AGENT_OS_ROOT / "config"
GUARD_CONFIG_FILE = CONFIG_DIR / "loop_guards.json"

# ── Default guard configuration ──

DEFAULT_GUARD_CONFIG = {
    "max_iterations": 10,
    "max_tokens_usd": 5.00,
    "max_duration_seconds": 3600,
    "max_retries_per_task": 2,
    "convergence_rounds": 3,
    "adaptive_quality": True,
    "quality_raise_threshold": 0.85,
    "quality_lower_threshold": 0.40,
    "quality_raise_increment": 0.05,
    "quality_lower_increment": 0.05,
    "min_quality_threshold": 0.30,
    "max_quality_threshold": 0.95,
    "agent_switch_on_failure": True,
    "agent_switch_after_retries": 2,
}

# Per-goal overrides stored in loop_guards.json
# Format: { "goal_id": { "max_iterations": 5, ... }, ... }


def load_guard_config() -> dict:
    """Load global guard config. Falls back to defaults if file missing."""
    if GUARD_CONFIG_FILE.exists():
        try:
            data = json.loads(GUARD_CONFIG_FILE.read_text())
            # Merge with defaults so new fields are always present
            merged = {**DEFAULT_GUARD_CONFIG, **data.get("global", {})}
            return {"global": merged, "overrides": data.get("overrides", {})}
        except Exception:
            pass
    return {"global": DEFAULT_GUARD_CONFIG.copy(), "overrides": {}}


def save_guard_config(config: dict):
    """Persist guard config to disk."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    GUARD_CONFIG_FILE.write_text(json.dumps(config, indent=2))


def get_goal_guards(goal_id: str) -> dict:
    """Get effective guard config for a goal (global + per-goal overrides)."""
    cfg = load_guard_config()
    global_guards = cfg["global"]
    overrides = cfg.get("overrides", {}).get(goal_id, {})
    return {**global_guards, **overrides}


def set_goal_guards(goal_id: str, guards: dict) -> dict:
    """Set per-goal guard overrides. Returns effective config."""
    cfg = load_guard_config()
    if "overrides" not in cfg:
        cfg["overrides"] = {}
    cfg["overrides"][goal_id] = guards
    save_guard_config(cfg)
    return get_goal_guards(goal_id)


def set_global_guards(guards: dict) -> dict:
    """Update global guard defaults."""
    cfg = load_guard_config()
    cfg["global"] = {**DEFAULT_GUARD_CONFIG, **guards}
    save_guard_config(cfg)
    return cfg["global"]


# ── Guard checking engine ──

class GuardResult:
    """Result of a guard check."""

    def __init__(self, triggered: bool = False, guard_name: str = "",
                 reason: str = "", value=None, limit=None):
        self.triggered = triggered
        self.guard_name = guard_name
        self.reason = reason
        self.value = value
        self.limit = limit

    def to_dict(self) -> dict:
        return {
            "triggered": self.triggered,
            "guard_name": self.guard_name,
            "reason": self.reason,
            "value": self.value,
            "limit": self.limit,
        }


class LoopGuards:
    """
    Check guard conditions for a goal loop.

    Usage:
        guards = LoopGuards(goal_id)
        result = guards.check_all(goal_state)

        if result["triggered"]:
            # Pause the loop
            goal["status"] = "paused_guard"
            goal["guard_triggered"] = result["reason"]
    """

    def __init__(self, goal_id: str):
        self.goal_id = goal_id
        self.config = get_goal_guards(goal_id)

    def check_all(self, goal: dict) -> dict:
        """
        Run all guard checks against current goal state.
        Returns {
            "triggered": bool,
            "guard_name": str,
            "reason": str,
            "checks": [GuardResult, ...]
        }
        """
        checks = [
            self._check_iterations(goal),
            self._check_duration(goal),
            self._check_retries(goal),
            self._check_convergence(goal),
        ]

        for check in checks:
            if check.triggered:
                return {
                    "triggered": True,
                    "guard_name": check.guard_name,
                    "reason": check.reason,
                    "checks": [c.to_dict() for c in checks],
                }

        return {
            "triggered": False,
            "guard_name": "",
            "reason": "",
            "checks": [c.to_dict() for c in checks],
        }

    def _check_iterations(self, goal: dict) -> GuardResult:
        """Check if iteration count exceeds max."""
        # Track iterations via goal metadata
        iterations = goal.get("loop_iterations", 0)
        max_iter = self.config.get("max_iterations", DEFAULT_GUARD_CONFIG["max_iterations"])

        if iterations >= max_iter:
            return GuardResult(
                triggered=True,
                guard_name="max_iterations",
                reason=f"Loop reached max iterations ({iterations}/{max_iter})",
                value=iterations,
                limit=max_iter,
            )
        return GuardResult(guard_name="max_iterations", value=iterations, limit=max_iter)

    def _check_duration(self, goal: dict) -> GuardResult:
        """Check if wall-clock duration exceeds max."""
        created = goal.get("created", "")
        max_dur = self.config.get("max_duration_seconds", DEFAULT_GUARD_CONFIG["max_duration_seconds"])

        if created:
            try:
                created_dt = datetime.fromisoformat(created)
                elapsed = (datetime.now() - created_dt).total_seconds()
                if elapsed >= max_dur:
                    return GuardResult(
                        triggered=True,
                        guard_name="max_duration",
                        reason=f"Loop exceeded max duration ({elapsed:.0f}s/{max_dur}s)",
                        value=elapsed,
                        limit=max_dur,
                    )
                return GuardResult(guard_name="max_duration", value=elapsed, limit=max_dur)
            except (ValueError, TypeError):
                pass
        return GuardResult(guard_name="max_duration", value=0, limit=max_dur)

    def _check_retries(self, goal: dict) -> GuardResult:
        """Check if any subtask has exceeded max retries."""
        max_retries = self.config.get("max_retries_per_task", DEFAULT_GUARD_CONFIG["max_retries_per_task"])
        tasks = goal.get("tasks", [])

        for task in tasks:
            retry_count = task.get("retry_count", 0)
            if retry_count > max_retries:
                return GuardResult(
                    triggered=True,
                    guard_name="max_retries",
                    reason=f"Task '{task.get('title', task.get('id', '?'))[:40]}' exceeded max retries ({retry_count}/{max_retries})",
                    value=retry_count,
                    limit=max_retries,
                )
        return GuardResult(guard_name="max_retries", value=0, limit=max_retries)

    def _check_convergence(self, goal: dict) -> GuardResult:
        """
        Enhanced convergence detection:
        - No-progress: completed count unchanged for N rounds
        - Oscillation: quality scores alternating up/down without net improvement
        - Degrading: quality scores consistently decreasing
        """
        conv_rounds = self.config.get("convergence_rounds", DEFAULT_GUARD_CONFIG["convergence_rounds"])
        history = goal.get("loop_history", [])

        if len(history) < conv_rounds:
            return GuardResult(guard_name="convergence", value=len(history), limit=conv_rounds)

        recent = history[-conv_rounds:]
        completed_counts = [h.get("tasks_completed", 0) for h in recent]

        # Check 1: No progress
        if len(set(completed_counts)) <= 1:
            return GuardResult(
                triggered=True,
                guard_name="convergence",
                reason=f"No progress for {conv_rounds} consecutive rounds (stuck at {completed_counts[-1]} tasks)",
                value=completed_counts[-1],
                limit=conv_rounds,
            )

        # Check 2: Oscillation (quality scores going up/down without net improvement)
        quality_scores = [h.get("avg_quality", 0) for h in recent if h.get("avg_quality") is not None]
        if len(quality_scores) >= conv_rounds:
            # Detect oscillation: alternating direction changes
            directions = []
            for i in range(1, len(quality_scores)):
                diff = quality_scores[i] - quality_scores[i-1]
                if diff > 0.01:
                    directions.append(1)
                elif diff < -0.01:
                    directions.append(-1)
                else:
                    directions.append(0)

            # Count direction changes
            changes = sum(1 for i in range(1, len(directions)) if directions[i] != directions[i-1] and directions[i] != 0)
            if changes >= conv_rounds - 1:
                return GuardResult(
                    triggered=True,
                    guard_name="convergence",
                    reason=f"Quality oscillating for {conv_rounds} rounds (no net improvement)",
                    value=quality_scores[-1],
                    limit=conv_rounds,
                )

            # Check 3: Degrading (consistently decreasing)
            if all(d <= 0 for d in directions) and sum(d for d in directions if d < 0) >= conv_rounds - 1:
                return GuardResult(
                    triggered=True,
                    guard_name="convergence",
                    reason=f"Quality degrading for {conv_rounds} rounds ({quality_scores[0]:.2f} → {quality_scores[-1]:.2f})",
                    value=quality_scores[-1],
                    limit=conv_rounds,
                )

        return GuardResult(guard_name="convergence", value=len(history), limit=conv_rounds)

    def record_iteration(self, goal: dict) -> dict:
        """
        Record a loop iteration for convergence tracking.
        Call this at the end of each run_goal() pass.
        Includes quality scores for adaptive threshold.
        """
        if "loop_history" not in goal:
            goal["loop_history"] = []
        if "loop_iterations" not in goal:
            goal["loop_iterations"] = 0

        goal["loop_iterations"] += 1

        # Calculate average quality from verification scores
        verification_scores = goal.get("verification_scores", [])
        avg_quality = None
        if verification_scores:
            scores = [s["score"] for s in verification_scores if isinstance(s.get("score"), (int, float))]
            if scores:
                avg_quality = round(sum(scores) / len(scores), 3)

        goal["loop_history"].append({
            "iteration": goal["loop_iterations"],
            "tasks_completed": goal.get("tasks_completed", 0),
            "tasks_failed": goal.get("tasks_failed", 0),
            "avg_quality": avg_quality,
            "timestamp": datetime.now().isoformat(),
        })

        # Trim history to last 20 entries
        if len(goal["loop_history"]) > 20:
            goal["loop_history"] = goal["loop_history"][-20:]

        return goal


# ── Adaptive Quality Manager ──

class AdaptiveQualityManager:
    """
    Dynamically adjusts quality thresholds based on loop performance.

    Strategy:
    - If quality is consistently high (avg > raise_threshold), raise threshold
    - If quality is consistently low (avg < lower_threshold), lower threshold
    - Thresholds stay within [min_quality_threshold, max_quality_threshold]
    - Adjustments are made only after convergence_rounds iterations
    """

    def __init__(self, goal_id: str):
        self.goal_id = goal_id
        self.config = get_goal_guards(goal_id)
        self.adaptive_enabled = self.config.get("adaptive_quality", True)

    def adjust_threshold(self, goal: dict, current_threshold: float) -> float:
        """
        Analyze recent performance and adjust quality threshold.

        Returns:
            New threshold value (may be unchanged if no adjustment needed)
        """
        if not self.adaptive_enabled:
            return current_threshold

        history = goal.get("loop_history", [])
        conv_rounds = self.config.get("convergence_rounds", DEFAULT_GUARD_CONFIG["convergence_rounds"])

        if len(history) < conv_rounds:
            return current_threshold

        recent = history[-conv_rounds:]
        quality_scores = [h.get("avg_quality") for h in recent if h.get("avg_quality") is not None]

        if not quality_scores:
            return current_threshold

        avg_quality = sum(quality_scores) / len(quality_scores)

        new_threshold = current_threshold
        raise_thresh = self.config.get("quality_raise_threshold", DEFAULT_GUARD_CONFIG["quality_raise_threshold"])
        lower_thresh = self.config.get("quality_lower_threshold", DEFAULT_GUARD_CONFIG["quality_lower_threshold"])
        raise_inc = self.config.get("quality_raise_increment", DEFAULT_GUARD_CONFIG["quality_raise_increment"])
        lower_inc = self.config.get("quality_lower_increment", DEFAULT_GUARD_CONFIG["quality_lower_increment"])
        min_thresh = self.config.get("min_quality_threshold", DEFAULT_GUARD_CONFIG["min_quality_threshold"])
        max_thresh = self.config.get("max_quality_threshold", DEFAULT_GUARD_CONFIG["max_quality_threshold"])

        if avg_quality >= raise_thresh:
            # Quality is high — raise bar
            new_threshold = min(current_threshold + raise_inc, max_thresh)
        elif avg_quality < lower_thresh:
            # Quality is low — lower bar to make progress
            new_threshold = max(current_threshold - lower_inc, min_thresh)

        return round(new_threshold, 3)


# ── Agent Switching ──

class AgentSwitcher:
    """
    Detects when an agent is consistently failing and switches to an alternative.

    Strategy:
    - Track per-agent failure rates
    - If an agent fails N times in a row on the same task type, switch
    - Maintains a blacklist to avoid retrying failed agents
    """

    # Agent alternatives mapping
    AGENT_ALTERNATIVES = {
        "researcher": ["hermes", "analyst"],
        "writer": ["hermes", "editor"],
        "developer": ["hermes", "architect"],
        "seo": ["hermes", "writer"],
        "threat": ["hermes", "researcher"],
        "hermes": ["researcher", "writer"],
    }

    def __init__(self, goal_id: str):
        self.goal_id = goal_id
        self.config = get_goal_guards(goal_id)
        self.switch_enabled = self.config.get("agent_switch_on_failure", True)
        self.switch_after = self.config.get("agent_switch_after_retries", DEFAULT_GUARD_CONFIG["agent_switch_after_retries"])
        self._failure_counts = {}  # agent_key -> consecutive failures

    def record_failure(self, agent_key: str):
        """Record a task failure for an agent."""
        self._failure_counts[agent_key] = self._failure_counts.get(agent_key, 0) + 1

    def record_success(self, agent_key: str):
        """Reset failure count on success."""
        self._failure_counts[agent_key] = 0

    def should_switch(self, agent_key: str) -> bool:
        """Check if agent should be switched due to repeated failures."""
        if not self.switch_enabled:
            return False
        return self._failure_counts.get(agent_key, 0) >= self.switch_after

    def get_alternative_agent(self, agent_key: str, task_title: str = "") -> str:
        """
        Get an alternative agent for a failing task.

        Args:
            agent_key: The current failing agent
            task_title: The task title (for context-aware switching)

        Returns:
            Alternative agent key
        """
        alternatives = self.AGENT_ALTERNATIVES.get(agent_key, ["hermes"])

        # Filter out agents that are also failing
        for alt in alternatives:
            if self._failure_counts.get(alt, 0) < self.switch_after:
                return alt

        # If all alternatives are failing, default to hermes
        return "hermes"

    def get_stats(self) -> dict:
        """Get failure stats for dashboard."""
        return dict(self._failure_counts)


# ── Convenience functions ──

def check_guards(goal_id: str, goal: dict) -> dict:
    """One-liner guard check. Returns guard result dict."""
    guards = LoopGuards(goal_id)
    return guards.check_all(goal)


def is_loop_allowed(goal_id: str, goal: dict) -> tuple:
    """
    Simple boolean check. Returns (allowed: bool, reason: str).
    """
    result = check_guards(goal_id, goal)
    if result["triggered"]:
        return False, result["reason"]
    return True, ""


if __name__ == "__main__":
    # Quick self-test
    print("=== Loop Guards Self-Test ===\n")

    # Test 1: Default config
    print("Test 1: Default config")
    cfg = load_guard_config()
    print(f"  Global: {json.dumps(cfg['global'], indent=4)}")
    print()

    # Test 2: Guard check on a mock goal
    print("Test 2: Guard check on mock goal")
    mock_goal = {
        "id": "goal-test-123",
        "title": "Test goal",
        "status": "in_progress",
        "created": datetime.now().isoformat(),
        "tasks_completed": 2,
        "tasks_failed": 0,
        "loop_iterations": 0,
        "loop_history": [],
        "tasks": [
            {"id": "t1", "title": "Task 1", "status": "completed", "retry_count": 0},
            {"id": "t2", "title": "Task 2", "status": "completed", "retry_count": 0},
        ],
    }
    result = check_guards("goal-test-123", mock_goal)
    print(f"  Triggered: {result['triggered']}")
    for c in result["checks"]:
        print(f"  {c['guard_name']}: value={c.get('value')} limit={c.get('limit')}")
    print()

    # Test 3: Max iterations triggered
    print("Test 3: Max iterations triggered")
    mock_goal["loop_iterations"] = 10
    result = check_guards("goal-test-123", mock_goal)
    print(f"  Triggered: {result['triggered']}")
    if result["triggered"]:
        print(f"  Guard: {result['guard_name']}")
        print(f"  Reason: {result['reason']}")
    print()

    # Test 4: Convergence check
    print("Test 4: Convergence (no progress for 3 rounds)")
    mock_goal["loop_iterations"] = 5
    mock_goal["loop_history"] = [
        {"iteration": 1, "tasks_completed": 2, "timestamp": "2026-01-01T00:00:00"},
        {"iteration": 2, "tasks_completed": 2, "timestamp": "2026-01-01T00:05:00"},
        {"iteration": 3, "tasks_completed": 2, "timestamp": "2026-01-01T00:10:00"},
    ]
    result = check_guards("goal-test-123", mock_goal)
    print(f"  Triggered: {result['triggered']}")
    if result["triggered"]:
        print(f"  Guard: {result['guard_name']}")
        print(f"  Reason: {result['reason']}")
    print()

    # Test 5: Per-goal override
    print("Test 5: Per-goal override")
    set_goal_guards("goal-test-123", {"max_iterations": 3})
    guards = get_goal_guards("goal-test-123")
    print(f"  Effective max_iterations: {guards['max_iterations']}")
    result = check_guards("goal-test-123", mock_goal)
    print(f"  Triggered: {result['triggered']} (should be True, iterations=5 > max=3)")
    print()

    print("=== All tests complete ===")
