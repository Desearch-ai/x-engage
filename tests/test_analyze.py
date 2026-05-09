"""Tests for analyze.py — multi-account queue generation."""
import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock
from analyze import (
    score_tweet,
    get_top_tweets,
    get_accounts,
    build_queue_items,
    qualify_and_route_signals,
    summarize_filter_decisions,
    write_pending_actions,
    load_config,
    _username,
)
import runtime_loader
from runtime_loader import write_social_os_review_rows

SAMPLE_CONFIG = {
    "x_accounts": [
        {
            "id": "personal",
            "label": "@cosmic_desearch (founder)",
            "lane": "founder",
            "browser_profile": "~/.x-engage-browser/personal",
            "min_confidence": 0.7,
            "action_types": ["retweet", "quote"],
        },
        {
            "id": "brand",
            "label": "@desearch_ai (brand)",
            "lane": "brand",
            "browser_profile": "~/.x-engage-browser/brand",
            "min_confidence": 0.8,
            "action_types": ["quote"],
        },
    ],
    "score_weights": {
        "likes": 3, "retweets": 5, "replies": 2,
        "views": 0.01, "quotes": 4, "bookmarks": 2,
    },
}

TWEET_A = {
    "id": "tweet_001",
    "url": "https://x.com/user/status/001",
    "text": "AI search is the future of developer tools",
    "user": {"username": "testuser"},
    "like_count": 100,
    "retweet_count": 50,
    "reply_count": 20,
    "view_count": 10_000,
    "quote_count": 5,
    "bookmark_count": 10,
    "_monitor_category": "ai",
    "_score": 650.0,
}

TWEET_B = {
    "id": "tweet_002",
    "url": "https://x.com/user/status/002",
    "text": "Bittensor SN22 is growing fast",
    "user": {"username": "other"},
    "like_count": 50,
    "retweet_count": 10,
    "reply_count": 5,
    "view_count": 3_000,
    "quote_count": 2,
    "bookmark_count": 3,
    "_monitor_category": "bittensor",
    "_score": 230.0,
}


# ── score_tweet ──────────────────────────────────────────────────────────────

class TestScoreTweet:
    def test_basic_score(self):
        weights = {"likes": 3, "retweets": 5, "replies": 2, "views": 0.01, "quotes": 4, "bookmarks": 2}
        t = {"like_count": 10, "retweet_count": 2, "reply_count": 5, "view_count": 1000, "quote_count": 1, "bookmark_count": 3}
        score = score_tweet(t, weights)
        expected = 10 * 3 + 2 * 5 + 5 * 2 + 1000 * 0.01 + 1 * 4 + 3 * 2
        assert score == pytest.approx(expected)

    def test_none_fields_treated_as_zero(self):
        t = {"like_count": None, "retweet_count": None}
        score = score_tweet(t, {"likes": 3, "retweets": 5})
        assert score == 0.0

    def test_missing_fields_treated_as_zero(self):
        score = score_tweet({}, {"likes": 3})
        assert score == 0.0


# ── get_top_tweets ───────────────────────────────────────────────────────────

class TestGetTopTweets:
    def test_returns_top_n(self):
        weights = SAMPLE_CONFIG["score_weights"]
        top = get_top_tweets([TWEET_A, TWEET_B], weights, 1)
        assert len(top) == 1

    def test_sorted_by_score_descending(self):
        weights = SAMPLE_CONFIG["score_weights"]
        top = get_top_tweets([TWEET_B, TWEET_A], weights, 2)
        assert top[0]["id"] == "tweet_001"  # higher score first

    def test_deduplicates_by_tweet_id(self):
        weights = SAMPLE_CONFIG["score_weights"]
        top = get_top_tweets([TWEET_A, TWEET_A], weights, 10)
        assert len(top) == 1

    def test_adds_score_field(self):
        weights = SAMPLE_CONFIG["score_weights"]
        top = get_top_tweets([TWEET_A], weights, 1)
        assert "_score" in top[0]


# ── get_accounts ─────────────────────────────────────────────────────────────

