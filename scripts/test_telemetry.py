"""Tests for telemetry enhancements (Feature 12, July 2026)."""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_test_dir = Path(os.environ["AGENT_OS_ROOT"])
os.environ["AGENT_OS_ROOT"] = str(_test_dir)

from telemetry import (
    record_run,
    get_summary,
    get_runs,
    get_timeseries,
    get_efficiency,
    set_alert,
    get_alerts,
    delete_alert,
    check_alerts,
    TELEMETRY_FILE,
)


def setup_module():
    (_test_dir / "config").mkdir(parents=True, exist_ok=True)


def teardown_module():
    import shutil
    shutil.rmtree(_test_dir, ignore_errors=True)


def _seed_runs():
    """Create sample telemetry data for testing."""
    record_run(agent="developer", tier="2", model="gemini-2.5-flash",
               duration_ms=5000, prompt_chars=2000, est_output_chars=4000, goal_id="g1", ok=True)
    record_run(agent="researcher", tier="2", model="gemini-2.5-pro",
               duration_ms=12000, prompt_chars=5000, est_output_chars=8000, goal_id="g1", ok=True)
    record_run(agent="developer", tier="3", model="ollama/ornith",
               duration_ms=27000, prompt_chars=3000, est_output_chars=6000, goal_id="g1", ok=True)
    record_run(agent="developer", tier="2", model="gemini-2.5-flash",
               duration_ms=6000, prompt_chars=1500, est_output_chars=3000, goal_id="g2", ok=False)
    record_run(agent="writer", tier="2", model="gemini-2.5-flash",
               duration_ms=3000, prompt_chars=800, est_output_chars=1500, goal_id="g2", ok=True)


def test_get_summary():
    """get_summary returns aggregated stats."""
    _seed_runs()
    summary = get_summary()
    assert summary["total_runs"] >= 5
    assert summary["successes"] >= 4
    assert summary["failures"] >= 1
    assert summary["total_cost_usd"] > 0
    assert summary["total_duration_ms"] > 0
    assert "by_tier" in summary
    assert "by_agent" in summary
    assert "by_model" in summary


def test_get_runs():
    """get_runs returns filtered runs."""
    runs = get_runs(limit=100)
    assert len(runs) >= 5
    all_agents = set(r.get("agent") for r in runs)
    assert "developer" in all_agents

    # Filter by tier
    tier3 = get_runs(limit=100, tier="3")
    for r in tier3:
        assert r.get("tier") == "3"

    # Filter by agent
    writers = get_runs(limit=100, agent="writer")
    for r in writers:
        assert r.get("agent") == "writer"


def test_get_timeseries_default():
    """get_timeseries with day granularity returns daily buckets."""
    _seed_runs()
    result = get_timeseries(granularity="day")
    assert result["granularity"] == "day"
    assert len(result["buckets"]) >= 1
    assert result["total_runs"] >= 5
    assert result["total_cost"] > 0

    bucket = result["buckets"][0]
    assert "ts" in bucket
    assert "cost" in bucket
    assert "duration_ms" in bucket
    assert "runs" in bucket
    assert "ok" in bucket
    assert "fail" in bucket
    assert "by_tier" in bucket
    assert "by_model" in bucket


def test_get_timeseries_hour():
    """get_timeseries with hour granularity works."""
    _seed_runs()
    result = get_timeseries(granularity="hour")
    assert result["granularity"] == "hour"
    assert len(result["buckets"]) >= 1


def test_get_timeseries_empty():
    """get_timeseries with no data returns empty result."""
    # Clear telemetry file
    TELEMETRY_FILE.unlink(missing_ok=True)
    result = get_timeseries()
    assert result["buckets"] == []
    assert result["total_runs"] == 0


def test_get_efficiency():
    """get_efficiency returns model rankings."""
    _seed_runs()
    eff = get_efficiency()
    assert eff["total_runs"] >= 5
    assert eff["avg_cost_per_run"] > 0
    assert eff["avg_duration_per_run"] > 0
    assert len(eff["model_rankings"]) >= 2
    assert eff["fastest_model"] != ""
    assert eff["cheapest_model"] != ""

    # Rankings should be sorted by avg_cost
    rankings = eff["model_rankings"]
    for i in range(len(rankings) - 1):
        assert rankings[i]["avg_cost"] <= rankings[i + 1]["avg_cost"]


def test_get_efficiency_empty():
    """get_efficiency with no data returns empty result."""
    TELEMETRY_FILE.unlink(missing_ok=True)
    eff = get_efficiency()
    assert eff["avg_cost_per_run"] == 0
    assert eff["model_rankings"] == []


def test_set_alert():
    """set_alert creates a new alert."""
    alert = set_alert("Daily cost > $1", "daily_cost", "gt", 1.0)
    assert alert["id"].startswith("alert-")
    assert alert["name"] == "Daily cost > $1"
    assert alert["metric"] == "daily_cost"
    assert alert["operator"] == "gt"
    assert alert["threshold"] == 1.0
    assert alert["enabled"] is True
    delete_alert(alert["id"])


