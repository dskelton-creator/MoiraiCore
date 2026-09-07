"""Root conftest: pin one shared, isolated AGENT_OS_ROOT for the whole run.

Several script modules (project_chat, webhooks, vault_index, ...) bind
paths at import time from AGENT_OS_ROOT. Each test module used to set its
OWN tmp dir at import, so the module imported first decided the path for
every later import — full-suite runs polluted each other while individual
files passed. A single shared root, set before any test module imports,
makes the whole suite order-stable.
"""
import os
import tempfile

_root = tempfile.mkdtemp(prefix="moirai_test_root_")
os.environ.setdefault("AGENT_OS_ROOT", _root)
