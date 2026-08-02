#!/usr/bin/env python3
"""
MoiraiCore — Activity Logger CLI
Log and query agent activity from the command line.

Usage:
    python3 agent_activity.py log --agent researcher --action "searched ASX trends" --status completed --duration 4500
    python3 agent_activity.py feed [--agent hydes] [--limit 20]
    python3 agent_activity.py stats
    python3 agent_activity.py route "write a blog post about cybersecurity"
"""

import argparse
import os
import json
import sys
import time
from pathlib import Path

AGENT_OS_ROOT = Path(os.environ.get("AGENT_OS_ROOT", str(Path(__file__).resolve().parents[1])))
sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))

from vault_index import log_activity, get_activity, get_activity_stats, log_output, get_outputs
from agent_registry import find_agent_for_task, list_agents, get_agent


def cmd_log(args):
    """Log an agent activity."""
    kwargs = {
        "agent": args.agent,
        "action": args.action,
        "task": args.task or "",
        "status": args.status or "completed",
        "model": args.model or "",
        "details": args.details or "",
    }
    if args.duration:
        kwargs["duration_ms"] = int(args.duration * 1000)
    log_activity(**kwargs)
    print(f"✅ Logged: {args.agent} — {args.action} [{kwargs['status']}]")


def cmd_feed(args):
    """Show activity feed."""
    entries = get_activity(agent=args.agent, status=args.status, limit=args.limit or 20)
    if not entries:
        print("No activity entries found.")
        return
    for e in entries:
        ts = e["timestamp"][:19] if e["timestamp"] else "?"
        dur = f" ({e['duration_ms']}ms)" if e.get("duration_ms") else ""
        task_str = f" | {e['task']}" if e.get("task") else ""
        print(f"  {ts}  {e['agent']:12s}  {e['action']}{task_str}{dur}  [{e['status']}]")


def cmd_stats(args):
    """Show activity statistics."""
    s = get_activity_stats()
    print(f"📊 Activity Stats — {s['total_entries']} total entries")
    print()
    print("By Agent:")
    for agent, data in s.get("by_agent", {}).items():
        ms = data.get("total_ms", 0)
        dur_str = f"{ms/1000:.1f}s" if ms else "—"
        print(f"  {agent:12s}  {data['count']:4d} entries  total: {dur_str}")
    print()
    print("By Status:")
    for status, count in s.get("by_status", {}).items():
        print(f"  {status:12s}  {count}")


def cmd_outputs(args):
    """Show logged outputs."""
    entries = get_outputs(agent=args.agent, limit=args.limit or 20)
    if not entries:
        print("No output entries found.")
        return
    for e in entries:
        ts = e["timestamp"][:19] if e["timestamp"] else "?"
        q = f" Q{e['quality_score']}" if e.get("quality_score") else ""
        print(f"  {ts}  {e['agent']:12s}  {e['output_type']:6s}  {e['output_path'] or '—'}{q}")


def cmd_agents(args):
    """List all agents."""
    agents = list_agents(status_filter=args.status or "all")
    for a in agents:
        status_icon = "🟢" if a["status"] == "active" else "⏳"
        files = f"{a['file_count']} files" if a["file_count"] else "no memory yet"
        print(f"  {status_icon} {a['emoji']} {a['name']:20s}  {a['role']:30s}  {files}")


def cmd_route(args):
    """Route a task to the best agent."""
    task = args.task
    agent_key, confidence, triggers = find_agent_for_task(task)
    agent = get_agent(agent_key)
    if agent:
        trig_str = ", ".join(triggers) if triggers else "default fallback"
        print(f"📡 Routed to: {agent['emoji']} {agent['name']} ({agent['role']})")
        print(f"   Confidence: {confidence}")
        print(f"   Matched: {trig_str}")
    else:
        print(f"Could not route: {task}")


def cmd_memory(args):
    """Read an agent's memory."""
    content = get_agent(args.name)
    if not content:
        print(f"Agent '{args.name}' not found.")
        return
    mem_path = content.get("memory_path")
    if mem_path and (Path(mem_path) / "memory.md").exists():
        print(Path(mem_path, "memory.md").read_text())
    elif mem_path:
        print(f"Memory path: {mem_path}")
        print("(No memory.md yet)")
    else:
        print(f"Agent '{args.name}' has no dedicated memory folder.")


def main():
    parser = argparse.ArgumentParser(description="MoiraiCore Activity Logger")
    sub = parser.add_subparsers(dest="command")

    # log
    p_log = sub.add_parser("log", help="Log an agent activity")
    p_log.add_argument("--agent", required=True, help="Agent name")
    p_log.add_argument("--action", required=True, help="Action description")
    p_log.add_argument("--task", default="", help="Task ID or title")
    p_log.add_argument("--status", default="completed", help="Status")
    p_log.add_argument("--duration", type=float, help="Duration in seconds")
    p_log.add_argument("--model", default="", help="Model used")
    p_log.add_argument("--details", default="", help="Extra details")

    # feed
    p_feed = sub.add_parser("feed", help="Show activity feed")
    p_feed.add_argument("--agent", help="Filter by agent")
    p_feed.add_argument("--status", help="Filter by status")
    p_feed.add_argument("--limit", type=int, default=20)

    # stats
    sub.add_parser("stats", help="Show activity statistics")

    # outputs
    p_out = sub.add_parser("outputs", help="Show logged outputs")
    p_out.add_argument("--agent", help="Filter by agent")
    p_out.add_argument("--limit", type=int, default=20)

    # agents
    p_agents = sub.add_parser("agents", help="List all agents")
    p_agents.add_argument("--status", help="Filter by status")

    # route
    p_route = sub.add_parser("route", help="Route a task to best agent")
    p_route.add_argument("task", help="Task description")

    # memory
    p_mem = sub.add_parser("memory", help="Read an agent's memory")
    p_mem.add_argument("name", help="Agent name")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    dispatch = {
        "log": cmd_log,
        "feed": cmd_feed,
        "stats": cmd_stats,
        "outputs": cmd_outputs,
        "agents": cmd_agents,
        "route": cmd_route,
        "memory": cmd_memory,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
