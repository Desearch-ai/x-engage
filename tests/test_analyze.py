"""Tests for analyze.py — multi-account queue generation."""
import json
import pytest
from pathlib import Path
from analyze import (
    score_tweet,
    get_top_tweets,
    get_accounts,
    build_queue_items,
    write_pending_actions,
    _username,
)

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
