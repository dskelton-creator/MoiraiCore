"""Tests for enhanced webhooks (Feature 8 Enhanced, July 2026)."""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_test_dir = Path(os.environ["AGENT_OS_ROOT"])
os.environ["AGENT_OS_ROOT"] = str(_test_dir)

from webhooks import (
    create_webhook,
    update_webhook,
    get_webhook,
    delete_webhook,
    set_active,
    load_webhooks,
    get_deliveries,
    get_dead_letters,
    emit,
    _validate_payload,
    _matches_filter,
    _check_rate_limit,
    replay_dead_letter,
    replay_all_dead_letters,
    EVENT_TYPES,
)


def setup_module():
    """Create test directory structure."""
    (_test_dir / "config").mkdir(parents=True, exist_ok=True)


def teardown_module():
    """Clean up."""
    import shutil
    shutil.rmtree(_test_dir, ignore_errors=True)


def test_event_types_count():
    """There should be 13 event types."""
    assert len(EVENT_TYPES) == 13


def test_create_webhook():
    """Create a basic webhook."""
    h = create_webhook("Test Hook", "https://example.com/hook", ["goal.created", "ping"])
    assert h["name"] == "Test Hook"
    assert h["url"] == "https://example.com/hook"
    assert "goal.created" in h["events"]
    assert "ping" in h["events"]
    assert h["active"] is True
    assert h["id"].startswith("wh-")
    assert h["schema"] == {}
    assert h["filters"] == {}
    assert h["max_rate"] == 0
    assert h["total_deliveries"] == 0
    assert h["successful_deliveries"] == 0
    assert h["failed_deliveries"] == 0
    delete_webhook(h["id"])


def test_create_webhook_with_schema():
    """Create a webhook with payload schema."""
    schema = {"type": "object", "required": ["goal_id"], "properties": {"goal_id": {"type": "string"}}}
    h = create_webhook("Schema Hook", "https://example.com/hook", ["goal.created"],
                       schema=schema)
    assert h["schema"] == schema
    delete_webhook(h["id"])


def test_create_webhook_with_filters():
    """Create a webhook with event filters."""
    filters = {"status": ["completed", "failed"]}
    h = create_webhook("Filtered Hook", "https://example.com/hook", ["task.executed"],
                       filters=filters)
    assert h["filters"] == filters
    delete_webhook(h["id"])


def test_create_webhook_with_rate_limit():
    """Create a webhook with rate limiting."""
    h = create_webhook("Rate Limited", "https://example.com/hook", ["ping"], max_rate=5)
    assert h["max_rate"] == 5
    delete_webhook(h["id"])


def test_create_webhook_empty_name():
    """Empty name raises ValueError."""
    try:
        create_webhook("", "https://example.com/hook", ["ping"])
        assert False, "Should have raised"
    except ValueError:
        pass


def test_create_webhook_bad_url():
    """Non-http URL raises ValueError."""
    try:
        create_webhook("Test", "ftp://bad.com/hook", ["ping"])
        assert False, "Should have raised"
    except ValueError:
        pass


def test_create_webhook_no_events():
    """No valid events raises ValueError."""
    try:
        create_webhook("Test", "https://example.com/hook", [])
        assert False, "Should have raised"
    except ValueError:
        pass


def test_get_webhook():
    """Get webhook by ID."""
    h = create_webhook("Get Test", "https://example.com/hook", ["ping"])
    loaded = get_webhook(h["id"])
    assert loaded is not None
    assert loaded["name"] == "Get Test"
    delete_webhook(h["id"])


def test_get_webhook_not_found():
    """Getting a non-existent webhook returns None."""
    assert get_webhook("wh-nonexistent") is None


def test_update_webhook():
    """Update a webhook's configuration."""
    h = create_webhook("Original", "https://example.com/hook", ["ping"])
    updated = update_webhook(h["id"], name="Updated", url="https://new.example.com/hook")
    assert updated is not None
    assert updated["name"] == "Updated"
    assert updated["url"] == "https://new.example.com/hook"
    assert updated["updated_at"] != h["created_at"]
    delete_webhook(h["id"])


def test_update_webhook_schema():
    """Update webhook schema."""
    h = create_webhook("Schema Update", "https://example.com/hook", ["goal.created"])
    new_schema = {"required": ["id"]}
    updated = update_webhook(h["id"], schema=new_schema)
    assert updated["schema"] == new_schema
    delete_webhook(h["id"])


def test_update_webhook_filters():
    """Update webhook filters."""
    h = create_webhook("Filter Update", "https://example.com/hook", ["task.executed"])
    new_filters = {"agent": ["developer"]}
    updated = update_webhook(h["id"], filters=new_filters)
    assert updated["filters"] == new_filters
    delete_webhook(h["id"])


def test_update_webhook_rate_limit():
    """Update webhook rate limit."""
    h = create_webhook("Rate Update", "https://example.com/hook", ["ping"])
    updated = update_webhook(h["id"], max_rate=10)
    assert updated["max_rate"] == 10
    delete_webhook(h["id"])


def test_update_webhook_not_found():
    """Updating a non-existent webhook returns None."""
    assert update_webhook("wh-nonexistent", name="Nope") is None


def test_set_active():
    """Toggle webhook active state."""
    h = create_webhook("Active Test", "https://example.com/hook", ["ping"])
    assert h["active"] is True
    set_active(h["id"], False)
    loaded = get_webhook(h["id"])
    assert loaded["active"] is False
    set_active(h["id"], True)
    loaded = get_webhook(h["id"])
    assert loaded["active"] is True
    delete_webhook(h["id"])


