import json

import runtime_loader
from runtime_loader import emit_social_runtime_event, safe_session_health_summary, _build_social_post_row


def test_safe_session_health_summary_redacts_full_profile_path(tmp_path):
    profile = tmp_path / "brand-session"
    account = {
        "id": "brand",
        "handle": "desearch_ai",
        "lane": "brand",
        "browser_profile": str(profile),
    }

    summary = safe_session_health_summary(account)

    assert summary["profile_key"] == "brand-session"
    assert summary["profile_configured"] is True
    assert summary["profile_exists"] is False
    assert str(tmp_path) not in json.dumps(summary)


def test_emit_social_runtime_event_posts_to_social_runtime_events(monkeypatch):
    calls = []

    class Response:
        ok = True
        text = ""
        status_code = 201
        def raise_for_status(self):
            return None

    def fake_post(url, headers, json, timeout):
        calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return Response()

    monkeypatch.setenv("SOCIAL_OS_SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SOCIAL_OS_SUPABASE_KEY", "service-key")
    monkeypatch.setattr(runtime_loader.requests, "post", fake_post)

    assert emit_social_runtime_event("info", "Queue refreshed", {"run_id": "run-1"}) is True
    assert calls[0]["url"].endswith("/rest/v1/social_runtime_events")
    assert calls[0]["json"][0]["service"] == "x-engage"
    assert calls[0]["json"][0]["event_type"] == "info"
    assert calls[0]["json"][0]["metadata"] == {"run_id": "run-1"}


def test_build_social_post_row_includes_filter_and_account_routing_metadata():
    item = {
        "account_id": "brand",
        "account_handle": "desearch_ai",
        "account_label": "@desearch_ai",
        "lane": "research",
        "action_types": ["quote"],
        "tweet_text": "SN22 AI search API signal",
        "author": "builder",
        "score": 900,
        "source_signal_id": "tweet-123",
        "source_signal_url": "https://x.com/builder/status/123",
        "rationale": "Selected after explicit filter/account-routing stage.",
        "account_fit_reason": "Matches @desearch_ai technical/product strategy.",
        "writing_standard_check": {"status": "passed", "summary": "Professional/product-focused copy required."},
        "filter_status": "qualified",
        "confidence": 0.83,
        "risk": "low",
        "risk_notes": "Requires explicit MC approval before execution.",
        "duplicate_notes": "No duplicate pending item detected.",
        "priority": "high",
        "generation_fingerprint": "abcdef1234567890",
        "monitored_source": {"username": "builder", "matched_keywords": ["SN22", "AI search"]},
    }

    row = _build_social_post_row(item, "x-engage:tweet-123:brand")

    assert row["account_handle"] == "desearch_ai"
    assert row["lane"] == "research"
    assert row["source_url"] == "https://x.com/builder/status/123"
    assert row["source_signal_id"] == "tweet-123"
    assert row["priority"] == "high"
    assert row["risk_level"] == "low"
    assert row["rationale"] == "Matches @desearch_ai technical/product strategy."
    assert "Writing standard: passed" in row["signal_rationale"]
    assert "Filter: qualified" in row["content"]
    assert "Account fit: Matches @desearch_ai technical/product strategy." in row["content"]
    assert "Confidence/risk: 0.83 / low" in row["content"]
