"""provider_breaker.py — shared circuit breaker for Hermes-bridge provider failures.

Both Hagent (~/agent-os/scripts) and MoiraiCore (~/MoiraiCore/scripts) call the
`hermes` CLI for all Tier 1 inference. When the upstream provider (e.g.
provider) dies or a reasoning model stalls, every bridge call still burns its
full timeout (60–900s) before failing. This module fails fast instead:

  - After CONSECUTIVE_FAILURE_THRESHOLD provider-failure results in a row, the
    breaker OPENS for COOLDOWN_SECONDS; run_hermes-style callers should check
    breaker_open() first and return immediately without invoking the CLI.
  - A successful call RESETS the breaker.
  - State is persisted to a small JSON file so separate server processes
    (7878/7879) share the open state. File locking is unnecessary: writes are
    atomic (tmp+rename) and worst case is a redundant fast-fail or one extra
    probe call.

Provider-failure signature (validated in the field, see
hagent skill references/provider-errors-and-tier2-model-swap.md):
  - "No response from provider" / "the model provider failed" (Hermes-internal)
  - empty response / stall ("Hermes returned an empty response")
  - timeout ("Hermes timed out")
  - "Connection error" / "ReadError" / "Broken pipe" / "RemoteProtocolError"

Usage inside run_hermes():
    from provider_breaker import breaker_open, record_success, record_failure, breaker_error
    if breaker_open():
        return {"ok": False, "response": None, "session_id": None,
                "error": breaker_error(), "exit_code": -2, "duration_ms": 0}
    ... run subprocess ...
    if result failed as provider error: record_failure(output)
    else: record_success()

Half-open probing: after the cooldown expires the breaker allows ONE call
through; if it fails the breaker re-opens for the full cooldown again.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

CONSECUTIVE_FAILURE_THRESHOLD = 2
COOLDOWN_SECONDS = int(os.environ.get("PROVIDER_BREAKER_COOLDOWN", "300"))

# Failure signatures, matched case-insensitively against the bridge result's
# response/error text.
FAILURE_PATTERNS = [
    "no response from provider",
    "model provider failed",
    "hermes returned an empty response",
    "hermes timed out",
    "connection error",
    "readerror",
    "broken pipe",
    "remoteprotocolerror",
    "api call failed after",
]

_STATE_FILENAME = "provider_breaker_state.json"


def _state_path() -> Path:
    """State file location. Env override for tests; default per-repo config dir."""
    override = os.environ.get("PROVIDER_BREAKER_STATE")
    if override:
        return Path(override)
    # Resolve caller's repo root: this file lives in <root>/scripts/
    root = Path(__file__).resolve().parents[1]
    return root / "config" / _STATE_FILENAME


def _load() -> dict:
    try:
        return json.loads(_state_path().read_text())
    except Exception:
        return {"consecutive_failures": 0, "opened_at": None, "last_error": None}


def _save(state: dict) -> None:
    p = _state_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(state, f)
        os.replace(tmp, p)
    except Exception:
        pass  # breaker must never take the bridge down


def _matches_failure(text: str) -> bool:
    t = (text or "").lower()
    return any(p in t for p in FAILURE_PATTERNS)


def is_provider_failure(result: dict) -> bool:
    """True if a run_hermes-style result dict looks like a provider failure."""
    if result.get("ok"):
        return False
    return _matches_failure(str(result.get("error", ""))) or _matches_failure(str(result.get("response", "")))


def breaker_open() -> bool:
    """True while the breaker is open (calls should fast-fail)."""
    s = _load()
    opened_at = s.get("opened_at")
    if opened_at is None:
        return False
    if time.time() - opened_at >= COOLDOWN_SECONDS:
        # Cooldown expired: half-open. Allow one probe through; if it fails,
        # record_failure() re-opens. Clear opened_at so we don't fast-fail the probe.
        s["opened_at"] = None
        _save(s)
        return False
    return True


def breaker_error() -> str:
    s = _load()
    remaining = max(0, int(COOLDOWN_SECONDS - (time.time() - (s.get("opened_at") or 0))))
    return (f"Provider circuit breaker OPEN — fast-failing Hermes call "
            f"({remaining}s of cooldown remaining). Last error: {s.get('last_error') or 'unknown'}. "
            f"Set PROVIDER_BREAKER_COOLDOWN=0 to disable, or delete "
            f"{_state_path()} to reset.")


def record_success() -> None:
    s = _load()
    if s.get("consecutive_failures") or s.get("opened_at"):
        _save({"consecutive_failures": 0, "opened_at": None, "last_error": None})


def record_failure(result: dict) -> None:
    """Record a provider failure; opens the breaker at the threshold."""
    s = _load()
    s["consecutive_failures"] = int(s.get("consecutive_failures", 0)) + 1
    s["last_error"] = str(result.get("error") or result.get("response") or "")[:300]
    if s["consecutive_failures"] >= CONSECUTIVE_FAILURE_THRESHOLD:
        s["opened_at"] = time.time()
    _save(s)


def status() -> dict:
    s = _load()
    return {
        "state_path": str(_state_path()),
        "consecutive_failures": s.get("consecutive_failures", 0),
        "open": breaker_open(),
        "cooldown_seconds": COOLDOWN_SECONDS,
        "last_error": s.get("last_error"),
        "threshold": CONSECUTIVE_FAILURE_THRESHOLD,
    }


if __name__ == "__main__":
    import sys
    if "--reset" in sys.argv:
        _save({"consecutive_failures": 0, "opened_at": None, "last_error": None})
        print("breaker reset")
    print(json.dumps(status(), indent=2))