def test_load_webhooks():
    """load_webhooks returns the persisted list."""
    create_webhook("List Test", "https://example.com/hook", ["ping"])
    hooks = load_webhooks()
    names = [h["name"] for h in hooks]
    assert "List Test" in names


def test_delete_webhook():
    """Delete a webhook."""
    h = create_webhook("Delete Me", "https://example.com/hook", ["ping"])
    assert delete_webhook(h["id"]) is True
    assert get_webhook(h["id"]) is None


def test_validate_payload_valid():
    """Valid payload passes schema validation."""
    schema = {"type": "object", "required": ["goal_id"], "properties": {"goal_id": {"type": "string"}}}
    valid, msg = _validate_payload({"goal_id": "g1", "title": "hi"}, schema)
    assert valid is True


def test_validate_payload_missing_required():
    """Missing required field fails validation."""
    schema = {"type": "object", "required": ["goal_id"]}
    valid, msg = _validate_payload({"title": "hi"}, schema)
    assert valid is False
    assert "goal_id" in msg


def test_validate_payload_wrong_type():
    """Wrong field type fails validation."""
    schema = {"properties": {"count": {"type": "integer"}}}
    valid, msg = _validate_payload({"count": "notanint"}, schema)
    assert valid is False
    assert "should be integer" in msg


def test_validate_payload_empty_schema():
    """Empty schema passes."""
    valid, msg = _validate_payload({"foo": "bar"}, {})
    assert valid is True


def test_validate_payload_enum():
    """Enum validation works."""
    schema = {"properties": {"status": {"type": "string", "enum": ["active", "completed"]}}}
    valid, msg = _validate_payload({"status": "active"}, schema)
    assert valid is True
    valid, msg = _validate_payload({"status": "unknown"}, schema)
    assert valid is False


def test_matches_filter_simple():
    """Simple filter matching."""
    filters = {"status": ["completed", "failed"]}
    assert _matches_filter({"status": "completed"}, filters) is True
    assert _matches_filter({"status": "failed"}, filters) is True
    assert _matches_filter({"status": "running"}, filters) is False


def test_matches_filter_no_filter():
    """No filters always matches."""
    assert _matches_filter({"status": "anything"}, {}) is True


def test_matches_filter_missing_key():
    """Missing key in payload does not filter out."""
    filters = {"agent": ["developer"]}
    assert _matches_filter({"status": "done"}, filters) is True


def test_matches_filter_multiple_keys():
    """Multiple filter keys use AND logic."""
    filters = {"status": ["completed"], "agent": ["developer"]}
    assert _matches_filter({"status": "completed", "agent": "developer"}, filters) is True
    assert _matches_filter({"status": "completed", "agent": "writer"}, filters) is False


def test_rate_limit_unlimited():
    """max_rate=0 means unlimited."""
    assert _check_rate_limit("test-wh", 0) is True


def test_rate_limit_basic():
    """Rate limiting works."""
    wh_id = "rate-test-1"
    # Allow 3 per minute
    for i in range(3):
        assert _check_rate_limit(wh_id, 3) is True, f"Attempt {i+1} should pass"
    # 4th should fail
    assert _check_rate_limit(wh_id, 3) is False


def test_rate_limit_resets():
    """Rate limit resets after 60 seconds."""
    wh_id = "rate-test-2"
    from webhooks import _rate_limit_tracker
    _rate_limit_tracker[wh_id] = [time.time() - 61]  # Old timestamps
    assert _check_rate_limit(wh_id, 1) is True


def test_emit_inactive_webhook():
    """Inactive webhooks are not targeted."""
    # Clear any leftover webhooks
    from webhooks import WEBHOOKS_FILE
    if WEBHOOKS_FILE.exists():
        WEBHOOKS_FILE.unlink()
    h = create_webhook("Inactive", "https://example.com/hook", ["ping"], active=False)
    n = emit("ping")
    assert n == 0, f"Expected 0, got {n}"
    delete_webhook(h["id"])


def test_emit_wrong_event():
    """Webhooks not subscribed to the event are not targeted."""
    from webhooks import WEBHOOKS_FILE
    if WEBHOOKS_FILE.exists():
        WEBHOOKS_FILE.unlink()
    h = create_webhook("Wrong Event", "https://example.com/hook", ["goal.created"])
    n = emit("ping")
    assert n == 0, f"Expected 0, got {n}"
    delete_webhook(h["id"])


def test_get_deliveries_empty():
    """get_deliveries returns empty list when no deliveries."""
    assert get_deliveries() == []


def test_get_dead_letters_empty():
    """get_dead_letters returns empty list when no dead letters."""
    assert get_dead_letters() == []


def test_replay_dead_letter_not_found():
    """Replaying a non-existent dead letter returns error."""
    result = replay_dead_letter("nonexistent")
    assert result.get("ok") is False


def test_replay_all_dead_letters_empty():
    """Replaying all dead letters when empty returns ok."""
    result = replay_all_dead_letters()
    assert result.get("ok") is True
    assert result.get("total") == 0


def test_persist_webhooks():
    """Webhooks persist in the JSON file between loads."""
    h = create_webhook("Persist Test", "https://example.com/persist", ["ping"])
    path = _test_dir / "config" / "webhooks.json"
    assert path.exists()
    data = json.loads(path.read_text())
    names = [w["name"] for w in data]
    assert "Persist Test" in names
    delete_webhook(h["id"])


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