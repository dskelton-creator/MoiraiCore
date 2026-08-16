"""
Model Router — Tier-based task delegation for multi-model orchestration.

Routes project tasks to the correct execution tier:
  Tier 1 (Director): Strategic, state management, backlog
  Tier 2 Architect:
    - Gemini Pro: Architecture, broad context, Artifact generation (API-based)
    - Antigravity: Multi-agent orchestration, complex IDE-based development
  Tier 3 (Builder): Single-file execution, test-driven fixes

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
    TIER_1_DIRECTOR = "tier_1_director"      # Director — strategic
    TIER_2_ARCHITECT = "tier_2_architect"    # Gemini Pro — blueprints
    TIER_3_BUILDER = "tier_3_builder"        # Builder — execution


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


# Natural-language descriptions for each task type, used by the embedding
# classifier to semantically match task descriptions against known types.
# Kept token-lean; each description is a phrase-length summary.
TASK_TYPE_DESCRIPTIONS = {
    TaskType.BACKLOG_MANAGEMENT: "manage the backlog, organize tasks, prioritize and reorder work items, sort and filter the task list",
    TaskType.GOAL_CREATION: "create a new goal or milestone, define objectives, set targets, establish a new objective",
    TaskType.GOAL_DECOMPOSE: "decompose a goal into subtasks, break down work into smaller pieces, split a large task",
    TaskType.STATE_UPDATE: "update task or project state, change status, update progress tracking",
    TaskType.PROJECT_SETUP: "set up a new project, scaffold a workspace, initialize project structure and configuration",
    TaskType.TASK_ROUTING: "route a task to an agent, assign work, determine which worker or agent should handle a task",
    TaskType.CODE_GENERATION: "generate new code, build a feature, implement functionality, create a module or function",
    TaskType.CODE_REFACTOR: "refactor existing code, improve code structure, clean up and reorganize for better design",
    TaskType.RESEARCH: "research a topic, investigate and analyze, gather information, explore and find answers",
    TaskType.DESIGN_DECISION: "make a design decision, choose an architecture pattern or approach, decide on a design",
    TaskType.ARTIFACT_GENERATION: "generate an artifact or document, produce a plan or deliverable, create a specification",
    TaskType.ARCHITECTURE: "design system architecture, create blueprints, define high-level structure and components",
    TaskType.BUG_FIX: "fix a bug or error, resolve broken behavior, repair a defect, correct a malfunction",
    TaskType.TEST_FIX: "fix a failing test, repair test code, make tests pass, correct test assertions",
    TaskType.SYNTAX_FIX: "fix a syntax error, correct indentation or formatting, repair parsing errors",
    TaskType.IMPORT_FIX: "fix a broken import, resolve missing module references, repair dependency imports",
    TaskType.SINGLE_FILE_EDIT: "edit a single file, make a small focused change to one file or one function",
    TaskType.TEST_EXECUTION: "run tests, execute the test suite, check test results, validate code via testing",
}

# Minimum cosine score to accept an embedding classification.
# TF-IDF produces sparse vectors with low scores (~0.2-0.5 for good matches);
# below this the match is likely word-overlap noise, so the keyword fallback
# takes over. Ollama embeddings (SEMANTIC_BACKEND=ollama) are the recommended
# backend — they score much higher and capture real semantics.
_EMBED_MIN_CONFIDENCE = 0.15

# Minimum margin ratio between top and second-best embedding score.  Prevents
# accepting a match when two unrelated types score similarly due to shared
# filler words (TF-IDF "the" / "and" noise). 1.5 = top must beat 2nd by 50%.
_EMBED_MIN_MARGIN = 1.25


def _classify_embedding(description: str) -> tuple[TaskType, float] | None:
    """Classify via semantic embedding matching against TASK_TYPE_DESCRIPTIONS.

    Uses semantic_search (TF-IDF by default, Ollama embeddings when
    SEMANTIC_BACKEND=ollama). Returns (task_type, confidence) or None when
    the match is ambiguous or the backend is unavailable — the caller must
    fall back to keyword matching.
    """
    try:
        from semantic_search import semantic_search
        docs = [
            {"id": tt.value, "name": tt.value, "content": desc,
             "folder": "", "path": tt.value, "size": len(desc)}
            for tt, desc in TASK_TYPE_DESCRIPTIONS.items()
        ]
        results = semantic_search(description, docs, top_k=2)
        if not results or results[0]["score"] <= 0:
            return None
        top = results[0]["score"]
        second = results[1]["score"] if len(results) > 1 else 0.0
        if top >= _EMBED_MIN_CONFIDENCE and (second == 0.0 or top / second >= _EMBED_MIN_MARGIN):
            task_type = TaskType(results[0]["id"])
            return task_type, top
    except Exception:
        pass
    return None


@dataclass
class RoutingDecision:
    """Result of routing a task to a tier."""
    tier: Tier
    task_type: TaskType
    confidence: float  # 0.0–1.0
    reasoning: str
    engine: str
    reasoning_effort: str = "medium"  # none|minimal|low|medium|high|xhigh|max|ultra

    def to_dict(self) -> dict:
        return {
            "tier": self.tier.value,
            "task_type": self.task_type.value,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "engine": self.engine,
            "reasoning_effort": self.reasoning_effort,
        }


# Reasoning effort per task type — tunes the Tier 1 director's compute spend.
# Strategic decomposition and architecture decisions get more reasoning;
# mechanical state/triage and local-model execution get less.
_REASONING_EFFORT_MAP = {
    # Tier 1 — strategic
    TaskType.GOAL_DECOMPOSE: "high",
    TaskType.GOAL_CREATION: "medium",
    TaskType.BACKLOG_MANAGEMENT: "low",
    TaskType.STATE_UPDATE: "low",
    TaskType.PROJECT_SETUP: "low",
    TaskType.TASK_ROUTING: "low",
    # Tier 2 — architecture
    TaskType.ARCHITECTURE: "high",
    TaskType.DESIGN_DECISION: "high",
    TaskType.RESEARCH: "medium",
    TaskType.CODE_GENERATION: "medium",
    TaskType.CODE_REFACTOR: "medium",
    TaskType.ARTIFACT_GENERATION: "medium",
    # Tier 3 — execution (local model; effort not applied there)
    TaskType.BUG_FIX: "low",
    TaskType.TEST_FIX: "low",
    TaskType.SYNTAX_FIX: "low",
    TaskType.IMPORT_FIX: "low",
    TaskType.SINGLE_FILE_EDIT: "low",
    TaskType.TEST_EXECUTION: "low",
}


def _reasoning_effort_for(task_type: TaskType) -> str:
    """Map a task type to a reasoning-effort level."""
    return _REASONING_EFFORT_MAP.get(task_type, "medium")


def classify_task(description: str, context: dict = None) -> TaskType:
    """
    Classify a task description into a TaskType.

    Strategy ("don't classify, hallucinate — then match"): try semantic
    embedding matching first (robust to paraphrase/synonym), then fall back to
    keyword matching. Both are offline-safe; the embedding path degrades to the
    keyword path on any failure or low confidence.
    """
    emb = _classify_embedding(description)
    if emb is not None and emb[1] >= _EMBED_MIN_CONFIDENCE:
        return emb[0]

    return _classify_keyword(description, context)


def _classify_keyword(description: str, context: dict = None) -> TaskType:
    """Keyword-matching classifier (fast, offline fallback)."""
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
        Tier.TIER_1_DIRECTOR: "deepseek/deepseek-v4-pro",
        Tier.TIER_2_ARCHITECT: "gemini-3.1-pro",
        Tier.TIER_3_BUILDER: "qwen2.5-coder:14b",
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
        reasoning_effort=_reasoning_effort_for(task_type),
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