class TestGetAccounts:
    def test_returns_all_configured_accounts(self):
        accounts = get_accounts(SAMPLE_CONFIG)
        assert len(accounts) == 2

    def test_returns_empty_list_when_no_x_accounts(self):
        assert get_accounts({}) == []

    def test_account_ids_preserved(self):
        accounts = get_accounts(SAMPLE_CONFIG)
        ids = [a["id"] for a in accounts]
        assert "personal" in ids
        assert "brand" in ids

    def test_accounts_no_longer_selected_by_active_account_key(self):
        cfg = {**SAMPLE_CONFIG, "active_account": "brand"}
        # get_accounts returns ALL accounts, not just the "active" one
        accounts = get_accounts(cfg)
        assert len(accounts) == 2


# ── build_queue_items ────────────────────────────────────────────────────────

class TestBuildQueueItems:
    def test_generates_one_item_per_tweet_per_account(self):
        accounts = get_accounts(SAMPLE_CONFIG)
        items = build_queue_items([TWEET_A, TWEET_B], accounts)
        assert len(items) == 4  # 2 tweets × 2 accounts

    def test_single_tweet_two_accounts(self):
        accounts = get_accounts(SAMPLE_CONFIG)
        items = build_queue_items([TWEET_A], accounts)
        assert len(items) == 2

    def test_required_fields_present(self):
        accounts = get_accounts(SAMPLE_CONFIG)
        items = build_queue_items([TWEET_A], accounts)
        for item in items:
            assert "tweet_id" in item
            assert "tweet_url" in item
            assert "tweet_text" in item
            assert "author" in item
            assert "score" in item
            assert "action" in item
            assert "account_id" in item
            assert "account_label" in item
            assert "lane" in item
            assert "source" in item
            assert "category" in item
            assert "timestamp" in item

    def test_action_starts_as_pending(self):
        accounts = get_accounts(SAMPLE_CONFIG)
        items = build_queue_items([TWEET_A], accounts)
        assert all(i["action"] == "pending" for i in items)

    def test_source_field(self):
        accounts = get_accounts(SAMPLE_CONFIG)
        items = build_queue_items([TWEET_A], accounts)
        assert all(i["source"] == "x-engage-analyzer" for i in items)

    def test_brand_account_gets_brand_lane(self):
        accounts = get_accounts(SAMPLE_CONFIG)
        items = build_queue_items([TWEET_A], accounts)
        brand_items = [i for i in items if i["account_id"] == "brand"]
        assert all(i["lane"] == "brand" for i in brand_items)

    def test_personal_account_gets_founder_lane(self):
        accounts = get_accounts(SAMPLE_CONFIG)
        items = build_queue_items([TWEET_A], accounts)
        personal_items = [i for i in items if i["account_id"] == "personal"]
        assert all(i["lane"] == "founder" for i in personal_items)

    def test_tweet_text_truncated_to_280(self):
        long_tweet = {**TWEET_A, "text": "x" * 400}
        accounts = [SAMPLE_CONFIG["x_accounts"][0]]
        items = build_queue_items([long_tweet], accounts)
        assert len(items[0]["tweet_text"]) <= 280

    def test_empty_tweets_list(self):
        accounts = get_accounts(SAMPLE_CONFIG)
        assert build_queue_items([], accounts) == []

    def test_empty_accounts_list(self):
        assert build_queue_items([TWEET_A], []) == []



# ── qualification / account routing ──────────────────────────────────────────

