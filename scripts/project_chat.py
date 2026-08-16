"""
Project Chat — Per-project chat storage and management.

Each project gets its own chat directory under projects/[NAME]/chat/:
  - messages.jsonl    → Append-only message log
  - sessions/         → Per-session message threads (optional)

Message schema (one JSON object per line in messages.jsonl):
  {
    "id": "msg_<uuid>",
    "role": "user" | "assistant" | "agent_transcript" | "system",
    "content": "message text",
    "agent_name": "user" | "moirai" | "researcher" | "writer" | ...,
    "timestamp": "2026-07-29T12:00:00",
    "type": "message" | "transcript" | "goal_update" | "task_complete",
    "session_id": "sess_<uuid>",
    "metadata": {}  # optional extra data (goal_id, task_id, etc.)
  }
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

AGENT_OS_ROOT = Path(os.environ.get("AGENT_OS_ROOT", str(Path(__file__).resolve().parents[1])))
PROJECTS_DIR = AGENT_OS_ROOT / "projects"


def _chat_dir(project_name: str) -> Path:
    """Get the chat directory for a project, creating it if needed."""
    chat_path = PROJECTS_DIR / project_name / "chat"
    chat_path.mkdir(parents=True, exist_ok=True)
    return chat_path


def _messages_file(project_name: str) -> Path:
    """Get the messages.jsonl path for a project."""
    return _chat_dir(project_name) / "messages.jsonl"


def ensure_chat_dir(project_name: str) -> Path:
    """Ensure the chat directory exists and return it."""
    return _chat_dir(project_name)


def get_messages(
    project_name: str,
    limit: int = 100,
    offset: int = 0,
    role_filter: Optional[str] = None,
) -> list[dict]:
    """Get chat messages for a project, newest last, paginated.

    Args:
        project_name: Name of the project.
        limit: Max messages to return (default 100).
        offset: Skip N newest messages (for pagination).
        role_filter: Optional 'user', 'assistant', 'agent_transcript', or 'system'.

    Returns:
        List of message dicts ordered by timestamp ascending.
    """
    msgs_file = _messages_file(project_name)
    if not msgs_file.exists():
        return []

    # Read all messages (file is append-only, lines are chronological)
    all_messages = []
    with open(msgs_file, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    msg = json.loads(line)
                    if role_filter and msg.get("role") != role_filter:
                        continue
                    all_messages.append(msg)
                except json.JSONDecodeError:
                    continue

    # Apply offset (skip N newest) and limit (take N from that point)
    if offset > 0:
        all_messages = all_messages[:-offset] if offset < len(all_messages) else []

    if limit > 0 and len(all_messages) > limit:
        all_messages = all_messages[-limit:]

    return all_messages


def add_message(
    project_name: str,
    role: str,
    content: str,
    agent_name: str = "user",
    msg_type: str = "message",
    session_id: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> dict:
    """Append a message to the project chat log.

    Args:
        project_name: Name of the project.
        role: 'user', 'assistant', 'agent_transcript', or 'system'.
        content: Message body text.
        agent_name: Display name of the sender (e.g. 'user', 'moirai', 'researcher').
        msg_type: 'message', 'transcript', 'goal_update', 'task_complete'.
        session_id: Optional session ID for threading.
        metadata: Optional dict with extra data (goal_id, task_id, etc.).

    Returns:
        The created message dict.
    """
    msg = {
        "id": f"msg_{uuid.uuid4().hex[:12]}",
        "role": role,
        "content": content,
        "agent_name": agent_name,
        "timestamp": datetime.now().isoformat(),
        "type": msg_type or "message",
        "session_id": session_id or "",
        "metadata": metadata or {},
    }

    msgs_file = _messages_file(project_name)
    with open(msgs_file, "a") as f:
        f.write(json.dumps(msg) + "\n")

    return msg


def add_agent_transcript(
    project_name: str,
    agent_name: str,
    content: str,
    msg_type: str = "transcript",
    goal_id: Optional[str] = None,
    task_id: Optional[str] = None,
) -> dict:
    """Add an agent-to-agent transcript message visible in the chat feed.

    This is used to show inter-agent communication as the project progresses.
    """
    metadata = {}
    if goal_id:
        metadata["goal_id"] = goal_id
    if task_id:
        metadata["task_id"] = task_id

    return add_message(
        project_name=project_name,
        role="agent_transcript",
        content=content,
        agent_name=agent_name,
        msg_type=msg_type,
        metadata=metadata,
    )


def add_system_event(
    project_name: str,
    content: str,
    msg_type: str = "goal_update",
    goal_id: Optional[str] = None,
) -> dict:
    """Add a system event (goal started, task completed, etc.)."""
    metadata = {}
    if goal_id:
        metadata["goal_id"] = goal_id

    return add_message(
        project_name=project_name,
        role="system",
        content=content,
        agent_name="system",
        msg_type=msg_type,
        metadata=metadata,
    )


def get_recent_activity(
    project_name: str,
    limit: int = 20,
) -> list[dict]:
    """Get the most recent activity messages (transcripts + system events only)."""
    msgs_file = _messages_file(project_name)
    if not msgs_file.exists():
        return []

    activity = []
    with open(msgs_file, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    msg = json.loads(line)
                    if msg.get("role") in ("agent_transcript", "system"):
                        activity.append(msg)
                except json.JSONDecodeError:
                    continue

    return activity[-limit:] if limit > 0 else activity


# ── SSE streaming support ──

class ChatEventStream:
    """Helper to produce SSE output for chat events.

    Usage in server.py:
        stream = ChatEventStream(project_name)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        for event in stream.poll():
            self.wfile.write(f"data: {json.dumps(event)}\\n\\n".encode())
            self.wfile.flush()
    """

    def __init__(self, project_name: str, poll_interval: float = 1.0):
        self.project_name = project_name
        self.poll_interval = poll_interval
        self._last_count = 0
        self._msg_file = _messages_file(project_name)

    def _message_count(self) -> int:
        """Count current messages in the log."""
        if not self._msg_file.exists():
            return 0
        line_count = 0
        with open(self._msg_file, "r") as f:
            for _ in f:
                line_count += 1
        return line_count

    def poll(self, max_events: int = 50):
        """Generator that yields new events as they appear.

        Keepalive messages do NOT count toward max_events, so the stream
        stays open for a reasonable duration even without new messages.
        Yields message dicts. Returns (stops) when max_events reached.
        """
        msg_count = 0
        keepalive_count = 0
        while msg_count < max_events and keepalive_count < 30:
            current_count = self._message_count()
            if current_count > self._last_count:
                # Read new messages
                new_msgs = []
                with open(self._msg_file, "r") as f:
                    lines = f.readlines()
                for line in lines[self._last_count:]:
                    line = line.strip()
                    if line:
                        try:
                            new_msgs.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
                self._last_count = current_count
                for msg in new_msgs:
                    yield msg
                    msg_count += 1
                    if msg_count >= max_events:
                        return

            # No new messages yet — yield a keepalive (not counted toward max_events)
            keepalive_count += 1
            if keepalive_count >= 30:
                return  # 30s idle timeout
            yield {"type": "keepalive", "timestamp": datetime.now().isoformat()}

            import time
            time.sleep(self.poll_interval)


# ── Project lookup by goal_id ──

import re as _re

# Leading @mention: "@researcher summarise this" -> ("researcher", "summarise this")
MENTION_RE = _re.compile(r"^\s*@([a-zA-Z0-9_\-]+)\s*(.*)$", _re.DOTALL)


def parse_mention(message: str):
    """Detect a leading @agent mention.

    Returns (agent_key_or_None, cleaned_message). If no leading mention is
    present returns (None, original). The cleaned message has the mention
    token removed so the target agent receives a natural instruction.
    """
    if not message:
        return None, message or ""
    m = MENTION_RE.match(message)
    if not m:
        # Still trim if merely whitespace
        return None, message.strip()
    return m.group(1).lower(), (m.group(2) or "").strip()


def list_agents_used(project_name: str, limit: int = 200) -> list[str]:
    """Return the distinct agent names that have appeared in this project's chat."""
    msgs = get_messages(project_name, limit=limit)
    seen = {}
    for msg in msgs:
        an = (msg.get("agent_name") or "").strip()
        if an and an != "user":
            seen.setdefault(an, msg)
    # Most recently seen first
    return list(reversed(sorted(seen.keys(), key=lambda k: seen[k].get("timestamp", ""))))


