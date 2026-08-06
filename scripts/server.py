#!/usr/bin/env python3
"""
MoiraiCore — Local Server v2
Serves the Mission Control dashboard and provides a simple API.
Uses a small set of helper modules from this directory (auth_mixin,
activity_log, semantic_search) imported best-effort so the server still
starts if an optional module is missing.

Usage:
    python3 server.py          # Start on port 7878
    python3 server.py --port X # Start on custom port
"""

import base64
import http.server
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from auth_mixin import install_auth
from auth_mixin import _auth

# Atomic JSON persistence (Feature 10)
try:
    from db import atomic_save_json, atomic_load_json
except ImportError:
    # Fallback: non-atomic write (pre-db.py behavior)
    def atomic_save_json(path, data, indent=2):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=indent, default=str))

    def atomic_load_json(path, default=None):
        if path.exists():
            try:
                return json.loads(path.read_text())
            except Exception:
                pass
        return default if default is not None else ([] if str(path).endswith("tasks.json") else {})

# OpenAI OAuth provider (Sign in with ChatGPT via the openai-oauth bridge)
try:
    from openai_oauth import bridge_status, connect, disconnect, openai_compatible_config
    _HAS_OPENAI_OAUTH = True
except Exception:  # pragma: no cover - optional module
    _HAS_OPENAI_OAUTH = False

# First-run system configuration (setup wizard).
try:
    import setup as _setup_mod
    _HAS_SETUP = True
except Exception:  # pragma: no cover - optional module
    _HAS_SETUP = False

# Google OAuth provider (Sign in with Google, PKCE).
try:
    import google_oauth as _google_mod
    _HAS_GOOGLE_OAUTH = True
except Exception:  # pragma: no cover - optional module
    _HAS_GOOGLE_OAUTH = False

_auth_installed = False


def _google_callback_html(token_payload, err=None):
    """Minimal same-origin HTML that hands a Google OAuth result to the SPA.

    Called from the GET /api/auth/google/callback route (Google redirects the
    browser there). Being same-origin, this page can write directly into
    localStorage, then reload the app root where the dashboard auth state is
    picked up. On error it records moirai_oauth_error for the dashboard to show.
    """
    import html as _html
    if token_payload:
        payload_json = json.dumps(token_payload)
        script = (
            "try{"
            "var d=" + payload_json + ";"
            "localStorage.setItem('moirai_token', d.token);"
            "localStorage.setItem('moirai_user', JSON.stringify({"
            "  username:d.username, display_name:d.display_name, role:d.role}));"
            "localStorage.removeItem('moirai_oauth_error');"
            "}catch(e){localStorage.setItem('moirai_oauth_error', String(e));}"
            "window.location.href='/';"
        )
        title = "Signed in with Google"
    else:
        msg = _html.escape(err or "Google sign-in failed")
        escaped = json.dumps({"message": msg})
        script = (
            "try{localStorage.setItem('moirai_oauth_error', "
            + escaped + ".message);}catch(e){}"
            "window.location.href='/';"
        )
        title = "Google sign-in error"
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\"><title>" + title
        + "</title></head><body><noscript><p>Redirecting…</p></noscript>"
        "<script>" + script + "</script></body></html>"
    )


PORT = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 7878

# Load environment variables from .env file (keys only, no values in source)
_env_file = Path(__file__).resolve().parents[1] / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            if _k.strip() and _v.strip():
                os.environ.setdefault(_k.strip(), _v.strip())

