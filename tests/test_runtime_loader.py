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


# ── Social OS approved-row loading ────────────────────────────────────────────

def _sample_social_post_row(**overrides) -> dict:
    base = {
        "id": "row-uuid-001",
        "angle": "x-engage:tweet-555:brand",
        "platform": "x",
        "post_type": "retweet",
        "source_signal_id": "tweet-555",
        "source_url": "https://x.com/author/status/555",
        "content": "Review text for Social OS operator.\n\nSuggested quote text",
        "monitored_account": "author",
        "account_handle": "desearch_ai",
        "account_label": "@desearch_ai",
        "lane": "brand",
        "approval_status": "approved",
        "approval_url": "https://mc.desearch.ai/tasks/802",
        "approved_by": "Giga",
        "status": "approved",
        "quote_text": None,
    }
    base.update(overrides)
    return base


def test_social_post_to_action_maps_all_fields():
    from runtime_loader import _social_post_to_action
    row = _sample_social_post_row()
    action = _social_post_to_action(row)

    assert action["tweet_id"] == "tweet-555"
    assert action["tweet_url"] == "https://x.com/author/status/555"
    assert action["author"] == "author"
    assert action["action"] == "retweet"
    assert action["account_id"] == "brand"
    assert action["account_handle"] == "desearch_ai"
    assert action["lane"] == "brand"
    assert action["status"] == "approved"
    assert action["approval_status"] == "approved"
    assert action["approval_url"] == "https://mc.desearch.ai/tasks/802"
    assert action["approved_by"] == "Giga"
    assert action["social_os_row_id"] == "row-uuid-001"
    assert action["_source"] == "social_os"


def test_social_post_to_action_falls_back_to_tweet_id_from_source_url():
    from runtime_loader import _social_post_to_action
    row = _sample_social_post_row(source_signal_id="")

    action = _social_post_to_action(row)

    assert action["tweet_id"] == "555"


def test_social_post_to_action_extracts_account_from_angle():
    from runtime_loader import _social_post_to_action
    row = _sample_social_post_row(angle="x-engage:tweet-999:personal", account_handle="cosmic_desearch")
    action = _social_post_to_action(row)
    assert action["account_id"] == "personal"


def test_social_post_to_action_falls_back_to_handle_when_angle_malformed():
    from runtime_loader import _social_post_to_action
    row = _sample_social_post_row(angle="bad-angle", account_handle="cosmic_desearch")
    action = _social_post_to_action(row)
    assert action["account_id"] == "cosmic_desearch"


def test_social_post_to_action_normalises_post_type_to_action():
    from runtime_loader import _social_post_to_action
    row = _sample_social_post_row(post_type="quote,retweet")
    action = _social_post_to_action(row)
    assert action["action"] == "quote"


def test_social_post_to_action_unknown_post_type_defaults_to_retweet():
    from runtime_loader import _social_post_to_action
    row = _sample_social_post_row(post_type="like")
    action = _social_post_to_action(row)
    assert action["action"] == "retweet"


def test_social_post_to_action_maps_deployed_schema_row_without_optional_author_or_quote():
    from runtime_loader import _social_post_to_action
    row = _sample_social_post_row(post_type="quote")
    row.pop("monitored_account")
    row.pop("quote_text")

    action = _social_post_to_action(row)

    assert action["author"] == "author"
    assert action["quote_text"] == ""
    assert action["social_os_row_id"] == "row-uuid-001"
    assert action["approval_status"] == "approved"
    assert action["approval_url"] == "https://mc.desearch.ai/tasks/802"
    assert action["approved_by"] == "Giga"


