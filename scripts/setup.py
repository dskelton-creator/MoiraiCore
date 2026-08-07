"""
MoiraiCore -- First-run System Configuration.

Manages the on-disk system configuration (config/system.json) that the initial
onboarding / setup wizard writes, and answers the "does this instance still
need to be set up?" question used to gate the first-run wizard.

The file is deliberately a *coarse* system manifest (instance identity, default
LLM routing, workspace root, OAuth enablement) — not a replacement for the
per-feature stores (config/auth, openai_oauth.json, tier3_manager, etc.).
Everything reads/writes atomically and degrades to sensible defaults so an
unconfigured instance never crashes the dashboard.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

AGENT_OS_ROOT = Path(os.environ.get("AGENT_OS_ROOT", str(Path(__file__).resolve().parents[1])))
CONFIG_DIR = AGENT_OS_ROOT / "config"
SYSTEM_FILE = CONFIG_DIR / "system.json"

# Defaults mirror the platform defaults in README / server.py so the wizard can
# pre-fill instead of guessing.
DEFAULTS: dict[str, Any] = {
    "onboarded": False,          # set True once the setup wizard is completed/skipped
    "onboarded_at": None,
    "organization_name": "MoiraiCore",
    "owner_email": "",           # optional; set by Google OAuth or manual entry
    "workspace": "workspace",    # relative to AGENT_OS_ROOT unless absolute
    "model": {
        "tier1": "director",                    # default Tier 1 / general role (informational)
        "tier2_provider": "openai",               # openai | gemini
        "tier2_model": "deepseek/deepseek-v4-flash-0731",
        "tier2_base_url": "https://openrouter.ai/api/v1",
        "tier3_model": "qwen2.5-coder:14b",       # Ollama
    },
    "google_oauth": {
        "enabled": False,
        "client_id": "",          # public OAuth client identifier (safe to store)
    },
    "security": {
        "lockout": True,
    },
}


def _ensure_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    """Load the persisted system manifest, deep-merged over defaults."""
    if SYSTEM_FILE.exists():
        try:
            raw = json.loads(SYSTEM_FILE.read_text())
            return _deep_merge(_deep_copy(DEFAULTS), raw if isinstance(raw, dict) else {})
        except Exception:
            pass
    return _deep_copy(DEFAULTS)


def save_config(cfg: dict) -> dict:
    """Persist the system manifest atomically. Returns the stored config."""
    _ensure_dirs()
    merged = _deep_merge(_deep_copy(DEFAULTS), cfg)
    tmp = SYSTEM_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(merged, indent=2, default=str))
    tmp.replace(SYSTEM_FILE)
    return merged


def complete(cfg: Optional[dict] = None) -> dict:
    """Mark the instance as configured and store the wizard's system settings."""
    merged = load_config()
    if cfg:
        # Merge only the wizard-supplied sub-trees over what's already stored.
        merged = _deep_merge(merged, cfg)
    merged["onboarded"] = True
    merged["onboarded_at"] = time.time()
    return save_config(merged)


def skip() -> dict:
    """Mark the wizard as done without writing any system settings."""
    merged = load_config()
    merged["onboarded"] = True
    merged["onboarded_at"] = time.time()
    return save_config(merged)


def has_users(users_file: Optional[Path] = None) -> bool:
    """True if at least one local account exists (i.e. onboarding produced an admin)."""
    users_file = users_file or (CONFIG_DIR / "auth" / "users.json")
    try:
        if users_file.exists():
            data = json.loads(users_file.read_text())
            return isinstance(data, dict) and len(data) > 0
    except Exception:
        pass
    return False


def status(users_file: Optional[Path] = None) -> dict:
    """Public status used by the dashboard to decide whether to show the wizard.

    `needs_onboarding` is the single gate the front-end should trust:
      - an instance that has NOT completed the wizard and has no local users
        must be onboarded (full setup wizard);
      - an instance that has users but never completed the wizard is allowed to
        skip straight into the app (their config just wasn't captured).
    """
    cfg = load_config()
    users = has_users(users_file)
    onboarded = bool(cfg.get("onboarded"))
    needs_onboarding = (not onboarded) and not users
    return {
        "onboarded": onboarded,
        "has_users": users,
        "needs_onboarding": needs_onboarding,
        "organization_name": cfg.get("organization_name"),
        "owner_email": cfg.get("owner_email"),
        "google_oauth_configured": bool(cfg.get("google_oauth", {}).get("client_id")),
        "configured": onboarded,
        # Full manifest so the dashboard wizard can pre-fill its fields.
        "system": {
            "organization_name": cfg.get("organization_name"),
            "workspace": cfg.get("workspace"),
            "model": _deep_copy(cfg.get("model", {}) or {}),
            "google_oauth": _deep_copy(cfg.get("google_oauth", {}) or {}),
        },
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _deep_copy(value):
    return json.loads(json.dumps(value))


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base (base wins at the scalar level only
    where override lacks a key; scalar override values replace base values)."""
    out = _deep_copy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def resolve_workspace(cfg: Optional[dict] = None) -> Path:
    """Resolve the configured workspace root to an absolute path."""
    cfg = cfg or load_config()
    ws = cfg.get("workspace", "workspace")
    p = Path(ws)
    if not p.is_absolute():
        p = AGENT_OS_ROOT / p
    return p


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status":
        print(json.dumps(status(), indent=2))
    elif cmd == "complete":
        print(json.dumps(complete(), indent=2))
    elif cmd == "skip":
        print(json.dumps(skip(), indent=2))
    else:
        print(json.dumps({"error": f"unknown command: {cmd}"}))
