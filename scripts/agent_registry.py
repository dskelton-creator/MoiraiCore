#!/usr/bin/env python3
"""
MoiraiCore — Agent Registry
Loads agent definitions, provides lookup, memory paths, and capability queries.

Usage:
    from agent_registry import AgentRegistry
    reg = AgentRegistry()
    reg.get_agent("researcher")
    reg.list_agents()
    reg.get_memory_path("writer")
    reg.find_agent_for_task("research the ASX market")
"""

import os
import json
from pathlib import Path

# ── Webhooks / Event Bus (Feature 8) ──
_emit_event = None
try:
    from webhooks import emit as _emit_event
except ImportError:
    _emit_event = None

def _fire(event: str, payload: dict) -> None:
    """Best-effort event emit. Never raises into the registry."""
    if _emit_event is not None:
        try:
            _emit_event(event, payload)
        except Exception:
            pass


AGENT_OS_ROOT = Path(os.environ.get("AGENT_OS_ROOT", str(Path(__file__).resolve().parents[1])))
VAULT = AGENT_OS_ROOT / "memory-vault"
AGENTS_DIR = VAULT / "agents"

AGENT_DEFINITIONS = {
    "hermes": {
        "name": "Hermes",
        "emoji": "🧠",
        "role": "General-Purpose Agent",
        "status": "active",
        "model": "",
        "memory_folder": "agents/hermes",
        "skills": [],
        "toolsets": ['terminal,file,session_search,clarify,memory,skills'],
        "strengths": [
            "Terminal access and file management",
            "Multi-step task execution",
            "Security analysis and auditing",
            "Research synthesis",
            "Cross-agent coordination",
        ],
        "triggers": [],
        "description": "Primary daily driver and orchestrator. Default for non-specialist tasks.",
        "config": "~/.hermes/config.yaml",
    },
    "researcher": {
        "name": "Researcher",
        "emoji": "🔍",
        "role": "Investigation & Analysis",
        "status": "active",
        "model": "Inherits from Hermes",
        "memory_folder": "agents/researcher",
        "skills": ["arxiv", "blogwatcher"],
        "toolsets": ['web,search,session_memory,clarify'],
        "strengths": [
            "Web research and source evaluation",
            "Competitive landscape analysis",
            "Market intelligence (ASX, tech trends)",
            "Technical deep-dives",
            "Source quality tracking",
        ],
        "triggers": [
            "research", "investigate", "analyse", "analyze", "analysis",
            "compare", "competitor", "competitive", "recommendation",
            "find sources", "what's happening", "intel", "landscape",
            "market", "trends", "report",
        ],
        "description": "Deep research, fact-finding, competitive intelligence, data analysis.",
        "output_pattern": "agents/researcher/findings/",
    },
    "writer": {
        "name": "Writer",
        "emoji": "✍️",
        "role": "Content Creation & Communication",
        "status": "active",
        "model": "Inherits from Hermes",
        "memory_folder": "agents/writer",
        "skills": ["writing-plans", "humanizer"],
        "toolsets": ['file,session_memory,clarify,skills'],
        "strengths": [
            "SEO-optimized content creation",
            "Technical documentation",
            "Client emails and professional communication",
            "Blog posts, articles, social content",
            "Consistent style via templates",
        ],
        "triggers": [
            "write", "draft", "content", "blog", "email", "document",
            "article", "post", "copy", "SEO content",
        ],
        "description": "Content writing, documentation, client communication.",
        "output_pattern": "agents/writer/drafts/",
    },
    "developer": {
        "name": "Developer",
        "emoji": "💻",
        "role": "Code & Architecture",
        "status": "active",
        "model": "Inherits from Hermes; delegates to Codex/Antigravity",
        "memory_folder": "agents/developer",
        "skills": ["systematic-debugging", "test-driven-development"],
        "toolsets": ['terminal,file,code_execution,session_memory,clarify'],
        "strengths": [
            "Python, JavaScript, TypeScript, shell",
            "Code review and refactoring",
            "Security auditing (path traversal, SSRF, XSS, injection)",
            "Architecture Decision Records",
            "MoiraiCore development",
        ],
        "triggers": [
            "code", "build", "fix", "debug", "implement", "refactor",
            "review", "secure", "architecture", "deploy",
        ],
        "description": "Code generation, debugging, architecture, security hardening.",
        "output_pattern": "agents/developer/decisions/",
    },
    "antigravity": {
        "name": "Google Antigravity 2.0",
        "emoji": "🎯",
        "role": "Multi-Agent Orchestration",
        "status": "active",
        "model": "Gemini (Google Pro subscription)",
        "memory_folder": "agents/antigravity",
        "skills": [],
        "toolsets": ['terminal,file,code_execution,session_memory,clarify'],
        "strengths": [
            "Parallel sub-agent execution",
            "Autonomous coding sessions",
            "IDE with agent manager",
            "Project grouping",
            "Google Pro integration (Gemini models)",
        ],
        "triggers": [
            "parallel", "multi-agent", "autonomous", "code project", "heavy coding",
        ],
        "description": "Parallel sub-agent execution, autonomous coding, and IDE integration. v2.0.6. Google Pro subscription active.",
        "location": "/Applications/Antigravity.app",
        "version": "2.0.6",
        "subscription": "Google Pro",
    },
    "codex": {
        "name": "Codex",
        "emoji": "🔧",
        "role": "Dedicated Code Generation (OpenAI OAuth)",
        "status": "active",
        "model": "OpenAI Codex (via Sign in with ChatGPT)",
        "backend": "openai_oauth",
        "memory_folder": "agents/developer",
        "skills": ["systematic-debugging", "test-driven-development"],
        "toolsets": ['terminal,file,code_execution,session_memory,clarify'],
        "strengths": [
            "Pure code generation",
            "Large refactors",
            "Test writing",
            "Free ChatGPT-account-backed inference (no paid API key)",
        ],
        "triggers": ["codex", "openai", "sign in with chatgpt", "free api"],
        "description": "Specialized code generation routed through the local openai-oauth bridge. Authenticate with your ChatGPT account (Settings → OpenAI OAuth) for key-free inference.",
    },
    "pm": {
        "name": "Project Manager",
        "emoji": "🎯",
        "role": "Multi-Agent Pipeline Orchestrator",
        "status": "active",
        "model": "Inherits from Hermes",
        "memory_folder": "agents/pm",
        "skills": [],
        "toolsets": ['file,session_memory,clarify,delegation'],
        "strengths": [
            "Project decomposition into multi-stage pipelines",
            "Multi-agent delegation and coordination",
            "Quality gates between pipeline stages",
            "Feedback loops with retry logic",
            "Final synthesis of all agent outputs",
            "Pipeline monitoring and adaptive routing",
        ],
        "triggers": [
            "pipeline", "orchestrate", "project", "manage",
            "multi-stage", "multi-agent", "pm", "coordinate",
            "plan and execute", "full analysis", "complete review",
            "end-to-end", "comprehensive",
        ],
        "description": "Project Manager — decomposes complex projects into multi-stage pipelines, delegates to specialist agents, enforces quality gates, and synthesizes final deliverables.",
        "output_pattern": "workspace/goal-reports/",
    }
}

