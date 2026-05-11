import json

from source_connectors import (
    MarkdownSourceConnector,
    XMonitorSignalConnector,
    load_source_signals,
    normalize_source_signal,
)


def test_x_monitor_normalized_signal_maps_to_analyzer_tweet_shape():
    signal = {
        "signal_id": "sig-123",
        "type": "keyword",
        "source": "x-monitor",
        "timestamp": "2026-05-12T00:00:00Z",
        "content": {
            "text": "Desearch SN22 signal for builders",
            "author": "@builder",
            "url": "https://x.com/builder/status/123",
        },
        "context": {"matched_term": "SN22", "watchlist_name": "bittensor"},
        "route_hints": {"channel": "#x-monitor", "priority": 2, "lanes": ["brand"]},
        "metrics": {"like_count": 10, "view_count": 1000},
    }

    tweet = normalize_source_signal(signal)

    assert tweet["id"] == "sig-123"
    assert tweet["url"] == "https://x.com/builder/status/123"
    assert tweet["text"] == "Desearch SN22 signal for builders"
    assert tweet["user"]["username"] == "builder"
    assert tweet["like_count"] == 10
    assert tweet["view_count"] == 1000
    assert tweet["_monitor_source"] == "x-monitor"
    assert tweet["_monitor_category"] == "keyword:bittensor"
    assert tweet["_monitor_lanes"] == ["brand"]
    assert "x-monitor/brand" in tweet["_monitor_route_hints"]
    assert tweet["_source_connector"] == "x_monitor_signal"


def test_x_monitor_connector_reads_mixed_legacy_and_normalized_window(tmp_path):
    path = tmp_path / "signals.json"
    path.write_text(json.dumps([
        {"id": "legacy-tweet", "text": "legacy", "user": {"username": "old"}},
        {
            "signal_id": "sig-456",
            "source": "x-monitor",
            "content": {"text": "Founder shipping note", "author": "@cosmic", "url": "https://x.com/cosmic/status/456"},
            "route_hints": {"lane": "founder"},
        },
    ]))

    result = XMonitorSignalConnector({"path": str(path)}).load()

    assert [item["id"] for item in result.signals] == ["legacy-tweet", "sig-456"]
    assert result.errors == []
    assert result.metadata["connector"] == "x_monitor_signal"


def test_markdown_connector_defines_future_non_x_source_interface(tmp_path):
    md = tmp_path / "idea.md"
    md.write_text("# Launch memo\n\nAPI source idea for Socialos drafts.")

    result = MarkdownSourceConnector({"paths": [str(md)], "default_lane": "research"}).load()

    assert len(result.signals) == 1
    signal = result.signals[0]
    assert signal["_source_connector"] == "markdown"
    assert signal["_monitor_source"] == "markdown"
    assert signal["_monitor_lanes"] == ["research"]
    assert signal["url"].startswith("file://")
    assert "API source idea" in signal["text"]


def test_load_source_signals_uses_configured_connectors_and_reports_non_fatal_errors(tmp_path):
    md = tmp_path / "brief.md"
    md.write_text("# Brief\n\nDesearch API launch notes")

    signals, report = load_source_signals({
        "source_connectors": [
            {"type": "markdown", "paths": [str(md)], "default_lane": "brand"},
            {"type": "unknown"},
        ]
    })

    assert len(signals) == 1
    assert report["loaded"] == 1
    assert report["connectors"][0]["type"] == "markdown"
    assert report["connectors"][1]["status"] == "error"
    assert "Unsupported source connector" in report["connectors"][1]["error"]
