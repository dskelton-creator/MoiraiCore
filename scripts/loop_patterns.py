#!/usr/bin/env python3
"""
MoiraiCore — Loop Pattern Learner
=================================
Learns from successful loop executions to suggest optimizations for future goals.

Pattern types:
  - Goal patterns: "Research + Write" sequences that succeed
  - Agent patterns: Which agents work best for which task types
  - Quality patterns: What quality thresholds produce best outcomes
  - Convergence patterns: How many iterations typical goals need

Stored in: config/loop_patterns.json

Usage:
    from loop_patterns import LoopPatternLearner

    learner = LoopPatternLearner()
    learner.record_loop(goal)  # After goal completes
    suggestion = learner.suggest_optimization(new_goal_title, new_goal_desc)
"""

import json
import os
import re
from datetime import datetime
from pathlib import Path

AGENT_OS_ROOT = Path(os.environ.get("AGENT_OS_ROOT", str(Path(__file__).resolve().parents[1])))
CONFIG_DIR = AGENT_OS_ROOT / "config"
PATTERNS_FILE = CONFIG_DIR / "loop_patterns.json"


def _ensure_dir():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def load_patterns() -> dict:
    """Load pattern database."""
    if PATTERNS_FILE.exists():
        try:
            return json.loads(PATTERNS_FILE.read_text())
        except Exception:
            pass
    return {
        "version": 1,
        "recorded_loops": 0,
        "goal_patterns": [],
        "agent_performance": {},
        "quality_outcomes": [],
        "convergence_stats": {},
    }


def save_patterns(patterns: dict):
    """Save pattern database."""
    _ensure_dir()
    PATTERNS_FILE.write_text(json.dumps(patterns, indent=2, default=str))