class TestQualificationAndRouting:
    def _accounts(self):
        return [
            {"id": "brand", "handle": "desearch_ai", "label": "@desearch_ai", "lane": "research", "action_types": ["quote"]},
            {"id": "personal", "handle": "cosmicquantum", "label": "@cosmicquantum", "lane": "personal", "action_types": ["quote"]},
        ]

    def test_routes_brand_vs_personal_and_skips_irrelevant_high_score(self):
        brand_signal = {**TWEET_A, "id": "brand-fit", "text": "SN22 AI search API builders need better web scraping infrastructure", "_score": 900.0}
        personal_signal = {**TWEET_A, "id": "personal-fit", "text": "Founder build in public note: shipping a startup on Bittensor teaches you fast", "_score": 850.0}
        irrelevant_signal = {**TWEET_A, "id": "irrelevant", "text": "Celebrity sports drama is trending today", "_score": 9999.0}
        risky_signal = {**TWEET_A, "id": "risky", "text": "Bittensor price prediction: buy now before this pumps", "_score": 950.0}

        decisions = qualify_and_route_signals(
            [irrelevant_signal, brand_signal, personal_signal, risky_signal],
            self._accounts(),
        )

        selected = [d for d in decisions if d["selected"]]
        skipped = [d for d in decisions if not d["selected"]]
        assert len(selected) == 2
        assert len(skipped) == 2
        assert {d["source_signal"]["id"]: d["selected_account"] for d in selected} == {
            "brand-fit": "desearch_ai",
            "personal-fit": "cosmicquantum",
        }
        assert {d["skipped_reason"] for d in skipped} == {"no_account_strategy_match", "negative_filter_match"}
        for decision in decisions:
            assert "account_fit_reason" in decision
            assert "writing_standard_check" in decision
            assert "source_signal" in decision
            assert "confidence" in decision
            assert "risk" in decision

    def test_build_queue_items_accepts_routing_decisions_not_account_cross_product(self):
        tweets = [
            {**TWEET_A, "id": "brand-fit", "text": "desearch SN22 AI search API launch for developers", "_score": 900.0},
            {**TWEET_A, "id": "personal-fit", "text": "founder build in public lesson from shipping a startup", "_score": 800.0},
            {**TWEET_A, "id": "irrelevant", "text": "unrelated viral entertainment update", "_score": 5000.0},
        ]
        decisions = qualify_and_route_signals(tweets, self._accounts())
        items = build_queue_items(tweets, self._accounts(), qualification_decisions=decisions)

        assert len(items) == 2
        assert {item["account_handle"] for item in items} == {"desearch_ai", "cosmicquantum"}
        assert all(item["filter_status"] == "qualified" for item in items)
        assert all(item["account_fit_reason"] for item in items)
        assert all(item["writing_standard_check"]["status"] == "passed" for item in items)

    def test_filter_summary_counts_selected_and_skipped_by_account_lane(self):
        tweets = [
            {**TWEET_A, "id": "brand-fit", "text": "desearch AI search API for Bittensor developers", "_score": 900.0},
            {**TWEET_A, "id": "skip-fit", "text": "generic viral post with no strategic match", "_score": 9000.0},
        ]
        decisions = qualify_and_route_signals(tweets, self._accounts())
        summary = summarize_filter_decisions(decisions)

        assert summary["selected"] == 1
        assert summary["skipped"] == 1
        assert summary["selected_by_account"] == {"desearch_ai": 1}
        assert summary["selected_by_lane"] == {"research": 1}
        assert summary["skipped_by_reason"] == {"no_account_strategy_match": 1}

    def test_x_monitor_route_hints_create_policy_routing_evidence(self):
        routed_signal = {
            **TWEET_A,
            "id": "monitor-routed-founder",
            "text": "The more skills you give codex, the less you have to prompt.",
            "_score": 5024.55,
            "_monitor_category": "openclaw",
            "_monitor_lanes": ["founder"],
            "_monitor_route_hints": ["x-engage/founder"],
            "_monitor_skipped_reason": "",
        }

        accounts = [
            {
                "id": "personal",
                "handle": "cosmicquantum",
                "label": "@cosmicquantum",
                "lane": "founder",
                "action_types": ["quote"],
            }
        ]
        decisions = qualify_and_route_signals([routed_signal], accounts)

        assert len(decisions) == 1
        decision = decisions[0]
        assert decision["selected"] is True
        assert decision["selected_account"] == "cosmicquantum"
        assert "x-monitor-route:founder" in decision["matched_keywords"]
        assert "x-monitor supplied policy route hint" in decision["account_fit_reason"]

    def test_negative_filters_still_override_x_monitor_route_hints(self):
        risky_routed_signal = {
            **TWEET_A,
            "id": "monitor-routed-risky",
            "text": "Bittensor price prediction: buy now before this pumps",
            "_score": 5024.55,
            "_monitor_lanes": ["brand"],
            "_monitor_route_hints": ["x-engage/brand"],
        }

        decisions = qualify_and_route_signals([risky_routed_signal], self._accounts())

        assert decisions[0]["selected"] is False
        assert decisions[0]["skipped_reason"] == "negative_filter_match"

# ── write_pending_actions ────────────────────────────────────────────────────