AGENT_OS_ROOT = Path(os.environ.get("AGENT_OS_ROOT", str(Path(__file__).resolve().parents[1])))
# Ensure project scripts are importable everywhere (modules import each other by name).
if str(AGENT_OS_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
# Live-activity bus (best-effort; never breaks server startup if missing).
try:
    from activity_log import log_activity as _log_activity
except Exception:
    _log_activity = None
# Semantic search config (best-effort; defaults to tfidf if module missing).
try:
    from semantic_search import SEMANTIC_BACKEND
except Exception:
    SEMANTIC_BACKEND = "tfidf"
DASHBOARD = AGENT_OS_ROOT / "dashboard" / "index.html"
WORKSPACE = AGENT_OS_ROOT / "workspace"
VAULT = AGENT_OS_ROOT / "memory-vault"
TASKS_FILE = AGENT_OS_ROOT / "config" / "tasks.json"
KANBAN_FILE = AGENT_OS_ROOT / "config" / "kanban.json"

HERMES_BRIDGE = AGENT_OS_ROOT / "scripts" / "hermes_bridge.py"
AGENT_REGISTRY_SCRIPT = AGENT_OS_ROOT / "scripts" / "agent_registry.py"

def _run_hermes_bridge(args: list[str], timeout: int = 300) -> dict:
    """Run the Hermes bridge and return parsed JSON result."""
    try:
        r = subprocess.run(
            [sys.executable, str(HERMES_BRIDGE)] + args,
            capture_output=True, text=True, timeout=timeout,
        )
        try:
            return json.loads(r.stdout)
        except Exception:
            return {"ok": r.returncode == 0, "response": r.stdout[-2000:], "error": r.stderr[-500:]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Hermes timed out after %ds" % timeout}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ── Agent Registry helper ──
def _get_agent_registry():
    """Lazy-load the agent registry."""
    from agent_registry import AgentRegistry
    return AgentRegistry()

def _get_vault_index():
    """Re-export vault_index (activity log lives here)."""
    return vault()

# ── File-based JSON caching (in-memory, per-process) ──
_file_cache = {}  # path -> (mtime, data)

def _cached_json_load(path, default=None):
    """Load JSON file with in-memory caching based on mtime."""
    try:
        mtime = path.stat().st_mtime
        if path in _file_cache:
            cached_mtime, cached_data = _file_cache[path]
            if cached_mtime == mtime:
                return cached_data
        data = json.loads(path.read_text())
        _file_cache[path] = (mtime, data)
        return data
    except Exception:
        return default if default is not None else []

def _invalidate_cache(path=None):
    """Invalidate cache for a specific path or all paths."""
    if path:
        _file_cache.pop(path, None)
    else:
        _file_cache.clear()

def load_tasks():
    # Deep-copy so callers mutate their own list and the cache stays pristine.
    # Required for save_tasks()'s transition diff to see the *previous* state.
    import copy as _copy
    return _copy.deepcopy(_cached_json_load(TASKS_FILE, []))

def save_tasks(tasks):
    # Diff previous statuses to emit live-activity transitions.
    # Use the in-memory cache snapshot (not a fresh disk read) so the diff is
    # accurate even when writes happen back-to-back within the same process.
    _prev_map = {}
    if TASKS_FILE in _file_cache:
        try:
            for t in _file_cache[TASKS_FILE][1]:
                if isinstance(t, dict):
                    _prev_map[str(t.get("id"))] = t.get("status")
        except Exception:
            _prev_map = {}
    # Atomic write (Feature 10) — prevents data corruption on crash
    atomic_save_json(TASKS_FILE, tasks)
    _invalidate_cache(TASKS_FILE)
    # Emit a transition event for any task whose status changed.
    if _log_activity is None:
        return
    for t in tasks:
        if not isinstance(t, dict):
            continue
        tid = str(t.get("id", ""))
        if not tid:
            continue
        new_status = str(t.get("status", ""))
        old_status = _prev_map.get(tid)
        if old_status != new_status:
            try:
                if old_status is None:
                    # New task created.
                    _log_activity(
                        "New task created: " + str(t.get("title") or "Untitled"),
                        kind="task", agent=str(t.get("agent", "any")),
                        goal_id=str(t.get("goal_id", "")), task_id=tid,
                        stage="queued", status="queued",
                    )
                else:
                    _stage = {"running": "running", "in_progress": "running",
                              "completed": "done", "failed": "failed",
                              "paused": "paused", "queued": "queued"}.get(new_status, new_status)
                    _label = {"running": "started", "in_progress": "started",
                               "completed": "completed", "failed": "failed",
                               "paused": "paused", "queued": "re-queued"}.get(new_status, new_status)
                    _log_activity(
                        "Task " + str(_label) + ": " + str(t.get("title") or "Untitled"),
                        kind="task", agent=str(t.get("agent", "any")),
                        goal_id=str(t.get("goal_id", "")), task_id=tid,
                        stage=str(_stage), status=new_status,
                    )
            except Exception:
                pass

GOAL_FILE = AGENT_OS_ROOT / "config" / "goals.json"

def load_goals():
    # Deep-copy so callers mutate their own list and the cache stays pristine.
    # Required for save_goals()'s transition diff to see the *previous* state.
    import copy as _copy
    return _copy.deepcopy(_cached_json_load(GOAL_FILE, []))

def save_goals(goals):
    # Diff previous statuses to emit live-activity transitions for goals.
    # Use the in-memory cache snapshot (accurate for back-to-back writes).
    _prev_map = {}
    if GOAL_FILE in _file_cache:
        try:
            for g in _file_cache[GOAL_FILE][1]:
                if isinstance(g, dict):
                    _prev_map[str(g.get("id"))] = g.get("status")
        except Exception:
            _prev_map = {}
    GOAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write (Feature 10) — prevents data corruption on crash
    atomic_save_json(GOAL_FILE, goals)
    _invalidate_cache(GOAL_FILE)
    if _log_activity is None:
        return
    for g in goals:
        if not isinstance(g, dict):
            continue
        gid = str(g.get("id", ""))
        if not gid:
            continue
        new_status = str(g.get("status", ""))
        old_status = _prev_map.get(gid)
        if old_status != new_status:
            try:
                if old_status is None:
                    _log_activity(
                        "New goal created: " + str(g.get("title") or "Untitled"),
                        kind="goal", agent=str(g.get("agent", "")),
                        goal_id=gid, stage="queued", status="queued",
                    )
                else:
                    _stage = {"decomposed": "running", "in_progress": "running",
                              "completed": "done", "failed": "failed",
                              "paused": "paused", "paused_guard": "guard",
                              "active": "running"}.get(new_status, new_status)
                    _label = {"decomposed": "decomposed into tasks",
                               "in_progress": "started", "completed": "completed",
                               "failed": "failed", "paused": "paused",
                               "paused_guard": "paused by guard",
                               "active": "activated"}.get(new_status, new_status)
                    _log_activity(
                        "Goal " + str(_label) + ": " + str(g.get("title") or "Untitled"),
                        kind="goal", agent=str(g.get("agent", "")),
                        goal_id=gid, stage=str(_stage), status=new_status,
                    )
            except Exception:
                pass

# ── Workspace scan caching ──
_ws_cache = {}  # None -> (mtime_tuple, files)

def scan_workspace():
    """Scan workspace with directory-mtime caching."""
    if not WORKSPACE.exists():
        return []
    try:
        mtimes = tuple((d, d.stat().st_mtime) for d in WORKSPACE.iterdir() if not d.name.startswith("."))
        if _ws_cache and _ws_cache[0] == mtimes:
            return _ws_cache[1]
    except Exception:
        mtimes = ()
    files = []
    for sub in WORKSPACE.iterdir():
        if sub.name.startswith("."):
            continue
        if sub.is_dir():
            for f in sub.rglob("*"):
                if f.is_file() and not f.name.startswith("."):
                    files.append(_file_info(f))
        elif sub.is_file():
            files.append(_file_info(sub))
    _ws_cache[0] = mtimes
    _ws_cache[1] = files
    return files

# ── Kanban persistence ──
LANE_MAP = {"queued": "backlog", "running": "progress", "completed": "done", "failed": "backlog", "in_progress": "progress"}

def load_kanban():
    return _cached_json_load(KANBAN_FILE, {"backlog": [], "progress": [], "review": [], "done": []})

def save_kanban(board):
    KANBAN_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write (Feature 10) — prevents data corruption on crash
    atomic_save_json(KANBAN_FILE, board)
    _invalidate_cache(KANBAN_FILE)

def kanban_card_for_task(task):
    """Build a Kanban card from a task dict."""
    return {
        "id": "task-" + task["id"],
        "task_id": task["id"],
        "title": task["title"][:80],
        "desc": (task.get("desc") or "")[:200],
        "agent": task.get("agent", ""),
        "priority": task.get("priority", "p2"),
        "goal_id": task.get("goal_id", ""),
        "goal_title": task.get("goal_title", ""),
        "triggers": task.get("matched_triggers", []),
        "synced": True,
    }

def sync_task_to_kanban(task):
    """Sync a task status to its Kanban lane. Creates or moves the card."""
    board = load_kanban()
    card_id = "task-" + task["id"]
    lane = LANE_MAP.get(task.get("status", "queued"), "backlog")
    for col in board:
        board[col] = [c for c in board[col] if c.get("id") != card_id]
    if task.get("status") == "deleted":
        save_kanban(board)
        return
    card = kanban_card_for_task(task)
    existing = [c for c in board[lane] if c.get("id") == card_id]
    if not existing:
        board[lane].append(card)
    save_kanban(board)

# ── SQLite Vault Index ──
def get_vault_index():
    """Lazy-load the vault index."""
    sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
    from vault_index import VaultIndex
    idx = VaultIndex()
    idx.sync()
    return idx

_vault_idx = None
def vault():
    global _vault_idx
    if _vault_idx is None:
        _vault_idx = get_vault_index()
    return _vault_idx

TYPE_MAP = {
    ".png": "images", ".jpg": "images", ".jpeg": "images", ".gif": "images",
    ".webp": "images", ".svg": "images", ".bmp": "images",
    ".mp4": "videos", ".mov": "videos", ".avi": "videos", ".webm": "videos", ".mkv": "videos",
    ".mp3": "audio", ".wav": "audio", ".ogg": "audio", ".m4a": "audio", ".flac": "audio",
    ".md": "text", ".txt": "text", ".pdf": "text", ".doc": "text", ".docx": "text", ".rtf": "text",
    ".py": "code", ".js": "code", ".ts": "code", ".sh": "code", ".bash": "code",
    ".yaml": "code", ".yml": "code", ".json": "code", ".toml": "code",
    ".html": "code", ".css": "code",
}
ICONS = {"images": "🖼", "videos": "🎬", "audio": "🔊", "text": "📄", "code": "💻", "research": "🔍", "unknown": "📁"}
KNOWN_AGENTS = ["hermes", "claude", "codex", "openclaw", "antigravity", "gemini", "chatgpt", "studio"]
MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
    ".mp4": "video/mp4", ".webm": "video/webm",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg",
    ".pdf": "application/pdf", ".md": "text/markdown", ".txt": "text/plain",
    ".json": "application/json", ".html": "text/html", ".css": "text/css",
    ".js": "application/javascript",
}

# ── Markdown preview stylesheet (dark, matches dashboard) ──
_MARKDOWN_CSS = """
:root{color-scheme:dark}
*{box-sizing:border-box}
body.md{margin:0;padding:24px 30px;background:#0f0f17;color:#e3e3ec;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:14px;line-height:1.65}
.md h1,.md h2,.md h3,.md h4{color:#fff;line-height:1.3;margin:1.4em 0 .5em;font-weight:650}
.md h1{font-size:1.7em;border-bottom:1px solid #2a2a3a;padding-bottom:.3em}
.md h2{font-size:1.35em;border-bottom:1px solid #22222e;padding-bottom:.25em}
.md h3{font-size:1.15em}
.md p{margin:.7em 0}
.md a{color:#7aa2f7;text-decoration:none}.md a:hover{text-decoration:underline}
.md code{background:#1b1b27;padding:.15em .4em;border-radius:4px;font-family:Menlo,Consolas,monospace;font-size:.88em;color:#e0af68}
.md pre{background:#15151f;border:1px solid #26263a;border-radius:8px;padding:14px 16px;overflow:auto}
.md pre code{background:none;padding:0;color:#c0caf5}
.md blockquote{margin:.8em 0;padding:.4em 1em;border-left:3px solid #3b4261;color:#a9b1d6;background:#15151f}
.md table{border-collapse:collapse;width:100%;margin:1em 0;font-size:.92em}
.md th,.md td{border:1px solid #2a2a3a;padding:7px 10px;text-align:left}
.md th{background:#1a1a26;color:#fff}
.md img{max-width:100%;border-radius:6px}
.md ul,.md ol{padding-left:1.4em;margin:.6em 0}
.md hr{border:none;border-top:1px solid #2a2a3a;margin:1.4em 0}
.md strong{color:#fff}
"""

def _sanitize_html(html):
    """Strip active/unsafe content for in-app rendering (display-only, local tool)."""
    import re as _re
    html = _re.sub(r"<script[\s\S]*?</script>", "", html, flags=_re.I)
    html = _re.sub(r"<iframe[\s\S]*?</iframe>", "", html, flags=_re.I)
    html = _re.sub(r"<object[\s\S]*?</object>", "", html, flags=_re.I)
    html = _re.sub(r"<embed[\s\S]*?>", "", html, flags=_re.I)
    html = _re.sub(r"<frame[\s\S]*?</frame>", "", html, flags=_re.I)
    html = _re.sub(r"\son\w+\s*=\s*\"[^\"]*\"", "", html, flags=_re.I)
    html = _re.sub(r"\son\w+\s*=\s*'[^']*'", "", html, flags=_re.I)
    html = _re.sub(r"(href|src)\s*=\s*\"javascript:[^\"]*\"", "", html, flags=_re.I)
    return html


def _file_info(f: Path):
    ext = f.suffix.lower()
    fname = f.name
    ftype = TYPE_MAP.get(ext, "unknown")
    agent = next((p.lower() for p in fname.replace("-", "_").split("_")
                  if p.lower() in KNOWN_AGENTS), "unknown")
    m = re.match(r"(\d{4}-\d{2}-\d{2})", fname)
    date = m.group(1) if m else ""
    return {
        "name": fname, "path": str(f.relative_to(WORKSPACE)),
        "abs_path": str(f), "type": ftype, "agent": agent,
        "date": date, "icon": ICONS.get(ftype, "📁"), "size": f.stat().st_size,
    }


def scan_vault():
    """Use SQLite index for vault data — fast indexed search."""
    try:
        v = vault()
        return v.graph()
    except Exception as e:
        return {"nodes": [], "links": [], "error": str(e)}

_wa_module = None
def _get_workspace_analyzer():
    """Lazy-load workspace_analyzer module (cached)."""
    global _wa_module
    if _wa_module is None:
        from workspace_analyzer import scan_workspace as _wa_scan
        _wa_module = _wa_scan
    return _wa_module

def _log_vault_access(action, path, status="success", details=""):
    """Log vault file access for audit trail."""
    try:
        import time
        log_file = AGENT_OS_ROOT / "config" / "vault-audit.log"
        ts = datetime.now().isoformat()
        entry = ts + "\t" + action + "\t" + path + "\t" + status + "\t" + details + "\n"
        with open(log_file, "a") as f:
            f.write(entry)
        # Also use central audit log
        try:
            from audit import audit_log
            audit_log(f"vault.{action}", path=path, status=status, details={"info": details} if details else None)
        except ImportError:
            pass
    except Exception:
        pass  # Never let audit logging break the main flow

def _generate_report_html(report_type, title, content, task_id="", goal_id="",
                          status_val="completed", agent="", duration_ms=0,
                          output_paths=None, stage_results=None):
    """Generate an HTML report for a completed task or goal."""
    import html as html_mod
    REPORTS_DIR = AGENT_OS_ROOT / "workspace" / "reports"
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_title = re.sub(r'[^a-zA-Z0-9_-]', '_', title[:60])
    fname = f"{report_type}-{safe_title}-{ts}.html"
    fpath = REPORTS_DIR / fname
    status_color = {"completed": "#4ade80", "partial": "#fbbf24", "failed": "#f87171"}.get(status_val, "#8888a0")
    status_icon = {"completed": "✅", "partial": "⚠️", "failed": "❌"}.get(status_val, "ℹ️")
    duration_str = f"{duration_ms/1000:.1f}s" if duration_ms else "—"
    agent_icons = {"hermes":"🦉","developer":"💻","researcher":"🔍","writer":"✍️","pm":"🎯","any":"🤖"}
    agent_icon = agent_icons.get(agent, "🤖")
    # Build stage results HTML
    stages_html = ""
    if stage_results:
        stages_html = '<div class="stages">'
        for sr in stage_results:
            s_icon = "✅" if sr.get("status") == "completed" else "❌"
            s_name = html_mod.escape(sr.get("stage", ""))
            s_agent = html_mod.escape(sr.get("agent", ""))
            s_dur = f"{sr.get('duration_ms',0)/1000:.1f}s" if sr.get('duration_ms') else "—"
            stages_html += f'<div class="stage-item"><span>{s_icon} {s_name}</span><span class="stage-agent">{s_agent}</span><span class="stage-dur">{s_dur}</span></div>'
        stages_html += '</div>'
    # Build output links HTML
    outputs_html = ""
    if output_paths:
        outputs_html = '<div class="outputs">'
        for op in output_paths:
            if op:
                op_name = html_mod.escape(op.split("/")[-1][:80])
                op_path = html_mod.escape(op)
                outputs_html += f'<a class="output-link" href="file://{op_path}" target="_blank">📄 {op_name}</a>'
        outputs_html += '</div>'
    # Convert markdown-ish content to HTML
    content_html = html_mod.escape(content or "No output recorded.")
    content_html = content_html.replace('\\n', '<br>')
    # Simple markdown: **bold**, *italic*, `code`, ### headings
    import re as re2
    content_html = re2.sub(r'### (.+?)(<br|$)', r'<h3>\1</h3>', content_html)
    content_html = re2.sub(r'## (.+?)(<br|$)', r'<h2>\1</h2>', content_html)
    content_html = re2.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', content_html)
    content_html = re2.sub(r'\*(.+?)\*', r'<em>\1</em>', content_html)
    content_html = re2.sub(r'`(.+?)`', r'<code>\1</code>', content_html)
    content_html = re2.sub(r'<br>[-•] ', r'<br><span class="bullet">•</span> ', content_html)
    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>{html_mod.escape(title)} — Report</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text','Segoe UI',sans-serif;background:#0a0a0f;color:#e4e4ef;padding:32px;max-width:900px;margin:0 auto;line-height:1.7}}
h1{{font-size:22px;margin-bottom:4px}}h2{{font-size:16px;color:#7c5bf5;margin-top:24px}}h3{{font-size:14px;color:#5b9bf5;margin-top:16px}}
.meta{{display:flex;gap:16px;margin:12px 0 24px;font-size:12px;color:#8888a0;flex-wrap:wrap}}
.meta span{{background:#12121a;border:1px solid #2a2a3a;padding:3px 10px;border-radius:6px}}
.status{{color:{status_color};font-weight:600}}
.stages{{margin:16px 0}}.stage-item{{display:flex;gap:12px;padding:6px 10px;background:#12121a;border-radius:6px;margin-bottom:4px;font-size:12px}}
.stage-agent{{color:#7c5bf5}}.stage-dur{{color:#8888a0;margin-left:auto}}
.outputs{{margin:12px 0}}.output-link{{display:inline-block;padding:4px 10px;background:#1a1a26;border:1px solid #2a2a3a;border-radius:6px;margin:2px;font-size:11px;color:#5b9bf5;text-decoration:none}}
.output-link:hover{{border-color:#5b9bf5}}
.content{{background:#12121a;border:1px solid #2a2a3a;border-radius:8px;padding:20px;margin-top:16px;font-size:13px;white-space:pre-wrap;overflow-x:auto}}
code{{background:#1a1a26;padding:1px 5px;border-radius:3px;font-size:12px}}
.bullet{{color:#7c5bf5}}
</style></head><body>
<h1>{status_icon} {html_mod.escape(title)}</h1>
<div class="meta"><span class="status">{status_icon} {status_val.upper()}</span><span>{agent_icon} {html_mod.escape(agent or 'unknown')}</span><span>⏱ {duration_str}</span><span>📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}</span></div>
{stages_html}{outputs_html}
<div class="content">{content_html}</div>
</body></html>"""
    fpath.write_text(html, encoding="utf-8")
    return str(fpath.relative_to(AGENT_OS_ROOT))

def _json(handler, data):
    body = json.dumps(data, indent=2).encode()
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Access-Control-Allow-Origin", "http://localhost:%d" % PORT)
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("X-Frame-Options", "DENY")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)

# ── Input validation helpers ──────────────────────────────────────
def _clamp(val, default, maximum):
    """Clamp an int query parameter to [1, maximum], falling back to default."""
    try:
        v = int(val)
    except (TypeError, ValueError):
        return default
    if v < 1:
        return default
    return min(v, maximum)

_rate_limit_store = {}  # ip -> [timestamps]
_RATE_LIMIT_WINDOW = 60  # seconds
_RATE_LIMIT_MAX = 100    # max requests per window

# ── Project Space Helpers ──

def _list_projects() -> dict:
    """List all projects in the projects directory."""
    import sys
    sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
    from project_manager import list_projects
    projects = list_projects()
    return {"ok": True, "projects": projects, "count": len(projects)}


def _create_project(body: dict) -> dict:
    """Create a new project."""
    import sys
    sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
    from project_manager import create_project
    name = body.get("name", "").strip()
    if not name:
        return {"ok": False, "error": "Project name is required"}
    try:
        manifest = create_project(
            name=name,
            description=body.get("description", ""),
            category=body.get("category", "other"),
            template=body.get("template", "blank"),
        )
        return {"ok": True, "project": manifest}
    except FileExistsError as e:
        return {"ok": False, "error": str(e)}
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"Failed to create project: {e}"}


def _get_project_state(name: str):
    """Get a project's .os_state.json."""
    import sys
    sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
    from project_manager import get_project_state
    return get_project_state(name)


def _get_project_files(name: str) -> list:
    """Get all files in a project."""
    import sys
    sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
    from project_manager import get_project_files
    try:
        return get_project_files(name)
    except ValueError:
        return []


def _check_rate_limit(client_ip):
    """Simple sliding-window rate limiter. Returns True if request is allowed."""
    import time
    now = time.time()
    if client_ip not in _rate_limit_store:
        _rate_limit_store[client_ip] = []
    # Prune old entries
    _rate_limit_store[client_ip] = [
        ts for ts in _rate_limit_store[client_ip]
        if now - ts < _RATE_LIMIT_WINDOW
    ]
    if len(_rate_limit_store[client_ip]) >= _RATE_LIMIT_MAX:
        return False
    _rate_limit_store[client_ip].append(now)
    return True

class Handler(http.server.BaseHTTPRequestHandler):

    # Hide server version
    server_version = ""

    def version_string(self):
        return ""

    def send_error(self, code, message=None, explain=None):
        """Override to always return JSON (not HTML) for API consistency."""
        body = json.dumps({"error": "http_error", "code": code,
                           "message": message or ""}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _collect_outputs(self, limit=50, type_filter=None, agent_filter=None, search=None):
        """Aggregate all output sources into a unified list."""
        outputs = []
        ORCH_DIR = AGENT_OS_ROOT / "config" / "orchestration-runs"
        GOAL_REPORTS_DIR = AGENT_OS_ROOT / "workspace" / "goal-reports"
        REPORTS_DIR = AGENT_OS_ROOT / "workspace" / "reports"
        DAILY_DIR = VAULT / "daily"

        # 1. Orchestration run outputs
        if ORCH_DIR.exists():
            _goal_agents = {}
            _goals_file = AGENT_OS_ROOT / "config" / "goals.json"
            if _goals_file.exists():
                try:
                    _goals_data = json.loads(_goals_file.read_text())
                    for _g in _goals_data:
                        _gid = _g.get("id") or _g.get("goal_id", "")
                        _agents = set()
                        for _t in _g.get("tasks", _g.get("subtasks", [])):
                            _a = _t.get("agent", "")
                            if _a: _agents.add(_a)
                        if _gid and _agents:
                            _goal_agents[_gid] = sorted(_agents)
                except Exception:
                    pass
            for f in sorted(ORCH_DIR.glob("*_output.md"), reverse=True):
                if len(outputs) >= limit + 20:
                    break
                agent = "unknown"
                stem = f.stem.replace("_output", "")
                for _gid, _agents in _goal_agents.items():
                    if stem.startswith(_gid.replace("goal-", "") + "-") or _gid in stem:
                        agent = _agents[0] if len(_agents) == 1 else str(len(_agents)) + " agents"
                        break
                if agent == "unknown":
                    for p_part in stem.split("-"):
                        if p_part in ("hermes", "researcher", "writer", "developer", "antigravity", "codex", "pm"):
                            agent = p_part
                            break
                outputs.append({
                    "name": f.stem, "type": "task_output",
                    "source": "orchestration", "agent": agent,
                    "mtime": f.stat().st_mtime, "size": f.stat().st_size,
                    "path": str(f.relative_to(AGENT_OS_ROOT)), "ext": "md", "icon": "⚡",
                })

        # 2. Goal reports
        if GOAL_REPORTS_DIR.exists():
            for f in sorted(GOAL_REPORTS_DIR.glob("*"), reverse=True):
                if len(outputs) >= limit + 20:
                    break
                outputs.append({
                    "name": f.stem, "type": "goal_report",
                    "source": "goal-reports", "agent": "hermes",
                    "mtime": f.stat().st_mtime, "size": f.stat().st_size,
                    "path": str(f.relative_to(AGENT_OS_ROOT)),
                    "ext": f.suffix.lstrip("."), "icon": "🎯",
                })

        # 3. Generated reports
        if REPORTS_DIR.exists():
            for f in sorted(REPORTS_DIR.glob("*.html"), reverse=True):
                if len(outputs) >= limit + 20:
                    break
                outputs.append({
                    "name": f.stem, "type": "report",
                    "source": "reports", "agent": "hermes",
                    "mtime": f.stat().st_mtime, "size": f.stat().st_size,
                    "path": str(f.relative_to(AGENT_OS_ROOT)),
                    "ext": "html", "icon": "📊",
                })

        # 4. Daily notes
        if DAILY_DIR.exists():
            for f in sorted(DAILY_DIR.glob("*.md"), reverse=True)[:14]:
                if len(outputs) >= limit + 20:
                    break
                outputs.append({
                    "name": f.stem, "type": "daily_note",
                    "source": "daily", "agent": "system",
                    "mtime": f.stat().st_mtime, "size": f.stat().st_size,
                    "path": str(f.relative_to(AGENT_OS_ROOT)),
                    "ext": "md", "icon": "📅",
                })

        # 5. Agent vault outputs
        _agent_vault_dirs = [
            VAULT / "research" / "reports",
            VAULT / "agents" / "researcher" / "findings",
            VAULT / "agents" / "writer" / "drafts",
        ]
        for subdir in _agent_vault_dirs:
            if subdir.exists():
                for f in sorted(subdir.glob("*.md"), reverse=True)[:10]:
                    if len(outputs) >= limit + 20:
                        break
                    outputs.append({
                        "name": f.stem, "type": "agent_output",
                        "source": subdir.name, "agent": subdir.name,
                        "mtime": f.stat().st_mtime, "size": f.stat().st_size,
                        "path": str(f.relative_to(AGENT_OS_ROOT)),
                        "ext": "md", "icon": "📝",
                    })

        # Apply filters
        if type_filter and type_filter != "all":
            outputs = [o for o in outputs if o["type"] == type_filter]
        if agent_filter and agent_filter != "all":
            outputs = [o for o in outputs if o["agent"] == agent_filter]
        if search:
            sq = search.lower()
            outputs = [o for o in outputs if sq in o["name"].lower()]

        outputs.sort(key=lambda x: x["mtime"], reverse=True)
        return {
            "outputs": outputs[:limit],
            "total": len(outputs),
            "agents": sorted(set(o["agent"] for o in outputs)),
            "types": ["all", "task_output", "goal_report", "report", "daily_note", "agent_output"],
        }

    def do_GET(self):
        p = urlparse(self.path)

        # ── CORS preflight ──
        if self.command == "OPTIONS":
            self.send_response(204)
            self._cors_headers()
            self.end_headers()
            return

        # ── Auth check (skip for public paths) ──
        self._user_payload = None
        if not self._is_public_path(p.path):
            try:
                self._user_payload = self._require_auth()
            except ValueError:
                return  # _require_auth already sent 401

        # ── Rate limiting ──
        client_ip = self.client_address[0]
        if not _check_rate_limit(client_ip):
            self.send_error(429, "Rate limit exceeded. Max %d requests per %d seconds." % (_RATE_LIMIT_MAX, _RATE_LIMIT_WINDOW))
            return

        if p.path in ("/", "/index.html"):
            if DASHBOARD.exists():
                body = DASHBOARD.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "SAMEORIGIN")
                self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
                self.send_header("Content-Security-Policy",
                                 "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                                 "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                                 "connect-src 'self' http://localhost:7878")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404, "Dashboard not found at %s" % DASHBOARD)
            return

        # ── SERVE VENDORED FRONTEND BUNDLES ──
        # Local-first: third-party bundles (Excalidraw, React) are vendored here
        # so the Diagrams view works fully offline with no CDN dependency.
        if p.path.startswith("/vendor/"):
            rel = p.path[len("/vendor/"):]
            # Prevent path traversal — only allow direct files under vendor/
            if "/" in rel or ".." in rel:
                self.send_error(403, "Forbidden")
                return
            vendor_path = AGENT_OS_ROOT / "dashboard" / "vendor" / rel
            if vendor_path.exists() and vendor_path.is_file():
                ctype = MIME.get(vendor_path.suffix.lower(), "application/octet-stream")
                if vendor_path.suffix.lower() == ".js":
                    ctype = "application/javascript; charset=utf-8"
                body = vendor_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "public, max-age=86400")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404, "Vendor asset not found")
            return

        # ── SERVE DOCS (Wiki) ──
        if p.path.startswith("/docs/"):
            doc_name = p.path[len("/docs/"):]
            doc_path = AGENT_OS_ROOT / "docs" / doc_name
            if doc_path.exists() and doc_path.suffix in (".html", ".md", ".css", ".js"):
                body = doc_path.read_bytes()
                content_type = "text/html; charset=utf-8"
                if doc_path.suffix == ".md":
                    content_type = "text/plain; charset=utf-8"
                elif doc_path.suffix == ".css":
                    content_type = "text/css; charset=utf-8"
                elif doc_path.suffix == ".js":
                    content_type = "application/javascript; charset=utf-8"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404, "Document not found")
            return

        # ── DIAGRAMS (Excalidraw) API ── (shared across GET/POST/DELETE)
        if self.handle_diagrams(p):
            return

        # ── PROJECT SPACE API (GET) ──
        if p.path == "/api/projects/list":
            _json(self, _list_projects())
            return

        # ── Dashboard UI config (read-only) ──
        if p.path == "/api/dashboard/config":
            cfg_path = AGENT_OS_ROOT / "config" / "dashboard.json"
            if cfg_path.exists():
                try:
                    _json(self, json.loads(cfg_path.read_text()))
                except Exception:
                    _json(self, {"ok": False, "error": "config_unreadable"})
            else:
                _json(self, {"ok": False, "error": "config_not_found"})
            return

        # ── OPENAI OAUTH (public status, pre-login) ──
        if p.path == "/api/auth/openai/status":
            if not _HAS_OPENAI_OAUTH:
                _json(self, {"ok": False, "available": False,
                             "message": "openai_oauth module not available"})
                return
            status = bridge_status()
            status["available"] = True
            _json(self, {"ok": True, **status})
            return

        # ── SETUP (first-run system configuration, public) ──
        if p.path == "/api/setup/status":
            if not _HAS_SETUP:
                _json(self, {"ok": False, "needs_onboarding": False,
                             "available": False})
                return
            st = _setup_mod.status()
            _json(self, {"ok": True, "available": True, **st})
            return

        # ── GOOGLE OAUTH (public, pre-login) ──
        if p.path == "/api/auth/google/status":
            if not _HAS_GOOGLE_OAUTH:
                _json(self, {"ok": False, "available": False,
                             "message": "google_oauth module not available"})
                return
            _json(self, {"ok": True, **(_google_mod.status())})
            return

        if p.path == "/api/auth/google/start":
            if not _HAS_GOOGLE_OAUTH:
                _json(self, {"ok": False, "error": "google_oauth_unavailable"})
                return
            q = parse_qs(urlparse(p.path).query)
            redirect_uri = (q.get("redirect_uri") or [None])[0] or \
                           f"http://localhost:{PORT}/api/auth/google/callback"
            try:
                flow = _google_mod.begin(redirect_uri)
                _json(self, {"ok": True, "auth_url": flow["auth_url"],
                             "state": flow["state"], "redirect_uri": redirect_uri})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        if p.path.startswith("/api/auth/google/callback"):
            if not _HAS_GOOGLE_OAUTH:
                self.send_error(503, "google_oauth unavailable")
                return
            q = parse_qs(urlparse(p.path).query)
            code = (q.get("code") or [None])[0]
            state = (q.get("state") or [None])[0]
            redirect_uri = f"http://localhost:{PORT}/api/auth/google/callback"
            err = None
            result = None
            try:
                if not code:
                    raise ValueError("missing authorization code")
                result = _google_mod.callback(code, state, redirect_uri, _auth)
            except Exception as e:  # noqa: BLE001 - surface as JSON/error page
                err = str(e)
            token_payload = None
            if result:
                token_payload = {
                    "token": result.get("access_token"),
                    "display_name": (result.get("user") or {}).get("display_name"),
                    "username": (result.get("user") or {}).get("username"),
                    "role": (result.get("user") or {}).get("role"),
                }
            html = _google_callback_html(token_payload, err)
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if p.path.startswith("/api/projects/") and p.path.endswith("/state"):
            name = p.path[len("/api/projects/"):-len("/state")]
            state = _get_project_state(name)
            if state:
                _json(self, {"ok": True, "project": name, "state": state})
            else:
                self.send_error(404, f"Project '{name}' not found")
            return

        if p.path.startswith("/api/projects/") and p.path.endswith("/files"):
            name = p.path[len("/api/projects/"):-len("/files")]
            _json(self, {"ok": True, "project": name, "files": _get_project_files(name)})
            return

        # ── Project goals/tasks/decisions (GET) ──
        if p.path.startswith("/api/projects/") and p.path.endswith("/goals"):
            name = p.path[len("/api/projects/"):-len("/goals")]
            state = _get_project_state(name)
            _json(self, {"ok": True, "project": name, "goals": state.get("goals", []) if state else []})
            return

        if p.path.startswith("/api/projects/") and p.path.endswith("/tasks"):
            name = p.path[len("/api/projects/"):-len("/tasks")]
            state = _get_project_state(name)
            _json(self, {"ok": True, "project": name, "tasks": state.get("tasks", []) if state else []})
            return

        # ── Project Chat (GET) ──
        if p.path.startswith("/api/projects/") and p.path.endswith("/chat/stream"):
            # SSE stream — returns new messages as they arrive
            # Accepts ?token= as a query param for EventSource (which can't set headers)
            name = p.path[len("/api/projects/"):-len("/chat/stream")]
            qs = parse_qs(p.query)
            token_param = qs.get("token", [None])[0]
            if token_param:
                # Validate the token from query param
                try:
                    self._user_payload = _auth.verify_token(token_param)
                except ValueError:
                    self.send_error(401, "Invalid token")
                    return
            sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
            from project_chat import ChatEventStream
            stream = ChatEventStream(name)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "http://localhost:%d" % PORT)
            self.end_headers()
            for event in stream.poll():
                self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()
            return

        if p.path.startswith("/api/projects/") and p.path.endswith("/chat"):
            name = p.path[len("/api/projects/"):-len("/chat")]
            qs = parse_qs(p.query)
            limit = int(qs.get("limit", [100])[0])
            offset = int(qs.get("offset", [0])[0])
            role_filter = qs.get("role", [None])[0]
            sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
            from project_chat import get_messages, ensure_chat_dir
            ensure_chat_dir(name)
            messages = get_messages(name, limit=limit, offset=offset, role_filter=role_filter)
            _json(self, {"ok": True, "project": name, "messages": messages, "count": len(messages)})
            return

        # ── Project Team Roster (GET) — Slack-like 'who's working on what' ──
        if p.path.startswith("/api/projects/") and p.path.endswith("/team"):
            name = p.path[len("/api/projects/"):-len("/team")]
            sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
            from project_chat import get_team_roster, list_agents_used
            try:
                roster = get_team_roster(name)
            except Exception as e:
                roster = []
            used = []
            try:
                used = list_agents_used(name)
            except Exception:
                pass
            _json(self, {"ok": True, "project": name, "agents": roster, "active_agents": used, "count": len(roster)})
            return

        # ── SERVE REPORTS ──
        if p.path.startswith("/reports/"):
            report_name = p.path[len("/reports/"):]
            report_path = AGENT_OS_ROOT / "workspace" / "reports" / report_name
            if report_path.exists() and report_path.suffix == ".html":
                body = report_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404, "Report not found")
            return

        if p.path == "/api/stats":
            files = scan_workspace()
            try:
                v = vault()
                vstats = v.stats()
                daily_count = vstats["by_folder"].get("daily", 0)
                loop_count = vstats["by_folder"].get("loop", 0)
            except Exception:
                daily_count = 0
                loop_count = 0
            _json(self, {
                "workspace_files": len(files),
                "daily_notes": daily_count,
                "loop_runs": loop_count,
                "vault_files": vstats.get("total_files", 0) if 'vstats' in dir() else 0,
                "agents": len(set(f["agent"] for f in files)),
            })
            return

        # ── LIVE ACTIVITY FEED ──
        if p.path == "/api/activity":
            try:
                from activity_log import get_activity
                limit = _clamp(parse_qs(p.query).get("limit", ["60"])[0], 10, 200)
                payload = get_activity(limit=limit)
            except ImportError:
                payload = {"ok": False, "error": "activity_log module not available",
                           "events": [], "running": [], "summary": {}}
            except Exception as e:
                payload = {"ok": False, "error": str(e), "events": [], "running": [], "summary": {}}
            _json(self, payload)
            return

        if p.path == "/api/vault/search":
            q = parse_qs(p.query).get("q", [""])[0]
            max_r = _clamp(parse_qs(p.query).get("max", ["10"])[0], 10, 50)
            try:
                results = vault().search(q, max_r)
            except Exception as e:
                results = []
            _json(self, {"query": q, "results": results, "count": len(results)})
            return

        if p.path == "/api/vault/list":
            folder = parse_qs(p.query).get("folder", ["all"])[0]
            try:
                files = vault().list_files(folder)
            except Exception as e:
                files = []
            _json(self, {"files": files, "count": len(files)})
            return

        if p.path == "/api/pipeline/contracts":
            try:
                from pipeline_contracts import list_contracts
                _json(self, {"ok": True, "contracts": list_contracts()})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e), "contracts": {}})
            return

        if p.path == "/api/pipeline/violations":
            try:
                from pipeline_contracts import read_violations
                limit = _clamp(parse_qs(p.query).get("limit", ["100"])[0], 1, 500)
                _json(self, {"ok": True, "violations": read_violations(limit),
                             "count": len(read_violations(limit))})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e), "violations": [], "count": 0})
            return

        if p.path == "/api/pipeline/status":
            # Live pipeline integrity: validate every goal's current stage data
            # against its contract where possible.
            try:
                from pipeline_contracts import validate as _pc_validate, list_contracts
                goals = load_goals()
                rows = []
                for g in goals:
                    status = g.get("status", "")
                    row = {"id": g.get("id"), "title": g.get("title"),
                           "status": status, "contract_issues": []}
                    if status == "contract_error":
                        row["contract_issues"].append(g.get("contract_error", "contract error"))
                    rows.append(row)
                _json(self, {"ok": True, "goals": rows,
                             "contracts": list_contracts()})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e), "goals": [], "contracts": {}})
            return

        # ── WEBHOOKS (GET) — Feature 8 ──
        if p.path == "/api/webhooks":
            try:
                from webhooks import load_webhooks
                _json(self, {"ok": True, "webhooks": load_webhooks()})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return
        if p.path == "/api/webhooks/events":
            try:
                from webhooks import EVENT_TYPES
                _json(self, {"ok": True, "events": [{"name": n, "desc": d} for n, d in EVENT_TYPES]})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return
        if p.path == "/api/webhooks/deliveries":
            try:
                from webhooks import get_deliveries
                limit = min(int(parse_qs(p.query).get("limit", ["100"])[0]), 1000)
                _json(self, {"ok": True, "deliveries": get_deliveries(limit)})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # ── WEBHOOKS: dead-letter (GET) — Feature 8 Enhanced ──
        if p.path == "/api/webhooks/dead-letter":
            try:
                from webhooks import get_dead_letters
                limit = min(int(parse_qs(p.query).get("limit", ["100"])[0]), 1000)
                _json(self, {"ok": True, "dead_letters": get_dead_letters(limit)})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        if p.path == "/api/tasks":
            agent_filter = parse_qs(p.query).get("agent", ["all"])[0]
            status_filter = parse_qs(p.query).get("status", ["all"])[0]
            tasks = load_tasks()
            if agent_filter != "all":
                tasks = [t for t in tasks if t.get("agent") == agent_filter]
            if status_filter != "all":
                tasks = [t for t in tasks if t.get("status") == status_filter]
            _json(self, {"tasks": tasks, "count": len(tasks)})
            return

        # ── KANBAN (GET) ──
        if p.path == "/api/kanban":
            board = load_kanban()
            # Merge goal titles into cards
            goals = load_goals()
            goal_map = {g["id"]: g["title"] for g in goals}
            for lane in board:
                for card in board[lane]:
                    gid = card.get("goal_id", "")
                    if gid and gid in goal_map:
                        card["goal_title"] = goal_map[gid]
            # Count per lane
            counts = {lane: len(board[lane]) for lane in board}
            _json(self, {"ok": True, "board": board, "counts": counts})
            return

        # ── GOALS (GET) ──
        if p.path == "/api/goals":
            goals = load_goals()
            tasks = load_tasks()
            # Embed linked tasks in each goal
            for g in goals:
                g["tasks"] = [t for t in tasks if t.get("goal_id") == g["id"]]
            _json(self, {"goals": goals, "count": len(goals)})
            return

        # ── LOOP GUARD ENDPOINTS ──

        # GET /api/goals/{id}/guards — Get guard status for a goal
        if "/guards" in p.path and p.path.startswith("/api/goals/"):
            goal_id = p.path[len("/api/goals/"):].split("/")[0]
            goals = load_goals()
            goal = next((g for g in goals if g["id"] == goal_id), None)
            if not goal:
                self.send_error(404, f"Goal '{goal_id}' not found")
                return
            try:
                from loop_guards import LoopGuards, load_guard_config, get_goal_guards, check_guards
                guards = LoopGuards(goal_id)
                guard_config = get_goal_guards(goal_id)
                guard_status = check_guards(goal_id, goal)
                _json(self, {
                    "ok": True,
                    "goal_id": goal_id,
                    "config": guard_config,
                    "status": guard_status,
                })
            except ImportError:
                _json(self, {"ok": False, "error": "Loop guards module not available"})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # POST /api/goals/{id}/guards — Update guard config for a goal
        if p.path.startswith("/api/goals/") and p.path.endswith("/guards"):
            goal_id = p.path[len("/api/goals/"):].split("/")[0]
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length))
            except Exception:
                self.send_error(400, "Invalid JSON")
                return
            try:
                from loop_guards import set_goal_guards
                new_config = set_goal_guards(goal_id, body)
                _json(self, {"ok": True, "config": new_config})
            except ImportError:
                _json(self, {"ok": False, "error": "Loop guards module not available"})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # GET /api/config/guards — Get global guard configuration
        if p.path == "/api/config/guards" and self.command == "GET":
            try:
                from loop_guards import load_guard_config
                config = load_guard_config()
                _json(self, {"ok": True, "config": config})
            except ImportError:
                _json(self, {"ok": False, "error": "Loop guards module not available"})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # POST /api/config/guards — Update global guard configuration
        if p.path == "/api/config/guards" and self.command == "POST":
            try:
                content_length = int(self.headers.get("Content-Length", 0) or 0)
                body = {}
                if content_length > 0:
                    body = json.loads(self.rfile.read(content_length))
                from loop_guards import set_global_guards, load_guard_config
                defaults = body.get("defaults", {})
                if defaults:
                    new_config = set_global_guards(defaults)
                    _json(self, {"ok": True, "config": load_guard_config()})
                else:
                    _json(self, {"ok": False, "error": "No defaults provided"})
            except ImportError:
                _json(self, {"ok": False, "error": "Loop guards module not available"})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # POST /api/goals/{id}/pause — Pause a goal
        if p.path.startswith("/api/goals/") and p.path.endswith("/pause"):
            goal_id = p.path[len("/api/goals/"):].split("/")[0]
            goals = load_goals()
            goal = next((g for g in goals if g["id"] == goal_id), None)
            if not goal:
                self.send_error(404, f"Goal '{goal_id}' not found")
                return
            goal["status"] = "paused"
            goal["updated"] = datetime.now().isoformat()
            save_goals(goals)
            _json(self, {"ok": True, "status": "paused"})
            return

        # POST /api/goals/{id}/resume — Resume a goal
        if p.path.startswith("/api/goals/") and p.path.endswith("/resume"):
            goal_id = p.path[len("/api/goals/"):].split("/")[0]
            goals = load_goals()
            goal = next((g for g in goals if g["id"] == goal_id), None)
            if not goal:
                self.send_error(404, f"Goal '{goal_id}' not found")
                return
            # Clear guard trigger and set back to in_progress
            goal["status"] = "in_progress"
            goal.pop("guard_triggered", None)
            goal["updated"] = datetime.now().isoformat()
            save_goals(goals)
            _json(self, {"ok": True, "status": "in_progress"})
            return

        # ── CHECKPOINT ENDPOINTS ──

        # GET /api/checkpoints — List all checkpoints
        if p.path == "/api/checkpoints":
            try:
                from loop_checkpoint import list_checkpoints
                cps = list_checkpoints()
                _json(self, {"ok": True, "checkpoints": cps, "count": len(cps)})
            except ImportError:
                _json(self, {"ok": False, "error": "Checkpoint module not available"})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # GET /api/goals/{id}/checkpoint — Get checkpoint for a goal
        if p.path.startswith("/api/goals/") and p.path.endswith("/checkpoint") and "save" not in p.path and "delete" not in p.path:
            goal_id = p.path[len("/api/goals/"):].split("/")[0]
            try:
                from loop_checkpoint import load_checkpoint, list_checkpoint_history
                cp = load_checkpoint(goal_id)
                history = list_checkpoint_history(goal_id)
                if cp:
                    _json(self, {"ok": True, "checkpoint": cp, "history": history})
                else:
                    _json(self, {"ok": True, "checkpoint": None, "history": [],
                                 "message": "No checkpoint found for this goal"})
            except ImportError:
                _json(self, {"ok": False, "error": "Checkpoint module not available"})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # POST /api/goals/{id}/checkpoint/save — Manually save checkpoint
        if p.path.startswith("/api/goals/") and "/checkpoint/save" in p.path:
            goal_id = p.path[len("/api/goals/"):].split("/")[0]
            goals = load_goals()
            goal = next((g for g in goals if g["id"] == goal_id), None)
            if not goal:
                self.send_error(404, f"Goal '{goal_id}' not found")
                return
            try:
                from loop_checkpoint import save_checkpoint
                tasks = load_tasks()
                goal_tasks = [t for t in tasks if t.get("goal_id") == goal_id]
                cp = save_checkpoint(goal_id, goal, goal_tasks)
                _json(self, {"ok": True, "checkpoint": cp})
            except ImportError:
                _json(self, {"ok": False, "error": "Checkpoint module not available"})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # DELETE /api/goals/{id}/checkpoint — Delete checkpoint (fresh start)
        if p.path.startswith("/api/goals/") and "/checkpoint/delete" in p.path:
            goal_id = p.path[len("/api/goals/"):].split("/")[0]
            try:
                from loop_checkpoint import delete_checkpoint
                deleted = delete_checkpoint(goal_id)
                _json(self, {"ok": True, "deleted": deleted})
            except ImportError:
                _json(self, {"ok": False, "error": "Checkpoint module not available"})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # ── VERIFICATION ENDPOINTS ──

        # GET /api/goals/{id}/verification — Get verification summary for a goal
        if p.path.startswith("/api/goals/") and p.path.endswith("/verification"):
            goal_id = p.path[len("/api/goals/"):].split("/")[0]
            try:
                from goal_verifier import GoalVerifier
                verifier = GoalVerifier()
                goals = load_goals()
                goal = next((g for g in goals if g["id"] == goal_id), None)
                if not goal:
                    self.send_error(404, f"Goal '{goal_id}' not found")
                    return
                # Build verification summary from goal data
                verification_scores = goal.get("verification_scores", [])
                summary = None
                if verification_scores:
                    scores = [s["score"] for s in verification_scores if isinstance(s.get("score"), (int, float))]
                    if scores:
                        avg_score = sum(scores) / len(scores)
                        summary = {
                            "average_score": round(avg_score, 2),
                            "total_verified": len(scores),
                            "pass_count": sum(1 for s in verification_scores if s.get("status") == "pass"),
                            "partial_count": sum(1 for s in verification_scores if s.get("status") == "partial"),
                            "fail_count": sum(1 for s in verification_scores if s.get("status") == "fail"),
                            "scores": verification_scores,
                        }
                # Get full verification history
                history = verifier.get_verification_history()
                goal_history = [h for h in history if any(
                    s.get("task_id") == h.get("task_id") for s in verification_scores
                )]
                _json(self, {
                    "ok": True,
                    "goal_id": goal_id,
                    "summary": summary,
                    "history": goal_history[:20],
                })
            except ImportError:
                _json(self, {"ok": False, "error": "Goal verifier module not available"})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # GET /api/verifications — List all verification records
        if p.path == "/api/verifications":
            try:
                from goal_verifier import GoalVerifier
                verifier = GoalVerifier()
                history = verifier.get_verification_history()
                _json(self, {"ok": True, "verifications": history, "count": len(history)})
            except ImportError:
                _json(self, {"ok": False, "error": "Goal verifier module not available"})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # POST /api/goals/{id}/verify — Manually trigger verification for all done tasks
        if p.path.startswith("/api/goals/") and p.path.endswith("/verify"):
            goal_id = p.path[len("/api/goals/"):].split("/")[0]
            goals = load_goals()
            goal = next((g for g in goals if g["id"] == goal_id), None)
            if not goal:
                self.send_error(404, f"Goal '{goal_id}' not found")
                return
            try:
                from goal_verifier import GoalVerifier
                verifier = GoalVerifier()
                tasks = load_tasks()
                done_tasks = [t for t in tasks if t.get("goal_id") == goal_id and t.get("status") == "done"]
                if not done_tasks:
                    _json(self, {"ok": True, "message": "No done tasks to verify", "verdicts": []})
                    return
                verdicts = verifier.verify_batch(
                    goal_title=goal.get("title", ""),
                    goal_desc=goal.get("desc", ""),
                    tasks=[{"task_id": t.get("id", ""), "title": t.get("title", ""), "output": t.get("output", "")} for t in done_tasks],
                )
                _json(self, {
                    "ok": True,
                    "verdicts": [v.to_dict() for v in verdicts],
                    "verified_count": len(verdicts),
                })
            except ImportError:
                _json(self, {"ok": False, "error": "Goal verifier module not available"})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # ── SELF-CORRECTION ENDPOINTS ──

        # GET /api/goals/{id}/corrections — Get correction history for a goal
        if p.path.startswith("/api/goals/") and p.path.endswith("/corrections"):
            goal_id = p.path[len("/api/goals/"):].split("/")[0]
            try:
                from self_correction import SelfCorrectionEngine
                engine = SelfCorrectionEngine()
                history = engine.get_correction_history(goal_id=goal_id)
                escalations = engine.get_escalations(goal_id=goal_id)
                _json(self, {
                    "ok": True,
                    "goal_id": goal_id,
                    "corrections": history,
                    "escalations": escalations,
                    "total_corrections": len(history),
                    "total_escalations": len(escalations),
                })
            except ImportError:
                _json(self, {"ok": False, "error": "Self-correction module not available"})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # GET /api/corrections — List all correction records
        if p.path == "/api/corrections":
            try:
                from self_correction import SelfCorrectionEngine
                engine = SelfCorrectionEngine()
                history = engine.get_correction_history()
                _json(self, {
                    "ok": True,
                    "corrections": history,
                    "count": len(history),
                })
            except ImportError:
                _json(self, {"ok": False, "error": "Self-correction module not available"})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # GET /api/escalations — List all escalated tasks (needs_human_input)
        if p.path == "/api/escalations":
            try:
                from self_correction import SelfCorrectionEngine
                engine = SelfCorrectionEngine()
                escalations = engine.get_escalations()
                _json(self, {
                    "ok": True,
                    "escalations": escalations,
                    "count": len(escalations),
                })
            except ImportError:
                _json(self, {"ok": False, "error": "Self-correction module not available"})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # POST /api/goals/{id}/correct — Manually trigger self-correction for a task
        if p.path.startswith("/api/goals/") and p.path.endswith("/correct"):
            goal_id = p.path[len("/api/goals/"):].split("/")[0]
            goals = load_goals()
            goal = next((g for g in goals if g["id"] == goal_id), None)
            if not goal:
                self.send_error(404, f"Goal '{goal_id}' not found")
                return
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0)) if int(self.headers.get("Content-Length", 0) or 0) > 0 else {}
            task_id = body.get("task_id", "")
            tasks = load_tasks()
            task = next((t for t in tasks if t.get("id") == task_id or t.get("task_id") == task_id), None)
            if not task:
                self.send_error(404, f"Task '{task_id}' not found")
                return
            try:
                from self_correction import SelfCorrectionEngine
                engine = SelfCorrectionEngine()
                result = engine.correct(
                    goal_title=goal.get("title", ""),
                    goal_desc=goal.get("desc", ""),
                    task_title=task.get("title", ""),
                    original_prompt=task.get("desc", task.get("title", "")),
                    failure_reason=body.get("failure_reason", "Manual correction requested"),
                    task_id=task_id,
                    goal_id=goal_id,
                    attempt=task.get("retry_count", 0),
                    task_output=task.get("output", ""),
                    error_message=task.get("error", ""),
                )
                _json(self, {"ok": True, "correction": result})
            except ImportError:
                _json(self, {"ok": False, "error": "Self-correction module not available"})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # DELETE /api/goals/{id}/corrections — Clear correction history for a goal
        if p.path.startswith("/api/goals/") and p.path.endswith("/corrections/clear"):
            goal_id = p.path[len("/api/goals/"):].split("/")[0]
            try:
                from self_correction import SelfCorrectionEngine
                engine = SelfCorrectionEngine()
                deleted = engine.clear_history(goal_id=goal_id)
                _json(self, {"ok": True, "deleted": deleted})
            except ImportError:
                _json(self, {"ok": False, "error": "Self-correction module not available"})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # ── Phase 6: Convergence + Adaptive + Pattern Learning ──

        # GET /api/goals/{id}/convergence — Get convergence analysis
        if p.path.startswith("/api/goals/") and p.path.endswith("/convergence"):
            goal_id = p.path[len("/api/goals/"):].split("/")[0]
            goals = load_goals()
            goal = next((g for g in goals if g["id"] == goal_id), None)
            if not goal:
                self.send_error(404, f"Goal '{goal_id}' not found")
                return
            try:
                from loop_guards import LoopGuards
                guards = LoopGuards(goal_id)
                guard_result = guards.check_all(goal)
                # Build convergence report
                history = goal.get("loop_history", [])
                verification_scores = goal.get("verification_scores", [])
                avg_quality = None
                if verification_scores:
                    scores = [s["score"] for s in verification_scores if isinstance(s.get("score"), (int, float))]
                    if scores:
                        avg_quality = round(sum(scores) / len(scores), 3)
                _json(self, {
                    "ok": True,
                    "convergence": {
                        "guard_result": guard_result,
                        "iterations": goal.get("loop_iterations", 0),
                        "history_length": len(history),
                        "avg_quality": avg_quality,
                        "adaptive_threshold": goal.get("quality_threshold", 0.6),
                        "threshold_history": goal.get("adaptive_threshold_history", []),
                        "agent_switches": [t.get("agent_switched") for t in goal.get("tasks", []) if t.get("agent_switched")],
                    },
                })
            except ImportError:
                _json(self, {"ok": False, "error": "Loop guards module not available"})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # GET /api/goals/background/status goal execution task
        if p.path == "/api/goals/background/status":
            task_id = parse_qs(urlparse(self.path).query).get("task_id", [""])[0]
            if not task_id:
                try:
                    from hermes_bridge import list_background_tasks
                    status_filter = parse_qs(urlparse(self.path).query).get("status", [None])[0]
                    tasks = list_background_tasks(status_filter=status_filter)
                    _json(self, {"ok": True, "tasks": tasks, "count": len(tasks)})
                except Exception as e:
                    _json(self, {"ok": False, "error": str(e)})
                return
            try:
                from hermes_bridge import poll_background_task
                result = poll_background_task(task_id)
                _json(self, result)
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # GET /api/goals/{id}/patterns — Get pattern suggestions for a goal
        if p.path.startswith("/api/goals/") and p.path.endswith("/patterns"):
            goal_id = p.path[len("/api/goals/"):].split("/")[0]
            goals = load_goals()
            goal = next((g for g in goals if g["id"] == goal_id), None)
            if not goal:
                self.send_error(404, f"Goal '{goal_id}' not found")
                return
            try:
                from loop_patterns import get_suggestion
                title = goal.get("title", "")
                desc = goal.get("desc", "")
                suggestion = get_suggestion(title, desc)
                _json(self, {"ok": True, "suggestion": suggestion})
            except ImportError:
                _json(self, {"ok": False, "error": "Pattern learner not available"})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # GET /api/patterns — Get all pattern learning stats
        if p.path == "/api/patterns":
            try:
                from loop_patterns import LoopPatternLearner
                learner = LoopPatternLearner()
                stats = learner.get_stats()
                _json(self, {"ok": True, **stats})
            except ImportError:
                _json(self, {"ok": False, "error": "Pattern learner not available"})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # POST /api/goals/{id}/patterns/suggest — Get suggestions for a new goal
        if p.path.startswith("/api/goals/") and p.path.endswith("/patterns/suggest"):
            goal_id = p.path[len("/api/goals/"):].split("/")[0]
            try:
                from loop_patterns import get_suggestion
                data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                title = data.get("title", "")
                desc = data.get("desc", "")
                suggestion = get_suggestion(title, desc)
                _json(self, {"ok": True, "suggestion": suggestion})
            except ImportError:
                _json(self, {"ok": False, "error": "Pattern learner not available"})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # ── Learn & Build (LnB) — GET endpoints ──

        # GET /api/lnb/skills — List all learned skills
        if p.path == "/api/lnb/skills":
            try:
                from lnb_engine import LearnAndBuild
                lnb = LearnAndBuild()
                topic_filter = parse_qs(p.query).get("topic", [""])[0]
                skills = lnb.list_skills(topic_filter=topic_filter)
                _json(self, {"ok": True, "skills": skills, "total": len(skills)})
            except ImportError:
                _json(self, {"ok": False, "error": "lnb_module_not_available"})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # GET /api/lnb/skills/{name} — Get a specific skill
        if p.path.startswith("/api/lnb/skills/"):
            skill_name = p.path[len("/api/lnb/skills/"):]
            try:
                from lnb_engine import LearnAndBuild
                lnb = LearnAndBuild()
                result = lnb.get_skill(skill_name)
                if result.get("ok"):
                    _json(self, result)
                else:
                    self.send_error(404, f"Skill '{skill_name}' not found")
            except ImportError:
                _json(self, {"ok": False, "error": "lnb_module_not_available"})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # GET /api/lnb/stats — Get LnB statistics
        if p.path == "/api/lnb/stats":
            try:
                from lnb_engine import LearnAndBuild
                lnb = LearnAndBuild()
                stats = lnb.get_stats()
                _json(self, {"ok": True, **stats})
            except ImportError:
                _json(self, {"ok": False, "error": "lnb_module_not_available"})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # GET /api/goals/{id} — Get single goal (catch-all, must be last)
        if p.path.startswith("/api/goals/") and "background" not in p.path:
            goal_id = p.path[len("/api/goals/"):].split("?")[0]
            goals = load_goals()
            goal = next((g for g in goals if g["id"] == goal_id), None)
            if not goal:
                self.send_error(404, f"Goal '{goal_id}' not found")
                return
            # Include linked tasks
            tasks = load_tasks()
            goal_tasks = [t for t in tasks if t.get("goal_id") == goal_id]
            goal["tasks"] = goal_tasks
            _json(self, goal)
            return

        if p.path == "/api/vault/read":
            path = parse_qs(p.query).get("path", [""])[0]
            if not path:
                self.send_error(400, "Missing path")
                return
            try:
                content = vault().read_file(path)
            except Exception as e:
                _log_vault_access("read", path, "error", str(e)[:100])
                content = None
            if content is None:
                self.send_error(404, "File not found")
                return
            _log_vault_access("read", path, "success")
            _json(self, {"path": path, "content": content})
            return
        if p.path == "/api/workspace":
            files = sorted(scan_workspace(), key=lambda x: x["name"], reverse=True)
            _json(self, files)
            return

        if p.path == "/api/workspace/categories":
            try:
                wa = _get_workspace_analyzer()
                result = wa(str(WORKSPACE))
                _json(self, result)
            except Exception as e:
                _json(self, {"error": str(e), "categories": {}, "files": [], "total": 0})
            return

        if p.path == "/api/workspace/preview":
            qs = parse_qs(p.query)
            file_path = qs.get("path", [None])[0]
            if not file_path:
                _json(self, {"error": "Missing path parameter", "status": 400})
                return
            try:
                from workspace_analyzer import get_file_content
                result = get_file_content(file_path, str(WORKSPACE))
                _json(self, result)
            except Exception as e:
                _json(self, {"error": str(e), "status": 500})
            return

        if p.path == "/api/daily":
            notes = []
            dd = VAULT / "daily"
            if dd.exists():
                for f in sorted(dd.glob("*.md"), reverse=True)[:30]:
                    notes.append({"date": f.stem, "content": f.read_text()})
            _json(self, notes)
            return

        if p.path == "/api/daily/today":
            today = datetime.now().strftime("%Y-%m-%d")
            dd = VAULT / "daily"
            today_file = dd / f"{today}.md"
            if today_file.exists():
                _json(self, {"ok": True, "date": today, "content": today_file.read_text(), "existed": True})
                return
            # Generate today's note via feedback loop
            try:
                sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
                from feedback_loop import generate_daily_note, scan_workspace as _fl_scan
                files = _fl_scan()
                # Find files modified today
                import time
                today_start = time.mktime(time.strptime(today, "%Y-%m-%d"))
                new_files = [f for f in files if f.get("mtime", 0) >= today_start]
                content = generate_daily_note(files, new_files, today)
                if content:
                    today_file.write_text(content)
                    _json(self, {"ok": True, "date": today, "content": content, "existed": False})
                else:
                    _json(self, {"ok": False, "error": "generation returned nothing"})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # ── AUTH (GET) ──
        if p.path == "/api/auth/me":
            try:
                payload = self._require_auth()
                _json(self, {"ok": True, "user": payload})
            except ValueError:
                pass  # _require_auth already sent 401
            return

        # ── HERMES BRIDGE (GET) ──
        if p.path == "/api/hermes/status":
            result = _run_hermes_bridge(["status"])
            _json(self, result)
            return

        if p.path == "/api/hermes/conversations":
            result = _run_hermes_bridge(["sessions"])
            _json(self, result)
            return

        # ── TIER 3 STATUS (Ollama) ──
        if p.path == "/api/tier3/status":
            try:
                sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
                from ollama_worker import is_ollama_available, get_ollama_models
                from model_router import load_project_config
                ollama_ok = is_ollama_available()
                models = get_ollama_models() if ollama_ok else []
                # Check if any project has tier3 configured
                tier3_projects = []
                projects_dir = AGENT_OS_ROOT / "projects"
                if projects_dir.exists():
                    for p_dir in projects_dir.iterdir():
                        if p_dir.is_dir() and not p_dir.name.startswith("."):
                            config = load_project_config(str(p_dir))
                            if config.get("execution_workers", {}).get("tier_3_builder"):
                                tier3_projects.append(p_dir.name)
                _json(self, {
                    "ok": True,
                    "ollama_available": ollama_ok,
                    "models": [{"name": m.get("name", ""), "size": m.get("size", 0)} for m in models],
                    "projects_configured": tier3_projects,
                })
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # ── TIER 3 CONFIG (admin view) ──
        if p.path == "/api/tier3/config":
            try:
                sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
                from tier3_manager import get_config
                _json(self, get_config())
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # ── TIER 2 STATUS (Antigravity) ──
        if p.path == "/api/tier2/status":
            try:
                sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
                from antigravity_worker import is_antigravity_available, get_antigravity_models, get_antigravity_agents
                from model_router import load_project_config
                ag_ok = is_antigravity_available()
                models = get_antigravity_models() if ag_ok else []
                agents = get_antigravity_agents() if ag_ok else []
                # Check which projects use Antigravity
                ag_projects = []
                projects_dir = AGENT_OS_ROOT / "projects"
                if projects_dir.exists():
                    for p_dir in projects_dir.iterdir():
                        if p_dir.is_dir() and not p_dir.name.startswith("."):
                            config = load_project_config(str(p_dir))
                            tier2 = config.get("execution_workers", {}).get("tier_2_architect", {})
                            if tier2.get("engine") == "antigravity":
                                ag_projects.append(p_dir.name)
                _json(self, {
                    "ok": True,
                    "antigravity_available": ag_ok,
                    "models": models,
                    "agents": agents,
                    "projects_using_antigravity": ag_projects,
                })
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # ── VAULT BACKUP ──
        if p.path == "/api/backup/status":
            try:
                sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
                from vault_backup import get_status
                result = get_status()
            except Exception as e:
                result = {"ok": False, "error": str(e)}
            _json(self, result)
            return

        if p.path == "/api/backup/run":
            try:
                sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
                from vault_backup import do_backup
                force = parse_qs(p.query).get("force", ["false"])[0] == "true"
                result = do_backup(force=force)
            except Exception as e:
                result = {"ok": False, "error": str(e)}
            _json(self, result)
            return

        # ── WORKFLOW ENGINE (GET) ──
        if p.path == "/api/workflows":
            WORKFLOWS_DIR = AGENT_OS_ROOT / "config" / "workflows"
            WORKFLOWS_DIR.mkdir(parents=True, exist_ok=True)
            workflows = []
            for f in sorted(WORKFLOWS_DIR.glob("*.json")):
                try:
                    w = json.loads(f.read_text())
                    workflows.append({
                        "id": w.get("id", f.stem),
                        "name": w.get("name", f.stem),
                        "description": w.get("description", ""),
                        "nodes": len(w.get("nodes", [])),
                        "edges": len(w.get("edges", [])),
                        "file": f.name,
                        "updated_at": w.get("updated_at", ""),
                    })
                except Exception:
                    pass
            _json(self, {"workflows": workflows, "count": len(workflows)})
            return

        if p.path.startswith("/api/workflows/"):
            wf_id = p.path[len("/api/workflows/"):].split("?")[0]
            wf_file = AGENT_OS_ROOT / "config" / "workflows" / f"{wf_id}.json"
            if not wf_file.exists():
                # Try by ID field
                wf_dir = AGENT_OS_ROOT / "config" / "workflows"
                found = None
                for f in wf_dir.glob("*.json"):
                    try:
                        w = json.loads(f.read_text())
                        if w.get("id") == wf_id:
                            found = f
                            break
                    except Exception:
                        pass
                if found:
                    wf_file = found
                else:
                    self.send_error(404, f"Workflow '{wf_id}' not found")
                    return
            _json(self, json.loads(wf_file.read_text()))
            return

        # ── WORKFLOW RUNS (GET) ──
        if p.path == "/api/workflow-runs":
            RUNS_DIR = AGENT_OS_ROOT / "config" / "workflow-runs"
            RUNS_DIR.mkdir(parents=True, exist_ok=True)
            limit = _clamp(parse_qs(p.query).get("limit", ["20"])[0], 20, 100)
            runs = []
            for f in sorted(RUNS_DIR.glob("wfrun-*.json"), reverse=True)[:limit]:
                try:
                    r = json.loads(f.read_text())
                    runs.append({
                        "run_id": r["run_id"],
                        "workflow_name": r["workflow_name"],
                        "status": r["status"],
                        "started_at": r["started_at"],
                    })
                except Exception:
                    pass
            _json(self, {"runs": runs, "count": len(runs)})
            return

        if p.path.startswith("/api/workflow-runs/"):
            run_id = p.path[len("/api/workflow-runs/"):].split("?")[0]
            run_file = AGENT_OS_ROOT / "config" / "workflow-runs" / f"{run_id}.json"
            if not run_file.exists():
                self.send_error(404, f"Run '{run_id}' not found")
                return
            _json(self, json.loads(run_file.read_text()))
            return

        # ── ORCHESTRATOR (GET) ──
        if p.path == "/api/orchestrate/runs":
            limit = _clamp(parse_qs(p.query).get("limit", ["20"])[0], 20, 100)
            try:
                sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
                from agent_orchestrator import list_recent_runs
                runs = list_recent_runs(limit=limit)
            except Exception as e:
                runs = []
            _json(self, {"runs": runs, "count": len(runs)})
            return

        if p.path.startswith("/api/orchestrate/run/"):
            run_id = p.path[len("/api/orchestrate/run/"):].split("?", 1)[0]
            try:
                sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
                from agent_orchestrator import get_run
                run = get_run(run_id)
            except Exception as e:
                run = None
            if not run:
                self.send_error(404, f"Run '{run_id}' not found")
                return
            _json(self, run)
            return

        # ── TASK RUNNER (GET) ──
        if p.path == "/api/task-runner/status":
            pid_file = AGENT_OS_ROOT / "config" / "task_runner.pid"
            running = False
            pid = None
            if pid_file.exists():
                try:
                    pid = int(pid_file.read_text().strip())
                    os.kill(pid, 0)
                    running = True
                except (OSError, ProcessLookupError):
                    pid = None
                except Exception:
                    pid = None

            tasks = load_tasks()
            by_status = {}
            for t in tasks:
                s = t.get("status", "unknown")
                by_status[s] = by_status.get(s, 0) + 1

            _json(self, {
                "running": running,
                "pid": pid,
                "tasks": by_status,
                "total_tasks": len(tasks),
            })
            return

        # ── ORCHESTRATOR (POST) ──
        # Handled in do_POST section below

        # ── AGENT CHANNELS (GET) ──
        if p.path == "/api/agents":
            try:
                reg = _get_agent_registry()
                status_filter = parse_qs(p.query).get("status", ["all"])[0]
                agents = reg.list_agents(status_filter=status_filter)
            except Exception as e:
                agents = []
            _json(self, {"agents": agents, "count": len(agents)})
            return

        if p.path.startswith("/api/agents/"):
            parts = p.path[len("/api/agents/"):].split("?", 1)
            agent_key = parts[0]
            try:
                reg = _get_agent_registry()
                q = parse_qs(parts[1]) if len(parts) > 1 and parts[1] else {}

                if agent_key == "route":
                    _json(self, {"ok": False, "hint": 'Use POST /api/agents/route with {"task": "..."}'})
                    return

                agent = reg.get_agent(agent_key)
                if not agent:
                    self.send_error(404, f"Agent '{agent_key}' not found")
                    return

                # Check sub-paths via query param
                sub = q.get("sub", [""])[0]
                if sub == "memory":
                    content = reg.read_memory(agent_key)
                    _json(self, {"agent": agent_key, "memory": content})
                    return
                elif sub == "activity":
                    limit = _clamp(q.get("limit", ["20"])[0], 20, 100)
                    try:
                        v = vault()
                        activity = v.get_activity(agent=agent_key, limit=limit)
                    except Exception:
                        activity = []
                    _json(self, {"agent": agent_key, "activity": activity, "count": len(activity)})
                    return
                elif sub == "outputs":
                    limit = _clamp(q.get("limit", ["20"])[0], 20, 100)
                    try:
                        v = vault()
                        outputs = v.get_outputs(agent=agent_key, limit=limit)
                    except Exception:
                        outputs = []
                    _json(self, {"agent": agent_key, "outputs": outputs, "count": len(outputs)})
                    return

                _json(self, {"agent": agent})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # ── ACTIVITY LOG (GET) ──
        if p.path == "/api/activity":
            q = parse_qs(p.query)
            agent_filter = q.get("agent", [None])[0]
            status_filter = q.get("status", [None])[0]
            limit = _clamp(q.get("limit", ["50"])[0], 50, 200)
            try:
                v = vault()
                entries = v.get_activity(agent=agent_filter, status=status_filter, limit=limit)
            except Exception as e:
                entries = []
            _json(self, {"activity": entries, "count": len(entries)})
            return

        if p.path == "/api/activity/stats":
            try:
                v = vault()
                stats = v.get_activity_stats()
            except Exception:
                stats = {"total_entries": 0, "by_agent": {}, "by_status": {}}
            _json(self, stats)
            return

        if p.path == "/api/graph":
            _json(self, scan_vault())
            return

        if p.path == "/api/graph/rebuild":
            try:
                v = vault()
                result = v.rebuild_graph()
            except Exception as e:
                result = {"error": str(e)}
            _json(self, result)
            return

        if p.path == "/api/logs":
            log_dir = Path.home() / ".hermes" / "logs"
            gateway_log = log_dir / "gateway.log"
            error_log = log_dir / "gateway.error.log"
            max_lines = 1000
            logs = {"ok": True, "entries": []}
            try:
                import re as _re
                from datetime import datetime as _dt, timedelta as _td
                # Matches a leading timestamp like "2026-07-02 09:51:47,768"
                _ts_re = _re.compile(r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)")
                # Only keep logs from the last 3 days.
                _cutoff = _dt.now() - _td(days=3)
                entries = []
                for log_file, label in [(gateway_log, "gateway"), (error_log, "error")]:
                    if log_file.exists():
                        with open(log_file, "r", errors="replace") as f:
                            lines = f.readlines()
                        # Take last max_lines from each file
                        tail = lines[-max_lines:] if len(lines) > max_lines else lines
                        # Track the last seen timestamp so continuation lines
                        # (stack traces, wrapped messages) inherit it and stay
                        # grouped with their parent entry when sorted.
                        last_ts = ""
                        last_dt_ok = False  # whether current entry is within window
                        # seq preserves original file order within the same timestamp
                        for seq, line in enumerate(tail):
                            line = line.rstrip("\n")
                            if line:
                                m = _ts_re.match(line)
                                if m:
                                    last_ts = m.group(1).replace(",", ".")
                                    try:
                                        last_dt_ok = _dt.fromisoformat(last_ts) >= _cutoff
                                    except ValueError:
                                        last_dt_ok = True  # keep unparseable timestamps
                                # Skip lines older than the 3-day window.
                                # Continuation lines inherit the parent's decision.
                                if last_ts and not last_dt_ok:
                                    continue
                                entries.append({
                                    "source": label,
                                    "line": line,
                                    "_ts": last_ts,
                                    "_seq": seq,
                                })
                # Sort most-recent first. Entries without any parsed timestamp
                # (before the first timestamped line) sort to the bottom.
                entries.sort(key=lambda e: (e["_ts"] or "", e["_seq"]), reverse=True)
                # Strip internal sort keys before returning
                for e in entries:
                    e.pop("_ts", None)
                    e.pop("_seq", None)
                logs["entries"] = entries
                logs["total"] = len(entries)
                logs["window_days"] = 3
                logs["sources"] = {
                    "gateway": gateway_log.exists(),
                    "error": error_log.exists()
                }
            except Exception as e:
                logs = {"ok": False, "error": str(e)}
            _json(self, logs)
            return

        if p.path == "/api/loop":
            r = subprocess.run(
                [sys.executable, str(AGENT_OS_ROOT / "scripts" / "feedback-loop.py")],
                capture_output=True, text=True, timeout=30,
            )
            _json(self, {"ok": r.returncode == 0, "output": r.stdout[-2000:], "error": r.stderr[-500:]})
            return

        if p.path == "/api/note":
            self.send_error(405, "Use POST for /api/note")
            return

        if p.path.startswith("/workspace/"):
            rel = p.path[len("/workspace/"):]
            # SECURITY: prevent path traversal — resolve and verify path is inside WORKSPACE
            try:
                fp = (WORKSPACE / rel).resolve()
                if not fp.resolve().is_relative_to(WORKSPACE.resolve()):
                    self.send_error(403, "Access denied")
                    return
            except (ValueError, RuntimeError):
                self.send_error(403, "Access denied")
                return
            if fp.exists() and fp.is_file():
                body = fp.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", MIME.get(fp.suffix.lower(), "application/octet-stream"))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_error(404)
            return

        # ── REPORTS (GET) ──
        if p.path == "/api/reports/list":
            REPORTS_DIR = AGENT_OS_ROOT / "workspace" / "reports"
            REPORTS_DIR.mkdir(parents=True, exist_ok=True)
            reports = []
            for f in sorted(REPORTS_DIR.glob("*.html"), reverse=True)[:50]:
                reports.append({"name": f.stem, "file": f.name, "size": f.stat().st_size, "mtime": f.stat().st_mtime})
            _json(self, {"ok": True, "reports": reports})
            return

        # ── UNIFIED OUTPUTS (GET) ──
        if p.path == "/api/outputs":
            q = parse_qs(p.query)
            limit = _clamp(q.get("limit", ["50"])[0], 50, 200)
            type_filter = q.get("type", [None])[0]
            agent_filter = q.get("agent", [None])[0]
            search = q.get("search", [None])[0]
            outputs = []
            ORCH_DIR = AGENT_OS_ROOT / "config" / "orchestration-runs"
            GOAL_REPORTS_DIR = AGENT_OS_ROOT / "workspace" / "goal-reports"
            REPORTS_DIR = AGENT_OS_ROOT / "workspace" / "reports"
            DAILY_DIR = VAULT / "daily"

            # 1. Orchestration run outputs (*_output.md)
            if ORCH_DIR.exists():
                # Build a lookup of goal_id -> agents from goals.json
                _goal_agents = {}
                _goals_file = AGENT_OS_ROOT / "config" / "goals.json"
                if _goals_file.exists():
                    try:
                        _goals_data = json.loads(_goals_file.read_text())
                        for _g in _goals_data:
                            _gid = _g.get("id") or _g.get("goal_id", "")
                            _agents = set()
                            for _t in _g.get("tasks", _g.get("subtasks", [])):
                                _a = _t.get("agent", "")
                                if _a: _agents.add(_a)
                            if _gid and _agents:
                                _goal_agents[_gid] = sorted(_agents)
                    except Exception:
                        pass

                for f in sorted(ORCH_DIR.glob("*_output.md"), reverse=True):
                    if len(outputs) >= limit + 20:
                        break
                    # Extract goal ID and subtask index from filename like "goal-1782306218362-0_output"
                    agent = "unknown"
                    stem = f.stem.replace("_output", "")
                    parts = stem.split("-")
                    # Find the goal ID by matching against known goals
                    for _gid, _agents in _goal_agents.items():
                        if stem.startswith(_gid.replace("goal-", "") + "-") or _gid in stem:
                            agent = _agents[0] if len(_agents) == 1 else str(len(_agents)) + " agents"
                            break
                    if agent == "unknown":
                        # Fallback: check if any known agent name appears in the filename
                        for p_part in parts:
                            if p_part in ("hermes", "researcher", "writer", "developer", "antigravity", "codex", "pm"):
                                agent = p_part
                                break
                    outputs.append({
                        "name": f.stem,
                        "type": "task_output" if type_filter in (None, "all", "task_output") else None,
                        "source": "orchestration",
                        "agent": agent,
                        "mtime": f.stat().st_mtime,
                        "size": f.stat().st_size,
                        "path": str(f.relative_to(AGENT_OS_ROOT)),
                        "ext": "md",
                        "icon": "⚡",
                    })

            # 2. Goal reports (goal-reports/*.md and *.html)
            if GOAL_REPORTS_DIR.exists():
                for f in sorted(GOAL_REPORTS_DIR.glob("*"), reverse=True):
                    if len(outputs) >= limit + 20:
                        break
                    outputs.append({
                        "name": f.stem,
                        "type": "goal_report" if type_filter in (None, "all", "goal_report") else None,
                        "source": "goal-reports",
                        "agent": "hermes",
                        "mtime": f.stat().st_mtime,
                        "size": f.stat().st_size,
                        "path": str(f.relative_to(AGENT_OS_ROOT)),
                        "ext": f.suffix.lstrip("."),
                        "icon": "🎯",
                    })

            # 3. Generated reports (workspace/reports/*.html)
            if REPORTS_DIR.exists():
                for f in sorted(REPORTS_DIR.glob("*.html"), reverse=True):
                    if len(outputs) >= limit + 20:
                        break
                    outputs.append({
                        "name": f.stem,
                        "type": "report" if type_filter in (None, "all", "report") else None,
                        "source": "reports",
                        "agent": "hermes",
                        "mtime": f.stat().st_mtime,
                        "size": f.stat().st_size,
                        "path": str(f.relative_to(AGENT_OS_ROOT)),
                        "ext": "html",
                        "icon": "📊",
                    })

            # 4. Daily notes (memory-vault/daily/*.md)
            if DAILY_DIR.exists():
                for f in sorted(DAILY_DIR.glob("*.md"), reverse=True)[:14]:
                    if len(outputs) >= limit + 20:
                        break
                    outputs.append({
                        "name": f.stem,
                        "type": "daily_note" if type_filter in (None, "all", "daily_note") else None,
                        "source": "daily",
                        "agent": "system",
                        "mtime": f.stat().st_mtime,
                        "size": f.stat().st_size,
                        "path": str(f.relative_to(AGENT_OS_ROOT)),
                        "ext": "md",
                        "icon": "📅",
                    })

            # 5. Agent output files from vault (research/reports, etc.)
            _agent_vault_dirs = [
                VAULT / "research" / "reports",
                VAULT / "agents" / "researcher" / "findings",
                VAULT / "agents" / "writer" / "drafts",
            ]
            for subdir in _agent_vault_dirs:
                if subdir.exists():
                    for f in sorted(subdir.glob("*.md"), reverse=True)[:10]:
                        if len(outputs) >= limit + 20:
                            break
                        agent_name = subdir.name
                        outputs.append({
                            "name": f.stem,
                            "type": "agent_output" if type_filter in (None, "all", "agent_output") else None,
                            "source": subdir.name,
                            "agent": agent_name,
                            "mtime": f.stat().st_mtime,
                            "size": f.stat().st_size,
                            "path": str(f.relative_to(AGENT_OS_ROOT)),
                            "ext": "md",
                            "icon": "📝",
                        })

            # Apply filters
            if type_filter and type_filter != "all":
                outputs = [o for o in outputs if o["type"] is not None]
            if agent_filter and agent_filter != "all":
                outputs = [o for o in outputs if o["agent"] == agent_filter]
            if search:
                sq = search.lower()
                outputs = [o for o in outputs if sq in o["name"].lower()]

            # Sort by mtime descending and limit
            outputs.sort(key=lambda x: x["mtime"], reverse=True)
            total_before_limit = len(outputs)
            outputs = outputs[:limit]

            # Get available agents for filter dropdown
            agents = sorted(set(o["agent"] for o in outputs))

            _json(self, {
                "ok": True,
                "outputs": outputs,
                "count": len(outputs),
                "total": total_before_limit,
                "agents": agents,
                "types": ["all", "task_output", "goal_report", "report", "daily_note", "agent_output"],
            })
            return

        # ── HOME DASHBOARD STATS ──
        if p.path == "/api/home-stats":
            try:
                v = vault()
                vstats = v.stats()
                vault_files = vstats.get("total_files", 0)
            except Exception:
                vault_files = 0
            try:
                goals_data = json.loads((AGENT_OS_ROOT / "config" / "goals.json").read_text())
                total_goals = len(goals_data)
                active_goals = sum(1 for g in goals_data if g.get("status") in ("in_progress", "decomposed"))
                completed_goals = sum(1 for g in goals_data if g.get("status") == "completed")
            except Exception:
                total_goals = active_goals = completed_goals = 0
            try:
                outputs_data = self._collect_outputs(limit=100)
                total_outputs = outputs_data.get("total", 0)
                recent_outputs = outputs_data.get("outputs", [])[:5]
            except Exception:
                total_outputs = 0
                recent_outputs = []
            try:
                files = scan_workspace()
                agents_count = len(set(f["agent"] for f in files))
            except Exception:
                agents_count = 0
            _json(self, {
                "ok": True,
                "vault_files": vault_files,
                "total_goals": total_goals,
                "active_goals": active_goals,
                "completed_goals": completed_goals,
                "total_outputs": total_outputs,
                "recent_outputs": recent_outputs,
                "agents_count": agents_count,
            })
            return

        # ── JARVIS CONVERSATION API (GET endpoints) ──
        if p.path == "/api/jarvis/greeting":
            personality = parse_qs(p.query).get("personality", ["professional"])[0]
            sys.path.insert(0, str(AGENT_OS_ROOT))
            from jarvis.engine import Jarvis
            jarvis = Jarvis(personality_preset=personality)
            _json(self, {"ok": True, "greeting": jarvis.greet()})
            return

        if p.path == "/api/jarvis/sessions":
            sys.path.insert(0, str(AGENT_OS_ROOT))
            from jarvis.memory import ConversationMemory
            mem = ConversationMemory()
            sessions = mem.get_recent_sessions(limit=20)
            _json(self, {"ok": True, "sessions": sessions})
            return

        if p.path == "/api/jarvis/memory":
            sys.path.insert(0, str(AGENT_OS_ROOT))
            from jarvis.memory import ConversationMemory
            mem = ConversationMemory()
            facts = mem.recall_all()
            _json(self, {"ok": True, "memory": facts})
            return

        # ── SERVE OUTPUT FILE CONTENT (auth-protected) ──
        # Backs the Outputs view "Open" button. Files live under AGENT_OS_ROOT;
        # the path is a relative, validated sub-path so traversal is impossible.
        if p.path.startswith("/api/outputs/file"):
            q = parse_qs(p.query)
            rel = (q.get("path", [""])[0] or "").strip()
            if not rel:
                self.send_error(400, "Missing 'path'")
                return
            # Resolve strictly inside AGENT_OS_ROOT
            target = (AGENT_OS_ROOT / rel).resolve()
            try:
                target.relative_to(AGENT_OS_ROOT.resolve())
            except ValueError:
                self.send_error(403, "Forbidden")
                return
            if not target.exists() or not target.is_file():
                self.send_error(404, "File not found")
                return
            suffix = target.suffix.lower()
            raw = target.read_bytes()
            if suffix in (".md", ".txt", ".text", ".markdown", ".rst"):
                try:
                    import markdown as _md
                    body_html = _md.markdown(
                        raw.decode("utf-8", "replace"),
                        extensions=["tables", "fenced_code", "nl2br"],
                    )
                    rendered = f"<html><head><meta charset='utf-8'><style>{_MARKDOWN_CSS}</style></head><body class='md'>{_sanitize_html(body_html)}</body></html>"
                except Exception:
                    # Fallback: render as preformatted text (no external deps needed)
                    from html import escape as _esc
                    rendered = f"<html><head><meta charset='utf-8'><style>{_MARKDOWN_CSS}</style></head><body class='md'><pre style='white-space:pre-wrap'>{_esc(raw.decode('utf-8','replace'))}</pre></body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(rendered)))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "SAMEORIGIN")
                self.end_headers()
                self.wfile.write(rendered.encode("utf-8"))
                return
            if suffix == ".html":
                # Already a full document — serve as-is, sanitized for safe in-app display
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "SAMEORIGIN")
                self.end_headers()
                self.wfile.write(_sanitize_html(raw.decode("utf-8", "replace")).encode("utf-8"))
                return
            # Binary / other (images, pdf, code) — return base64 for inline preview
            b64 = base64.b64encode(raw).decode("ascii")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Length", str(len(json.dumps({"ok": True, "b64": b64, "mime": MIME.get(suffix, "application/octet-stream")}).encode())))
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "b64": b64, "mime": MIME.get(suffix, "application/octet-stream")}).encode())
            return

        self.send_error(404)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            return json.loads(self.rfile.read(length)) if length > 0 else {}
        except Exception:
            self.send_error(400, "Invalid JSON")
            return None

    def handle_diagrams(self, p):
        """Handle Excalidraw diagram storage. Returns True if the request was
        handled (any method), False to fall through to other routes.
        Supports: GET list, GET /<name>, POST/PUT save, DELETE /<name>."""
        DIAGRAMS_DIR = AGENT_OS_ROOT / "data" / "diagrams"
        cmd = self.command
        # GET list
        if p.path == "/api/diagrams" and cmd == "GET":
            try:
                DIAGRAMS_DIR.mkdir(parents=True, exist_ok=True)
                files = sorted(f.name for f in DIAGRAMS_DIR.glob("*.excalidraw"))
                out = []
                for name in files:
                    try:
                        data = json.loads((DIAGRAMS_DIR / name).read_text())
                        out.append({
                            "name": name,
                            "title": data.get("title", name),
                            "updated": data.get("updated", ""),
                            "elements": len(data.get("elements", []) or []),
                        })
                    except Exception:
                        out.append({"name": name, "title": name, "updated": "", "elements": 0})
                _json(self, {"ok": True, "diagrams": out})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return True
        # GET single
        if p.path.startswith("/api/diagrams/") and cmd == "GET":
            name = p.path[len("/api/diagrams/"):]
            if "/" in name or ".." in name or not name.endswith(".excalidraw"):
                self.send_error(403, "Forbidden"); return True
            fpath = DIAGRAMS_DIR / name
            if fpath.exists():
                _json(self, {"ok": True, "name": name, "data": json.loads(fpath.read_text())})
            else:
                self.send_error(404, "Diagram not found")
            return True
        # POST/PUT save
        if p.path == "/api/diagrams" and cmd in ("POST", "PUT"):
            try:
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length) or b"{}")
                name = payload.get("name")
                if not name or "/" in name or ".." in name:
                    _json(self, {"ok": False, "error": "invalid_name"}); return True
                if not name.endswith(".excalidraw"):
                    name += ".excalidraw"
                data = payload.get("data", {})
                data["title"] = payload.get("title", name)
                data["updated"] = datetime.now().isoformat()
                if "type" not in data:
                    data["type"] = "excalidraw"
                if "version" not in data:
                    data["version"] = 2
                DIAGRAMS_DIR.mkdir(parents=True, exist_ok=True)
                (DIAGRAMS_DIR / name).write_text(json.dumps(data, indent=2))
                _json(self, {"ok": True, "name": name})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return True
        # DELETE single
        if p.path.startswith("/api/diagrams/") and cmd == "DELETE":
            name = p.path[len("/api/diagrams/"):]
            if "/" in name or ".." in name or not name.endswith(".excalidraw"):
                self.send_error(403, "Forbidden"); return True
            fpath = DIAGRAMS_DIR / name
            if fpath.exists():
                fpath.unlink()
                _json(self, {"ok": True})
            else:
                self.send_error(404, "Diagram not found")
            return True
        return False

    def do_POST(self):
        p = urlparse(self.path)

        # ── DIAGRAMS (Excalidraw) — POST save / DELETE (DELETE delegates here) ──
        if self.handle_diagrams(p):
            return

        # ── JARVIS CHAT (POST) ──
        if p.path == "/api/jarvis/chat":
            body = self._read_json_body()
            if not body:
                return
            message = body.get("message", "").strip()
            if not message:
                self.send_error(400, "Missing message")
                return
            session_id = body.get("session_id")
            personality = body.get("personality", "professional")
            user_id = body.get("user_id", "default")
            sys.path.insert(0, str(AGENT_OS_ROOT))
            from jarvis.engine import Jarvis
            jarvis = Jarvis(personality_preset=personality, user_id=user_id)
            if session_id:
                jarvis.resume_session(session_id)
            else:
                jarvis.start_session()
            result = jarvis.send(message)
            result["session_id"] = jarvis.session_id
            _json(self, result)
            return

        # ── PROJECT SPACE CREATE (POST) ──
        if p.path == "/api/projects/create":
            body = self._read_json_body()
            if not body:
                return
            _json(self, _create_project(body))
            return

        # ── Project goals/tasks/decisions (POST) ──
        if p.path.startswith("/api/projects/") and p.path.endswith("/goals"):
            body = self._read_json_body() or {}
            name = p.path[len("/api/projects/"):-len("/goals")]
            text = body.get("text", "").strip()
            if not text:
                self.send_error(400, "Missing goal text")
                return
            sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
            from project_manager import add_project_goal
            goal = add_project_goal(name, text, body.get("agent", "hermes"))
            _json(self, {"ok": True, "goal": goal})
            return

        if p.path.startswith("/api/projects/") and p.path.endswith("/tasks"):
            body = self._read_json_body() or {}
            name = p.path[len("/api/projects/"):-len("/tasks")]
            text = body.get("text", "").strip()
            if not text:
                self.send_error(400, "Missing task text")
                return
            sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
            from project_manager import add_project_task
            task = add_project_task(name, text, body.get("goal_id"))
            _json(self, {"ok": True, "task": task})
            return

        if p.path.startswith("/api/projects/") and p.path.endswith("/decisions"):
            body = self._read_json_body() or {}
            name = p.path[len("/api/projects/"):-len("/decisions")]
            decision = body.get("decision", "").strip()
            if not decision:
                self.send_error(400, "Missing decision text")
                return
            sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
            from project_manager import add_project_decision
            entry = add_project_decision(name, decision, body.get("rationale", ""))
            _json(self, {"ok": True, "decision": entry})
            return

        # ── Project Chat (POST) — send a message ──
        if p.path.startswith("/api/projects/") and p.path.endswith("/chat"):
            body = self._read_json_body() or {}
            name = p.path[len("/api/projects/"):-len("/chat")]
            message = body.get("message", "").strip()
            if not message:
                self.send_error(400, "Missing message")
                return
            personality = body.get("personality", "professional")
            session_id = body.get("session_id")

            # Log the user message
            sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
            from project_chat import add_message, ensure_chat_dir
            ensure_chat_dir(name)
            user_msg = add_message(
                project_name=name,
                role="user",
                content=message,
                agent_name="user",
                msg_type="message",
                session_id=session_id,
            )

            # ── Send the message — route to a specific agent via @mention, else Jarvis ──
            import time
            sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
            from project_chat import parse_mention
            from agent_registry import merged_definitions

            mention, cleaned = parse_mention(message)
            agent_key = None
            agent_meta = None

            # Resolve the @mention to a real agent key (accept key, display name, or role word)
            if mention:
                registry = merged_definitions()
                if mention in registry:
                    agent_key = mention
                else:
                    for k, a in registry.items():
                        nm = (a.get("name") or "").lower()
                        rl = (a.get("role") or "").lower()
                        if mention == nm or mention in nm.split() or mention in rl.split():
                            agent_key = k
                            break
                if agent_key:
                    agent_meta = registry.get(agent_key)
                    if cleaned:
                        message = cleaned  # route the natural instruction to the agent

            send_to_agent = agent_meta is not None

            def _route_to_agent(agent_key_, text_):
                """Call the Hermes bridge with a forced agent + project context."""
                import subprocess
                bridge = AGENT_OS_ROOT / "scripts" / "hermes_bridge.py"
                cmd = [sys.executable, str(bridge), "ask", text_,
                       "--agent", agent_key_, "--project_id", name, "--log"]
                try:
                    r = subprocess.run(cmd, capture_output=True, text=True,
                                       timeout=300, env={**os.environ})
                    out = r.stdout
                    try:
                        return json.loads(out)
                    except Exception:
                        return {"ok": r.returncode == 0,
                                "response": out.strip()[-2000:] if out else None,
                                "error": r.stderr.strip()[-300:] if r.stderr else None}
                except Exception as e:
                    return {"ok": False, "error": str(e), "response": None}

            if send_to_agent:
                result = _route_to_agent(agent_key, message)
                response_text = result.get("response", "")
                resp_session = session_id
                if response_text:
                    assistant_msg = add_message(
                        project_name=name,
                        role="assistant",
                        content=response_text,
                        agent_name=agent_meta.get("name", agent_key),
                        msg_type="message",
                        session_id=session_id,
                        metadata={"agent_key": agent_key, "emoji": agent_meta.get("emoji", "🤖")},
                    )
                else:
                    assistant_msg = None
            else:
                sys.path.insert(0, str(AGENT_OS_ROOT))
                from jarvis.engine import Jarvis
                jarvis = Jarvis(personality_preset=personality, user_id=name)
                if session_id:
                    jarvis.resume_session(session_id)
                else:
                    jarvis.start_session(title=f"Project Chat: {name}")
                result = jarvis.send(message)
                response_text = result.get("response", "")
                resp_session = jarvis.session_id
                if response_text:
                    assistant_msg = add_message(
                        project_name=name,
                        role="assistant",
                        content=response_text,
                        agent_name="jarvis",
                        msg_type="message",
                        session_id=jarvis.session_id,
                    )
                else:
                    assistant_msg = None

            _json(self, {
                "ok": result.get("ok", False),
                "response": response_text,
                "session_id": resp_session,
                "routed_agent": agent_key if send_to_agent else None,
                "duration_ms": result.get("duration_ms", 0),
                "user_message": user_msg,
                "assistant_message": assistant_msg,
            })
            return

        # ── Project Chat — auto-discover transcript by goal_id (exact path, checked first) ──
        if p.path == "/api/projects/chat/transcript":
            body = self._read_json_body() or {}
            goal_id = body.get("goal_id", "").strip()
            task_id = body.get("task_id", "").strip()
            agent_name = body.get("agent_name", "agent").strip()
            message = body.get("message", "").strip()
            msg_type = body.get("msg_type", "transcript").strip()
            if not goal_id or not message:
                self.send_error(400, "Missing goal_id or message")
                return
            sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
            from project_chat import emit_task_transcript
            ok = emit_task_transcript(
                goal_id=goal_id, task_id=task_id,
                agent_name=agent_name, message=message, msg_type=msg_type,
            )
            if ok:
                _json(self, {"ok": True, "project_found": True})
            else:
                _json(self, {"ok": False, "project_found": False,
                             "error": "No project found for this goal_id"})
            return

        # ── Project Chat (POST) — transcript / system event (scoped to project) ──
        if p.path.startswith("/api/projects/") and p.path.endswith("/chat/transcript"):
            body = self._read_json_body() or {}
            name = p.path[len("/api/projects/"):-len("/chat/transcript")]
            agent_name = body.get("agent_name", "agent").strip()
            content = body.get("content", "").strip()
            msg_type = body.get("type", "transcript")
            goal_id = body.get("goal_id", "")
            task_id = body.get("task_id", "")
            if not content:
                self.send_error(400, "Missing content")
                return
            sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
            from project_chat import add_agent_transcript, add_system_event
            if msg_type == "system_event":
                msg = add_system_event(name, content, msg_type="goal_update", goal_id=goal_id)
            else:
                msg = add_agent_transcript(name, agent_name=agent_name, content=content, msg_type=msg_type, goal_id=goal_id, task_id=task_id)
            _json(self, {"ok": True, "message": msg})
            return

        # ── Project update (status, description) ──
        if p.path.startswith("/api/projects/") and p.path.endswith("/update"):
            body = self._read_json_body() or {}
            name = p.path[len("/api/projects/"):-len("/update")]
            sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
            from project_manager import update_project
            manifest = update_project(name, body)
            _json(self, {"ok": True, "project": manifest})
            return

        # ── Project delete ──
        if p.path.startswith("/api/projects/") and p.path.endswith("/delete"):
            name = p.path[len("/api/projects/"):-len("/delete")]
            sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
            from project_manager import delete_project
            ok = delete_project(name)
            _json(self, {"ok": ok, "deleted": name})
            return

        # ── Guardian (PII / Credential Guardrail) ──
        if p.path == "/api/guardian/scan":
            body = self._read_json_body() or {}
            text = body.get("text", "")
            if not text:
                self.send_error(400, "Missing text to scan")
                return
            sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
            from guardian import scan_text
            report = scan_text(text, custom_blocklist=body.get("blocklist"))
            _json(self, {
                "ok": True,
                "result": report.result.value,
                "findings_count": len(report.findings),
                "high_severity": report.high_severity_count,
                "scan_time_ms": report.scan_time_ms,
                "findings": [
                    {
                        "category": f.category,
                        "severity": f.severity,
                        "action": f.action.value,
                        "position": f.position,
                    }
                    for f in report.findings
                ],
                "redacted_text": report.redacted_text,
            })
            return

        if p.path == "/api/guardian/status":
            sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
            from guardian import load_custom_patterns
            load_custom_patterns()
            from guardian import CUSTOM_PATTERNS
            _json(self, {
                "ok": True,
                "enabled": os.environ.get("GUARDIAN_DISABLED", "0") != "1",
                "pii_patterns": 12,
                "credential_patterns": 14,
                "custom_patterns": len(CUSTOM_PATTERNS),
            })
            return

        # ── Ollama / Tier 3 ──
        if p.path == "/api/ollama/status":
            sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
            from ollama_worker import is_ollama_available, get_ollama_models
            available = is_ollama_available()
            models = get_ollama_models() if available else []
            _json(self, {
                "ok": True,
                "available": available,
                "models": [{"name": m.get("name", ""), "size": m.get("size", 0)} for m in models],
            })
            return

        if p.path == "/api/ollama/run":
            body = self._read_json_body() or {}
            project_space = body.get("project_space", "")
            task = body.get("task", {})
            if not project_space or not task:
                self.send_error(400, "Missing project_space or task")
                return
            sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
            from ollama_worker import execute_tier3, ExecutionTask, OllamaConfig
            from model_router import load_project_config

            proj_config = load_project_config(project_space)
            worker_config = proj_config.get("execution_workers", {}).get("tier_3_builder", {})

            ollama_config = OllamaConfig(
                base_url=worker_config.get("base_url", "http://localhost:11434/v1"),
                model_name=worker_config.get("model_name", "qwen2.5-coder:14b"),
                max_retries=worker_config.get("max_retries", 5),
                per_iteration_timeout=worker_config.get("per_iteration_timeout", 60),
            )

            exec_task = ExecutionTask(
                file_path=task.get("file_path", ""),
                task_description=task.get("description", ""),
                test_command=task.get("test_command", "echo no-test"),
                project_space=project_space,
                context=task.get("context", ""),
            )

            result = execute_tier3(exec_task, ollama_config)
            _json(self, {"ok": True, "result": result.to_dict()})
            return

        # ── TIER 3 SWITCH MODEL ──
        if p.path == "/api/tier3/switch":
            body = self._read_json_body() or {}
            model_name = body.get("model_name", "").strip()
            if not model_name:
                _json(self, {"ok": False, "error": "Missing model_name"})
                return
            timeout = int(body.get("timeout", 600))
            try:
                sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
                from tier3_manager import switch_model
                _json(self, switch_model(model_name, timeout=timeout))
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # ── Model Router ──
        if p.path == "/api/router/route":
            body = self._read_json_body() or {}
            description = body.get("description", "")
            context = body.get("context", {})
            if not description:
                self.send_error(400, "Missing task description")
                return
            sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
            from model_router import route_task
            decision = route_task(description, context)
            _json(self, {"ok": True, "routing": decision.to_dict()})
            return

        # ── Project Task Graph (public endpoint) ──
        if p.path == "/api/projects/tasks/graph":
            body = self._read_json_body() or {}
            name = body.get("project", "default")
            try:
                sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
                from scrum_master import ScrumMaster
                sm = ScrumMaster(name)
                sm._load_state()
                graph = sm.get_dependency_graph()
                critical_path = sm.get_critical_path()
                _json(self, {"ok": True, "graph": graph, "critical_path": critical_path})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # ── Dry-Run Mode Control ──
        if p.path == "/api/projects/dry-run":
            self._user_payload = self._require_auth()  # Require auth
            body = self._read_json_body() or {}
            name = body.get("project", "default")
            enabled = body.get("enabled", True)
            try:
                sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
                from scrum_master import ScrumMaster
                sm = ScrumMaster(name)
                sm._load_state()
                sm.set_dry_run(enabled)
                _json(self, {"ok": True, "dry_run": sm._dry_run})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # ── Telemetry / Economics API (public read-only endpoint) ──
        if p.path == "/api/telemetry/summary":
            try:
                sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
                from telemetry import get_summary, get_runs
                summary = get_summary(limit=5000)
                runs = get_runs(limit=body.get("limit", 50) if (body := self._read_json_body() or {}) else 50)
                _json(self, {"ok": True, "summary": summary, "runs": runs})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # ── TELEMETRY: timeseries (GET) — Feature 12 ──
        if p.path == "/api/telemetry/timeseries":
            try:
                sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
                from telemetry import get_timeseries
                granularity = parse_qs(p.query).get("granularity", ["day"])[0]
                if granularity not in ("hour", "day"):
                    granularity = "day"
                _json(self, {"ok": True, **get_timeseries(granularity=granularity)})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # ── TELEMETRY: efficiency (GET) — Feature 12 ──
        if p.path == "/api/telemetry/efficiency":
            try:
                sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
                from telemetry import get_efficiency
                _json(self, {"ok": True, **get_efficiency()})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # ── TELEMETRY: alerts (GET) — Feature 12 ──
        if p.path == "/api/telemetry/alerts":
            try:
                sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
                from telemetry import get_alerts
                _json(self, {"ok": True, "alerts": get_alerts()})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # ── Audit Events API (public read-only endpoint) ──
        if p.path == "/api/audit/events":
            body = self._read_json_body() or {}
            action = body.get("action")
            user = body.get("user")
            limit = min(body.get("limit", 100), 1000)
            try:
                sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
                from audit import get_audit_events
                events = get_audit_events(action=action, user=user, limit=limit)
                _json(self, {"ok": True, "events": events})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # ── TELEMETRY: create alert (POST) — Feature 12 ──
        if p.path == "/api/telemetry/alerts" and self.command == "POST":
            self._user_payload = self._require_auth()
            if not self._user_payload:
                return
            body = self._read_json_body() or {}
            try:
                sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
                from telemetry import set_alert
                alert = set_alert(
                    name=body.get("name", ""),
                    metric=body.get("metric", ""),
                    operator=body.get("operator", "gt"),
                    threshold=float(body.get("threshold", 0)),
                    enabled=body.get("enabled", True),
                )
                _json(self, {"ok": True, "alert": alert})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # ── TELEMETRY: check alerts (POST) — Feature 12 ──
        if p.path == "/api/telemetry/alerts/check" and self.command == "POST":
            try:
                sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
                from telemetry import check_alerts
                triggered = check_alerts()
                _json(self, {"ok": True, "triggered": triggered, "count": len(triggered)})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # ── AGENT REGISTRY: create/update (POST) ──
        if p.path == "/api/agents" and self.command == "POST":
            self._user_payload = self._require_auth()
            if not self._user_payload:
                return
            body = self._read_json_body() or {}
            key = (body.get("key") or "").lower().strip()
            if not key:
                _json(self, {"ok": False, "error": "agent 'key' is required"})
                return
            try:
                sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
                from agent_registry import get_registry
                saved = get_registry().save_agent(key, body)
                _json(self, {"ok": True, "agent": saved})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # ── WEBHOOKS: create (POST) — Feature 8 ──
        if p.path == "/api/webhooks" and self.command == "POST":
            self._user_payload = self._require_auth()
            if not self._user_payload:
                return
            body = self._read_json_body() or {}
            try:
                sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
                from webhooks import create_webhook
                hook = create_webhook(
                    name=body.get("name", ""),
                    url=body.get("url", ""),
                    events=body.get("events", []),
                    secret=body.get("secret", ""),
                    active=body.get("active", True),
                )
                _json(self, {"ok": True, "webhook": hook})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # ── WEBHOOKS: test delivery (POST) — Feature 8 ──
        if p.path == "/api/webhooks/test" and self.command == "POST":
            self._user_payload = self._require_auth()
            if not self._user_payload:
                return
            body = self._read_json_body() or {}
            try:
                sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
                from webhooks import create_webhook, emit, delete_webhook
                # Fire a synthetic 'ping' to the supplied URL (no persisted webhook).
                url = body.get("url", "")
                secret = body.get("secret", "")
                if not url or not url.startswith(("http://", "https://")):
                    _json(self, {"ok": False, "error": "valid 'url' is required"})
                    return
                # Create a temporary single-event webhook, emit, then clean up.
                tmp = create_webhook("__test__", url, ["ping"], secret=secret, active=True)
                n = emit("ping", {"message": "MoiraiCore webhook connectivity test"})
                delete_webhook(tmp["id"])
                _json(self, {"ok": True, "targeted": n, "note": "check the Deliveries log for the result (async)"})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # ── WEBHOOKS: delete (DELETE /api/webhooks/<id>) — Feature 8 ──
        if p.path.startswith("/api/webhooks/") and self.command == "DELETE":
            self._user_payload = self._require_auth()
            if not self._user_payload:
                return
            wh_id = p.path[len("/api/webhooks/"):].split("?")[0]
            try:
                sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
                from webhooks import delete_webhook
                ok = delete_webhook(wh_id)
                _json(self, {"ok": ok, "deleted": wh_id})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # ── WEBHOOKS: update (PUT /api/webhooks/<id>) — Feature 8 Enhanced ──
        if p.path.startswith("/api/webhooks/") and self.command == "PUT":
            wh_id = p.path[len("/api/webhooks/"):].split("?")[0]
            # Only match wh- IDs, not "dead-letter" sub-paths
            if not wh_id.startswith("wh-"):
                # Not a valid webhook ID — continue to other routes
                pass
            else:
                self._user_payload = self._require_auth()
                if not self._user_payload:
                    return
                body = self._read_json_body() or {}
                try:
                    sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
                    from webhooks import update_webhook
                    updated = update_webhook(wh_id, **body)
                    if updated:
                        _json(self, {"ok": True, "webhook": updated})
                    else:
                        _json(self, {"ok": False, "error": "Webhook not found"})
                except Exception as e:
                    _json(self, {"ok": False, "error": str(e)})
                return

        # ── WEBHOOKS: dead-letter replay (POST) — Feature 8 Enhanced ──
        if p.path == "/api/webhooks/dead-letter/replay" and self.command == "POST":
            self._user_payload = self._require_auth()
            if not self._user_payload:
                return
            body = self._read_json_body() or {}
            dl_id = body.get("delivery_id", "")
            try:
                sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
                from webhooks import replay_dead_letter
                result = replay_dead_letter(dl_id)
                _json(self, result)
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # ── WEBHOOKS: dead-letter replay-all (POST) — Feature 8 Enhanced ──
        if p.path == "/api/webhooks/dead-letter/replay-all" and self.command == "POST":
            self._user_payload = self._require_auth()
            if not self._user_payload:
                return
            try:
                sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
                from webhooks import replay_all_dead_letters
                result = replay_all_dead_letters()
                _json(self, result)
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # ── AGENT REGISTRY: delete (DELETE method, auth-gated) ──
        if p.path.startswith("/api/agents/") and self.command == "DELETE":
            self._user_payload = self._require_auth()
            if not self._user_payload:
                return
            agent_key = p.path[len("/api/agents/"):].split("?")[0]
            try:
                sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
                from agent_registry import get_registry
                ok = get_registry().delete_agent(agent_key)
                _json(self, {"ok": ok, "deleted": agent_key})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # ── Dependency Vulnerability Scan (POST) ──
        if p.path == "/api/scan/dependencies":
            self._user_payload = self._require_auth()
            body = self._read_json_body() or {}
            project = body.get("project", "default")
            try:
                sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
                from scrum_master import ScrumMaster
                sm = ScrumMaster(project)
                sm._load_state()
                result = sm._scan_dependencies()
                _json(self, result)
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # ── Auth check (skip for public paths) ──
        self._user_payload = None
        if not self._is_public_path(p.path):
            try:
                self._user_payload = self._require_auth()
            except ValueError:
                return  # _require_auth already sent 401

        # ── Rate limiting ──
        client_ip = self.client_address[0]
        if not _check_rate_limit(client_ip):
            self.send_error(429, "Rate limit exceeded. Max %d requests per %d seconds." % (_RATE_LIMIT_MAX, _RATE_LIMIT_WINDOW))
            return

        # ── Learn & Build (LnB) — POST endpoints ──

        if p.path == "/api/lnb/learn":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length)) if length > 0 else {}
            except Exception:
                self.send_error(400, "Invalid JSON")
                return
            source = body.get("source", "")
            topic = body.get("topic", "")
            source_type = body.get("type", "auto")
            if not source:
                _json(self, {"ok": False, "error": "missing_source", "message": "source is required"})
                return
            try:
                from lnb_engine import LearnAndBuild
                lnb = LearnAndBuild()
                result = lnb.learn(source, topic=topic, source_type=source_type)
                _json(self, result)
            except ImportError:
                _json(self, {"ok": False, "error": "lnb_module_not_available"})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        if p.path == "/api/note":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length))
            except Exception:
                self.send_error(400, "Invalid JSON")
                return
            note = body.get("note", "").strip()
            if note:
                today = datetime.now().strftime("%Y-%m-%d")
                df = VAULT / "daily" / f"{today}.md"
                df.parent.mkdir(parents=True, exist_ok=True)
                with open(df, "a") as fh:
                    fh.write("\n- %s — %s\n" % (datetime.now().strftime("%H:%M"), note))
                _json(self, {"ok": True})
            else:
                self.send_error(400, "Empty note")
            return
        if p.path == "/api/vault/save":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length))
            except Exception:
                self.send_error(400, "Invalid JSON")
                return
            path = body.get("path", "").strip()
            content = body.get("content", "")
            if not path:
                self.send_error(400, "Missing path")
                return
            # SECURITY: prevent path traversal
            try:
                fp = (VAULT / path).resolve()
                if not fp.is_relative_to(VAULT.resolve()):
                    self.send_error(403, "Access denied")
                    return
            except (ValueError, RuntimeError):
                self.send_error(403, "Access denied")
                return
            # Only allow .md files
            if fp.suffix.lower() != ".md":
                self.send_error(400, "Only .md files can be saved")
                return
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(content, encoding="utf-8")
            # Reindex
            try:
                _log_vault_access("write", path, "success", "size=" + str(len(content)))
                v = vault()
                v.index_file(fp)
            except Exception:
                pass
            _json(self, {"ok": True, "path": path})
            return

        if p.path == "/api/vault/semantic":
            user = self._require_auth()
            if not user:
                return
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length)) if length else {}
            except Exception:
                body = {}
            q = (body.get("query") or body.get("q") or "").strip()
            top_k = _clamp(int(body.get("top_k", body.get("max", 10))), 1, 50)
            backend = body.get("backend")  # None -> SEMANTIC_BACKEND env
            if not q:
                _json(self, {"query": q, "results": [], "count": 0, "backend": backend or "tfidf"})
                return
            try:
                results = vault().semantic_search(q, top_k=top_k, backend=backend)
            except Exception as e:
                results = []
            _json(self, {
                "query": q,
                "results": results,
                "count": len(results),
                "backend": backend or SEMANTIC_BACKEND,
                "mode": "semantic",
            })
            return

        if p.path == "/api/vault/delete":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length))
            except Exception:
                self.send_error(400, "Invalid JSON")
                return
            path = body.get("path", "").strip()
            if not path:
                self.send_error(400, "Missing path")
                return
            try:
                fp = (VAULT / path).resolve()
                if not fp.is_relative_to(VAULT.resolve()):
                    self.send_error(403, "Access denied")
                    return
            except (ValueError, RuntimeError):
                self.send_error(403, "Access denied")
                return
            if fp.exists() and fp.is_file():
                fp.unlink()
                try:
                    _log_vault_access("delete", path, "success")
                    v = vault()
                    v.remove_file(path)
                except Exception:
                    pass
                _json(self, {"ok": True})
            else:
                self.send_error(404, "File not found")
            return

        if p.path == "/api/tasks/create":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length))
            except Exception:
                self.send_error(400, "Invalid JSON")
                return
            title = body.get("title", "").strip()
            if not title:
                self.send_error(400, "Missing title")
                return
            tasks = load_tasks()
            # Normalise skills: accept string or list, store as list
            raw_skills = body.get("skills", [])
            if isinstance(raw_skills, str):
                skills_list = [s.strip() for s in raw_skills.split(",") if s.strip()]
            else:
                skills_list = [str(s).strip() for s in raw_skills if str(s).strip()]
            task = {
                "id": str(int(time.time() * 1000)),
                "title": title,
                "desc": body.get("desc", "").strip(),
                "agent": body.get("agent", "any"),
                "priority": body.get("priority", "p2"),
                "skills": skills_list,
                "status": "queued",
                "created": datetime.now().isoformat(),
                "updated": datetime.now().isoformat(),
            }
            tasks.insert(0, task)
            save_tasks(tasks)
            _json(self, {"ok": True, "task": task})
            return

        if p.path == "/api/tasks/update":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length))
            except Exception:
                self.send_error(400, "Invalid JSON")
                return
            task_id = body.get("id", "").strip()
            if not task_id:
                self.send_error(400, "Missing id")
                return
            tasks = load_tasks()
            task = next((t for t in tasks if t["id"] == task_id), None)
            if not task:
                self.send_error(404, "Task not found")
                return
            for field in ["title", "desc", "agent", "priority", "status", "skills"]:
                if field in body:
                    if field == "skills":
                        raw = body[field]
                        if isinstance(raw, str):
                            task[field] = [s.strip() for s in raw.split(",") if s.strip()]
                        else:
                            task[field] = [str(s).strip() for s in raw if str(s).strip()]
                    else:
                        task[field] = body[field]
            task["updated"] = datetime.now().isoformat()
            save_tasks(tasks)
            sync_task_to_kanban(task)
            _json(self, {"ok": True, "task": task})
            return

        if p.path == "/api/tasks/delete":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length))
            except Exception:
                self.send_error(400, "Invalid JSON")
                return
            task_id = body.get("id", "").strip()
            if not task_id:
                self.send_error(400, "Missing id")
                return
            tasks = load_tasks()
            tasks = [t for t in tasks if t["id"] != task_id]
            save_tasks(tasks)
            _json(self, {"ok": True})
            return

        # ── KANBAN SYNC (POST) ──
        if p.path == "/api/kanban/sync":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length)) if length > 0 else {}
            except Exception:
                body = {}
            # Receive manual cards from dashboard localStorage, merge with server
            manual_cards = body.get("cards", {})
            board = load_kanban()
            for lane in ("backlog", "progress", "review", "done"):
                submitted = manual_cards.get(lane, [])
                for card in submitted:
                    if not card.get("synced"):  # only manual cards
                        existing = [c for c in board[lane] if c.get("id") == card.get("id")]
                        if not existing:
                            board[lane].append(card)
            save_kanban(board)
            counts = {lane: len(board[lane]) for lane in board}
            _json(self, {"ok": True, "board": board, "counts": counts})
            return

        # ── TASK RUNNER (POST) ──
        if p.path == "/api/task-runner/start":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length)) if length > 0 else {}
            except Exception:
                body = {}
            # Start the daemon
            runner_script = str(AGENT_OS_ROOT / "scripts" / "task_runner.py")
            try:
                subprocess.Popen(
                    [sys.executable, runner_script, "--daemon"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    cwd=str(AGENT_OS_ROOT),
                )
                _json(self, {"ok": True, "message": "Task runner started"})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        if p.path == "/api/task-runner/stop":
            pid_file = AGENT_OS_ROOT / "config" / "task_runner.pid"
            if pid_file.exists():
                try:
                    pid = int(pid_file.read_text().strip())
                    os.kill(pid, 15)  # SIGTERM
                    _json(self, {"ok": True, "message": f"Task runner stopped (PID {pid})"})
                except Exception as e:
                    _json(self, {"ok": False, "error": str(e)})
            else:
                _json(self, {"ok": True, "message": "Task runner was not running"})
            return

        if p.path == "/api/task-runner/run-once":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length)) if length > 0 else {}
            except Exception:
                body = {}
            try:
                result = subprocess.run(
                    [sys.executable, str(AGENT_OS_ROOT / "scripts" / "task_runner.py"), "--once"],
                    capture_output=True, text=True, timeout=600,
                    cwd=str(AGENT_OS_ROOT),
                )
                output = result.stdout.strip()
                tasks = load_tasks()
                queued = [t for t in tasks if t.get("status") == "queued"]
                _json(self, {"ok": True, "output": output, "remaining_queued": len(queued)})
            except subprocess.TimeoutExpired:
                _json(self, {"ok": False, "error": "Task runner timed out (tasks may still be running)"})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # ── GOALS (POST) ──
        if p.path == "/api/goals/create":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length))
            except Exception:
                self.send_error(400, "Invalid JSON")
                return
            title = body.get("title", "").strip()
            if not title:
                self.send_error(400, "Missing title")
                return
            try:
                from goal_engine import GoalEngine
                engine = GoalEngine()
                goal = engine.create_goal(
                    title=title,
                    desc=body.get("desc", "").strip(),
                    priority=body.get("priority", "p2"),
                    auto_run=body.get("auto_run", True),
                )
                _json(self, {"ok": True, "goal": goal})
            except Exception as e:
                # Fallback: basic goal creation without LLM decomposition
                goals = load_goals()
                goal = {
                    "id": "goal-" + str(int(time.time() * 1000)),
                    "title": title,
                    "desc": body.get("desc", "").strip(),
                    "priority": body.get("priority", "p2"),
                    "status": "decomposed",
                    "created": datetime.now().isoformat(),
                    "updated": datetime.now().isoformat(),
                    "subtasks": [],
                    "tasks_total": 0,
                    "tasks_completed": 0,
                    "tasks_failed": 0,
                    "report_path": None,
                }
                goals.insert(0, goal)
                save_goals(goals)
                _json(self, {"ok": True, "goal": goal, "warning": f"Goal Engine unavailable: {e}"})
            return

        if p.path == "/api/goals/decompose":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length))
            except Exception:
                self.send_error(400, "Invalid JSON")
                return
            goal_id = body.get("id", "").strip()
            if not goal_id:
                self.send_error(400, "Missing goal id")
                return
            goals = load_goals()
            goal = next((g for g in goals if g["id"] == goal_id), None)
            if not goal:
                self.send_error(404, f"Goal '{goal_id}' not found")
                return
            # Decompose via orchestrator
            try:
                sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
                from agent_orchestrator import decompose_task
                subtasks = decompose_task(goal["title"] + ". " + goal.get("desc", ""))
            except Exception as e:
                import traceback as _tb
                _json(self, {"ok": False, "error": f"Decomposition failed: {e}", "trace": _tb.format_exc()})
                return
            # Create tasks linked to this goal
            tasks = load_tasks()
            created_tasks = []
            for st in subtasks:
                task = {
                    "id": str(int(time.time() * 1000)) + "-" + st["agent"],
                    "title": st["subtask"][:120],
                    "desc": st["subtask"],
                    "agent": st["agent"],
                    "priority": goal.get("priority", "p2"),
                    "status": "queued",
                    "goal_id": goal_id,
                    "created": datetime.now().isoformat(),
                    "updated": datetime.now().isoformat(),
                }
                tasks.insert(0, task)
                created_tasks.append(task)
            save_tasks(tasks)
            # Sync new tasks to Kanban
            for t in created_tasks:
                t["goal_title"] = goal["title"]
                sync_task_to_kanban(t)
            # Update goal status
            goal["status"] = "decomposed"
            goal["updated"] = datetime.now().isoformat()
            save_goals(goals)
            _json(self, {"ok": True, "goal": goal, "tasks": created_tasks, "count": len(created_tasks)})
            return

        if p.path == "/api/goals/run":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length))
            except Exception:
                self.send_error(400, "Invalid JSON")
                return
            goal_id = body.get("goal_id", "").strip()
            if not goal_id:
                self.send_error(400, "Missing goal_id")
                return
            try:
                from goal_engine import GoalEngine
                engine = GoalEngine()
                result = engine.run_goal(goal_id)
                _json(self, result)
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        if p.path == "/api/goals/run/background":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length)) if length > 0 else {}
            except Exception:
                self.send_error(400, "Invalid JSON")
                return
            goal_id = body.get("goal_id", "").strip()
            if not goal_id:
                self.send_error(400, "Missing goal_id")
                return
            try:
                from goal_engine import GoalEngine
                engine = GoalEngine()
                task_id = engine.run_goal_background(goal_id)
                _json(self, {"ok": True, "task_id": task_id, "goal_id": goal_id, "status": "started"})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        if p.path == "/api/goals/synthesize":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length))
            except Exception:
                self.send_error(400, "Invalid JSON")
                return
            goal_id = body.get("goal_id", "").strip()
            if not goal_id:
                self.send_error(400, "Missing goal_id")
                return
            try:
                from goal_engine import GoalEngine
                engine = GoalEngine()
                result = engine.synthesize_goal(goal_id)
                _json(self, result)
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        if p.path == "/api/goals/delete":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length))
            except Exception:
                self.send_error(400, "Invalid JSON")
                return
            goal_id = body.get("goal_id", "").strip()
            if not goal_id:
                self.send_error(400, "Missing goal_id")
                return
            try:
                goals = load_goals()
                goals = [g for g in goals if g["id"] != goal_id]
                save_goals(goals)
                _json(self, {"ok": True})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        if p.path == "/api/goals/update":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length))
            except Exception:
                self.send_error(400, "Invalid JSON")
                return
            goal_id = body.get("id", "").strip()
            if not goal_id:
                self.send_error(400, "Missing id")
                return
            goals = load_goals()
            goal = next((g for g in goals if g["id"] == goal_id), None)
            if not goal:
                self.send_error(404, f"Goal'{goal_id}' not found")
                return
            for field in ["title", "desc", "priority", "status"]:
                if field in body:
                    goal[field] = body[field]
            goal["updated"] = datetime.now().isoformat()
            save_goals(goals)
            _json(self, {"ok": True, "goal": goal})
            return

        if p.path == "/api/goals/delete":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length))
            except Exception:
                self.send_error(400, "Invalid JSON")
                return
            goal_id = body.get("id", "").strip()
            if not goal_id:
                self.send_error(400, "Missing id")
                return
            goals = load_goals()
            goals = [g for g in goals if g["id"] != goal_id]
            save_goals(goals)
            # Unlink tasks from this goal
            tasks = load_tasks()
            for t in tasks:
                if t.get("goal_id") == goal_id:
                    t.pop("goal_id", None)
            save_tasks(tasks)
            _json(self, {"ok": True})
            return

        # ── HERMES BRIDGE (POST) ──
        if p.path == "/api/hermes/ask":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length)) if length > 0 else {}
            except Exception:
                self.send_error(400, "Invalid JSON")
                return
            query = body.get("query", "").strip()
            if not query:
                self.send_error(400, "Missing query")
                return
            context = body.get("context", "")
            model = body.get("model", "")
            skills = body.get("skills", "")
            do_route = body.get("route", True)  # Default: auto-route every request
            project_id = body.get("project_id", "")
            project_space = body.get("project_space", "")
            tier3_task = body.get("tier3_task", "")

            bridge_args = ["ask", query]
            if do_route:
                bridge_args.append("--route")
            bridge_args.append("--log")
            if context:
                bridge_args += ["--context", context]
            if model:
                bridge_args += ["--model", model]
            if skills:
                bridge_args += ["--skills", skills]
            if project_id:
                bridge_args += ["--project_id", project_id]
            if project_space:
                bridge_args += ["--project_space", project_space]
            if tier3_task:
                bridge_args += ["--tier3_task", tier3_task]
            result = _run_hermes_bridge(bridge_args)
            _json(self, result)
            return

        # ── PROJECT STATE MANAGEMENT (POST) ──
        if p.path == "/api/projects/state":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length)) if length > 0 else {}
            except Exception:
                self.send_error(400, "Invalid JSON")
                return
            project_name = body.get("project_name", "").strip()
            action = body.get("action", "get")
            if not project_name:
                self.send_error(400, "Missing project_name")
                return
            try:
                sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
                from sandbox import ProjectSandbox, SandboxViolation
                sandbox = ProjectSandbox(project_name)

                if action == "get":
                    _json(self, {"ok": True, "state": sandbox.get_state()})
                elif action == "add_goal":
                    text = body.get("text", "")
                    agent = body.get("agent", "hermes")
                    if not text:
                        self.send_error(400, "Missing text")
                        return
                    goal = sandbox.add_goal(text, agent)
                    _json(self, {"ok": True, "goal": goal})
                elif action == "add_task":
                    text = body.get("text", "")
                    goal_id = body.get("goal_id")
                    if not text:
                        self.send_error(400, "Missing text")
                        return
                    task = sandbox.add_task(text, goal_id)
                    _json(self, {"ok": True, "task": task})
                elif action == "add_decision":
                    decision = body.get("decision", "")
                    rationale = body.get("rationale", "")
                    if not decision:
                        self.send_error(400, "Missing decision")
                        return
                    entry = sandbox.add_decision(decision, rationale)
                    _json(self, {"ok": True, "entry": entry})
                elif action == "update":
                    updates = body.get("updates", {})
                    state = sandbox.update_state(updates)
                    _json(self, {"ok": True, "state": state})
                elif action == "validate_key":
                    key = body.get("api_key", "")
                    valid = sandbox.validate_api_key(key)
                    _json(self, {"ok": True, "valid": valid})
                else:
                    self.send_error(400, f"Unknown action: {action}")
            except FileNotFoundError:
                self.send_error(404, f"Project '{project_name}' not found")
            except SandboxViolation as e:
                self.send_error(403, str(e))
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # ── PROJECT SANDBOX FILES (POST) ──
        if p.path == "/api/projects/files":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length)) if length > 0 else {}
            except Exception:
                self.send_error(400, "Invalid JSON")
                return
            project_name = body.get("project_name", "").strip()
            action = body.get("action", "list")
            if not project_name:
                self.send_error(400, "Missing project_name")
                return
            try:
                sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
                from sandbox import ProjectSandbox, SandboxViolation
                sandbox = ProjectSandbox(project_name)

                if action == "list":
                    subdir = body.get("subdir", "src")
                    files = sandbox.list_files(subdir)
                    _json(self, {"ok": True, "files": files})
                elif action == "read":
                    file_path = body.get("file_path", "")
                    if not file_path:
                        self.send_error(400, "Missing file_path")
                        return
                    content = sandbox.read_file(file_path)
                    _json(self, {"ok": True, "content": content})
                elif action == "write":
                    file_path = body.get("file_path", "")
                    content = body.get("content", "")
                    if not file_path:
                        self.send_error(400, "Missing file_path")
                        return
                    sandbox.verify_write(file_path)  # Raises SandboxViolation
                    sandbox.write_file(file_path, content)
                    _json(self, {"ok": True, "path": file_path})
                else:
                    self.send_error(400, f"Unknown action: {action}")
            except FileNotFoundError:
                self.send_error(404, f"Project '{project_name}' not found")
            except SandboxViolation as e:
                self.send_error(403, str(e))
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # ── PROJECT PIPELINE (POST) ──
        if p.path == "/api/projects/pipeline":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length)) if length > 0 else {}
            except Exception:
                self.send_error(400, "Invalid JSON")
                return
            project_name = body.get("project_name", "").strip()
            pipeline = body.get("pipeline", "build-and-test")
            if not project_name:
                self.send_error(400, "Missing project_name")
                return
            try:
                sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
                from project_orchestrator import ProjectOrchestrator
                orch = ProjectOrchestrator(project_name)
                result = orch.run_pipeline(pipeline)
                _json(self, result)
            except FileNotFoundError:
                self.send_error(404, f"Project '{project_name}' not found")
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # ── PROJECT EVALUATE (POST) ──
        if p.path == "/api/projects/evaluate":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length)) if length > 0 else {}
            except Exception:
                self.send_error(400, "Invalid JSON")
                return
            project_name = body.get("project_name", "").strip()
            if not project_name:
                self.send_error(400, "Missing project_name")
                return
            try:
                sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
                from project_orchestrator import ProjectOrchestrator
                orch = ProjectOrchestrator(project_name)
                result = orch.evaluate()
                _json(self, result)
            except FileNotFoundError:
                self.send_error(404, f"Project '{project_name}' not found")
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # ── PROJECT INIT (POST) — Antigravity Workspace Engine ──
        if p.path == "/api/projects/init":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length)) if length > 0 else {}
            except Exception:
                self.send_error(400, "Invalid JSON")
                return
            name = body.get("name", "").strip()
            if not name:
                self.send_error(400, "Missing name")
                return
            try:
                sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
                from antigravity_workspace import AntigravityWorkspace
                from scrum_master import ScrumMaster
                workspace = AntigravityWorkspace(name)
                ws_config = workspace.create_project(
                    description=body.get("description", ""),
                    template=body.get("template", "blank"),
                )
                # Initialize ScrumMaster with the project
                sm = ScrumMaster(name)
                sm.set_goal(body.get("description", f"Build {name}"))
                sm.decompose_backlog()
                _json(self, {"ok": True, "workspace": ws_config, "backlog_size": len(sm.backlog)})
            except FileExistsError as e:
                self.send_error(409, str(e))
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # ── SCRUM STATUS (POST) ──
        if p.path == "/api/projects/scrum":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length)) if length > 0 else {}
            except Exception:
                self.send_error(400, "Invalid JSON")
                return
            project_name = body.get("project_name", "").strip()
            action = body.get("action", "status")
            if not project_name:
                self.send_error(400, "Missing project_name")
                return
            try:
                sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
                from scrum_master import ScrumMaster, Tier
                from pathlib import Path as _Path
                sm = ScrumMaster(project_name)
                sm._load_state()

                if action == "status":
                    _json(self, sm.status_report())
                elif action == "next_task":
                    task = sm.get_next_task()
                    _json(self, {"ok": True, "task": task.to_dict() if task else None})
                elif action == "assign":
                    task_id = body.get("task_id", "")
                    tier_val = body.get("tier", 2)
                    tier = Tier(tier_val)
                    task = sm.assign_task(task_id, tier)
                    _json(self, {"ok": True, "task": task.to_dict()})
                elif action == "submit_artifact":
                    task_id = body.get("task_id", "")
                    art_type = body.get("artifact_type", "implementation_plan")
                    content = body.get("content", {})
                    file_path = body.get("file_path", "")
                    artifact = sm.submit_artifact(task_id, art_type, content, file_path)
                    _json(self, {"ok": True, "artifact": artifact.to_dict()})
                elif action == "evaluate":
                    task_id = body.get("task_id", "")
                    result = sm.evaluate_task(task_id)
                    _json(self, result)
                elif action == "all_tasks":
                    _json(self, {"ok": True, "tasks": sm.get_all_tasks()})
                else:
                    self.send_error(400, f"Unknown action: {action}")
            except FileNotFoundError:
                self.send_error(404, f"Project '{project_name}' not found")
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # ── PROJECT ARTIFACTS (GET/POST) ──
        if p.path == "/api/projects/artifacts":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length)) if length > 0 else {}
            except Exception:
                self.send_error(400, "Invalid JSON")
                return
            project_name = body.get("project_name", "").strip()
            action = body.get("action", "list")
            if not project_name:
                self.send_error(400, "Missing project_name")
                return
            try:
                sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
                from antigravity_workspace import AntigravityWorkspace
                workspace = AntigravityWorkspace(project_name)

                if action == "list":
                    artifacts = workspace.list_artifacts()
                    _json(self, {"ok": True, "artifacts": artifacts})
                elif action == "read":
                    artifact_name = body.get("artifact_name", "")
                    if not artifact_name:
                        self.send_error(400, "Missing artifact_name")
                        return
                    result = workspace.read_artifact(artifact_name)
                    _json(self, result)
                elif action == "evaluate":
                    task_id = body.get("task_id")
                    result = workspace.evaluate_completion(task_id)
                    _json(self, {"ok": True, **result})
                else:
                    self.send_error(400, f"Unknown action: {action}")
            except FileNotFoundError:
                self.send_error(404, f"Project '{project_name}' not found")
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # ── AGENT CHANNELS (POST) ──
        if p.path == "/api/agents/route":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length)) if length > 0 else {}
            except Exception:
                self.send_error(400, "Invalid JSON")
                return
            task = body.get("task", "").strip()
            if not task:
                self.send_error(400, "Missing task")
                return
            try:
                reg = _get_agent_registry()
                agent_key, confidence, triggers = reg.find_agent_for_task(task)
                agent = reg.get_agent(agent_key)
                _json(self, {
                    "ok": True,
                    "routed_to": agent_key,
                    "agent_name": agent["name"] if agent else agent_key,
                    "confidence": confidence,
                    "matched_triggers": triggers,
                    "task": task,
                })
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # ── VAULT BACKUP (POST) ──
        if p.path == "/api/backup/run":
            try:
                sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
                from vault_backup import do_backup
                length = int(self.headers.get("Content-Length", 0))
                body = {}
                if length > 0:
                    try:
                        body = json.loads(self.rfile.read(length))
                    except Exception:
                        pass
                force = body.get("force", False)
                result = do_backup(force=force)
            except Exception as e:
                result = {"ok": False, "error": str(e)}
            _json(self, result)
            return

        # ── ORCHESTRATOR (POST) ──
        if p.path == "/api/orchestrate/run":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length)) if length > 0 else {}
            except Exception:
                self.send_error(400, "Invalid JSON")
                return
            task = body.get("task", "").strip()
            if not task:
                self.send_error(400, "Missing task")
                return
            async_mode = body.get("async", True)
            preferred = body.get("agents", None)
            if preferred and isinstance(preferred, str):
                preferred = [a.strip() for a in preferred.split(",")]
            parallel = body.get("parallel", True)
            timeout = body.get("timeout", 900)

            if async_mode:
                # Spawn orchestrator in background, return run_id immediately
                import uuid as _uuid
                run_id = f"orch-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{_uuid.uuid4().hex[:6]}"
                log_file = AGENT_OS_ROOT / "config" / "orchestration-runs" / f"{run_id}.log"
                log_file.parent.mkdir(parents=True, exist_ok=True)
                cmd = [
                    sys.executable, str(AGENT_OS_ROOT / "scripts" / "agent_orchestrator.py"),
                    "run", task,
                    "--run-id", run_id,
                    "--timeout", str(timeout),
                ]
                if parallel:
                    pass  # default is parallel
                else:
                    cmd.append("--sequential")
                if preferred:
                    cmd += ["--agents", ",".join(preferred)]
                try:
                    proc = subprocess.Popen(
                        cmd,
                        stdout=open(log_file, "w"),
                        stderr=subprocess.STDOUT,
                        cwd=str(AGENT_OS_ROOT),
                    )
                    _json(self, {"ok": True, "run_id": run_id, "status": "running",
                                 "message": "Orchestration started. Poll /api/orchestrate/runs for results."})
                    # Live-activity: mark orchestration as running.
                    try:
                        if _log_activity is not None:
                            _log_activity(
                                "Orchestrating: " + task[:120],
                                kind="orchestration", agent="orchestrator",
                                task_id=run_id, stage="running", status="running",
                            )
                    except Exception:
                        pass
                except Exception as e:
                    _json(self, {"ok": False, "error": str(e)})
            else:
                # Synchronous mode (blocks until complete)
                try:
                    sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
                    from agent_orchestrator import run_orchestration
                    result = run_orchestration(
                        task=task,
                        preferred_agents=preferred,
                        parallel=parallel,
                        timeout=timeout,
                    )
                except Exception as e:
                    result = {"ok": False, "error": str(e)}
                _json(self, result)
            return

        # ── PM ORCHESTRATOR (POST) ──
        if p.path == "/api/pm/pipelines":
            try:
                sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
                from pm_orchestrator import PMOrchestrator
                pm = PMOrchestrator()
                _json(self, {"ok": True, "pipelines": pm.list_pipelines()})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        if p.path == "/api/pm/run":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length)) if length > 0 else {}
            except Exception:
                self.send_error(400, "Invalid JSON")
                return
            pipeline_id = body.get("pipeline", "").strip()
            params = body.get("params", {})
            async_mode = body.get("async", True)
            if not pipeline_id:
                self.send_error(400, "Missing pipeline")
                return
            if async_mode:
                import uuid as _uuid
                run_id = f"pm-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{_uuid.uuid4().hex[:6]}"
                log_file = AGENT_OS_ROOT / "config" / "pm-runs" / f"{run_id}.log"
                log_file.parent.mkdir(parents=True, exist_ok=True)
                cmd = [
                    sys.executable, str(AGENT_OS_ROOT / "scripts" / "pm_orchestrator.py"),
                    "run", pipeline_id, "--params", json.dumps(params),
                ]
                try:
                    subprocess.Popen(
                        cmd, stdout=open(log_file, "w"), stderr=subprocess.STDOUT,
                        cwd=str(AGENT_OS_ROOT),
                    )
                    _json(self, {"ok": True, "run_id": run_id, "status": "running",
                                 "message": "Pipeline started. Poll /api/pm/runs for results."})
                except Exception as e:
                    _json(self, {"ok": False, "error": str(e)})
            else:
                try:
                    sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
                    from pm_orchestrator import PMOrchestrator
                    pm = PMOrchestrator()
                    result = pm.run_pipeline(pipeline_id, params)
                    _json(self, result)
                except Exception as e:
                    _json(self, {"ok": False, "error": str(e)})
            return

        if p.path == "/api/pm/runs":
            try:
                sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
                from pm_orchestrator import PMOrchestrator
                pm = PMOrchestrator()
                limit = _clamp(parse_qs(p.query).get("limit", ["20"])[0], 20, 100)
                _json(self, {"ok": True, "runs": pm.list_runs(limit=limit)})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        if p.path.startswith("/api/pm/run/"):
            run_id = p.path[len("/api/pm/run/"):].split("?", 1)[0]
            try:
                sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
                from pm_orchestrator import PMOrchestrator
                pm = PMOrchestrator()
                run = pm.get_run(run_id)
            except Exception as e:
                run = None
            if not run:
                self.send_error(404, f"Run '{run_id}' not found")
                return
            _json(self, run)
            return

        if p.path == "/api/activity/log":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length)) if length > 0 else {}
            except Exception:
                self.send_error(400, "Invalid JSON")
                return
            agent = body.get("agent", "").strip()
            action = body.get("action", "").strip()
            if not agent or not action:
                self.send_error(400, "Missing agent or action")
                return
            try:
                v = vault()
                v.log_activity(
                    agent=agent,
                    action=action,
                    task=body.get("task", ""),
                    status=body.get("status", "completed"),
                    duration_ms=body.get("duration_ms", 0),
                    model=body.get("model", ""),
                    details=body.get("details", ""),
                )
                _json(self, {"ok": True})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        if p.path == "/api/activity/output":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length)) if length > 0 else {}
            except Exception:
                self.send_error(400, "Invalid JSON")
                return
            agent = body.get("agent", "").strip()
            if not agent:
                self.send_error(400, "Missing agent")
                return
            try:
                v = vault()
                v.log_output(
                    agent=agent,
                    task_id=body.get("task_id", ""),
                    output_path=body.get("output_path", ""),
                    output_type=body.get("output_type", "text"),
                    quality_score=body.get("quality_score", 0),
                )
                _json(self, {"ok": True})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # ── WORKFLOW ENGINE (POST) ──
        if p.path == "/api/workflows/save":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length)) if length > 0 else {}
            except Exception:
                self.send_error(400, "Invalid JSON")
                return
            wf = body.get("workflow")
            if not wf:
                self.send_error(400, "Missing workflow data")
                return
            wf_id = wf.get("id") or f"wf-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            wf["id"] = wf_id
            wf["updated_at"] = datetime.now().isoformat()
            WORKFLOWS_DIR = AGENT_OS_ROOT / "config" / "workflows"
            WORKFLOWS_DIR.mkdir(parents=True, exist_ok=True)
            wf_file = WORKFLOWS_DIR / f"{wf_id}.json"
            wf_file.write_text(json.dumps(wf, indent=2))
            _json(self, {"ok": True, "id": wf_id, "message": f"Workflow '{wf.get('name', wf_id)}' saved."})
            return

        if p.path == "/api/workflows/delete":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length)) if length > 0 else {}
            except Exception:
                self.send_error(400, "Invalid JSON")
                return
            wf_id = body.get("id", "").strip()
            if not wf_id:
                self.send_error(400, "Missing workflow id")
                return
            WORKFLOWS_DIR = AGENT_OS_ROOT / "config" / "workflows"
            wf_file = WORKFLOWS_DIR / f"{wf_id}.json"
            if wf_file.exists():
                wf_file.unlink()
                _json(self, {"ok": True, "message": f"Workflow '{wf_id}' deleted."})
            else:
                self.send_error(404, f"Workflow '{wf_id}' not found")
            return

        if p.path == "/api/workflows/validate":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length)) if length > 0 else {}
            except Exception:
                self.send_error(400, "Invalid JSON")
                return
            wf = body.get("workflow")
            if not wf:
                self.send_error(400, "Missing workflow data")
                return
            try:
                sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
                from workflow_engine import validate_workflow
                errors = validate_workflow(wf)
                _json(self, {"valid": len(errors) == 0, "errors": errors})
            except Exception as e:
                _json(self, {"valid": False, "errors": [str(e)]})
            return

        if p.path == "/api/workflows/run":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length)) if length > 0 else {}
            except Exception:
                self.send_error(400, "Invalid JSON")
                return
            wf_id = body.get("id", "").strip()
            async_mode = body.get("async", True)
            if not wf_id:
                self.send_error(400, "Missing workflow id")
                return
            WORKFLOWS_DIR = AGENT_OS_ROOT / "config" / "workflows"
            wf_file = WORKFLOWS_DIR / f"{wf_id}.json"
            if not wf_file.exists():
                found = None
                for f in WORKFLOWS_DIR.glob("*.json"):
                    try:
                        w = json.loads(f.read_text())
                        if w.get("id") == wf_id:
                            found = f
                            break
                    except Exception:
                        pass
                if found:
                    wf_file = found
                else:
                    self.send_error(404, f"Workflow '{wf_id}' not found")
                    return
            if async_mode:
                import uuid as _uuid
                run_id = f"wfrun-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{_uuid.uuid4().hex[:6]}"
                RUNS_DIR = AGENT_OS_ROOT / "config" / "workflow-runs"
                RUNS_DIR.mkdir(parents=True, exist_ok=True)
                log_file = RUNS_DIR / f"{run_id}.log"
                cmd = [
                    sys.executable, str(AGENT_OS_ROOT / "scripts" / "workflow_engine.py"),
                    "run", str(wf_file),
                ]
                try:
                    subprocess.Popen(
                        cmd, stdout=open(log_file, "w"), stderr=subprocess.STDOUT,
                        cwd=str(AGENT_OS_ROOT),
                    )
                    _json(self, {"ok": True, "run_id": run_id, "status": "running",
                                 "message": "Workflow started. Poll /api/workflow-runs for results."})
                except Exception as e:
                    _json(self, {"ok": False, "error": str(e)})
            else:
                try:
                    sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
                    from workflow_engine import WorkflowRunner
                    workflow = json.loads(wf_file.read_text())
                    runner = WorkflowRunner(workflow)
                    record = runner.execute()
                    RUNS_DIR = AGENT_OS_ROOT / "config" / "workflow-runs"
                    RUNS_DIR.mkdir(parents=True, exist_ok=True)
                    run_file = RUNS_DIR / f"{record['run_id']}.json"
                    run_file.write_text(json.dumps(record, indent=2, default=str))
                    _json(self, record)
                except Exception as e:
                    _json(self, {"ok": False, "error": str(e)})
            return

        # ── REPORTS ──
        if p.path == "/api/reports/generate":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length)) if length != 0 else {}
            except Exception:
                self.send_error(400, "Invalid JSON")
                return
            report_type = body.get("type", "task")
            title = body.get("title", "Report")
            content = body.get("content", "")
            task_id = body.get("task_id", "")
            goal_id = body.get("goal_id", "")
            status_val = body.get("status", "completed")
            agent = body.get("agent", "")
            duration_ms = body.get("duration_ms", 0)
            html_file = _generate_report_html(
                report_type=report_type, title=title, content=content,
                task_id=task_id, goal_id=goal_id, status_val=status_val,
                agent=agent, duration_ms=duration_ms,
                output_paths=body.get("output_paths", []),
                stage_results=body.get("stage_results", []),
            )
            _json(self, {"ok": True, "report_path": html_file})
            return

        # ── AUTH ENDPOINTS ──
        if p.path == "/api/auth/login":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length)) if length > 0 else {}
            except Exception:
                self.send_error(400, "Invalid JSON")
                return
            username = body.get("username", "").strip()
            password = body.get("password", "")
            if not username or not password:
                self._send_json(400, {"error": "missing_credentials",
                                       "message": "username and password required"})
                return
            try:
                result = _auth.login(username, password)
                self._send_json(200, {"ok": True, **result})
            except ValueError as e:
                self._send_json(401, {"error": "auth_failed", "message": str(e)})
            return

        if p.path == "/api/auth/register":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length)) if length > 0 else {}
            except Exception:
                self.send_error(400, "Invalid JSON")
                return
            username = body.get("username", "").strip()
            password = body.get("password", "")
            display_name = body.get("display_name", "").strip()
            if not username or not password:
                self._send_json(400, {"error": "missing_fields",
                                       "message": "username and password required"})
                return
            try:
                user = _auth.create_user(username, password,
                                          role="admin" if not _auth._users else "user",
                                          display_name=display_name)
                result = _auth.login(username, password)
                self._send_json(201, {"ok": True, "user": user, **result})
            except ValueError as e:
                self._send_json(400, {"error": "registration_failed", "message": str(e)})
            return

        if p.path == "/api/auth/logout":
            token = self._get_access_token()
            if token:
                _auth.logout(token)
            self._send_json(200, {"ok": True, "message": "Logged out"})
            return

        if p.path == "/api/auth/refresh":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length)) if length > 0 else {}
            except Exception:
                self.send_error(400, "Invalid JSON")
                return
            refresh_token = body.get("refresh_token", "").strip()
            if not refresh_token:
                self._send_json(400, {"error": "missing_token",
                                       "message": "refresh_token required"})
                return
            try:
                result = _auth.refresh(refresh_token)
                self._send_json(200, {"ok": True, **result})
            except ValueError as e:
                self._send_json(401, {"error": "refresh_failed", "message": str(e)})
            return

        if p.path == "/api/auth/me":
            try:
                payload = self._require_auth()
                self._send_json(200, {"ok": True, "user": payload})
            except ValueError:
                pass  # _require_auth already sent 401
            return

        # ── OPENAI OAUTH (Sign in with ChatGPT via openai-oauth bridge) ──
        # Public status endpoint so the dashboard can show connection state
        # before the user logs in to MoiraiCore.
        if p.path == "/api/auth/openai/status":
            if not _HAS_OPENAI_OAUTH:
                self._send_json(200, {"ok": False, "available": False,
                                      "message": "openai_oauth module not available"})
                return
            status = bridge_status()
            status["available"] = True
            self._send_json(200, {"ok": True, **status})
            return

        if p.path == "/api/auth/openai/connect":
            if not _HAS_OPENAI_OAUTH:
                self._send_json(503, {"error": "openai_oauth_unavailable"})
                return
            try:
                payload = self._require_auth()
            except ValueError:
                return  # 401 already sent
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length)) if length > 0 else {}
            except Exception:
                self.send_error(400, "Invalid JSON")
                return
            try:
                status = connect(
                    account_email=body.get("account_email"),
                    port=body.get("port"),
                    model=body.get("model"),
                    autostart=body.get("autostart", True),
                )
                self._send_json(200, {"ok": True, **status})
            except Exception as e:
                self._send_json(500, {"error": "connect_failed", "message": str(e)})
            return

        if p.path == "/api/auth/openai/disconnect":
            if not _HAS_OPENAI_OAUTH:
                _json(self, {"ok": False, "error": "openai_oauth_unavailable"})
                return
            try:
                self._require_auth()
            except ValueError:
                return
            try:
                result = disconnect()
                _json(self, {"ok": True, **result})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # ── SETUP (first-run system configuration, auth) ──
        if p.path == "/api/setup/complete":
            try:
                self._require_auth()
            except ValueError:
                return
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length)) if length > 0 else {}
            except Exception:
                self.send_error(400, "Invalid JSON")
                return
            if not _HAS_SETUP:
                _json(self, {"ok": False, "error": "setup_unavailable"})
                return
            try:
                saved = _setup_mod.complete(body if isinstance(body, dict) else None)
                _json(self, {"ok": True, "onboarded": True,
                             "organization_name": saved.get("organization_name")})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        if p.path == "/api/setup/skip":
            try:
                self._require_auth()
            except ValueError:
                return
            if not _HAS_SETUP:
                _json(self, {"ok": False, "error": "setup_unavailable"})
                return
            try:
                saved = _setup_mod.skip()
                _json(self, {"ok": True, "onboarded": bool(saved.get("onboarded"))})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        # Configure Google OAuth (client_id). On a fresh instance (no users yet)
        # this is allowed without auth so "Sign in with Google" can bootstrap the
        # first admin; once accounts exist it requires an authenticated admin.
        if p.path == "/api/setup/google":
            is_fresh = (not _HAS_SETUP) or (not _setup_mod.has_users())
            if not is_fresh:
                try:
                    self._require_auth()
                except ValueError:
                    return
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length)) if length > 0 else {}
            except Exception:
                self.send_error(400, "Invalid JSON")
                return
            cid = (body.get("client_id") or "").strip()
            enabled = bool(body.get("enabled", True))
            if not _HAS_GOOGLE_OAUTH:
                _json(self, {"ok": False, "error": "google_oauth_unavailable"})
                return
            try:
                st = _google_mod.set_config(cid, enabled)
                # Mirror into the system manifest too.
                if _HAS_SETUP:
                    cfg = _setup_mod.load_config()
                    cfg["google_oauth"] = {"enabled": enabled, "client_id": cid}
                    _setup_mod.save_config(cfg)
                _json(self, {"ok": True, **st})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        if p.path == "/api/auth/google/disconnect":
            try:
                self._require_auth()
            except ValueError:
                return
            if not _HAS_GOOGLE_OAUTH:
                _json(self, {"ok": False, "error": "google_oauth_unavailable"})
                return
            try:
                st = _google_mod.clear()
                if _HAS_SETUP:
                    cfg = _setup_mod.load_config()
                    cfg["google_oauth"] = {"enabled": False, "client_id": ""}
                    _setup_mod.save_config(cfg)
                _json(self, {"ok": True, **st})
            except Exception as e:
                _json(self, {"ok": False, "error": str(e)})
            return

        self.send_error(405)

    def do_DELETE(self):
        """Agent registry deletion (and any future DELETE endpoints)."""
        self.do_POST()

    def do_PUT(self):
        """Webhook update and any future PUT endpoints."""
        self.do_POST()

    def log_message(self, fmt, *args):
        pass  # quiet

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

