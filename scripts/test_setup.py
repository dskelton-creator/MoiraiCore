"""Unit tests for scripts/setup.py (first-run system configuration)."""
import importlib
import json
import os
import tempfile
import unittest

import setup


class SetupConfigTest(unittest.TestCase):
    def setUp(self):
        self._root = tempfile.mkdtemp()
        self._prev = os.environ.get("AGENT_OS_ROOT")
        os.environ["AGENT_OS_ROOT"] = self._root
        # Reload so module-level path constants point at the temp root.
        importlib.reload(setup)

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("AGENT_OS_ROOT", None)
        else:
            os.environ["AGENT_OS_ROOT"] = self._prev

    def test_fresh_instance_needs_onboarding(self):
        st = setup.status()
        self.assertTrue(st["needs_onboarding"])
        self.assertFalse(st["configured"])
        self.assertFalse(st["has_users"])

    def test_complete_marks_onboarded_and_merges(self):
        saved = setup.complete({
            "organization_name": "Acme",
            "model": {"tier2_provider": "gemini"},
        })
        self.assertTrue(saved["onboarded"])
        self.assertEqual(saved["organization_name"], "Acme")
        self.assertEqual(saved["model"]["tier2_provider"], "gemini")
        # Deep-merge preserved the other defaults.
        self.assertEqual(saved["model"]["tier1"], "hermes")
        st = setup.status()
        self.assertFalse(st["needs_onboarding"])
        self.assertTrue(st["configured"])

    def test_skip_marks_onboarded_without_config(self):
        saved = setup.skip()
        self.assertTrue(saved["onboarded"])
        self.assertEqual(saved["organization_name"], "MoiraiCore")  # default kept
        self.assertFalse(setup.status()["needs_onboarding"])

    def test_has_users_detects_auth_store(self):
        users_file = setup.CONFIG_DIR / "auth" / "users.json"
        users_file.parent.mkdir(parents=True, exist_ok=True)
        users_file.write_text(json.dumps({"alice": {"id": "x"}}))
        self.assertTrue(setup.has_users())
        # And with users present but not onboarded, needs_onboarding stays False.
        self.assertFalse(setup.status()["needs_onboarding"])

    def test_resolve_workspace_absolute(self):
        p = setup.resolve_workspace({"workspace": "/tmp/custom"})
        self.assertEqual(str(p), "/tmp/custom")

    def test_resolve_workspace_relative_to_root(self):
        p = setup.resolve_workspace({"workspace": "workspace"})
        self.assertTrue(str(p).startswith(self._root))


if __name__ == "__main__":
    unittest.main()
