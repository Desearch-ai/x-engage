from datetime import datetime, timezone
import json

from post_tweet import is_within_send_window, resolve_send_window


def test_legacy_human_window_still_blocks_outside_window():
    allowed, reason = is_within_send_window(
        "Tue-Thu morning Tbilisi",
        now=datetime(2026, 4, 27, 9, 0, tzinfo=timezone.utc),  # Mon 13:00 Tbilisi
    )
    assert allowed is False
    assert "outside allowed days" in reason


def test_managed_runtime_window_allows_when_inside_window(tmp_path, monkeypatch):
    runtime_path = tmp_path / "runtime.json"
    runtime_path.write_text(json.dumps({
        "lane_routing": {"brand": ["desearch_ai"]},
        "session_mappings": {"desearch_ai": "brand-session"},
        "send_window": {"start": "08:00", "end": "22:00", "tz": "Asia/Tbilisi"},
        "rate_limits": {"max_posts_per_day": 10, "max_replies_per_day": 20},
        "check_interval_seconds": 3600,
    }))
    monkeypatch.setenv("X_ENGAGE_RUNTIME_PATH", str(runtime_path))

    window = resolve_send_window(None)
    allowed, reason = is_within_send_window(
        window,
        now=datetime(2026, 4, 27, 6, 0, tzinfo=timezone.utc),  # 10:00 Tbilisi
    )
    assert allowed is True
    assert "within window" in reason


def test_managed_runtime_window_blocks_when_outside_window(tmp_path, monkeypatch):
    runtime_path = tmp_path / "runtime.json"
    runtime_path.write_text(json.dumps({
        "send_window_start": "08:00",
        "send_window_end": "22:00",
        "send_window_tz": "Asia/Tbilisi",
        "lane_routing": {"brand": ["desearch_ai"]},
        "session_mappings": {"desearch_ai": "brand-session"},
    }))
    monkeypatch.setenv("X_ENGAGE_RUNTIME_PATH", str(runtime_path))

    window = resolve_send_window(None)
    allowed, reason = is_within_send_window(
        window,
        now=datetime(2026, 4, 27, 3, 59, tzinfo=timezone.utc),  # 07:59 Tbilisi
    )
    assert allowed is False
    assert "outside send window" in reason