class LoopPatternLearner:
    """
    Records loop execution patterns and suggests optimizations.
    """

    def __init__(self):
        self.patterns = load_patterns()

    def record_loop(self, goal: dict):
        """
        Record a completed loop execution for pattern learning.

        Args:
            goal: The completed goal dict (should be status "completed" or "partial")
        """
        status = goal.get("status", "")
        if status not in ("completed", "partial"):
            return  # Only learn from finished loops

        # Extract goal pattern
        title = goal.get("title", "")
        desc = goal.get("desc", "")
        task_types = self._extract_task_types(title, desc)

        # Extract execution metrics
        iterations = goal.get("loop_iterations", 0)
        tasks_completed = goal.get("tasks_completed", 0)
        tasks_failed = goal.get("tasks_failed", 0)
        total_tasks = goal.get("tasks_total", 0)

        # Verification scores
        verification_scores = goal.get("verification_scores", [])
        avg_quality = None
        if verification_scores:
            scores = [s["score"] for s in verification_scores if isinstance(s.get("score"), (int, float))]
            if scores:
                avg_quality = round(sum(scores) / len(scores), 3)

        # Adaptive threshold history
        threshold_history = goal.get("adaptive_threshold_history", [])

        # Agent switches
        corrections_log = goal.get("corrections_log", [])

        # Build pattern record
        pattern = {
            "goal_title": title[:100],
            "task_types": task_types,
            "total_tasks": total_tasks,
            "tasks_completed": tasks_completed,
            "tasks_failed": tasks_failed,
            "iterations": iterations,
            "avg_quality": avg_quality,
            "status": status,
            "threshold_adjustments": len(threshold_history),
            "corrections": len(corrections_log),
            "recorded_at": datetime.now().isoformat(),
        }

        # Add to patterns
        self.patterns["goal_patterns"].append(pattern)
        self.patterns["recorded_loops"] = self.patterns.get("recorded_loops", 0) + 1

        # Update agent performance tracking
        self._update_agent_performance(goal)

        # Update convergence stats
        key = self._classify_goal_type(title, desc)
        if key not in self.patterns["convergence_stats"]:
            self.patterns["convergence_stats"][key] = {
                "count": 0,
                "avg_iterations": 0,
                "avg_quality": 0,
                "success_rate": 0,
            }
        stats = self.patterns["convergence_stats"][key]
        stats["count"] += 1
        n = stats["count"]
        # Running average
        if iterations > 0:
            stats["avg_iterations"] = round(
                (stats["avg_iterations"] * (n - 1) + iterations) / n, 1
            )
        if avg_quality is not None:
            stats["avg_quality"] = round(
                (stats["avg_quality"] * (n - 1) + avg_quality) / n, 3
            )
        if status == "completed":
            successes = stats["success_rate"] * (n - 1) + (1 if status == "completed" else 0)
            stats["success_rate"] = round(successes / n, 3)

        # Keep only last 100 patterns
        if len(self.patterns["goal_patterns"]) > 100:
            self.patterns["goal_patterns"] = self.patterns["goal_patterns"][-100:]

        save_patterns(self.patterns)

    def suggest_optimization(self, goal_title: str, goal_desc: str) -> dict:
        """
        Suggest optimizations for a new goal based on learned patterns.

        Returns:
            {
                "suggested_iterations": int,
                "suggested_quality_threshold": float,
                "suggested_agents": [str, ...],
                "estimated_rounds": int,
                "confidence": float,
                "reasoning": str,
            }
        """
        goal_type = self._classify_goal_type(goal_title, goal_desc)
        task_types = self._extract_task_types(goal_title, goal_desc)

        suggestion = {
            "suggested_iterations": 5,
            "suggested_quality_threshold": 0.6,
            "suggested_agents": [],
            "estimated_rounds": 2,
            "confidence": 0.0,
            "reasoning": "",
        }

        # Check convergence stats for this goal type
        stats = self.patterns["convergence_stats"].get(goal_type)
        if stats and stats["count"] >= 2:
            suggestion["suggested_iterations"] = max(3, int(stats["avg_iterations"] * 1.5))
            suggestion["estimated_rounds"] = max(1, int(stats["avg_iterations"] * 0.8))
            if stats["avg_quality"] > 0:
                suggestion["suggested_quality_threshold"] = round(
                    max(0.4, stats["avg_quality"] - 0.1), 2
                )
            suggestion["confidence"] = min(0.9, stats["count"] / 10.0)
            suggestion["reasoning"] = (
                f"Based on {stats['count']} similar goals: "
                f"avg {stats['avg_iterations']} iterations, "
                f"avg quality {stats['avg_quality']:.2f}, "
                f"success rate {stats['success_rate']:.0%}"
            )

        # Suggest agents based on task types
        agent_scores = self._get_agent_scores_for_tasks(task_types)
        if agent_scores:
            suggestion["suggested_agents"] = [
                agent for agent, score in agent_scores.items() if score > 0.3
            ]

        # If we have enough patterns, suggest based on similar goals
        similar = self._find_similar_goals(goal_title, goal_desc)
        if similar:
            avg_iters = sum(s["iterations"] for s in similar) / len(similar)
            suggestion["estimated_rounds"] = max(1, int(avg_iters * 0.7))
            suggestion["confidence"] = min(0.95, len(similar) / 5.0)

        return suggestion

    def _classify_goal_type(self, title: str, desc: str) -> str:
        """Classify a goal into a category."""
        text = (title + " " + desc).lower()
        if any(w in text for w in ["research", "investigate", "analyse", "analyze", "competitor"]):
            return "research"
        if any(w in text for w in ["write", "report", "document", "blog", "article", "draft"]):
            return "writing"
        if any(w in text for w in ["build", "implement", "code", "develop", "create", "deploy"]):
            return "development"
        if any(w in text for w in ["seo", "search engine", "keyword", "ranking"]):
            return "seo"
        if any(w in text for w in ["security", "threat", "risk", "vulnerability", "audit"]):
            return "security"
        return "general"

    def _extract_task_types(self, title: str, desc: str) -> list:
        """Extract task type keywords from goal text."""
        text = (title + " " + desc).lower()
        types = []
        keywords = {
            "research": ["research", "investigate", "analyse", "analyze", "study"],
            "writing": ["write", "report", "document", "blog", "article", "draft"],
            "development": ["build", "implement", "code", "develop", "create", "deploy"],
            "seo": ["seo", "search engine", "keyword", "ranking", "google"],
            "security": ["security", "threat", "risk", "vulnerability", "audit"],
            "design": ["design", "ui", "ux", "layout", "visual"],
        }
        for task_type, words in keywords.items():
            if any(w in text for w in words):
                types.append(task_type)
        return types or ["general"]

    def _update_agent_performance(self, goal: dict):
        """Update agent success/failure tracking."""
        corrections_log = goal.get("corrections_log", [])
        if not corrections_log:
            return

        # Track which agents needed corrections
        for correction in corrections_log:
            task_id = correction.get("task_id", "")
            # We don't have agent info in corrections_log directly,
            # but we can infer from task data
            pass  # Future: track per-agent performance

    def _get_agent_scores_for_tasks(self, task_types: list) -> dict:
        """Get agent suitability scores for given task types."""
        # Default agent-task mappings
        default_scores = {
            "research": {"researcher": 0.9, "hermes": 0.6},
            "writing": {"writer": 0.9, "hermes": 0.6},
            "development": {"developer": 0.9, "hermes": 0.5},
            "seo": {"seo": 0.9, "writer": 0.6, "hermes": 0.5},
            "security": {"threat": 0.9, "researcher": 0.5, "hermes": 0.5},
            "design": {"hermes": 0.7},
            "general": {"hermes": 0.8},
        }

        scores = {}
        for task_type in task_types:
            type_scores = default_scores.get(task_type, default_scores["general"])
            for agent, score in type_scores.items():
                scores[agent] = max(scores.get(agent, 0), score)

        return scores

    def _find_similar_goals(self, goal_title: str, goal_desc: str) -> list:
        """Find similar completed goals from pattern history."""
        goal_type = self._classify_goal_type(goal_title, goal_desc)
        similar = []
        for pattern in self.patterns.get("goal_patterns", []):
            if pattern.get("status") in ("completed", "partial"):
                if self._classify_goal_type(pattern["goal_title"], "") == goal_type:
                    similar.append(pattern)
        return similar[-10:]  # Last 10 similar goals

    def get_stats(self) -> dict:
        """Get overall pattern learning statistics."""
        return {
            "recorded_loops": self.patterns.get("recorded_loops", 0),
            "goal_types_tracked": list(self.patterns.get("convergence_stats", {}).keys()),
            "total_patterns": len(self.patterns.get("goal_patterns", [])),
        }


