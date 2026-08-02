"""
Model Router — Tier-based task delegation for multi-model orchestration.

Routes project tasks to the correct execution tier:
  Tier 1 (Owl Alpha): Strategic, state management, backlog
  Tier 2 Architect:
    - Gemini Pro: Architecture, broad context, Artifact generation (API-based)
    - Antigravity: Multi-agent orchestration, complex IDE-based development
  Tier 3 (Ornith 9B): Single-file execution, test-driven fixes

Tier 2 selection:
  - Default: Gemini Pro (fast, API-based, no IDE required)
  - Antigravity: Used when project has antigravity enabled in sandbox.json
    or task triggers include 'parallel', 'multi-agent', 'heavy coding'
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional


class Tier(Enum):
    TIER_1_DIRECTOR = "tier_1_director"      # Owl Alpha — strategic
    TIER_2_ARCHITECT = "tier_2_architect"    # Gemini Pro — blueprints
    TIER_3_BUILDER = "tier_3_builder"        # Ornith 9B — execution


class TaskType(Enum):
    # Tier 1 — Strategic
    BACKLOG_MANAGEMENT = "backlog_management"
    GOAL_CREATION = "goal_creation"
    GOAL_DECOMPOSE = "goal_decompose"
    STATE_UPDATE = "state_update"
    PROJECT_SETUP = "project_setup"
    TASK_ROUTING = "task_routing"

    # Tier 2 — Architecture
    CODE_GENERATION = "code_generation"
    CODE_REFACTOR = "code_refactor"
    RESEARCH = "research"
    DESIGN_DECISION = "design_decision"
    ARTIFACT_GENERATION = "artifact_generation"
    ARCHITECTURE = "architecture"

    # Tier 3 — Execution
    BUG_FIX = "bug_fix"
    TEST_FIX = "test_fix"
    SYNTAX_FIX = "syntax_fix"
    IMPORT_FIX = "import_fix"
    SINGLE_FILE_EDIT = "single_file_edit"
    TEST_EXECUTION = "test_execution"


# ── Routing Rules → Tier mapping ──

TASK_TIER_MAP = {
    # Tier 1
    TaskType.BACKLOG_MANAGEMENT: Tier.TIER_1_DIRECTOR,
    TaskType.GOAL_CREATION: Tier.TIER_1_DIRECTOR,
    TaskType.GOAL_DECOMPOSE: Tier.TIER_1_DIRECTOR,
    TaskType.STATE_UPDATE: Tier.TIER_1_DIRECTOR,
    TaskType.PROJECT_SETUP: Tier.TIER_1_DIRECTOR,
    TaskType.TASK_ROUTING: Tier.TIER_1_DIRECTOR,

    # Tier 2
    TaskType.CODE_GENERATION: Tier.TIER_2_ARCHITECT,
    TaskType.CODE_REFACTOR: Tier.TIER_2_ARCHITECT,
    TaskType.RESEARCH: Tier.TIER_2_ARCHITECT,
    TaskType.DESIGN_DECISION: Tier.TIER_2_ARCHITECT,
    TaskType.ARTIFACT_GENERATION: Tier.TIER_2_ARCHITECT,
    TaskType.ARCHITECTURE: Tier.TIER_2_ARCHITECT,

    # Tier 3
    TaskType.BUG_FIX: Tier.TIER_3_BUILDER,
    TaskType.TEST_FIX: Tier.TIER_3_BUILDER,
    TaskType.SYNTAX_FIX: Tier.TIER_3_BUILDER,
    TaskType.IMPORT_FIX: Tier.TIER_3_BUILDER,
    TaskType.SINGLE_FILE_EDIT: Tier.TIER_3_BUILDER,
    TaskType.TEST_EXECUTION: Tier.TIER_3_BUILDER,
}


@dataclass
class RoutingDecision:
    """Result of routing a task to a tier."""
    tier: Tier
    task_type: TaskType
    confidence: float  # 0.0–1.0
    reasoning: str
    engine: str

    def to_dict(self) -> dict:
        return {
            "tier": self.tier.value,
            "task_type": self.task_type.value,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "engine": self.engine,
        }


def classify_task(description: str, context: dict = None) -> TaskType:
    """
    Classify a task description into a TaskType using keyword matching.
    In production, this could use an LLM classifier.
    """
    desc_lower = description.lower()
    context = context or {}

    # Tier 3 keywords — specific, execution-oriented
    tier3_keywords = [
        "fix", "bug", "error", "failing", "broken", "syntax", "import",
        "typo", "missing", "undefined", "referenceerror", "nameerror",
        "test failing", "not working", "doesn't work", "crash",
        "single file", "one file", "line", "function", "method",
    ]
    if any(kw in desc_lower for kw in tier3_keywords):
        if "test" in desc_lower:
            return TaskType.TEST_FIX
        if "syntax" in desc_lower or "indent" in desc_lower:
            return TaskType.SYNTAX_FIX
        if "import" in desc_lower or "module" in desc_lower:
            return TaskType.IMPORT_FIX
        return TaskType.BUG_FIX

    # Tier 2 keywords — broad, architectural
    tier2_keywords = [
        "create", "generate", "build", "implement", "design", "refactor",
        "architecture", "structure", "blueprint", "scaffold", "boilerplate",
        "research", "analyze", "plan", "design decision", "pattern",
        "component", "module", "service", "api endpoint",
    ]
    if any(kw in desc_lower for kw in tier2_keywords):
        if "research" in desc_lower or "analyze" in desc_lower:
            return TaskType.RESEARCH
        if "design" in desc_lower or "pattern" in desc_lower:
            return TaskType.DESIGN_DECISION
        return TaskType.CODE_GENERATION

    # Tier 1 keywords — strategic, state-oriented
    tier1_keywords = [
        "backlog", "goal", "sprint", "priority", "task", "milestone",
        "roadmap", "plan project", "decompose", "break down",
        "add to", "remove from", "update state", "status",
    ]
    if any(kw in desc_lower for kw in tier1_keywords):
        if "backlog" in desc_lower:
            return TaskType.BACKLOG_MANAGEMENT
        if "goal" in desc_lower or "milestone" in desc_lower:
            return TaskType.GOAL_CREATION
        if "decompose" in desc_lower or "break down" in desc_lower:
            return TaskType.GOAL_DECOMPOSE
        return TaskType.STATE_UPDATE

    # Default: Tier 3 for specific fixes, Tier 2 for everything else
    if context.get("file_path"):
        return TaskType.SINGLE_FILE_EDIT
    return TaskType.CODE_GENERATION


def route_task(description: str, context: dict = None) -> RoutingDecision:
    """
    Route a task description to the correct tier.

    Returns a RoutingDecision with tier, task_type, and reasoning.
    """
    task_type = classify_task(description, context)
    tier = TASK_TIER_MAP.get(task_type, Tier.TIER_3_BUILDER)

    engine_map = {
        Tier.TIER_1_DIRECTOR: "owl-alpha",
        Tier.TIER_2_ARCHITECT: "gemini-3.1-pro",
        Tier.TIER_3_BUILDER: "hf.co/deepreinforce-ai/Ornith-1.0-9B-GGUF:Q4_K_M",
    }

    confidence = 0.8 if task_type in TASK_TIER_MAP else 0.5
    reasoning = f"Classified as {task_type.value} → routed to {tier.value}"
    engine = engine_map[tier]

    # Check if project config specifies Antigravity as Tier 2 engine
    desc_lower = description.lower()
    antigravity_enabled = False
    if context and context.get("project_space"):
        try:
            proj_config = load_project_config(context["project_space"])
            tier2 = proj_config.get("execution_workers", {}).get("tier_2_architect", {})
            if tier2.get("engine") == "antigravity":
                antigravity_enabled = True
        except Exception:
            pass

    # Auto-detect Antigravity triggers in task description
    antigravity_keywords = ["parallel", "multi-agent", "heavy coding", "autonomous", "code project", "ide"]
    if any(kw in desc_lower for kw in antigravity_keywords):
        antigravity_enabled = True

    # Override engine if Antigravity is active
    if antigravity_enabled and tier == Tier.TIER_2_ARCHITECT:
        engine = "antigravity"
        confidence = min(confidence + 0.1, 1.0)
        reasoning = f"Classified as {task_type.value} → routed to {tier.value} (Antigravity)"

    return RoutingDecision(
        tier=tier,
        task_type=task_type,
        confidence=confidence,
        reasoning=reasoning,
        engine=engine,
    )


def load_project_config(project_space: str) -> dict:
    """Load sandbox config for a project."""
    config_path = Path(project_space) / "sandbox.json"
    if config_path.exists():
        return json.loads(config_path.read_text())
    return {}


def save_project_config(project_space: str, config: dict) -> None:
    """Save sandbox config for a project."""
    config_path = Path(project_space) / "sandbox.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2))