def test_set_alert_invalid_metric():
    """Invalid metric raises ValueError."""
    try:
        set_alert("Bad", "invalid_metric", "gt", 1.0)
        assert False, "Should have raised"
    except ValueError:
        pass


def test_set_alert_invalid_operator():
    """Invalid operator raises ValueError."""
    try:
        set_alert("Bad", "daily_cost", "eq", 1.0)
        assert False, "Should have raised"
    except ValueError:
        pass


def test_get_alerts():
    """get_alerts returns all configured alerts."""
    a1 = set_alert("Alert 1", "daily_cost", "gt", 1.0)
    a2 = set_alert("Alert 2", "failure_rate", "gt", 0.1)
    alerts = get_alerts()
    names = [a["name"] for a in alerts]
    assert "Alert 1" in names
    assert "Alert 2" in names
    delete_alert(a1["id"])
    delete_alert(a2["id"])


def test_delete_alert():
    """delete_alert removes an alert."""
    a = set_alert("Delete Me", "hourly_cost", "lt", 0.5)
    assert delete_alert(a["id"]) is True
    assert delete_alert(a["id"]) is False  # Already deleted
    alerts = get_alerts()
    ids = [x["id"] for x in alerts]
    assert a["id"] not in ids


def test_check_alerts_no_alerts():
    """check_alerts returns empty list when no alerts configured."""
    assert check_alerts() == []


def test_check_alerts_triggered():
    """check_alerts returns triggered alerts when thresholds exceeded."""
    _seed_runs()
    # Set a very low threshold that should definitely trigger
    alert = set_alert("Very low daily cost", "daily_cost", "gt", 0.000001)
    triggered = check_alerts()
    assert len(triggered) >= 1
    found = any(t["alert_id"] == alert["id"] for t in triggered)
    assert found, f"Alert {alert['id']} should have triggered. Triggered: {triggered}"
    delete_alert(alert["id"])


def test_check_alerts_not_triggered():
    """check_alerts returns empty when thresholds are not exceeded."""
    _seed_runs()
    # Set a very high threshold that should not trigger
    alert = set_alert("Very high daily cost", "daily_cost", "gt", 999999.0)
    triggered = check_alerts()
    assert len(triggered) == 0
    delete_alert(alert["id"])


def test_get_alerts_persistence():
    """Alerts persist in the JSON file between loads."""
    TELEMETRY_FILE.unlink(missing_ok=True)
    from telemetry import ALERTS_FILE
    if ALERTS_FILE.exists():
        ALERTS_FILE.unlink()
    a = set_alert("Persist Test", "daily_cost", "gt", 5.0)
    assert ALERTS_FILE.exists()
    data = json.loads(ALERTS_FILE.read_text())
    names = [x["name"] for x in data]
    assert "Persist Test" in names
    delete_alert(a["id"])


def test_check_alerts_disabled():
    """Disabled alerts are not checked."""
    _seed_runs()
    a = set_alert("Disabled Alert", "daily_cost", "gt", 0.0, enabled=False)
    triggered = check_alerts()
    found = any(t["alert_id"] == a["id"] for t in triggered)
    assert not found, "Disabled alert should not trigger"
    delete_alert(a["id"])


def test_summary_by_tier():
    """get_summary breaks down by tier correctly."""
    _seed_runs()
    summary = get_summary()
    assert "2" in summary["by_tier"]
    assert "3" in summary["by_tier"]
    tier3 = summary["by_tier"]["3"]
    assert tier3["runs"] >= 1
    assert tier3["cost_usd"] >= 0  # Local model should be free or very cheap


def test_summary_by_agent():
    """get_summary breaks down by agent correctly."""
    _seed_runs()
    summary = get_summary()
    assert "developer" in summary["by_agent"]
    assert "researcher" in summary["by_agent"]
    dev = summary["by_agent"]["developer"]
    assert dev["runs"] >= 2


def test_telemetry_file_append():
    """record_run appends to the JSONL file."""
    TELEMETRY_FILE.unlink(missing_ok=True)
    rec = record_run(agent="test-agent", tier="2", model="gemini-2.5-flash",
                     duration_ms=100, prompt_chars=100, goal_id="test")
    assert TELEMETRY_FILE.exists()
    lines = TELEMETRY_FILE.read_text().splitlines()
    # This test's run + any previous
    assert len(lines) >= 1
    last = json.loads(lines[-1])
    assert last["agent"] == "test-agent"


def test_summary_since_latest():
    """get_summary returns since/latest timestamps."""
    _seed_runs()
    summary = get_summary()
    assert summary.get("since") is not None
    assert summary.get("latest") is not None


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