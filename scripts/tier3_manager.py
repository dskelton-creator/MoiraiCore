"""tier3_manager — Manage Tier 3 (Ollama) model lifecycle.

Provides:
  - get_config(): read current model names from both config sources
  - pull_model(model_name): download a model via the Ollama CLI
  - update_config(model_name): update ollama_worker.py + hermes_bridge.py defaults
  - switch_model(model_name): full pipeline — pull, update, return result
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

AGENT_OS_ROOT = Path(os.environ.get("AGENT_OS_ROOT", str(Path(__file__).resolve().parents[1])))


def _read_file_safe(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception as e:
        return f"# ERROR reading {path}: {e}"


# ── Read current config ──────────────────────────────────────────────────────

def get_config() -> dict:
    """Return the current Tier 3 model configuration from all sources."""
    ollama_py = AGENT_OS_ROOT / "scripts" / "ollama_worker.py"
    hermes_py = AGENT_OS_ROOT / "scripts" / "hermes_bridge.py"
    server_py = AGENT_OS_ROOT / "scripts" / "server.py"

    result = {
        "ok": True,
        "ollama_worker_model": _extract_default_model(ollama_py) or "",
        "hermes_bridge_model": _extract_bridge_fallback(hermes_py) or "",
        "server_route_model": _extract_server_fallback(server_py) or "",
        "ollama_available": False,
        "pulled_models": [],
        "tier1": "MoiraiCore / ScrumMaster (configured Hermes model)",
        "tier2": "Gemini Pro API (antigravity worker)",
        "tier3": "Ollama (local model)",
    }

    # Check Ollama availability
    try:
        sys.path.insert(0, str(AGENT_OS_ROOT / "scripts"))
        from ollama_worker import is_ollama_available, get_ollama_models

        result["ollama_available"] = is_ollama_available()
        if result["ollama_available"]:
            result["pulled_models"] = [
                {"name": m.get("name", ""), "size": m.get("size", 0)}
                for m in get_ollama_models()
            ]
    except Exception:
        pass

    return result


def _extract_default_model(py_path: Path) -> Optional[str]:
    """Parse model_name default from OllamaConfig dataclass."""
    try:
        content = py_path.read_text(encoding="utf-8")
        # Match: model_name: str = "..."   (potentially with quotes that could be single or double)
        m = re.search(
            r'model_name\s*:\s*str\s*=\s*["\']([^"\']+)["\']', content
        )
        if m:
            return m.group(1)
        return None
    except Exception:
        return None


def _extract_bridge_fallback(py_path: Path) -> Optional[str]:
    """Parse the fallback model_name in hermes_bridge.py's OllamaConfig construction."""
    try:
        content = py_path.read_text(encoding="utf-8")
        # Match: model_name=worker_config.get("model_name", "...")
        m = re.search(
            r'model_name=worker_config\.get\(["\']model_name["\'],\s*["\']([^"\']+)["\']',
            content,
        )
        if m:
            return m.group(1)
        return None
    except Exception:
        return None


def _extract_server_fallback(py_path: Path) -> Optional[str]:
    """Parse the fallback model_name in server.py's OllamaConfig construction."""
    try:
        content = py_path.read_text(encoding="utf-8")
        # Match: model_name=worker_config.get("model_name", "...")
        m = re.search(
            r'model_name=worker_config\.get\(["\']model_name["\'],\s*["\']([^"\']+)["\']',
            content,
        )
        if m:
            return m.group(1)
        return None
    except Exception:
        return None


# ── Pull model via Ollama CLI ────────────────────────────────────────────────