AGENT_OS_ROOT = Path(os.environ.get("AGENT_OS_ROOT", str(Path(__file__).resolve().parents[1])))
VAULT = AGENT_OS_ROOT / "memory-vault"
AGENTS_DIR = VAULT / "agents"

# ── Override store ──
# Per-agent definitions are hard-coded in AGENT_DEFINITIONS above, but the
# platform must be extensible without editing source. User-created/edited agents
# are persisted to config/agents.override.json and merged over the built-ins at
# load time. This is the structural seam Feature 3 (Agent Registry UI) depends on:
# the UI can register/configure agents and they survive restart.
OVERRIDE_FILE = AGENT_OS_ROOT / "config" / "agents.override.json"


def load_overrides() -> dict:
    """Load user-defined agent overrides from JSON (empty dict if none)."""
    if not OVERRIDE_FILE.exists():
        return {}
    try:
        data = json.loads(OVERRIDE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_overrides(overrides: dict) -> None:
    """Persist the override dict to JSON atomically."""
    OVERRIDE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = OVERRIDE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(overrides, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(OVERRIDE_FILE)


def merged_definitions() -> dict:
    """Built-in definitions merged with user overrides (overrides win)."""
    merged = dict(AGENT_DEFINITIONS)
    for key, agent in load_overrides().items():
        if isinstance(agent, dict):
            merged[key] = agent
    return merged


class AgentRegistry:
    """Loads and queries agent definitions."""

    def __init__(self, agents_dir=None):
        self.agents_dir = agents_dir or AGENTS_DIR
        self._agents = merged_definitions()

    # ── Persistence operations (Feature 3) ──
    def save_agent(self, key: str, definition: dict) -> dict:
        """Create or update an agent definition in the override store."""
        key = key.lower().strip()
        if not key:
            raise ValueError("agent key is required")
        definition = dict(definition)
        definition["key"] = key
        definition.setdefault("status", "active")
        definition.setdefault("skills", [])
        definition.setdefault("toolsets", [])
        definition.setdefault("triggers", [])
        overrides = load_overrides()
        overrides[key] = definition
        save_overrides(overrides)
        self._agents = merged_definitions()
        _fire("agent.registered", {"key": key, "name": definition.get("name", key), "model": definition.get("model", "")})
        return self._agent_summary(key, self._agents[key])

    def delete_agent(self, key: str) -> bool:
        """Remove a user-defined agent. Built-in agents cannot be deleted."""
        key = key.lower().strip()
        if key in AGENT_DEFINITIONS:
            raise ValueError(f"'{key}' is a built-in agent and cannot be deleted")
        overrides = load_overrides()
        if key not in overrides:
            return False
        del overrides[key]
        save_overrides(overrides)
        self._agents = merged_definitions()
        _fire("agent.deleted", {"key": key})
        return True

    def list_agents(self, status_filter="all"):
            """List all agents, optionally filtered by status."""
            results = []
            for key, agent in self._agents.items():
                if agent["status"] != "active" or key == "hermes":
                    continue
                if status_filter != "all" and agent["status"] != status_filter:
                    continue
                results.append(self._agent_summary(key, agent))
            return results

    def get_agent(self, name):
        """Get full agent definition by key name."""
        key = name.lower().strip()
        agent = self._agents.get(key)
        if not agent:
            return None
        result = dict(agent)
        result["key"] = key
        if agent.get("memory_folder"):
            result["memory_path"] = VAULT / agent["memory_folder"]
        return result

    def get_memory_path(self, name):
        """Get the memory folder path for an agent."""
        agent = self.get_agent(name)
        if not agent:
            return None
        mem = agent.get("memory_folder")
        return VAULT / mem if mem else None

    def read_memory(self, name):
        """Read an agent's memory.md if it exists."""
        mem_path = self.get_memory_path(name)
        if not mem_path:
            return None
        memory_file = mem_path / "memory.md"
        if memory_file.exists():
            return memory_file.read_text(errors="ignore")
        return None

    def find_agent_for_task(self, task_description):
        """
        Task router — determines which agent should handle a task.
        Returns (agent_key, confidence, matched_triggers).
        Improved version with better token matching and scoring.
        """
        # Tokenize the task description (lowercase, word boundaries only)
        import re
        def _tokenize(text):
            if not text:
                return set()
            return set(re.findall(r'\b[a-z0-9]+\b', text))
        
        task_tokens = set(_tokenize(task_description.lower()))
        
        best_match = ("hermes", "low", [], 0.0)  # (agent_key, confidence, matched_triggers, score)
        
        for key, agent in self._agents.items():
            if agent["status"] != "active" or key == "hermes":
                continue
                
            triggers = agent.get("triggers", [])
            if not triggers:
                continue
                
            # Tokenize triggers
            trigger_tokens = set()
            trigger_to_original = {}  # Map tokenized trigger to original
            for trigger in triggers:
                tokenized = _tokenize(trigger.lower())
                if tokenized:  # Only add if tokenization produced results
                    for token in tokenized:
                        trigger_tokens.add(token)
                        # Map token back to original trigger (for reporting)
                        if token not in trigger_to_original:
                            trigger_to_original[token] = []
                        trigger_to_original[token].append(trigger)
            
            # Find intersection of task tokens and trigger tokens
            matched_tokens = task_tokens.intersection(trigger_tokens)
            
            if matched_tokens:
                # Calculate score based on:
                # 1. Number of matched tokens (primary factor)
                # 2. Rarity of matches (less common triggers = higher score)
                # 3. Proportion of triggers matched (coverage)
                
                match_count = len(matched_tokens)
                
                # Calculate rarity: how few agents have this trigger
                rarity_score = 0
                for token in matched_tokens:
                    # Count how many agents have this trigger
                    agent_count = 0
                    for a in self._agents.values():
                        if a["status"] == "active":
                            a_tokens = set()
                            for t in a.get("triggers", []):
                                a_tokens.update(_tokenize(t.lower()))
                            if token in a_tokens:
                                agent_count += 1
                    # Inverse frequency weighting (rarer = higher score)
                    if agent_count > 0:
                        rarity_score += 1.0 / agent_count
                
                # Coverage: what fraction of the agent's triggers we matched
                total_trigger_tokens = len(trigger_tokens)
                coverage = match_count / max(total_trigger_tokens, 1)
                
                # Combined score
                score = float(match_count) + (rarity_score * 0.5) + (coverage * 2.0)
                
                # Determine confidence level
                if match_count >= 3 or (match_count >= 2 and rarity_score > 1.0):
                    confidence = "high"
                elif match_count >= 1:
                    confidence = "medium"
                else:
                    confidence = "low"
                
                # Update best match if this score is higher
                if score > best_match[3]:
                    # Convert matched tokens back to original trigger strings for reporting
                    matched_triggers = []
                    seen = set()
                    for token in matched_tokens:
                        if token in trigger_to_original:
                            for orig in trigger_to_original[token]:
                                if orig not in seen:
                                    matched_triggers.append(orig)
                                    seen.add(orig)
                    best_match = (key, confidence, matched_triggers, score)
        
        # Return tuple without the score
        return (best_match[0], best_match[1], best_match[2])
    def active_count(self):
        """Count active agents."""
        return sum(1 for a in self._agents.values() if a["status"] == "active")

    def _agent_summary(self, key, agent):
        mem_size = 0
        file_count = 0
        if agent.get("memory_folder"):
            mem_path = VAULT / agent["memory_folder"]
            if mem_path.exists():
                files = list(mem_path.rglob("*"))
                file_count = len([f for f in files if f.is_file()])
                mem_size = sum(f.stat().st_size for f in files if f.is_file())

        return {
            "key": key,
            "name": agent["name"],
            "emoji": agent["emoji"],
            "role": agent["role"],
            "status": agent["status"],
            "model": agent["model"],
            "memory_folder": agent.get("memory_folder"),
            "memory_path": str(VAULT / agent["memory_folder"]) if agent.get("memory_folder") else None,
            "file_count": file_count,
            "memory_size": mem_size,
            "triggers": agent.get("triggers", []),
            "description": agent["description"],
        }


# Module-level convenience
_default_registry = None

def get_registry():
    global _default_registry
    if _default_registry is None:
        _default_registry = AgentRegistry()
    return _default_registry

def get_agent(name):
    return get_registry().get_agent(name)

def list_agents(status_filter="all"):
    return get_registry().list_agents(status_filter)

def find_agent_for_task(task):
    return get_registry().find_agent_for_task(task)

def get_memory_path(name):
    return get_registry().get_memory_path(name)

def read_memory(name):
    return get_registry().read_memory(name)

def save_agent(key, definition):
    return get_registry().save_agent(key, definition)

def delete_agent(key):
    return get_registry().delete_agent(key)

