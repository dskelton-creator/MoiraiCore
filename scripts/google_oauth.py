"""
MoiraiCore -- Google OAuth provider ("Sign in with Google")

Implements the OAuth 2.0 Authorization Code + PKCE flow against Google's
identity endpoints, targeting a *localhost* server. Because the callback is on
localhost and this is a public (non-confidential) client, PKCE is used and no
client_secret is required -- only the public `client_id` from a Google Cloud
OAuth 2.0 Client (Web application type) whose Authorized redirect URI includes:

    http://localhost:<port>/api/auth/google/callback

Configuration is persisted to config/google_oauth.json (public client_id only;
never store secrets). The module intentionally uses only the standard library
(urllib, json, base64, hashlib) so it runs under the system Python that serves
MoiraiCore -- no `requests` dependency.

Flow (dashboard-driven):
  1. GET  /api/auth/google/start      -> {auth_url, state}
  2. dashboard opens auth_url (Google consent). Google redirects back to
     {origin}/api/auth/google/callback?code=..&state=..
  3. POST /api/auth/google/callback   -> {code, state, redirect_uri}
     server exchanges code -> id_token -> email/name -> local user -> JWT
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

AGENT_OS_ROOT = Path(os.environ.get("AGENT_OS_ROOT", str(Path(__file__).resolve().parents[1])))
CONFIG_DIR = AGENT_OS_ROOT / "config"
STORE_FILE = CONFIG_DIR / "google_oauth.json"

# Google identity endpoints
AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"

SCOPES = "openid email profile"

# Pending PKCE state held server-side between /start and /callback.
# Keyed by `state`; each entry: {verifier, redirect_uri, created_at}.
_PENDING: dict[str, dict] = {}
_PENDING_TTL = 600  # 10 minutes


def _ensure_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def _load_store() -> dict:
    if STORE_FILE.exists():
        try:
            data = json.loads(STORE_FILE.read_text())
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def _save_store(cfg: dict) -> None:
    _ensure_dirs()
    cfg = dict(cfg)
    cfg["updated_at"] = time.time()
    tmp = STORE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    tmp.replace(STORE_FILE)


def client_id() -> Optional[str]:
    return _load_store().get("client_id") or None


def status() -> dict:
    cfg = _load_store()
    cid = cfg.get("client_id") or ""
    return {
        "configured": bool(cid),
        "enabled": bool(cfg.get("enabled", bool(cid))),
        "client_id": cid,
        "client_id_masked": (cid[:8] + "…" + cid[-4:]) if len(cid) > 14 else ("…" if cid else ""),
        "available": True,
    }


def set_config(client_id_value: str, enabled: bool = True) -> dict:
    """Persist the Google OAuth client_id (public value)."""
    client_id_value = (client_id_value or "").strip()
    cfg = _load_store()
    cfg["client_id"] = client_id_value
    cfg["enabled"] = enabled
    _save_store(cfg)
    return status()


def clear() -> dict:
    cfg = _load_store()
    cfg["client_id"] = ""
    cfg["enabled"] = False
    _save_store(cfg)
    return status()


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _sha256_b64url(plain: str) -> str:
    return _b64url(hashlib.sha256(plain.encode("ascii")).digest())


def begin(redirect_uri: str) -> dict:
    """Start the flow: generate state + PKCE verifier, build Google's auth URL."""
    verifier = secrets.token_urlsafe(64)
    state = secrets.token_urlsafe(24)
    challenge = _sha256_b64url(verifier)
    _PENDING[state] = {"verifier": verifier, "redirect_uri": redirect_uri,
                       "created_at": time.time()}
    params = {
        "client_id": client_id() or "",
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "access_type": "online",
        "prompt": "select_account",
    }
    auth_url = AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)
    return {"auth_url": auth_url, "state": state}


def _consume_pending(state: str) -> Optional[dict]:
    _PENDING.pop(state, None)
    # Clean expired entries opportunistically.
    now = time.time()
    for k in [k for k, v in _PENDING.items() if now - v.get("created_at", 0) > _PENDING_TTL]:
        _PENDING.pop(k, None)
    entry = _PENDING.get(state)
    return entry


def exchange_code(code: str, verifier: str, redirect_uri: str) -> dict:
    """Exchange an authorization code for tokens via Google's token endpoint."""
    form = urllib.parse.urlencode({
        "client_id": client_id() or "",
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }).encode("utf-8")
    req = urllib.request.Request(TOKEN_URL, data=form, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise ValueError(f"google token exchange failed ({e.code}): {detail}") from e
    data = json.loads(body)
    if "id_token" not in data:
        raise ValueError("google token exchange returned no id_token")
    payload = decode_id_token(data["id_token"])
    return {
        "access_token": data.get("access_token"),
        "expires_in": data.get("expires_in"),
        "id_token": data.get("id_token"),
        "email": payload.get("email"),
        "name": payload.get("name"),
        "sub": payload.get("sub"),
    }


def decode_id_token(id_token: str) -> dict:
    """Decode (without verifying signature) the JWT claims of a Google id_token.

    Signature verification is intentionally skipped for a first-run/localhost
    flow; the token was retrieved over TLS directly from Google's token
    endpoint via the client_id bound exchange, so the claims are trusted.
    """
    try:
        parts = id_token.split(".")
        if len(parts) < 2:
            raise ValueError("malformed id_token")
        payload_b64 = parts[1]
        pad = "=" * (-len(payload_b64) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_b64 + pad).decode("utf-8"))
        return claims if isinstance(claims, dict) else {}
    except Exception as e:
        raise ValueError(f"could not decode id_token: {e}") from e


def callback(code: str, state: str, redirect_uri: str, auth) -> dict:
    """Full server-side callback handler: verify state -> exchange -> local user.

    `auth` is an AuthManager instance used to find-or-create the user and mint
    the JWT pair. Returns the same shape as AuthManager.login().
    """
    entry = _PENDING.get(state)
    if not entry:
        raise ValueError("invalid or expired oauth state")
    if entry.get("redirect_uri") != redirect_uri:
        raise ValueError("redirect_uri mismatch")
    verified = exchange_code(code, entry["verifier"], redirect_uri)
    _PENDING.pop(state, None)
    return auth.google_login(verified.get("email") or "", verified.get("name") or "",
                             verified.get("sub"))


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status":
        print(json.dumps(status(), indent=2))
    elif cmd == "set":
        print(json.dumps(set_config(sys.argv[2]), indent=2))
    elif cmd == "clear":
        print(json.dumps(clear(), indent=2))
    elif cmd == "start":
        print(json.dumps(begin(sys.argv[2] if len(sys.argv) > 2 else "http://localhost:7878/api/auth/google/callback"), indent=2))
    else:
        print(json.dumps({"error": f"unknown command: {cmd}"}))