def get_team_roster(project_name: str, limit: int = 100) -> list[dict]:
    """Build the Slack-like 'Team' roster for a project.

    Each row = a registry agent merged with live state:
      - registry metadata (key, name, emoji, role, status, skills, triggers, description)
      - current_task: derived from the project's scrum status (active tasks + tier)
      - last_activity / active: from the project chat transcript feed
    """
    roster = []

    # Agent definitions from the registry
    try:
        from agent_registry import merged_definitions
        agents = merged_definitions()
    except Exception:
        agents = {}

    # Current work from scrum status
    working = _scrum_working(project_name)

    # Recent activity attribution
    activity = {}
    try:
        msgs = get_messages(project_name, limit=limit)
        for msg in msgs:
            an = (msg.get("agent_name") or "").strip()
            if an and an != "user":
                activity[an] = {
                    "activity": (msg.get("content") or "")[:160],
                    "timestamp": msg.get("timestamp", ""),
                }
    except Exception:
        pass

    for key, agent in agents.items():
        name = agent.get("name") or key
        entry = {
            "key": key,
            "name": name,
            "emoji": agent.get("emoji", "🤖"),
            "role": agent.get("role", ""),
            "status": agent.get("status", "active"),
            "skills": agent.get("skills", []),
            "triggers": agent.get("triggers", []),
            "description": agent.get("description", ""),
            "model": agent.get("model", ""),
        }
        # Current work (match by tier label or agent name)
        work = _match_work(working, key, name)
        entry["current_task"] = work
        # Recent activity
        act = activity.get(name) or activity.get(key)
        entry["last_activity"] = act["activity"] if act else ""
        entry["last_active_at"] = act["timestamp"] if act else ""
        entry["is_working"] = bool(work) or bool(act)
        roster.append(entry)

    # Sort: working agents first, then alphabetical
    roster.sort(key=lambda r: (not r["is_working"], r["name"].lower()))
    return roster