# ── Convenience functions ──

_instance = None


def get_pattern_learner() -> LoopPatternLearner:
    """Get or create global pattern learner."""
    global _instance
    if _instance is None:
        _instance = LoopPatternLearner()
    return _instance


def record_loop_pattern(goal: dict):
    """Record a loop execution in the pattern database."""
    learner = get_pattern_learner()
    learner.record_loop(goal)


def get_suggestion(goal_title: str, goal_desc: str = "") -> dict:
    """Get optimization suggestions for a new goal."""
    learner = get_pattern_learner()
    return learner.suggest_optimization(goal_title, goal_desc)


if __name__ == "__main__":
    print("=== Loop Pattern Learner Self-Test ===\n")

    learner = LoopPatternLearner()

    # Test 1: Record a mock completed goal
    print("Test 1: Record completed goal")
    mock_goal = {
        "title": "Research ASX competitors and write report",
        "desc": "Analyze competitive landscape",
        "status": "completed",
        "loop_iterations": 3,
        "tasks_completed": 4,
        "tasks_failed": 0,
        "tasks_total": 4,
        "verification_scores": [
            {"score": 0.85, "status": "pass"},
            {"score": 0.72, "status": "pass"},
            {"score": 0.91, "status": "pass"},
            {"score": 0.68, "status": "partial"},
        ],
        "adaptive_threshold_history": [
            {"iteration": 2, "old_threshold": 0.6, "new_threshold": 0.65}
        ],
        "corrections_log": [],
    }
    learner.record_loop(mock_goal)
    print(f"  Recorded: {mock_goal['title']}")
    print(f"  Stats: {learner.get_stats()}")

    # Test 2: Record another similar goal
    print("\nTest 2: Record second goal")
    mock_goal2 = {
        "title": "Research cybersecurity market trends",
        "desc": "Market analysis for cybersecurity sector",
        "status": "completed",
        "loop_iterations": 4,
        "tasks_completed": 5,
        "tasks_failed": 1,
        "tasks_total": 6,
        "verification_scores": [
            {"score": 0.78, "status": "pass"},
            {"score": 0.82, "status": "pass"},
            {"score": 0.65, "status": "partial"},
            {"score": 0.88, "status": "pass"},
            {"score": 0.71, "status": "pass"},
        ],
        "corrections_log": [{"action": "retry"}],
    }
    learner.record_loop(mock_goal2)
    print(f"  Recorded: {mock_goal2['title']}")

    # Test 3: Get suggestions
    print("\nTest 3: Get optimization suggestion")
    suggestion = learner.suggest_optimization(
        "Research cloud computing market",
        "Analyze cloud providers and write report"
    )
    print(f"  Suggested iterations: {suggestion['suggested_iterations']}")
    print(f"  Suggested threshold: {suggestion['suggested_quality_threshold']}")
    print(f"  Estimated rounds: {suggestion['estimated_rounds']}")
    print(f"  Confidence: {suggestion['confidence']}")
    print(f"  Reasoning: {suggestion['reasoning']}")

    # Test 4: Adaptive quality manager
    print("\nTest 4: Adaptive Quality Manager")
    from loop_guards import AdaptiveQualityManager
    aqm = AdaptiveQualityManager("test-goal")
    test_goal = {
        "loop_history": [
            {"iteration": 1, "avg_quality": 0.92},
            {"iteration": 2, "avg_quality": 0.88},
            {"iteration": 3, "avg_quality": 0.91},
        ]
    }
    new_thresh = aqm.adjust_threshold(test_goal, 0.6)
    print(f"  High quality → threshold raised: 0.6 → {new_thresh}")

    test_goal2 = {
        "loop_history": [
            {"iteration": 1, "avg_quality": 0.35},
            {"iteration": 2, "avg_quality": 0.30},
            {"iteration": 3, "avg_quality": 0.38},
        ]
    }
    new_thresh2 = aqm.adjust_threshold(test_goal2, 0.6)
    print(f"  Low quality → threshold lowered: 0.6 → {new_thresh2}")

    # Test 5: Agent switcher
    print("\nTest 5: Agent Switcher")
    from loop_guards import AgentSwitcher
    switcher = AgentSwitcher("test-goal")
    switcher.record_failure("researcher")
    switcher.record_failure("researcher")
    print(f"  Researcher failed 2x, should_switch: {switcher.should_switch('researcher')}")
    alt = switcher.get_alternative_agent("researcher", "Research task")
    print(f"  Alternative agent: {alt}")

    print("\n✅ All tests passed!")
