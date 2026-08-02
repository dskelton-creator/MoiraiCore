"""
MoiraiCore -- OpenAI OAuth provider (Sign in with ChatGPT)

Wraps the open-source `openai-oauth` bridge (https://github.com/evanjodav/openai-oauth)
featured in the "OpenAI's $3B Loophole" video. Instead of a paid OpenAI API key,
the user authenticates with their ChatGPT / Codex account and the bridge exposes a
standard OpenAI-compatible HTTP endpoint locally.

This module is the lifecycle + config manager:
  - launches / stops the `npx openai-oauth` bridge process (background)
  - tracks the bridge base URL + status
  - persists OAuth connection state to a local JSON store (config/openai_oauth.json)
  - exposes helpers used by the server routes and the codex agent backend

Security notes:
  - Credentials never leave the local machine; the bridge stores the ChatGPT
    OAuth token locally (per the upstream project's design).
  - We only ever talk to the loopback bridge endpoint.
  - Usage remains subject to OpenAI's Terms of Use (surfaced in the UI).
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

AGENT_OS_ROOT = Path(os.environ.get("AGENT_OS_ROOT", str(Path(__file__).resolve().parents[1])))
CONFIG_DIR = AGENT_OS_ROOT / "config"
STORE_FILE = CONFIG_DIR / "openai_oauth.json"

# Default bridge port for the upstream `openai-oauth` package
# (EvanZhouDev/openai-oauth). The package default is 10531, NOT 8787.
DEFAULT_BRIDGE_PORT = 10531
DEFAULT_BRIDGE_HOST = "127.0.0.1"

# Where the bridge writes its live status / credentials (project-local).
BRIDGE_STATE_DIR = CONFIG_DIR / "openai_oauth_bridge"


# ----------------------------------------------------------------------------
# Config store
# ----------------------------------------------------------------------------

def _ensure_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    BRIDGE_STATE_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    """Load persisted OpenAI OAuth connection config (no secrets in plaintext)."""
    if STORE_FILE.exists():
        try:
            return json.loads(STORE_FILE.read_text())
        except Exception:
            pass
    return _default_config()


def _default_config() -> dict:
    return {
        "connected": False,
        "account_email": None,      # display only, set after a successful bridge ping
        "bridge_port": DEFAULT_BRIDGE_PORT,
        "bridge_host": DEFAULT_BRIDGE_HOST,
        "model": "gpt-4o",          # default model the bridge should proxy
        "autostart": True,          # launch bridge on MoiraiCore boot when connected
        "last_error": None,
        "connected_at": None,
        "updated_at": None,
    }


def save_config(cfg: dict) -> None:
    _ensure_dirs()
    cfg["updated_at"] = time.time()
    tmp = STORE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    tmp.replace(STORE_FILE)


def bridge_base_url(cfg: Optional[dict] = None) -> str:
    cfg = cfg or load_config()
    host = cfg.get("bridge_host", DEFAULT_BRIDGE_HOST)
    port = cfg.get("bridge_port", DEFAULT_BRIDGE_PORT)
    return f"http://{host}:{port}"


# ----------------------------------------------------------------------------
# Bridge lifecycle
# ----------------------------------------------------------------------------

def _npx_path() -> str:
    # Prefer the Hermes-managed node toolchain if present.
    candidate = Path.home() / ".hermes" / "node" / "bin" / "npx"
    if candidate.exists():
        return str(candidate)
    found = shutil.which("npx")
    if found:
        return found
    return "npx"


def _bridge_cmd(port: int) -> list[str]:
    """Construct the bridge launch command.

    `npx openai-oauth` starts a local OpenAI-compatible server. We pin the port
    so MoiraiCore can route agents to it deterministically.
    """
    return [_npx_path(), "openai-oauth", "--port", str(port)]


def bridge_running(port: Optional[int] = None) -> bool:
    """Best-effort check that the bridge port is listening."""
    import socket
    cfg = load_config()
    port = port or cfg.get("bridge_port", DEFAULT_BRIDGE_PORT)
    host = cfg.get("bridge_host", DEFAULT_BRIDGE_HOST)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.0)
    try:
        return s.connect_ex((host, port)) == 0
    except Exception:
        return False
    finally:
        s.close()


def start_bridge(port: Optional[int] = None) -> dict:
    """Launch the openai-oauth bridge in the background.

    Returns a status dict. The bridge process is detached so it survives this
    Python call; we track its pid via the state dir for later cleanup.
    """
    _ensure_dirs()
    cfg = load_config()
    port = port or cfg.get("bridge_port", DEFAULT_BRIDGE_PORT)
    if bridge_running(port):
        return {"ok": True, "running": True, "port": port,
                "message": "bridge already listening"}

    cmd = _bridge_cmd(port)
    pid_file = BRIDGE_STATE_DIR / f"bridge-{port}.pid"
    log_file = BRIDGE_STATE_DIR / f"bridge-{port}.log"

    try:
        log_fh = open(log_file, "a")
        proc = subprocess.Popen(
            cmd,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,  # detach from MoiraiCore's process group
            env={**os.environ},
        )
        pid_file.write_text(str(proc.pid))
        # Give it a moment; poll for the port to come up.
        for _ in range(20):
            time.sleep(0.5)
            if bridge_running(port):
                cfg["last_error"] = None
                save_config(cfg)
                return {"ok": True, "running": True, "port": port,
                        "pid": proc.pid, "message": "bridge started"}
        # Not up yet — but process may still be initialising (npx install).
        return {"ok": True, "running": False, "port": port, "pid": proc.pid,
                "message": "bridge launching (npx may be installing package)"}
    except Exception as e:  # pragma: no cover - defensive
        cfg["last_error"] = str(e)
        save_config(cfg)
        return {"ok": False, "running": False, "port": port, "error": str(e)}


def stop_bridge(port: Optional[int] = None) -> dict:
    """Stop a running bridge by pid tracked in the state dir."""
    cfg = load_config()
    port = port or cfg.get("bridge_port", DEFAULT_BRIDGE_PORT)
    pid_file = BRIDGE_STATE_DIR / f"bridge-{port}.pid"
    killed = False
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 15)  # SIGTERM
            killed = True
        except Exception:
            pass
        try:
            pid_file.unlink()
        except Exception:
            pass
    # Also try the port directly as a fallback.
    if not killed and bridge_running(port):
        try:
            import subprocess as _sp
            _sp.run(["pkill", "-f", f"openai-oauth --port {port}"],
                    capture_output=True, text=True)
            killed = True
        except Exception:
            pass
    return {"ok": True, "stopped": killed, "port": port}


def bridge_status() -> dict:
    cfg = load_config()
    running = bridge_running()
    return {
        "connected": cfg.get("connected", False),
        "running": running,
        "base_url": bridge_base_url(cfg),
        "port": cfg.get("bridge_port", DEFAULT_BRIDGE_PORT),
        "model": cfg.get("model", "gpt-4o"),
        "account_email": cfg.get("account_email"),
        "autostart": cfg.get("autostart", True),
        "last_error": cfg.get("last_error"),
        "connected_at": cfg.get("connected_at"),
    }


# ----------------------------------------------------------------------------
# Connection (the "Sign in with ChatGPT" handshake)
# ----------------------------------------------------------------------------

def connect(account_email: Optional[str] = None, port: Optional[int] = None,
            model: Optional[str] = None, autostart: bool = True) -> dict:
    """Record a connection + ensure the bridge is running.

    The actual OAuth handshake with ChatGPT happens inside the bridge (the user
    authenticates in their browser the first time the bridge starts). Here we
    persist the connection intent and make sure the local endpoint is up.

    GUARD: if a bridge is already listening on the target port (e.g. a
    user-managed desktop instance), we do NOT launch a second process — that
    would fail to bind the port or split state across two instances. We simply
    attach to the existing one.
    """
    _ensure_dirs()
    cfg = load_config()
    if port:
        cfg["bridge_port"] = port
    if model:
        cfg["model"] = model
    cfg["autostart"] = autostart
    cfg["connected"] = True
    cfg["account_email"] = account_email
    cfg["connected_at"] = time.time()
    cfg["last_error"] = None
    save_config(cfg)

    target_port = cfg["bridge_port"]
    already_up = bridge_running(target_port)
    if already_up:
        # A bridge is already serving on this port (likely user-managed).
        # Attach instead of spawning a duplicate.
        launch = {"ok": True, "running": True, "port": target_port,
                  "skipped": True,
                  "message": "bridge already running on this port — attached (no duplicate launched)"}
    else:
        launch = start_bridge(target_port)
    status = bridge_status()
    status["launch"] = launch
    status["bridge_already_running"] = already_up
    return status


def disconnect() -> dict:
    """Tear down the connection: stop bridge + clear connection state."""
    cfg = load_config()
    cfg["connected"] = False
    cfg["account_email"] = None
    save_config(cfg)
    stopped = stop_bridge(cfg.get("bridge_port"))
    return {"ok": True, "disconnected": True, **stopped}


# ----------------------------------------------------------------------------
# Agent backend helper
# ----------------------------------------------------------------------------

def openai_compatible_config() -> Optional[dict]:
    """Return an OpenAI-compatible base_url for agents, or None if not connected."""
    cfg = load_config()
    if not cfg.get("connected") or not bridge_running():
        return None
    return {
        "base_url": bridge_base_url(cfg) + "/v1",
        "api_key": "openai-oauth",   # bridge ignores the key; placeholder
        "model": cfg.get("model", "gpt-4o"),
    }


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status":
        print(json.dumps(bridge_status(), indent=2))
    elif cmd == "start":
        print(json.dumps(start_bridge(), indent=2))
    elif cmd == "stop":
        print(json.dumps(stop_bridge(), indent=2))
    elif cmd == "connect":
        print(json.dumps(connect(), indent=2))
    elif cmd == "disconnect":
        print(json.dumps(disconnect(), indent=2))
    else:
        print(json.dumps({"error": f"unknown command: {cmd}"}))