class TestWritePendingActions:
    def test_writes_file(self, tmp_path):
        out = tmp_path / "pending.json"
        accounts = get_accounts(SAMPLE_CONFIG)
        items = build_queue_items([TWEET_A], accounts)
        write_pending_actions(items, str(out))
        assert out.exists()

    def test_written_items_have_all_fields(self, tmp_path):
        out = tmp_path / "pending.json"
        accounts = get_accounts(SAMPLE_CONFIG)
        items = build_queue_items([TWEET_A], accounts)
        write_pending_actions(items, str(out))
        data = json.loads(out.read_text())
        for item in data:
            assert "lane" in item
            assert "account_id" in item
            assert "source" in item

    def test_deduplicates_by_tweet_id_and_account_id(self, tmp_path):
        out = tmp_path / "pending.json"
        accounts = get_accounts(SAMPLE_CONFIG)
        items = build_queue_items([TWEET_A], accounts)
        write_pending_actions(items, str(out))
        write_pending_actions(items, str(out))   # second call = same items
        data = json.loads(out.read_text())
        assert len(data) == 2  # 1 tweet × 2 accounts, not 4

    def test_merges_new_tweets_with_existing(self, tmp_path):
        out = tmp_path / "pending.json"
        accounts = [SAMPLE_CONFIG["x_accounts"][0]]
        items_a = build_queue_items([TWEET_A], accounts)
        items_b = build_queue_items([TWEET_B], accounts)
        write_pending_actions(items_a, str(out))
        write_pending_actions(items_b, str(out))
        data = json.loads(out.read_text())
        assert len(data) == 2
        tweet_ids = {d["tweet_id"] for d in data}
        assert "tweet_001" in tweet_ids
        assert "tweet_002" in tweet_ids

    def test_same_tweet_different_accounts_both_kept(self, tmp_path):
        out = tmp_path / "pending.json"
        items_p = build_queue_items([TWEET_A], [SAMPLE_CONFIG["x_accounts"][0]])
        items_b = build_queue_items([TWEET_A], [SAMPLE_CONFIG["x_accounts"][1]])
        write_pending_actions(items_p, str(out))
        write_pending_actions(items_b, str(out))
        data = json.loads(out.read_text())
        assert len(data) == 2
        account_ids = {d["account_id"] for d in data}
        assert "personal" in account_ids
        assert "brand" in account_ids

    def test_queue_summary_records_filter_run_metadata(self, tmp_path):
        out = tmp_path / "pending.json"
        accounts = [
            {"id": "brand", "handle": "desearch_ai", "label": "@desearch_ai", "lane": "research", "action_types": ["quote"]},
        ]
        tweets = [
            {**TWEET_A, "id": "brand-fit", "text": "desearch SN22 AI search API launch", "_score": 900.0},
            {**TWEET_A, "id": "skip-fit", "text": "generic viral entertainment update", "_score": 9000.0},
        ]
        decisions = qualify_and_route_signals(tweets, accounts)
        items = build_queue_items(tweets, accounts, qualification_decisions=decisions)
        filter_summary = summarize_filter_decisions(decisions)

        summary = write_pending_actions(
            items,
            str(out),
            run_metadata={
                "filter_criteria": {"stage": "filter_route_before_draft"},
                "filter_summary": filter_summary,
                "skipped_filter_decisions": [d for d in decisions if not d["selected"]],
            },
        )

        assert summary["filter_criteria"] == {"stage": "filter_route_before_draft"}
        assert summary["filter_summary"]["selected"] == 1
        assert summary["filter_summary"]["skipped"] == 1
        assert summary["skipped_filter_decisions"][0]["skipped_reason"] == "no_account_strategy_match"


# ── managed runtime contract ───────────────────────────────────────────────────