def pull_model(model_name: str, timeout: int = 600) -> dict:
    """Pull an Ollama model. Returns dict with ok, output, error."""
    try:
        # Sanitize model name
        model_name = model_name.strip()
        if not model_name:
            return {"ok": False, "error": "Model name is empty"}

        proc = subprocess.run(
            ["ollama", "pull", model_name],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode == 0:
            return {
                "ok": True,
                "output": proc.stdout or proc.stderr or "Model pulled successfully",
            }
        else:
            # Check for partial files
            partial_blobs = list(
                Path.home().glob(".ollama/models/blobs/sha256-*-partial*")
            )
            cleanup_hint = ""
            if partial_blobs:
                cleanup_hint = (
                    " Try cleaning partial blobs: "
                    "rm -f ~/.ollama/models/blobs/sha256-*-partial*"
                )
            return {
                "ok": False,
                "error": f"ollama pull failed (exit {proc.returncode}): "
                f"{proc.stderr or proc.stdout}{cleanup_hint}",
            }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": f"ollama pull timed out after {timeout}s. "
            f"The model may be very large; try pulling manually: ollama pull {model_name}",
        }
    except FileNotFoundError:
        return {
            "ok": False,
            "error": "Ollama CLI not found. Is Ollama installed? "
            "Install from https://ollama.com",
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Update config files ──────────────────────────────────────────────────────

def update_config(model_name: str) -> dict:
    """Update ollama_worker.py and hermes_bridge.py with the new model name.

    Returns dict with ok, updated_files list, or error.
    """
    model_name = model_name.strip()
    if not model_name:
        return {"ok": False, "error": "Model name is empty"}

    updated = []
    errors = []

    # 1. Update ollama_worker.py
    ow_path = AGENT_OS_ROOT / "scripts" / "ollama_worker.py"
    try:
        content = ow_path.read_text(encoding="utf-8")
        old_val = _extract_default_model(ow_path)
        if old_val:
            # Replace the old model_name default string
            new_content = content.replace(
                f'model_name: str = "{old_val}"',
                f'model_name: str = "{model_name}"',
            )
            if new_content != content:
                ow_path.write_text(new_content, encoding="utf-8")
                updated.append("scripts/ollama_worker.py")
            else:
                errors.append(
                    f"ollama_worker.py: could not replace '{old_val}'"
                )
        else:
            errors.append("ollama_worker.py: could not find existing model_name")
    except Exception as e:
        errors.append(f"ollama_worker.py: {e}")

    # 2. Update hermes_bridge.py
    hb_path = AGENT_OS_ROOT / "scripts" / "hermes_bridge.py"
    try:
        content = hb_path.read_text(encoding="utf-8")
        old_val = _extract_bridge_fallback(hb_path)
        if old_val:
            new_content = content.replace(
                f'model_name=worker_config.get("model_name", "{old_val}")',
                f'model_name=worker_config.get("model_name", "{model_name}")',
            )
            if new_content != content:
                hb_path.write_text(new_content, encoding="utf-8")
                updated.append("scripts/hermes_bridge.py")
            else:
                errors.append(
                    f"hermes_bridge.py: could not replace '{old_val}'"
                )
        else:
            errors.append("hermes_bridge.py: could not find existing model_name fallback")
    except Exception as e:
        errors.append(f"hermes_bridge.py: {e}")

    # 3. Update server.py (has its own OllamaConfig construction)
    sv_path = AGENT_OS_ROOT / "scripts" / "server.py"
    try:
        content = sv_path.read_text(encoding="utf-8")
        old_val = _extract_server_fallback(sv_path)
        if old_val:
            new_content = content.replace(
                f'model_name=worker_config.get("model_name", "{old_val}")',
                f'model_name=worker_config.get("model_name", "{model_name}")',
            )
            if new_content != content:
                sv_path.write_text(new_content, encoding="utf-8")
                updated.append("scripts/server.py")
            else:
                errors.append(
                    f"server.py: could not replace '{old_val}'"
                )
        else:
            errors.append("server.py: could not find existing model_name fallback")
    except Exception as e:
        errors.append(f"server.py: {e}")

    # Verify syntax of updated files
    for rel_path in updated:
        full = AGENT_OS_ROOT / rel_path
        try:
            ast.parse(full.read_text(encoding="utf-8"))
        except SyntaxError as e:
            errors.append(f"{rel_path} has SYNTAX ERROR after update: {e}")
            # Revert
            # (we could revert but the replace was atomic; if this fires, the
            # file is broken and needs manual fix)
            pass

    return {
        "ok": len(errors) == 0,
        "updated_files": updated,
        "errors": errors,
    }


# ── Full switch pipeline ─────────────────────────────────────────────────────

def switch_model(model_name: str, timeout: int = 600) -> dict:
    """Full pipeline: pull the model, then update all config files.

    Returns a dict with:
      - ok: bool
      - steps: list of step results
      - current_config: the config after the switch
    """
    steps = []

    # Step 1: Pull
    steps.append({"step": "pull", "model": model_name, **pull_model(model_name)})

    # Step 2: Update config files (even if pull had issues — the model
    # might already be pulled locally)
    update_result = update_config(model_name)
    steps.append({"step": "update_config", **update_result})

    overall_ok = any(
        s.get("ok") for s in steps
    )

    # Step 3: Get fresh config
    fresh = get_config()

    return {
        "ok": overall_ok,
        "model_name": model_name,
        "steps": steps,
        "current_config": fresh,
    }