install_auth(Handler)

class ReusableHTTPServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True
    protocol_version = "HTTP/1.1"
if __name__ == "__main__":
    os.chdir(AGENT_OS_ROOT)
    print("\n🧠 MoiraiCore Server v2")
    print("─" * 35)
    print("  Dashboard:  http://localhost:%d" % PORT)
    print("  API:        http://localhost:%d/api/" % PORT)
    print("  Workspace:  %s" % WORKSPACE)
    print("  Memory:     %s" % VAULT)

    # ── Auto-resume goals from checkpoints ──
    try:
        from loop_checkpoint import get_resumable_goals, resume_goal as _resume_from_cp
        resumable = get_resumable_goals()
        if resumable:
            print(f"\n  🔄 Found {len(resumable)} resumable goal(s):")
            for rg in resumable:
                print(f"     - {rg['goal_id']}: {rg['status']} (iter {rg.get('loop_iterations', 0)})")
                try:
                    result = _resume_from_cp(rg["goal_id"])
                    if result.get("ok"):
                        print(f"       ✅ Restored {result.get('restored_tasks', 0)} task(s) from checkpoint")
                    else:
                        print(f"       ⚠️ Resume failed: {result.get('error', 'unknown')}")
                except Exception as e:
                    print(f"       ⚠️ Resume error: {e}")
            print()
    except ImportError:
        pass  # Checkpoint module not available
    except Exception as e:
        print(f"\n  ⚠️ Auto-resume check failed: {e}")

    print("\n  Press Ctrl+C to stop.\n")
    try:
        ReusableHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Server stopped.")