def _scrum_working(project_name: str) -> list[dict]:
    """Return active (in-progress/assigned) tasks from the project scrum status."""
    state_path = PROJECTS_DIR / project_name / ".antigravity" / "scrum_status.json"
    if not state_path.exists():
        state_path = PROJECTS_DIR / project_name / ".os_state.json"
    try:
        data = json.loads(state_path.read_text())
    except Exception:
        return []
    tasks = data.get("tasks") or []
    out = []
    active_states = {"in_progress", "assigned", "IN_PROGRESS", "ASSIGNED", "evaluation", "EVALUATION"}
    for t in tasks:
        st = (t.get("status") or "").lower()
        if st in {"in_progress", "assigned", "evaluation"}:
            tier = t.get("tier")
            tier_label = "Tier 2 Architect (Gemini)" if tier in (2, "2") else ("Tier 3 Builder (Ollama)" if tier in (3, "3") else "Pipeline")
            out.append({
                "task_id": t.get("id", ""),
                "title": t.get("title") or t.get("text") or "",
                "tier": tier,
                "tier_label": tier_label,
                "status": st,
                "agent_label": tier_label,
            })
    return out


def _match_work(working: list[dict], agent_key: str, agent_name: str) -> dict | None:
    """Match scrum work to a roster agent by tier label or name keywords."""
    if not working:
        return None
    key = agent_key.lower()
    name = agent_name.lower()
    for w in working:
        label = (w.get("agent_label") or "").lower()
        if key == "developer" and "tier 3" in label:
            return w
        if key == "architect" and "tier 2" in label:
            return w
        if key in ("pm", "project manager") and "pipeline" in label:
            return w
        if key in label or name in label:
            return w
    return working[0] if key == "hermes" else None


def find_project_by_goal_id(goal_id: str) -> Optional[str]:
    """Scan all projects to find which one contains a given goal_id.

    Checks both the 'id' field and the project task's 'goal_id' field.
    Returns the project name, or None if not found.
    """
    if not goal_id or not PROJECTS_DIR.exists():
        return None

    for item in sorted(PROJECTS_DIR.iterdir()):
        if not item.is_dir() or item.name.startswith("."):
            continue
        state_path = item / ".os_state.json"
        if not state_path.exists():
            continue
        try:
            state = json.loads(state_path.read_text())
            # Check goals
            for g in state.get("goals", []):
                if g.get("id") == goal_id:
                    return item.name
            # Check tasks (which reference goal_id)
            for t in state.get("tasks", []):
                if t.get("goal_id") == goal_id:
                    return item.name
        except Exception:
            continue
    return None