def test_load_social_os_approved_rows_returns_empty_when_no_env(monkeypatch):
    from runtime_loader import load_social_os_approved_rows
    monkeypatch.delenv("SOCIAL_OS_SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("VITE_SUPABASE_URL", raising=False)
    result = load_social_os_approved_rows()
    assert result == []


def test_load_social_os_approved_rows_fetches_and_maps(monkeypatch):
    from runtime_loader import load_social_os_approved_rows
    import runtime_loader

    class Response:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return [_sample_social_post_row()]

    calls = []
    def fake_get(url, headers, params, timeout):
        calls.append({"url": url, "params": params})
        return Response()

    monkeypatch.setenv("SOCIAL_OS_SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SOCIAL_OS_SUPABASE_KEY", "service-key")
    monkeypatch.setattr(runtime_loader.requests, "get", fake_get)

    result = load_social_os_approved_rows()

    assert len(result) == 1
    assert result[0]["tweet_id"] == "tweet-555"
    assert result[0]["approval_status"] == "approved"
    assert result[0]["_source"] == "social_os"
    assert calls[0]["params"]["approval_status"] == "eq.approved"
    assert calls[0]["params"]["platform"] == "eq.x"


def test_load_social_os_approved_rows_uses_deployed_schema_columns(monkeypatch):
    from runtime_loader import load_social_os_approved_rows
    import runtime_loader

    deployed_row = _sample_social_post_row()
    deployed_row.pop("monitored_account")
    deployed_row.pop("quote_text")

    class Response:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return [deployed_row]

    calls = []
    def fake_get(url, headers, params, timeout):
        calls.append({"url": url, "params": params})
        return Response()

    monkeypatch.setenv("SOCIAL_OS_SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SOCIAL_OS_SUPABASE_KEY", "service-key")
    monkeypatch.setattr(runtime_loader.requests, "get", fake_get)

    result = load_social_os_approved_rows()

    selected_columns = calls[0]["params"]["select"].split(",")
    assert "monitored_account" not in selected_columns
    assert "quote_text" not in selected_columns
    assert len(result) == 1
    assert result[0]["author"] == "author"


def test_load_social_os_approved_rows_excludes_untrusted_pending_rejected_and_posted_rows(monkeypatch):
    from runtime_loader import load_social_os_approved_rows
    import runtime_loader

    rows = [
        _sample_social_post_row(id="approved-row"),
        _sample_social_post_row(id="pending-row", approval_status="pending"),
        _sample_social_post_row(id="rejected-row", approval_status="rejected"),
        _sample_social_post_row(id="posted-row", status="posted"),
        _sample_social_post_row(id="wrong-platform-row", platform="linkedin"),
        _sample_social_post_row(id="missing-source-row", source_url="", source_signal_id=""),
    ]

    class Response:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return rows

    monkeypatch.setenv("SOCIAL_OS_SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SOCIAL_OS_SUPABASE_KEY", "service-key")
    monkeypatch.setattr(runtime_loader.requests, "get", lambda *args, **kwargs: Response())

    result = load_social_os_approved_rows()

    assert [item["social_os_row_id"] for item in result] == ["approved-row"]


def test_load_social_os_approved_rows_returns_empty_on_http_query_error(monkeypatch):
    from runtime_loader import load_social_os_approved_rows
    import runtime_loader
    import requests

    class Response:
        status_code = 400
        text = '{"message":"column social_posts.monitored_account does not exist"}'
        def raise_for_status(self):
            raise requests.HTTPError("400 Client Error: Bad Request")
        def json(self):
            raise AssertionError("json should not be read after a query error")

    monkeypatch.setenv("SOCIAL_OS_SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SOCIAL_OS_SUPABASE_KEY", "service-key")
    monkeypatch.setattr(runtime_loader.requests, "get", lambda *args, **kwargs: Response())

    result = load_social_os_approved_rows()
    assert result == []


def test_load_social_os_approved_rows_returns_empty_on_error(monkeypatch):
    from runtime_loader import load_social_os_approved_rows
    import runtime_loader

    def fake_get(*args, **kwargs):
        raise Exception("network error")

    monkeypatch.setenv("SOCIAL_OS_SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SOCIAL_OS_SUPABASE_KEY", "service-key")
    monkeypatch.setattr(runtime_loader.requests, "get", fake_get)

    result = load_social_os_approved_rows()
    assert result == []
