"""Unit tests for scripts/google_oauth.py (Sign in with Google, PKCE flow)."""
import base64
import importlib
import json
import os
import tempfile
import unittest
from unittest import mock

import auth_core
import auth_manager
import google_oauth


def _b64(d):
    return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()


def _fake_id_token(claims):
    return _b64({"alg": "RS256"}) + "." + _b64(claims) + "." + _b64({"x": 1})


class GoogleOAuthConfigTest(unittest.TestCase):
    def setUp(self):
        self._root = tempfile.mkdtemp()
        self._prev = os.environ.get("AGENT_OS_ROOT")
        os.environ["AGENT_OS_ROOT"] = self._root
        # Reload so module-level path constants point at the temp root.
        importlib.reload(auth_core)
        importlib.reload(auth_manager)
        importlib.reload(google_oauth)

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("AGENT_OS_ROOT", None)
        else:
            os.environ["AGENT_OS_ROOT"] = self._prev

    def test_status_not_configured(self):
        st = google_oauth.status()
        self.assertFalse(st["configured"])

    def test_set_config_and_mask(self):
        google_oauth.set_config("1234567890-abcdefghij.apps.googleusercontent.com")
        st = google_oauth.status()
        self.assertTrue(st["configured"])
        self.assertTrue(st["enabled"])
        self.assertEqual(st["client_id"], "1234567890-abcdefghij.apps.googleusercontent.com")
        self.assertNotIn("1234567890-", st["client_id_masked"].replace("…", "").replace(".", ""))

    def test_clear(self):
        google_oauth.set_config("abc.apps.googleusercontent.com")
        google_oauth.clear()
        self.assertFalse(google_oauth.status()["configured"])

    def test_begin_builds_pkce_url(self):
        google_oauth.set_config("cid.apps.googleusercontent.com")
        r = google_oauth.begin("http://localhost:7878/api/auth/google/callback")
        self.assertIn("state=" + r["state"], r["auth_url"])
        self.assertIn("code_challenge_method=S256", r["auth_url"])
        self.assertIn("client_id=cid.apps.googleusercontent.com", r["auth_url"])
        self.assertIn("redirect_uri=http%3A%2F%2Flocalhost%3A7878", r["auth_url"])

    def test_callback_rejects_unknown_state(self):
        am = auth_manager.AuthManager()
        with self.assertRaises(ValueError):
            google_oauth.callback("code", "bogus-state", "http://localhost:7878/api/auth/google/callback", am)

    def test_decode_id_token(self):
        claims = google_oauth.decode_id_token(_fake_id_token({"email": "a@b.com", "name": "A", "sub": "s"}))
        self.assertEqual(claims["email"], "a@b.com")
        self.assertEqual(claims["sub"], "s")

    def test_full_callback_creates_admin_and_issues_tokens(self):
        google_oauth.set_config("cid.apps.googleusercontent.com")
        am = auth_manager.AuthManager()
        begin = google_oauth.begin("http://localhost:7878/api/auth/google/callback")
        fake = _fake_id_token({"email": "owner@example.com", "name": "Owner", "sub": "g1"})
        with mock.patch.object(google_oauth, "exchange_code",
                               return_value={"email": "owner@example.com", "name": "Owner",
                                             "sub": "g1", "id_token": fake}):
            result = google_oauth.callback("AUTH", begin["state"],
                                           "http://localhost:7878/api/auth/google/callback", am)
        self.assertEqual(result["user"]["role"], "admin")  # first account is admin
        self.assertTrue(result["access_token"])
        # google_sub must be persisted on the stored user.
        reloaded = auth_manager.AuthManager()
        self.assertEqual(reloaded._users["owner@example.com"].get("google_sub"), "g1")
        # Reusing the same email returns the same account.
        again = reloaded.google_login("owner@example.com", "Owner", "g1")
        self.assertEqual(again["user"]["id"], result["user"]["id"])


if __name__ == "__main__":
    unittest.main()