def find_project_by_task_id(task_id: str) -> Optional[str]:
    """Scan all projects to find which one contains a given task_id."""
    if not task_id or not PROJECTS_DIR.exists():
        return None

    for item in sorted(PROJECTS_DIR.iterdir()):
        if not item.is_dir() or item.name.startswith("."):
            continue
        state_path = item / ".os_state.json"
        if not state_path.exists():
            continue
        try:
            state = json.loads(state_path.read_text())
            for t in state.get("tasks", []):
                if t.get("id") == task_id:
                    return item.name
        except Exception:
            continue
    return None


def emit_task_transcript(
    goal_id: str,
    task_id: str,
    agent_name: str,
    message: str,
    msg_type: str = "transcript",
) -> bool:
    """Emit an agent transcript for a task, auto-discovering the project.

    Looks up the project by goal_id or task_id, then logs the transcript.

    Args:
        goal_id: The goal ID this task belongs to.
        task_id: The task ID.
        agent_name: The agent name (e.g. 'researcher', 'developer').
        message: The transcript message.
        msg_type: 'transcript', 'goal_update', 'task_complete'.

    Returns:
        True if the transcript was logged, False if no project was found.
    """
    project_name = find_project_by_goal_id(goal_id) or find_project_by_task_id(task_id)
    if not project_name:
        return False

    add_agent_transcript(
        project_name=project_name,
        agent_name=agent_name,
        content=message,
        msg_type=msg_type,
        goal_id=goal_id,
        task_id=task_id,
    )
    return True


def emit_task_started(goal_id: str, task_id: str, agent_name: str, task_title: str) -> bool:
    """Emit a 'task started' transcript for the project chat with enhanced context."""
    project_name = find_project_by_goal_id(goal_id) or find_project_by_task_id(task_id)
    if not project_name:
        return False
    
    # Get goal context for better messaging
    goal_title = ""
    try:
        from goal_engine import load_goals
        goals = load_goals()
        goal = next((g for g in goals if g.get("id") == goal_id), None)
        if goal:
            goal_title = f" for '{goal.get('title', '')}'"
    except Exception:
        pass  # Best effort
    
    message = f"🚀 Started task: {task_title}{goal_title}"
    return add_agent_transcript(
        project_name=project_name,
        agent_name=agent_name,
        content=message,
        msg_type="transcript",
        goal_id=goal_id,
        task_id=task_id
    )
def emit_task_completed(goal_id: str, task_id: str, agent_name: str, task_title: str, ok: bool = True) -> bool:
    """Emit a 'task completed/failed' transcript for the project chat."""
    project_name = find_project_by_goal_id(goal_id) or find_project_by_task_id(task_id)
    if not project_name:
        return False
    
    if ok:
        message = f"✅ Completed task: {task_title}"
    else:
        message = f"❌ Failed task: {task_title}"
    
    return add_agent_transcript(
        project_name=project_name,
        agent_name=agent_name,
        content=message,
        msg_type="transcript",
        goal_id=goal_id,
        task_id=task_id
    )





def emit_agent_result(goal_id: str, task_id: str, agent_name: str, result_summary: str, artifacts: list = None) -> bool:
    """Emit a summary of what an agent produced."""
    project_name = find_project_by_goal_id(goal_id) or find_project_by_task_id(task_id)
    if not project_name:
        return False
    
    artifacts_part = ""
    if artifacts:
        artifacts_part = f" Created: {', '.join(artifacts[:3])}"
        if len(artifacts) > 3:
            artifacts_part += f" and {len(artifacts)-3} more"
    
    message = f"✨ {agent_name} completed: {result_summary}{artifacts_part}"
    
    return add_agent_transcript(
        project_name=project_name,
        agent_name=agent_name,
        content=message,
        msg_type="transcript",
        goal_id=goal_id,
        task_id=task_id
    )