class TestManagedRuntimeAccounts:
    def test_prefers_managed_runtime_accounts_over_local_config(self, tmp_path, monkeypatch):
        runtime_path = tmp_path / "runtime.json"
        runtime_path.write_text(json.dumps({
            "send_window_start": "08:00",
            "send_window_end": "22:00",
            "send_window_tz": "Asia/Tbilisi",
            "lane_routing": {
                "founder": ["cosmic_desearch"],
                "brand": ["desearch_ai"],
            },
            "session_mappings": {
                "cosmic_desearch": "founder-session",
                "desearch_ai": "brand-session",
            },
            "engage_check_interval_seconds": 3600,
        }))
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(SAMPLE_CONFIG))
        monkeypatch.setenv("X_ENGAGE_RUNTIME_PATH", str(runtime_path))
        monkeypatch.setenv("X_ENGAGE_CONFIG", str(config_path))
        monkeypatch.setenv("X_ENGAGE_BROWSER_PROFILE_ROOT", str(tmp_path / "profiles"))

        cfg = load_config()
        accounts = get_accounts(cfg)

        assert cfg["runtime_source"] == "managed_file"
        assert [(a["id"], a["lane"]) for a in accounts] == [
            ("personal", "founder"),
            ("brand", "brand"),
        ]
        assert accounts[0]["browser_profile"].endswith("founder-session")
        assert accounts[1]["browser_profile"].endswith("brand-session")

    def test_managed_runtime_can_introduce_handle_without_local_fallback(self, tmp_path, monkeypatch):
        runtime_path = tmp_path / "runtime.json"
        runtime_path.write_text(json.dumps({
            "lane_routing": {"research": ["new_handle"]},
            "session_mappings": {"new_handle": "research-session"},
            "send_window": {"start": "08:00", "end": "22:00", "tz": "UTC"},
            "rate_limits": {"max_posts_per_day": 10, "max_replies_per_day": 20},
            "check_interval_seconds": 3600,
        }))
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(SAMPLE_CONFIG))
        monkeypatch.setenv("X_ENGAGE_RUNTIME_PATH", str(runtime_path))
        monkeypatch.setenv("X_ENGAGE_CONFIG", str(config_path))
        monkeypatch.setenv("X_ENGAGE_BROWSER_PROFILE_ROOT", str(tmp_path / "profiles"))

        accounts = get_accounts(load_config())

        assert len(accounts) == 1
        assert accounts[0]["id"] == "new_handle"
        assert accounts[0]["handle"] == "new_handle"
        assert accounts[0]["label"] == "@new_handle"
        assert accounts[0]["lane"] == "research"

# ── Social OS telemetry + queue lifecycle ─────────────────────────────────────

class TestQueueTelemetrySchema:
    def test_queue_item_includes_social_os_lifecycle_fields(self):
        accounts = get_accounts(SAMPLE_CONFIG)
        items = build_queue_items([TWEET_A], accounts, criteria={"score_weights": SAMPLE_CONFIG["score_weights"]})
        item = items[0]

        assert item["source_signal_id"] == "tweet_001"
        assert item["source_signal_url"] == "https://x.com/user/status/001"
        assert item["monitored_source"]["username"] == "testuser"
        assert item["target_account"]["id"] == item["account_id"]
        assert item["target_account"]["lane"] == item["lane"]
        assert item["rationale"]
        assert item["priority"] in {"high", "medium", "low"}
        assert "risk_notes" in item
        assert "duplicate_notes" in item
        assert "generated_at" in item
        assert item["status"] == "pending"
        assert item["generation_criteria"]["score_weights"] == SAMPLE_CONFIG["score_weights"]
        assert item["generation_fingerprint"]

    def test_write_pending_actions_returns_counts_and_keeps_rejected_out_when_unchanged(self, tmp_path):
        out = tmp_path / "pending.json"
        accounts = [SAMPLE_CONFIG["x_accounts"][0]]
        items = build_queue_items([TWEET_A], accounts, criteria={"score_weights": SAMPLE_CONFIG["score_weights"]})
        first = write_pending_actions(items, str(out))
        assert first["created"] == 1

        data = json.loads(out.read_text())
        data[0]["status"] = "rejected"
        data[0]["review_notes"] = "not relevant"
        out.write_text(json.dumps(data))

        second = write_pending_actions(items, str(out))
        updated = json.loads(out.read_text())
        assert second["created"] == 0
        assert second["refreshed"] == 0
        assert second["skipped"] == 1
        assert updated[0]["status"] == "rejected"
        assert updated[0]["review_notes"] == "not relevant"

    def test_write_pending_actions_reopens_rejected_when_criteria_materially_change(self, tmp_path):
        out = tmp_path / "pending.json"
        accounts = [SAMPLE_CONFIG["x_accounts"][0]]
        original = build_queue_items([TWEET_A], accounts, criteria={"score_weights": SAMPLE_CONFIG["score_weights"]})
        write_pending_actions(original, str(out))
        data = json.loads(out.read_text())
        data[0]["status"] = "rejected"
        out.write_text(json.dumps(data))

        changed = build_queue_items([{**TWEET_A, "_score": 999.0}], accounts, criteria={"score_weights": {"likes": 99}})
        summary = write_pending_actions(changed, str(out))
        updated = json.loads(out.read_text())
        assert summary["refreshed"] == 1
        assert summary["skipped"] == 0
        assert updated[0]["status"] == "pending"
        assert updated[0]["previous_status"] == "rejected"


