"""
MoiraiCore -- AuthManager
User management, login/logout, token lifecycle, OAuth2 client registry.
"""

import secrets
import time
from pathlib import Path
from typing import Optional

from auth_core import (
    AUTH_DIR, USERS_FILE, SESSIONS_FILE, CLIENTS_FILE,
    ACCESS_TOKEN_TTL, REFRESH_TOKEN_TTL, AUTH_CODE_TTL,
    MAX_LOGIN_ATTEMPTS, LOCKOUT_SECONDS,
    ensure_dirs, load_json, save_json,
    hash_password, verify_password, make_jwt, decode_jwt, log,
)


class AuthManager:
    """Central auth manager for MoiraiCore."""

    def __init__(self):
        ensure_dirs()
        self._users: dict = load_json(USERS_FILE, default={})
        self._sessions: dict = load_json(SESSIONS_FILE, default={})
        self._clients: dict = load_json(CLIENTS_FILE, default={})

    # ------------------------------------------------------------------
    # User CRUD
    # ------------------------------------------------------------------

    def create_user(self, username: str, password: str, role: str = "user",
                    display_name: str = "") -> dict:
        """Register a new user. Returns user dict or raises."""
        username = username.strip().lower()
        if not username or not password:
            raise ValueError("username and password required")
        if username in self._users:
            raise ValueError(f"user '{username}' already exists")
        if len(password) < 8:
            raise ValueError("password must be at least 8 characters")

        user = {
            "id": secrets.token_hex(8),
            "username": username,
            "password_hash": hash_password(password),
            "role": role,
            "display_name": display_name or username,
            "created_at": time.time(),
            "last_login": None,
            "login_attempts": 0,
            "locked_until": 0,
            "active": True,
        }
        self._users[username] = user
        save_json(USERS_FILE, self._users)
        log("user_created", username)
        # Audit log
        try:
            from audit import audit_log
            audit_log("auth.user_created", user=username, status="success")
        except ImportError:
            pass
        return {k: v for k, v in user.items() if k != "password_hash"}

    def get_user(self, username: str) -> Optional[dict]:
        return self._users.get(username.strip().lower())

    # ------------------------------------------------------------------
    # Login / Logout
    # ------------------------------------------------------------------

    def login(self, username: str, password: str) -> dict:
        """
        Authenticate user, return {access_token, refresh_token, user}
        or raise on failure.
        """
        username = username.strip().lower()
        user = self._users.get(username)
        if not user:
            log("login_fail", f"unknown_user:{username}")
            raise ValueError("invalid credentials")

        # Lockout check
        if user["locked_until"] and time.time() < user["locked_until"]:
            remaining = int(user["locked_until"] - time.time())
            log("login_locked", username)
            raise ValueError(f"account locked. try again in {remaining}s")

        if not verify_password(password, user["password_hash"]):
            user["login_attempts"] = user.get("login_attempts", 0) + 1
            if user["login_attempts"] >= MAX_LOGIN_ATTEMPTS:
                user["locked_until"] = time.time() + LOCKOUT_SECONDS
                log("account_locked", username)
            save_json(USERS_FILE, self._users)
            log("login_fail", username)
            # Audit log for failed login
            try:
                from audit import audit_log
                audit_log("auth.login", user=username, status="failure", 
                         details={"reason": "invalid_credentials"})
            except ImportError:
                pass
            raise ValueError("invalid credentials")

        # Success -- reset counters
        user["login_attempts"] = 0
        user["locked_until"] = 0
        user["last_login"] = time.time()
        save_json(USERS_FILE, self._users)

        tokens = self._issue_tokens(user)
        log("login_success", username)
        # Audit log
        try:
            from audit import audit_log
            audit_log("auth.login", user=username, status="success")
        except ImportError:
            pass
        return {
            "access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"],
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_TTL,
            "user": {
                "id": user["id"],
                "username": user["username"],
                "role": user["role"],
                "display_name": user["display_name"],
            },
        }

    def _issue_tokens(self, user: dict) -> dict:
        """Create access + refresh JWT pair."""
        base = {"sub": user["id"], "username": user["username"], "role": user["role"]}
        access_token = make_jwt({**base, "type": "access"}, ttl=ACCESS_TOKEN_TTL)
        refresh_token = make_jwt({**base, "type": "refresh"}, ttl=REFRESH_TOKEN_TTL)
        return {"access_token": access_token, "refresh_token": refresh_token}

    def verify_token(self, token: str) -> dict:
        """Decode + validate a JWT. Returns payload or raises."""
        payload = decode_jwt(token)
        if "error" in payload:
            raise ValueError(payload["error"])
        if payload.get("type") != "access":
            raise ValueError("expected access token")
        return payload

    def refresh(self, refresh_token: str) -> dict:
        """Exchange a refresh token for a new access+refresh pair."""
        payload = decode_jwt(refresh_token)
        if "error" in payload:
            raise ValueError(payload["error"])
        if payload.get("type") != "refresh":
            raise ValueError("expected refresh token")
        user = self._users.get(payload["username"])
        if not user or not user.get("active"):
            raise ValueError("user not found or inactive")
        tokens = self._issue_tokens(user)
        log("token_refresh", user["username"])
        return {
            "access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"],
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_TTL,
        }

    def logout(self, access_token: str):
        """Revoke a session by blacklisting the token jti."""
        try:
            payload = decode_jwt(access_token)
            jti = payload.get("jti")
            username = payload.get("username", "unknown")
            if jti:
                self._sessions[jti] = {"revoked_at": time.time()}
                save_json(SESSIONS_FILE, self._sessions)
            log("logout", username)
            # Audit log
            try:
                from audit import audit_log
                audit_log("auth.logout", user=username, status="success")
            except ImportError:
                pass
        except Exception:
            pass

    # ------------------------------------------------------------------
    # OAuth2 -- Client Registry
    # ------------------------------------------------------------------

    def register_client(self, name: str, redirect_uris: list[str],
                        scopes: list[str] = None) -> dict:
        """Register an OAuth2 client (confidential)."""
        client_id = secrets.token_hex(16)
        client_secret = secrets.token_hex(32)
        client = {
            "client_id": client_id,
            "client_secret": client_secret,
            "name": name,
            "redirect_uris": redirect_uris,
            "scopes": scopes or ["read"],
            "created_at": time.time(),
        }
        self._clients[client_id] = client
        save_json(CLIENTS_FILE, self._clients)
        log("oauth_client_registered", name)
        return {"client_id": client_id, "client_secret": client_secret, **client}

    def get_client(self, client_id: str) -> Optional[dict]:
        return self._clients.get(client_id)

    def create_authorization_code(self, client_id: str, username: str,
                                   redirect_uri: str, scopes: list[str]) -> str:
        """Generate an OAuth2 authorization code."""
        code = secrets.token_hex(32)
        self._sessions[f"authcode:{code}"] = {
            "client_id": client_id,
            "username": username,
            "redirect_uri": redirect_uri,
            "scopes": scopes,
            "expires_at": time.time() + AUTH_CODE_TTL * 60,
        }
        save_json(SESSIONS_FILE, self._sessions)
        log("auth_code_created", f"client={client_id} user={username}")
        return code

    def exchange_authorization_code(self, code: str, client_id: str,
                                     client_secret: str, redirect_uri: str) -> dict:
        """Exchange an authorization code for tokens."""
        session_key = f"authcode:{code}"
        session = self._sessions.get(session_key)
        if not session:
            raise ValueError("invalid authorization code")
        if session.get("expires_at", 0) < time.time():
            del self._sessions[session_key]
            save_json(SESSIONS_FILE, self._sessions)
            raise ValueError("authorization code expired")
        if session["client_id"] != client_id:
            raise ValueError("client_id mismatch")
        if session["redirect_uri"] != redirect_uri:
            raise ValueError("redirect_uri mismatch")

        client = self._clients.get(client_id)
        if not client or client["client_secret"] != client_secret:
            raise ValueError("invalid client credentials")

        # Clean up used code
        del self._sessions[session_key]
        save_json(SESSIONS_FILE, self._sessions)

        user = self._users.get(session["username"])
        if not user:
            raise ValueError("user not found")

        tokens = self._issue_tokens(user)
        log("oauth_token_exchange", f"client={client_id} user={user['username']}")
        return {
            "access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"],
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_TTL,
            "scope": " ".join(session["scopes"]),
        }
