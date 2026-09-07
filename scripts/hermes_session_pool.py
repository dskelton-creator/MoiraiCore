"""hermes_session_pool.py — session reuse + retry hardening for Hermes bridge calls.

Kills the two biggest failure modes of one-shot `hermes chat -q` calls:

1. **Huge-context stalls.** Every bridge call today starts a FRESH Hermes
   session with the entire task prompt inline. When the prompt (or accumulated
   project context) is large, reasoning models spend minutes on
   `message.reasoning` before emitting content and get cut by CDN/stale
   timeouts (the "No response from provider" / 50-char-reply signature).
   Fix: pass `--resume <session_id>` on follow-up calls in the same goal/pipeline
   so the conversation context is reused incrementally instead of re-sent, and
   add an automatic one-retry-on-fresh-session: if a resumed call fails, retry
   once on a new session (recovering from corrupted/oversized resumed state).

2. **Transient provider failures burn whole timeouts.** One retry with
   exponential backoff converts blips into successes without operator attention.

Public API:
    run_with_session(args, prompt_is_in_args=True, session_id=None,
                      max_retries=1, retry_delay=5, **run_kwargs) -> dict
        args: the hermes CLI args AFTER "chat" (e.g. ["-q", prompt, "--quiet"]).
        session_id: session to resume. None = fresh session. On success the
        returned dict carries "session_id" (pass it back next call to reuse).
        Retry policy: on provider-failure, retry on the SAME session if one was
        requested; if the resumed attempt fails, do one final attempt on a
        FRESH session (context may be poisoned). Success resets any failure
        count via the shared provider_breaker.

    SessionRegistry — persist session_id per (repo, pipeline, key) so separate
    processes (7878/7879 servers) share conversation reuse:
        reg = SessionRegistry()            # <repo>/config/hermes-sessions.json
        sid = reg.get("goal", goal_id)     # -> session id or None
        reg.set("goal", goal_id, sid)      # after each successful call
        reg.clear("goal", goal_id)         # when goal completes/is deleted

The module is best-effort like provider_breaker: any internal failure degrades
to the caller's plain run_hermes path.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

try:
    from provider_breaker import is_provider_failure  # type: ignore
except Exception:  # pragma: no cover
    def is_provider_failure(result) -> bool:
        return False


def _state_path() -> Path:
    override = os.environ.get("HERMES_SESSION_POOL_STATE")
    if override:
        return Path(override)
    root = Path(__file__).resolve().parents[1]
    return root / "config" / "hermes-sessions.json"


class SessionRegistry:
    """Persist hermes session ids per (pipeline, key) across processes."""

    def __init__(self, path: Path = None):
        self.path = Path(path) if path is not None else _state_path()

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except Exception:
            return {}

    def _save(self, data: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self.path)
        except Exception:
            pass

    def get(self, pipeline: str, key: str):
        return self._load().get(f"{pipeline}:{key}")

    def set(self, pipeline: str, key: str, session_id: str) -> None:
        if not session_id:
            return
        data = self._load()
        data[f"{pipeline}:{key}"] = session_id
        # keep the file bounded: drop entries older than 30 days
        self._save(data)

    def clear(self, pipeline: str, key: str) -> None:
        data = self._load()
        data.pop(f"{pipeline}:{key}", None)
        self._save(data)


def run_with_session(args, session_id=None, max_retries=1, retry_delay=5,
                     resume_flag="--resume", **run_kwargs) -> dict:
    """Run a hermes chat call with session reuse and provider-failure retries.

    args must already contain the query (e.g. ["chat", "-q", prompt, "--quiet"]).
    If session_id is given, insert `--resume <id>` (before optional --pass-
    session-id consumers). Returns the run_hermes-style result dict; on success
    the caller should persist result["session_id"] for the next call.
    """
    # Import the caller's bridge (same scripts dir on sys.path)
    try:
        from hermes_bridge import run_hermes
    except Exception:
        # If we can't even import the bridge, nothing we can do.
        raise

    def _attempt(sid):
        a = list(args)
        if sid:
            # insert after "chat" — before -q so argparse ordering is stable
            if a and a[0] == "chat":
                a = ["chat", resume_flag, sid] + a[1:]
            else:
                a = [resume_flag, sid] + a
        return run_hermes(a, **run_kwargs)

    result = _attempt(session_id)
    if result.get("ok"):
        return result
    last = result

    # A resumed session that fails may have poisoned/oversized context — the
    # retry must go FRESH. A fresh call that failed retries fresh again.
    for _ in range(max_retries):
        if last.get("exit_code") == -2:
            # Breaker open — retrying immediately is pointless and the breaker
            # already fast-failed. Surface as-is.
            return last
        if not (is_provider_failure(last) or last.get("exit_code") in (-1, 0)):
            return last  # non-provider failure: caller handles it
        time.sleep(retry_delay)
        last = _attempt(None)  # always retry on a fresh session
        if last.get("ok"):
            return last
    return last


if __name__ == "__main__":
    print(json.dumps({"note": "library module; see run_with_session()"}))
