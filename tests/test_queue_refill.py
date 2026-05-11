import json
from pathlib import Path

import analyze
import queue_refill


def _refill_payload(**overrides):
    payload = {
        "source": "social-os-ui",
        "mode": "review_queue_only",
        "allow_live_actions": False,
        "requested_by": "Giga",
        "requested_at": "2026-05-04T07:49:00+00:00",
    }
    payload.update(overrides)
    return payload


def test_refill_request_rejects_live_actions(monkeypatch):
    called = False

    def fake_run(**kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(queue_refill.analyze, "run", fake_run)

    response = queue_refill.handle_refill_request(_refill_payload(allow_live_actions=True))

    assert response["ok"] is False
    assert response["status"] == "rejected"
    assert "allow_live_actions=false" in response["message"]
    assert called is False


def test_refill_request_runs_review_queue_only_and_normalizes_response(monkeypatch):
    calls = []

    def fake_run(**kwargs):
        calls.append(kwargs)
        return {
            "trigger": {
                "run_id": kwargs["run_id"],
                "trigger": kwargs["trigger"],
                "next_run_at": "2026-05-04T11:49:00+00:00",
            },
            "queue_summary": {"created": 2, "refreshed": 1, "skipped": 0},
            "social_os_summary": {"created": 1, "refreshed": 1, "skipped": 0, "total": 2},
            "filter_summary": {"selected": 2, "skipped": 3},
            "refill_outcome": {"status": "approval_ready_rows_available", "policy_valid": True},
        }

    monkeypatch.setattr(queue_refill.analyze, "run", fake_run)

    response = queue_refill.handle_refill_request(_refill_payload(run_id="social-os-run-1"))

    assert response["ok"] is True
    assert response["status"] == "accepted"
    assert response["created"] == 1
    assert response["refreshed"] == 1
    assert response["skipped"] == 0
    assert response["run_id"] == "social-os-run-1"
    assert response["next_run_at"] == "2026-05-04T11:49:00+00:00"
    assert response["queue_total"] == 0
    assert response["refill_outcome"] == {"status": "approval_ready_rows_available", "policy_valid": True}
    assert response["policy_valid_noop"] is None
    assert calls == [{
        "dry_run": False,
        "skip_llm": True,
        "trigger": "manual",
        "run_id": "social-os-run-1",
        "review_queue_only": True,
        "request_metadata": {
            "source": "social-os-ui",
            "mode": "review_queue_only",
            "allow_live_actions": False,
            "requested_by": "Giga",
            "requested_at": "2026-05-04T07:49:00+00:00",
        },
    }]



def test_refill_request_generates_stable_run_id_when_missing(monkeypatch):
    run_ids = []

    def fake_run(**kwargs):
        run_ids.append(kwargs["run_id"])
        return {
            "trigger": {"run_id": kwargs["run_id"], "trigger": kwargs["trigger"], "schedule_not_configured": True},
            "social_os_summary": {"created": 0, "refreshed": 0, "skipped": 0},
        }

    monkeypatch.setattr(queue_refill.analyze, "run", fake_run)

    first = queue_refill.handle_refill_request(_refill_payload())
    second = queue_refill.handle_refill_request(_refill_payload())

    assert first["run_id"] == second["run_id"]
    assert first["run_id"].startswith("x-engage-manual-20260504T074900")
    assert run_ids == [first["run_id"], second["run_id"]]

def test_refill_request_surfaces_policy_valid_noop(monkeypatch):
    def fake_run(**kwargs):
        return {
            "trigger": {"run_id": kwargs["run_id"], "trigger": kwargs["trigger"]},
            "queue_summary": {"created": 0, "refreshed": 0, "skipped": 0, "total": 0},
            "social_os_summary": {"created": 0, "refreshed": 0, "skipped": 0, "total": 0},
            "filter_summary": {"selected": 0, "skipped": 10, "skipped_by_reason": {"no_account_strategy_match": 10}},
            "refill_outcome": {
                "status": "policy_valid_noop",
                "policy_valid": True,
                "reason": "No approval-ready rows were generated because every selected signal failed deterministic account routing/filter criteria.",
                "skipped_by_reason": {"no_account_strategy_match": 10},
            },
        }

    monkeypatch.setattr(queue_refill.analyze, "run", fake_run)

    response = queue_refill.handle_refill_request(_refill_payload(run_id="noop-run"))

    assert response["ok"] is True
    assert response["created"] == 0
    assert response["queue_total"] == 0
    assert response["refill_outcome"]["status"] == "policy_valid_noop"
    assert response["policy_valid_noop"]["policy_valid"] is True

def test_refill_request_reports_schedule_not_configured(monkeypatch):
    def fake_run(**kwargs):
        return {
            "trigger": {
                "run_id": kwargs["run_id"],
                "trigger": kwargs["trigger"],
                "schedule_not_configured": True,
            },
            "social_os_summary": {"created": 0, "refreshed": 0, "skipped": 0},
        }

    monkeypatch.setattr(queue_refill.analyze, "run", fake_run)

    response = queue_refill.handle_refill_request(_refill_payload(run_id="run-no-schedule"))

    assert response["schedule_not_configured"] is True
    assert "next_run_at" not in response


def test_analyze_review_queue_only_uses_generation_path_and_skips_discord(tmp_path, monkeypatch):
    window_path = tmp_path / "tweets_window.json"
    window_path.write_text(json.dumps([
        {
            "id": "tweet-943",
            "url": "https://x.com/builder/status/943",
            "text": "Desearch SN22 AI search API gives Bittensor builders better data",
            "user": {"username": "builder"},
            "like_count": 100,
            "retweet_count": 20,
            "reply_count": 5,
            "view_count": 5000,
            "quote_count": 2,
            "bookmark_count": 1,
            "_monitor_category": "bittensor",
        }
    ]))
    pending_path = tmp_path / "pending_actions.json"
    cfg = {
        "x_monitor_window_path": str(window_path),
        "pending_actions_path": str(pending_path),
        "discord_channel_id": "unused-in-review-queue-only",
        "top_n": 10,
        "top_deep_dive": 3,
        "engage_check_interval_seconds": 3600,
        "runtime_source": "managed_file",
        "score_weights": {"likes": 3, "retweets": 5, "replies": 2, "views": 0.01, "quotes": 4, "bookmarks": 2},
        "x_accounts": [
            {"id": "brand", "handle": "desearch_ai", "label": "@desearch_ai", "lane": "brand", "action_types": ["quote"]},
        ],
    }
    events = []
    social_writes = []

    monkeypatch.setattr(analyze, "load_runtime_config", lambda: cfg)
    monkeypatch.setattr(analyze, "emit_social_runtime_event", lambda event_type, message, metadata=None: events.append({"event_type": event_type, "message": message, "metadata": metadata}) or True)
    monkeypatch.setattr(analyze, "write_social_os_review_rows", lambda items: social_writes.append(items) or {"created": len(items), "refreshed": 0, "skipped": 0, "total": len(items)})
    monkeypatch.setattr(analyze, "post_to_discord", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Discord must not be called for review_queue_only")))

    result = analyze.run(
        dry_run=False,
        skip_llm=True,
        trigger="manual",
        run_id="run-943",
        review_queue_only=True,
        request_metadata={
            "source": "social-os-ui",
            "mode": "review_queue_only",
            "allow_live_actions": False,
            "requested_by": "Giga",
            "requested_at": "2026-05-04T07:49:00+00:00",
        },
    )

    assert result["trigger"]["run_id"] == "run-943"
    assert result["trigger"]["trigger"] == "manual"
    assert result["trigger"]["owner"] == "socialos"
    assert result["trigger"]["mode"] == "review_queue_only"
    assert result["trigger"]["allow_live_actions"] is False
    assert result["trigger"]["next_run_at"] == "2026-05-04T08:49:00+00:00"
    assert result["queue_summary"]["created"] == 1
    assert result["social_os_summary"]["created"] == 1
    assert pending_path.exists()
    assert len(social_writes[0]) == 1
    assert social_writes[0][0]["source"] == "x-engage-analyzer"
    assert social_writes[0][0]["source_signal_id"] == "tweet-943"
    completion_events = [e for e in events if "Review queue refill completed" in e["message"]]
    assert completion_events
    metadata = completion_events[-1]["metadata"]
    assert metadata["service"] == "socialos-runtime"
    assert metadata["owner"] == "socialos"
    assert metadata["trigger"] == "manual"
    assert metadata["x_monitor_window_path"] == str(window_path)
    assert metadata["signal_counts"]["loaded"] == 1
    assert metadata["filter_summary"]["selected"] == 1
    assert metadata["queue_summary"]["created"] == 1
    assert metadata["social_os_summary"]["created"] == 1
    assert metadata["request"]["source"] == "social-os-ui"

def test_analyze_review_queue_only_consumes_normalized_x_monitor_signal(tmp_path, monkeypatch):
    window_path = tmp_path / "signals.json"
    window_path.write_text(json.dumps([
        {
            "signal_id": "sig-normalized-1",
            "type": "keyword",
            "source": "x-monitor",
            "content": {
                "text": "Desearch SN22 API signal for builders",
                "author": "@builder",
                "url": "https://x.com/builder/status/1616",
            },
            "context": {"matched_term": "SN22", "watchlist_name": "bittensor"},
            "route_hints": {"lanes": ["brand"], "channel": "#x-monitor", "priority": 1},
            "metrics": {"like_count": 20, "view_count": 2000},
        }
    ]))
    pending_path = tmp_path / "pending_actions.json"
    cfg = {
        "x_monitor_window_path": str(window_path),
        "pending_actions_path": str(pending_path),
        "discord_channel_id": "unused",
        "top_n": 10,
        "top_deep_dive": 3,
        "engage_check_interval_seconds": 3600,
        "runtime_source": "managed_file",
        "score_weights": {"likes": 3, "retweets": 5, "replies": 2, "views": 0.01, "quotes": 4, "bookmarks": 2},
        "x_accounts": [
            {"id": "brand", "handle": "desearch_ai", "label": "@desearch_ai", "lane": "brand", "action_types": ["quote"]},
        ],
    }
    social_writes = []

    monkeypatch.setattr(analyze, "load_runtime_config", lambda: cfg)
    monkeypatch.setattr(analyze, "emit_social_runtime_event", lambda *args, **kwargs: True)
    monkeypatch.setattr(analyze, "write_social_os_review_rows", lambda items: social_writes.append(items) or {"created": len(items), "refreshed": 0, "skipped": 0, "total": len(items)})

    result = analyze.run(dry_run=False, skip_llm=True, trigger="manual", run_id="normalized-run", review_queue_only=True)

    assert result["signal_counts"]["loaded"] == 1
    assert result["source_report"]["connectors"][0]["type"] == "x_monitor_signal"
    assert result["filter_summary"]["selected"] == 1
    assert social_writes[0][0]["source_signal_id"] == "sig-normalized-1"
    assert social_writes[0][0]["monitored_source"]["source"] == "x-monitor"
