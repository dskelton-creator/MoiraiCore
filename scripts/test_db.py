"""Tests for MoiraiCore DB persistence layer (Feature 10)."""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

# Add scripts dir to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Force test mode — use tempdir for all file operations
_test_dir = Path(os.environ["AGENT_OS_ROOT"])
os.environ["AGENT_OS_ROOT"] = str(_test_dir)

from db import (
    atomic_save_json,
    atomic_load_json,
    init_db,
    get_goals,
    save_goals,
    get_tasks,
    save_tasks,
    get_kanban,
    save_kanban,
    get_tasks_by_status,
    get_tasks_by_goal,
    get_task_count,
    migrate_from_json,
    DB_PATH,
    TASKS_FILE,
    GOALS_FILE,
    KANBAN_FILE,
)


def setup_module():
    """Create test directory structure."""
    (_test_dir / "config").mkdir(parents=True, exist_ok=True)


def teardown_module():
    """Clean up test files."""
    import shutil
    shutil.rmtree(_test_dir, ignore_errors=True)


def test_atomic_save_json_creates_file():
    """Atomic write creates the file with correct content."""
    path = _test_dir / "test_atomic.json"
    data = {"name": "test", "value": 42}
    atomic_save_json(path, data)
    assert path.exists()
    loaded = json.loads(path.read_text())
    assert loaded == data
    path.unlink()


def test_atomic_save_json_overwrites():
    """Atomic write overwrites existing file."""
    path = _test_dir / "test_overwrite.json"
    atomic_save_json(path, {"old": "data"})
    atomic_save_json(path, {"new": "data"})
    loaded = json.loads(path.read_text())
    assert loaded == {"new": "data"}
    path.unlink()


def test_atomic_save_json_no_corrupt_tmp():
    """Atomic write cleans up temp files."""
    path = _test_dir / "test_cleanup.json"
    atomic_save_json(path, {"clean": True})
    # Check no .tmp files remain
    tmp_files = list(_test_dir.glob("*.tmp.*"))
    assert len(tmp_files) == 0, f"Leftover tmp files: {tmp_files}"
    path.unlink()


def test_atomic_load_json_missing():
    """Loading a missing file returns the default."""
    path = _test_dir / "nonexistent.json"
    result = atomic_load_json(path, default={"fallback": True})
    assert result == {"fallback": True}


def test_atomic_load_json_corrupt_recovery():
    """Loading a corrupt file falls back to default."""
    path = _test_dir / "corrupt.json"
    path.write_text("{invalid json!!!")
    result = atomic_load_json(path, default=[])
    assert result == []


def test_atomic_load_json_valid():
    """Loading a valid file returns the data."""
    path = _test_dir / "valid.json"
    data = {"valid": True, "items": [1, 2, 3]}
    atomic_save_json(path, data)
    loaded = atomic_load_json(path)
    assert loaded == data


def test_atomic_save_json_list():
    """Atomic write works with list data (tasks format)."""
    path = _test_dir / "tasks_list.json"
    tasks = [
        {"id": "t1", "title": "Task 1", "status": "backlog"},
        {"id": "t2", "title": "Task 2", "status": "done"},
    ]
    atomic_save_json(path, tasks)
    loaded = json.loads(path.read_text())
    assert isinstance(loaded, list)
    assert len(loaded) == 2
    assert loaded[0]["id"] == "t1"


def test_atomic_save_json_nested():
    """Atomic write with nested dicts."""
    path = _test_dir / "nested.json"
    data = {
        "backlog": [{"id": "c1", "title": "Card 1"}],
        "progress": [],
        "done": [{"id": "c2", "title": "Card 2"}],
    }
    atomic_save_json(path, data)
    loaded = json.loads(path.read_text())
    assert loaded["backlog"][0]["title"] == "Card 1"
    assert len(loaded["done"]) == 1


def test_atomic_save_json_default_str():
    """Atomic write handles non-serializable types with default=str."""
    path = _test_dir / "datetime.json"
    from datetime import datetime
    data = {"timestamp": datetime(2026, 7, 30, 12, 0, 0)}
    atomic_save_json(path, data)
    loaded = json.loads(path.read_text())
    assert "2026-07-30" in loaded["timestamp"]


def test_sqlite_init_db():
    """SQLite database initializes correctly."""
    result = init_db()
    assert result is True
    assert DB_PATH.exists()


def test_sqlite_goals_crud():
    """Save and retrieve goals via SQLite."""
    init_db()
    goals = [
        {"id": "g1", "title": "Goal 1", "status": "active"},
        {"id": "g2", "title": "Goal 2", "status": "completed"},
    ]
    save_goals(goals)
    loaded = get_goals()
    assert len(loaded) >= 2
    titles = [g["title"] for g in loaded]
    assert "Goal 1" in titles
    assert "Goal 2" in titles


def test_sqlite_tasks_crud():
    """Save and retrieve tasks via SQLite."""
    init_db()
    tasks = [
        {"id": "t1", "title": "Task 1", "status": "backlog", "goal_id": "g1"},
        {"id": "t2", "title": "Task 2", "status": "done", "goal_id": "g1"},
    ]
    save_tasks(tasks)
    loaded = get_tasks()
    assert len(loaded) >= 2


