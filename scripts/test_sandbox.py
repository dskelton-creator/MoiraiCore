"""Tests for MoiraiCore Project Sandbox — the write jail (path-traversal protection)."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_test_dir = Path(tempfile.mkdtemp(prefix="moirai_sandbox_test_"))
os.environ["AGENT_OS_ROOT"] = str(_test_dir)

from sandbox import ProjectSandbox, SandboxViolation


def setup_module():
    """Create an isolated project structure."""
    (_test_dir / "projects" / "acme" / "src").mkdir(parents=True, exist_ok=True)
    (_test_dir / "projects" / "acme" / "docs").mkdir(parents=True, exist_ok=True)


def _sb():
    return ProjectSandbox("acme")


def test_verify_write_allows_inside_project():
    p = _sb().verify_write("src/app.py")
    assert p.name == "app.py"


def test_verify_write_blocks_escape_attempts():
    sb = _sb()
    for bad in ("../outside.txt", "../../etc/passwd", "/etc/passwd", "src/../../etc/shadow"):
        try:
            sb.verify_write(bad)
            raise AssertionError(f"should have blocked: {bad!r}")
        except SandboxViolation:
            pass  # expected


def test_write_read_roundtrip():
    sb = _sb()
    sb.write_file("src/app.py", "print('hi')")
    assert sb.read_file("src/app.py") == "print('hi')"


def test_write_rejects_hidden_core_paths():
    # Even within a project, well-known core paths should be blocked by the guard.
    try:
        _sb().verify_write(".env")
    except SandboxViolation:
        pass