# ── Social OS review-row writer ───────────────────────────────────────────────

def _make_response(status_code=201, json_data=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data if json_data is not None else []
    resp.raise_for_status.return_value = None
    return resp


class TestSocialOSReviewRows:
    def _queue_items(self):
        accounts = [SAMPLE_CONFIG["x_accounts"][0]]
        return build_queue_items([TWEET_A], accounts, criteria={"score_weights": SAMPLE_CONFIG["score_weights"]})

    def test_queue_item_payload_has_required_social_os_fields(self):
        items = self._queue_items()
        item = items[0]
        assert item["source_signal_id"] == "tweet_001"
        assert item["source_signal_url"] == "https://x.com/user/status/001"
        assert "account_id" in item
        assert "account_handle" in item
        assert "lane" in item
        assert "action_types" in item
        assert item["generation_fingerprint"]
        assert item["rationale"]

    def test_creates_rows_without_manual_seeding(self, monkeypatch):
        """Normal analyzer run produces Social OS review rows; no manual DB seeding needed."""
        monkeypatch.setenv("SOCIAL_OS_SUPABASE_URL", "https://example.supabase.co")
        monkeypatch.setenv("SOCIAL_OS_SUPABASE_KEY", "service-key")

        get_calls = []
        post_calls = []

        def fake_get(url, headers, params, timeout):
            get_calls.append(params)
            return _make_response(200, [])  # no existing rows

        def fake_post(url, headers, json, timeout):
            post_calls.append(json)
            return _make_response(201)

        monkeypatch.setattr(runtime_loader.requests, "get", fake_get)
        monkeypatch.setattr(runtime_loader.requests, "post", fake_post)

        items = self._queue_items()
        summary = write_social_os_review_rows(items)

        assert summary["created"] == 1
        assert summary["refreshed"] == 0
        assert summary["skipped"] == 0
        assert len(post_calls) == 1
        inserted = post_calls[0][0]
        assert inserted["platform"] == "x"
        assert inserted["status"] == "draft"
        assert inserted["created_by"] == "x-engage-analyzer"
        assert "tweet_001" in inserted["angle"]
        assert "https://x.com/user/status/001" in inserted["content"]
        assert inserted["content"].count("[x-engage/") == 1

    def test_dedupes_inactive_row_with_same_generation_fingerprint(self, monkeypatch):
        """Rejected/approved row with same fingerprint is skipped, not duplicated."""
        monkeypatch.setenv("SOCIAL_OS_SUPABASE_URL", "https://example.supabase.co")
        monkeypatch.setenv("SOCIAL_OS_SUPABASE_KEY", "service-key")

        items = self._queue_items()
        fingerprint = items[0]["generation_fingerprint"]
        angle = f"x-engage:tweet_001:{items[0]['account_id']}"
        existing_content = f"some content [x-engage/{fingerprint}]"

        def fake_get(url, headers, params, timeout):
            return _make_response(200, [
                {"id": "row-uuid-1", "angle": angle, "status": "rejected", "content": existing_content},
            ])

        post_calls = []
        patch_calls = []
        monkeypatch.setattr(runtime_loader.requests, "get", fake_get)
        monkeypatch.setattr(runtime_loader.requests, "post", lambda *a, **kw: post_calls.append(True) or _make_response(201))
        monkeypatch.setattr(runtime_loader.requests, "patch", lambda *a, **kw: patch_calls.append(True) or _make_response(200))

        summary = write_social_os_review_rows(items)

        assert summary["skipped"] == 1
        assert summary["created"] == 0
        assert summary["refreshed"] == 0
        assert not post_calls
        assert not patch_calls

    def test_refreshes_inactive_row_when_generation_fingerprint_changes(self, monkeypatch):
        """Rejected row reopened as draft when source signal or criteria materially changed."""
        monkeypatch.setenv("SOCIAL_OS_SUPABASE_URL", "https://example.supabase.co")
        monkeypatch.setenv("SOCIAL_OS_SUPABASE_KEY", "service-key")

        items = self._queue_items()
        angle = f"x-engage:tweet_001:{items[0]['account_id']}"
        stale_content = "old content [x-engage/0000000000000000]"

        def fake_get(url, headers, params, timeout):
            return _make_response(200, [
                {"id": "row-uuid-2", "angle": angle, "status": "rejected", "content": stale_content},
            ])

        patch_calls = []

        def fake_patch(url, headers, params, json, timeout):
            patch_calls.append({"params": params, "json": json})
            return _make_response(200)

        monkeypatch.setattr(runtime_loader.requests, "get", fake_get)
        monkeypatch.setattr(runtime_loader.requests, "post", lambda *a, **kw: _make_response(201))
        monkeypatch.setattr(runtime_loader.requests, "patch", fake_patch)

        summary = write_social_os_review_rows(items)

        assert summary["refreshed"] == 1
        assert summary["skipped"] == 0
        assert summary["created"] == 0
        assert patch_calls[0]["json"]["status"] == "draft"
        assert "https://x.com/user/status/001" in patch_calls[0]["json"]["content"]

    def test_refreshes_draft_row_without_changing_status(self, monkeypatch):
        """Existing draft row gets updated content without toggling its status."""
        monkeypatch.setenv("SOCIAL_OS_SUPABASE_URL", "https://example.supabase.co")
        monkeypatch.setenv("SOCIAL_OS_SUPABASE_KEY", "service-key")

        items = self._queue_items()
        angle = f"x-engage:tweet_001:{items[0]['account_id']}"

        def fake_get(url, headers, params, timeout):
            return _make_response(200, [
                {"id": "row-uuid-3", "angle": angle, "status": "draft", "content": "old content"},
            ])

        patch_calls = []

        def fake_patch(url, headers, params, json, timeout):
            patch_calls.append({"params": params, "json": json})
            return _make_response(200)

        monkeypatch.setattr(runtime_loader.requests, "get", fake_get)
        monkeypatch.setattr(runtime_loader.requests, "post", lambda *a, **kw: _make_response(201))
        monkeypatch.setattr(runtime_loader.requests, "patch", fake_patch)

        summary = write_social_os_review_rows(items)

        assert summary["refreshed"] == 1
        assert summary["created"] == 0
        assert "status" not in patch_calls[0]["json"]
        assert "content" in patch_calls[0]["json"]


    def test_rejected_row_with_operator_feedback_regenerates_pending_review_draft(self, monkeypatch):
        """Rejected/commented Social OS feedback creates a new pending review draft with lineage."""
        monkeypatch.setenv("SOCIAL_OS_SUPABASE_URL", "https://example.supabase.co")
        monkeypatch.setenv("SOCIAL_OS_SUPABASE_KEY", "service-key")

        items = self._queue_items()
        item = items[0]
        base_angle = f"x-engage:tweet_001:{item['account_id']}"
        rejected_row = {
            "id": "row-rejected-1",
            "angle": base_angle,
            "status": "rejected",
            "approval_status": "rejected",
            "content": f"old review text [x-engage/{item['generation_fingerprint']}]",
            "rejection_reason": "Too generic — make it more technical and mention validator relevance.",
            "regenerated_from_post_id": None,
            "metadata": {},
        }

        get_calls = []
        post_calls = []
        patch_calls = []

        def fake_get(url, headers, params, timeout):
            get_calls.append(params)
            if len(get_calls) == 1:
                return _make_response(200, [rejected_row])
            return _make_response(200, [])

        def fake_post(url, headers, json, timeout):
            post_calls.append(json)
            return _make_response(201)

        def fake_patch(url, headers, params, json, timeout):
            patch_calls.append({"params": params, "json": json})
            return _make_response(200)

        monkeypatch.setattr(runtime_loader.requests, "get", fake_get)
        monkeypatch.setattr(runtime_loader.requests, "post", fake_post)
        monkeypatch.setattr(runtime_loader.requests, "patch", fake_patch)

        summary = write_social_os_review_rows(items)

        assert summary["created"] == 1
        assert summary["regenerated"] == 1
        assert summary["refreshed"] == 0
        assert summary["skipped"] == 0
        assert len(post_calls) == 1
        assert not patch_calls

        regenerated = post_calls[0][0]
        assert regenerated["status"] == "draft"
        assert regenerated["approval_status"] == "pending"
        assert regenerated["angle"].startswith(f"{base_angle}:regen:")
        assert regenerated["source_signal_id"] == "tweet_001"
        assert regenerated["account_handle"] == item["account_handle"]
        assert regenerated["lane"] == item["lane"]
        assert "Operator feedback" in regenerated["content"]
        assert "Too generic" in regenerated["content"]
        assert regenerated["regenerated_from_post_id"] == "row-rejected-1"
        assert regenerated["metadata"]["regenerated_from_post_id"] == "row-rejected-1"
        assert regenerated["metadata"]["operator_feedback"] == rejected_row["rejection_reason"]

    def test_rejected_feedback_regeneration_is_idempotent(self, monkeypatch):
        """Retrying the same rejected/commented row does not create duplicate regen drafts."""
        monkeypatch.setenv("SOCIAL_OS_SUPABASE_URL", "https://example.supabase.co")
        monkeypatch.setenv("SOCIAL_OS_SUPABASE_KEY", "service-key")

        items = self._queue_items()
        item = items[0]
        base_angle = f"x-engage:tweet_001:{item['account_id']}"
        rejected_row = {
            "id": "row-rejected-1",
            "angle": base_angle,
            "status": "commented",
            "approval_status": "pending",
            "content": f"old review text [x-engage/{item['generation_fingerprint']}]",
            "review_notes": "Rewrite with sharper founder voice.",
        }

        def fake_get(url, headers, params, timeout):
            if params["angle"].startswith(f"in.({base_angle}:regen:"):
                existing_regen_angle = params["angle"][4:-1]
                return _make_response(200, [{"id": "row-regen-1", "angle": existing_regen_angle}])
            return _make_response(200, [rejected_row])

        post_calls = []
        monkeypatch.setattr(runtime_loader.requests, "get", fake_get)
        monkeypatch.setattr(runtime_loader.requests, "post", lambda *a, **kw: post_calls.append(True) or _make_response(201))

        summary = write_social_os_review_rows(items)

        assert summary["created"] == 0
        assert summary["regenerated"] == 0
        assert summary["skipped"] == 1
        assert not post_calls

    def test_returns_zero_counts_when_no_supabase_env(self, monkeypatch):
        """Gracefully no-ops when Supabase credentials are absent."""
        monkeypatch.delenv("SOCIAL_OS_SUPABASE_URL", raising=False)
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        monkeypatch.delenv("VITE_SUPABASE_URL", raising=False)
        monkeypatch.delenv("SOCIAL_OS_SUPABASE_KEY", raising=False)
        monkeypatch.delenv("SOCIAL_OS_SUPABASE_ANON_KEY", raising=False)
        monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)
        monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
        monkeypatch.delenv("VITE_SUPABASE_ANON_KEY", raising=False)

        items = self._queue_items()
        summary = write_social_os_review_rows(items)

        assert summary["created"] == 0
        assert summary["refreshed"] == 0
        assert summary["skipped"] == 0

    def test_emits_telemetry_on_query_failure(self, monkeypatch):
        """GET failure emits error telemetry rather than propagating the exception."""
        monkeypatch.setenv("SOCIAL_OS_SUPABASE_URL", "https://example.supabase.co")
        monkeypatch.setenv("SOCIAL_OS_SUPABASE_KEY", "service-key")

        def boom(*a, **kw):
            raise RuntimeError("connection refused")

        telemetry = []

        def fake_post(url, headers, json, timeout):
            if "social_runtime_events" in url:
                telemetry.append(json)
            return _make_response(201)

        monkeypatch.setattr(runtime_loader.requests, "get", boom)
        monkeypatch.setattr(runtime_loader.requests, "post", fake_post)

        items = self._queue_items()
        summary = write_social_os_review_rows(items)

        assert summary["created"] == 0
        assert "error" in summary
        assert any("dedup query failed" in str(t) for t in telemetry)
