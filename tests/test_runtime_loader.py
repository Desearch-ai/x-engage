import json

import runtime_loader
from runtime_loader import emit_social_runtime_event, safe_session_health_summary


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