def emit_agent_collaboration(goal_id: str, task_id: str, from_agent: str, 
                            to_agent: str, message: str, 
                            collaboration_type: str = "handoff") -> bool:
    """Emit when agents collaborate or handoff work."""
    project_name = find_project_by_goal_id(goal_id) or find_project_by_task_id(task_id)
    if not project_name:
        return False
    
    # Determine emoji based on collaboration type
    emoji_map = {
        "handoff": "🔄",
        "review": "👀",
        "approval": "👍",
        "feedback": "💬",
        "question": "❓",
        "request": "🙋‍♂️"
    }
    emoji = emoji_map.get(collaboration_type, "🤝")
    
    collab_msg = f"{emoji} {from_agent} → {to_agent}: {message}"
    
    return add_agent_transcript(
        project_name=project_name,
        agent_name=f"{from_agent}→{to_agent}",
        content=collab_msg,
        msg_type="transcript",
        goal_id=goal_id,
        task_id=task_id
    )

def emit_progress_update(goal_id: str, task_id: str, agent_name: str, 
                         progress: str, percentage: int = None) -> bool:
    """Emit a progress update from an agent."""
    project_name = find_project_by_goal_id(goal_id) or find_project_by_task_id(task_id)
    if not project_name:
        return False
    
    if percentage is not None:
        progress_msg = f"📈 {agent_name} progress ({percentage}%): {progress}"
    else:
        progress_msg = f"📈 {agent_name} progress: {progress}"
    
    return add_agent_transcript(
        project_name=project_name,
        agent_name=agent_name,
        content=progress_msg,
        msg_type="transcript",
        goal_id=goal_id,
        task_id=task_id
    )


# Enhanced transcript helper functions
def emit_agent_thinking(goal_id: str, task_id: str, agent_name: str, thought: str) -> bool:
    """Emit an agent's thought process or reasoning."""
    project_name = find_project_by_goal_id(goal_id) or find_project_by_task_id(task_id)
    if not project_name:
        return False
    
    # Truncate long thoughts for readability
    display_thought = thought[:200] + ("..." if len(thought) > 200 else "")
    message = f"💭 {agent_name} thinking: {display_thought}"
    
    return add_agent_transcript(
        project_name=project_name,
        agent_name=agent_name,
        content=message,
        msg_type="transcript",
        goal_id=goal_id,
        task_id=task_id
    )


def emit_tool_usage(goal_id: str, task_id: str, agent_name: str, tool_name: str, 
                   tool_input: str = "", tool_output: str = "") -> bool:
    """Emit when an agent uses a specific tool."""
    project_name = find_project_by_goal_id(goal_id) or find_project_by_task_id(task_id)
    if not project_name:
        return False
    
    # Build the message
    parts = [f"🔧 {agent_name} used tool: {tool_name}"]
    if tool_input:
        # Truncate for readability
        input_preview = tool_input[:100] + ("..." if len(tool_input) > 100 else "")
        parts.append(f"   Input: {input_preview}")
    if tool_output:
        # Truncate for readability
        output_preview = tool_output[:100] + ("..." if len(tool_output) > 100 else "")
        parts.append(f"   Output: {output_preview}")
    
    message = "\n".join(parts)
    
    return add_agent_transcript(
        project_name=project_name,
        agent_name=agent_name,
        content=message,
        msg_type="transcript",
        goal_id=goal_id,
        task_id=task_id
    )


def emit_artifact_created(goal_id: str, task_id: str, agent_name: str, 
                         artifact_type: str, artifact_name: str, 
                         description: str = "") -> bool:
    """Emit when an agent creates an artifact (file, document, code, etc.)."""
    project_name = find_project_by_goal_id(goal_id) or find_project_by_task_id(task_id)
    if not project_name:
        return False
    
    # Choose emoji based on artifact type
    emoji_map = {
        "code": "💻",
        "document": "📄", 
        "image": "🖼️",
        "data": "📊",
        "plan": "📝",
        "design": "🎨",
        "report": "📋",
        "script": "⚡"
    }
    emoji = emoji_map.get(artifact_type, "📎")
    
    message = f"{emoji} {agent_name} created {artifact_type}: {artifact_name}"
    if description:
        # Truncate description
        desc_preview = description[:100] + ("..." if len(description) > 100 else "")
        message += f" - {desc_preview}"
    
    return add_agent_transcript(
        project_name=project_name,
        agent_name=agent_name,
        content=message,
        msg_type="transcript",
        goal_id=goal_id,
        task_id=task_id
    )
