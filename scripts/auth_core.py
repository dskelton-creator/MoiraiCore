"""
MoiraiCore -- Core Auth Utilities
JWT secret management, JSON store helpers, password hashing.
"""

import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import bcrypt
import jwt as pyjwt

AGENT_OS_ROOT = Path(os.environ.get("AGENT_OS_ROOT", str(Path(__file__).resolve().parents[1])))
AUTH_DIR = AGENT_OS_ROOT / "config" / "auth"
USERS_FILE = AUTH_DIR / "users.json"
SESSIONS_FILE = AUTH_DIR / "sessions.json"
CLIENTS_FILE = AUTH_DIR / "oauth_clients.json"
AUTH_LOG = AUTH_DIR / "auth.log"
JWT_SECRET_FILE = AUTH_DIR / ".jwt_secret"

JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_TTL = 3600       # 1 hour
REFRESH_TOKEN_TTL = 86400 * 7 # 7 days
AUTH_CODE_TTL = 600            # 10 min
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_SECONDS = 900         # 15 min


def ensure_dirs():
    AUTH_DIR.mkdir(parents=True, exist_ok=True)


def jwt_secret() -> str:
    if JWT_SECRET_FILE.exists():
        return JWT_SECRET_FILE.read_text().strip()
    s = secrets.token_hex(32)
    JWT_SECRET_FILE.write_text(s)
    os.chmod(JWT_SECRET_FILE, 0o600)
    return s


def load_json(path: Path, default: Any = None) -> Any:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return default if default is not None else {}


def save_json(path: Path, data: Any):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    os.replace(tmp, path)


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt(rounds=12)).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


def make_jwt(payload: dict, ttl: int = ACCESS_TOKEN_TTL) -> str:
    p = dict(payload)
    p["iat"] = int(time.time())
    p["exp"] = p["iat"] + ttl
    p["jti"] = secrets.token_hex(16)
    return pyjwt.encode(p, jwt_secret(), algorithm=JWT_ALGORITHM)


def decode_jwt(token: str) -> dict:
    try:
        return pyjwt.decode(token, jwt_secret(), algorithms=[JWT_ALGORITHM])
    except pyjwt.ExpiredSignatureError:
        return {"error": "token_expired"}
    except pyjwt.InvalidTokenError:
        return {"error": "token_invalid"}


def log(event: str, detail: str = ""):
    AUTH_LOG.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat()
    with open(AUTH_LOG, "a") as f:
        f.write(f"{ts} {event} {detail}\n")
