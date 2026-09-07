"""
MoiraiCore -- Auth mixin for the HTTP Request Handler.
Import and call install_auth(Handler) to wire auth into the server.
"""

# ── Global AuthManager singleton ──
from auth_manager import AuthManager
_auth = AuthManager()


def install_auth(handler_cls):
    """Install auth methods and middleware onto the Handler class."""

    def _get_access_token(self):
        """Extract Bearer token from Authorization header."""
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:]
        # Also check cookie as fallback
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            part = part.strip()
            if part.startswith("moirai_token="):
                return part[len("moirai_token="):]
        return ""

    def _require_auth(self):
        """Require a valid access token. Returns payload or sends 401."""
        token = self._get_access_token()
        if not token:
            self._send_json(401, {"error": "missing_token",
                                   "message": "Bearer token required"})
            raise ValueError("no token")
        try:
            return _auth.verify_token(token)
        except ValueError as e:
            self._send_json(401, {"error": "invalid_token",
                                   "message": str(e)})
            raise

    def _send_json(self, code, data):
        """Send a JSON response with security headers."""
        import json
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _is_public_path(path):
        """Check if a path is accessible without authentication."""
        public_exact = {
            "/", "/index.html",
            "/favicon.ico",
            "/api/setup/status",  # first-run gate: minimal fields only
        }
        public_prefixes = (
            "/api/auth/",               # login/token endpoints only (no status leaks)
            "/api/health",
            "/dashboard/",  # First-party dashboard assets (style.css, app.js)
            "/vendor/",  # Vendored third-party frontend bundles (same-origin static JS)
        )
        if path in public_exact:
            return True
        if any(path.startswith(p) for p in public_prefixes):
            return True
        # SSE chat stream accepts token via ?token= query param (for EventSource)
        if "/chat/stream" in path:
            return True
        return False

    def _cors_headers(self):
        """Set CORS headers — restrictive, localhost only (uses the live server port)."""
        port = getattr(getattr(self, "server", None), "server_port", 7878)
        origin = f"http://localhost:{port}"
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Max-Age", "600")

    # ── Install methods ──
    handler_cls._get_access_token = _get_access_token
    handler_cls._require_auth = _require_auth
    handler_cls._send_json = _send_json
    handler_cls._is_public_path = staticmethod(_is_public_path)
    handler_cls._cors_headers = _cors_headers