def test_sqlite_tasks_filter_by_status():
    """Filter tasks by status using SQLite query."""
    init_db()
    tasks = [
        {"id": "t_s1", "title": "Backlog Task", "status": "backlog"},
        {"id": "t_s2", "title": "Done Task", "status": "done"},
    ]
    save_tasks(tasks)
    done = get_tasks_by_status("done")
    assert len(done) >= 1
    assert all(t["status"] == "done" for t in done)


def test_sqlite_tasks_filter_by_goal():
    """Filter tasks by goal_id using SQLite query."""
    init_db()
    tasks = [
        {"id": "t_g1", "title": "Goal A Task", "status": "backlog", "goal_id": "goal_a"},
        {"id": "t_g2", "title": "Goal B Task", "status": "backlog", "goal_id": "goal_b"},
    ]
    save_tasks(tasks)
    goal_a_tasks = get_tasks_by_goal("goal_a")
    assert len(goal_a_tasks) >= 1
    assert all(t["goal_id"] == "goal_a" for t in goal_a_tasks)


def test_sqlite_task_count():
    """Get task counts by status."""
    init_db()
    tasks = [
        {"id": "t_c1", "title": "T1", "status": "backlog"},
        {"id": "t_c2", "title": "T2", "status": "done"},
        {"id": "t_c3", "title": "T3", "status": "done"},
    ]
    save_tasks(tasks)
    counts = get_task_count()
    assert isinstance(counts, dict)
    assert counts.get("backlog", 0) >= 1
    assert counts.get("done", 0) >= 2


def test_sqlite_kanban_crud():
    """Save and retrieve kanban board via SQLite."""
    init_db()
    board = {
        "backlog": [{"id": "b1", "title": "Card 1"}, {"id": "b2", "title": "Card 2"}],
        "progress": [{"id": "p1", "title": "In Progress"}],
        "review": [],
        "done": [{"id": "d1", "title": "Done Card"}],
    }
    save_kanban(board)
    loaded = get_kanban()
    assert "backlog" in loaded
    assert "done" in loaded
    assert len(loaded["backlog"]) >= 2
    assert len(loaded["done"]) >= 1


def test_sqlite_goals_overwrite():
    """Saving goals replaces the previous set."""
    init_db()
    save_goals([{"id": "g_old", "title": "Old Goal"}])
    save_goals([{"id": "g_new", "title": "New Goal"}])
    loaded = get_goals()
    titles = [g["title"] for g in loaded]
    assert "New Goal" in titles
    # Old goal should be gone (replaced)
    assert "Old Goal" not in titles


def test_sqlite_empty_goals():
    """Empty goals list is handled correctly."""
    init_db()
    save_goals([])
    loaded = get_goals()
    assert loaded == []


def test_sqlite_empty_tasks():
    """Empty tasks list is handled correctly."""
    init_db()
    save_tasks([])
    loaded = get_tasks()
    assert loaded == []


def test_sqlite_empty_kanban():
    """Empty kanban board is handled correctly."""
    init_db()
    save_kanban({})
    loaded = get_kanban()
    assert isinstance(loaded, dict)
    assert {"backlog", "progress", "review", "done"}.issubset(loaded.keys())


def test_atomic_save_concurrent_safety():
    """Multiple rapid atomic writes don't corrupt data."""
    path = _test_dir / "concurrent.json"
    import threading

    def writer(n):
        for i in range(10):
            atomic_save_json(path, {"writer": n, "iteration": i, "data": "x" * 100})
            time.sleep(0.001)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # File should be valid JSON
    loaded = json.loads(path.read_text())
    assert isinstance(loaded, dict)
    assert "writer" in loaded
    assert "iteration" in loaded


def test_atomic_save_large_data():
    """Atomic write handles large data (~1MB)."""
    path = _test_dir / "large.json"
    data = {"items": [{"id": i, "data": "x" * 1000} for i in range(1000)]}
    atomic_save_json(path, data)
    loaded = json.loads(path.read_text())
    assert len(loaded["items"]) == 1000


def test_migrate_from_json():
    """Migration from JSON to SQLite works."""
    # Seed JSON files
    atomic_save_json(GOALS_FILE, [{"id": "m_g1", "title": "Migrated Goal"}])
    atomic_save_json(TASKS_FILE, [{"id": "m_t1", "title": "Migrated Task", "goal_id": "m_g1"}])
    atomic_save_json(KANBAN_FILE, {"backlog": [{"id": "m_k1", "title": "Migrated Card"}], "progress": [], "review": [], "done": []})

    summary = migrate_from_json()
    assert summary["goals"] >= 1
    assert summary["tasks"] >= 1
    assert summary["kanban"] >= 1

    # Verify data is in SQLite
    goals = get_goals()
    titles = [g["title"] for g in goals]
    assert "Migrated Goal" in titles


if __name__ == "__main__":
    setup_module()
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed = 0
    failed = 0
    for test_fn in tests:
        try:
            test_fn()
            print(f"  ✓ {test_fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  ✗ {test_fn.__name__}: ASSERTION: {e}")
            failed += 1
        except Exception as e:
            print(f"  ✗ {test_fn.__name__}: {type(e).__name__}: {e}")
            failed += 1

    teardown_module()
    print(f"\n{'='*40}")
    print(f"Results: {passed} passed, {failed} failed, {len(tests)} total")
    sys.exit(0 if failed == 0 else 1)