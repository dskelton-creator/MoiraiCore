#!/usr/bin/env python3
"""
MoiraiCore — Vault MCP Server
MCP server that exposes the memory vault as tools for any AI agent.
Uses SQLite FTS5 index for fast search and graph queries.

Tools:
- vault_search: Full-text search across all vault files (FTS5)
- vault_read: Read a specific vault file
- vault_list: List all vault files by folders
- vault_write: Write/update a vault file
- vault_goals: List active goals
- vault_graph: Get vault knowledge graph data
- vault_stats: Get vault statistics
- vault_backlinks: Get backlinks for a file
"""

import json
import os
import sys
from pathlib import Path

_root = Path(os.environ.get("AGENT_OS_ROOT", str(Path(__file__).resolve().parents[1])))
VAULT = _root / "memory-vault"
WORKSPACE = _root / "workspace"
GOALS_FILE = _root / "config" / "goals.json"
SCRIPTS = _root / "scripts"

# Ensure vault_index is importable
sys.path.insert(0, str(SCRIPTS))


def _get_index():
    from vault_index import VaultIndex
    idx = VaultIndex()
    idx.sync()
    return idx


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def handle_initialize(req):
    return {
        "protocolVersion": "2024-11-05",
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "moiraicore-os-vault", "version": "2.0.0"},
    }


def handle_tools_list(req):
    return {
        "tools": [
            {
                "name": "vault_search",
                "description": "Full-text search across all vault markdown files using SQLite FTS5. Supports natural language queries, phrases, and keywords. Returns ranked results with snippets.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query — keywords, phrases, or natural language"},
                        "max_results": {"type": "integer", "description": "Max results (default 10)", "default": 10},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "vault_read",
                "description": "Read the full content of a vault file by path (relative to vault root).",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path relative to vault root, e.g. 'context/about-me.md'"},
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "vault_list",
                "description": "List all vault files, optionally filtered by folder.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "folder": {"type": "string", "description": "Filter by folder: context, daily, projects, agents, skills, loop (or 'all')", "default": "all"},
                    },
                },
            },
            {
                "name": "vault_write",
                "description": "Create or update a vault file. Index is updated automatically.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path relative to vault root"},
                        "content": {"type": "string", "description": "Markdown content to write"},
                        "mode": {"type": "string", "description": "'write' to overwrite, 'append' to add to end", "default": "write"},
                    },
                    "required": ["path", "content"],
                },
            },
            {
                "name": "vault_goals",
                "description": "List active goals with priority and agent assignment.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string", "description": "Filter: 'active', 'done', 'all'", "default": "all"},
                    },
                },
            },
            {
                "name": "vault_graph",
                "description": "Get vault knowledge graph — nodes (files) and links (wikilags, tags, folders). Uses SQLite index.",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "vault_stats",
                "description": "Get vault statistics — file counts by folder, total notes, last sync.",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "vault_backlinks",
                "description": "Get all files that link to a given file (reverse links).",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path to get backlinks for"},
                    },
                    "required": ["path"],
                },
            },
        ]
    }


def vault_search(query, max_results=10):
    try:
        idx = _get_index()
        return idx.search(query, max_results)
    except Exception as e:
        return [{"error": str(e)}]


def vault_read(path):
    try:
        idx = _get_index()
        result = idx.read_file(path)
        if not result:
            return {"error": f"File not found: {path}"}
        return result
    except Exception as e:
        return {"error": str(e)}


def vault_list(folder="all"):
    try:
        idx = _get_index()
        return idx.list_files(folder)
    except Exception as e:
        return []


def vault_write(path, content, mode="write"):
    try:
        idx = _get_index()
        return idx.write_file(path, content, mode)
    except Exception as e:
        return {"error": str(e)}


def vault_goals(status="all"):
    if GOALS_FILE.exists():
        goals = json.loads(GOALS_FILE.read_text())
    else:
        goals = []
    if status != "all":
        goals = [g for g in goals if (g.get("done") == (status == "done"))]
    return goals


def vault_graph():
    try:
        idx = _get_index()
        return idx.graph()
    except Exception as e:
        return {"nodes": [], "links": [], "error": str(e)}


def vault_stats():
    try:
        idx = _get_index()
        return idx.stats()
    except Exception as e:
        return {"error": str(e)}


def vault_backlinks(path):
    try:
        idx = _get_index()
        return idx.get_backlinks(path)
    except Exception as e:
        return []


def handle_tool_call(req):
    name = req.get("name", "")
    args = req.get("arguments", {})
    try:
        if name == "vault_search":
            results = vault_search(args.get("query", ""), args.get("max_results", 10))
        elif name == "vault_read":
            results = vault_read(args.get("path", ""))
        elif name == "vault_list":
            results = vault_list(args.get("folder", "all"))
        elif name == "vault_write":
            results = vault_write(args.get("path", ""), args.get("content", ""), args.get("mode", "write"))
        elif name == "vault_goals":
            results = vault_goals(args.get("status", "all"))
        elif name == "vault_graph":
            results = vault_graph()
        elif name == "vault_stats":
            results = vault_stats()
        elif name == "vault_backlinks":
            results = vault_backlinks(args.get("path", ""))
        else:
            return {"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True}
        return {"content": [{"type": "text", "text": json.dumps(results, indent=2)}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Error: {str(e)}"}], "isError": True}


def main():
    log(f"MoiraiCore Vault MCP v2 (SQLite) — vault: {VAULT}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = req.get("method", "")
        req_id = req.get("id")
        if method == "initialize":
            resp = {"jsonrpc": "2.0", "id": req_id, "result": handle_initialize(req)}
        elif method == "tools/list":
            resp = {"jsonrpc": "2.0", "id": req_id, "result": handle_tools_list(req)}
        elif method == "tools/call":
            resp = {"jsonrpc": "2.0", "id": req_id, "result": handle_tool_call(req.get("params", {}))}
        elif method == "notifications/initialized":
            continue
        elif method == "ping":
            resp = {"jsonrpc": "2.0", "id": req_id, "result": {}}
        else:
            resp = {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Unknown method: {method}"}}
        print(json.dumps(resp), flush=True)


if __name__ == "__main__":
    main()
