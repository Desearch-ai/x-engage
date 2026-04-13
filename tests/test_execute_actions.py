"""Tests for execute_actions.py — per-account browser profile resolution."""
import pytest
from pathlib import Path
from execute_actions import get_browser_profile, load_actions, save_actions, get_approved

SAMPLE_CONFIG = {
    "x_accounts": [
        {
            "id": "personal",
            "label": "@cosmicquantum (personal)",
            "lane": "founder",
            "browser_profile": "~/.x-engage-browser/personal",
        },
        {
            "id": "brand",
            "label": "@desearch_ai (brand)",
            "lane": "brand",
            "browser_profile": "~/.x-engage-browser/brand",
        },
    ]
}


# ── get_browser_profile ───────────────────────────────────────────────────────

class TestGetBrowserProfile:
    def test_personal_profile(self):
        p = get_browser_profile(SAMPLE_CONFIG, "personal")
        assert str(p).endswith("personal")

    def test_brand_profile(self):
        p = get_browser_profile(SAMPLE_CONFIG, "brand")
        assert str(p).endswith("brand")

    def test_tilde_expanded(self):
        p = get_browser_profile(SAMPLE_CONFIG, "personal")
        assert "~" not in str(p)
        assert str(p).startswith("/")

    def test_returns_path_object(self):
        p = get_browser_profile(SAMPLE_CONFIG, "personal")
        assert isinstance(p, Path)

    def test_unknown_account_returns_default(self):
        p = get_browser_profile(SAMPLE_CONFIG, "nonexistent")
        assert p is not None
        assert isinstance(p, Path)

    def test_empty_config_returns_default(self):
        p = get_browser_profile({}, "personal")
        assert p is not None


# ── load_actions / save_actions / get_approved ────────────────────────────────

class TestLoadSaveActions:
    def test_returns_empty_list_when_file_missing(self, tmp_path, monkeypatch):
        import execute_actions
        monkeypatch.setattr(execute_actions, "PENDING_ACTIONS_PATH", tmp_path / "nonexistent.json")
        actions = load_actions()
        assert actions == []

    def test_save_then_load(self, tmp_path, monkeypatch):
        import execute_actions
        path = tmp_path / "pending.json"
        monkeypatch.setattr(execute_actions, "PENDING_ACTIONS_PATH", path)
        original = [{"tweet_id": "1", "action": "retweet", "status": "approved"}]
        save_actions(original)
        loaded = load_actions()
        assert loaded == original

    def test_save_is_atomic(self, tmp_path, monkeypatch):
        """save_actions writes a tmp file then replaces — no partial writes."""
        import execute_actions
        path = tmp_path / "pending.json"
        monkeypatch.setattr(execute_actions, "PENDING_ACTIONS_PATH", path)
        save_actions([{"tweet_id": "1"}])
        assert path.exists()
        assert not (path.parent / "pending.json.tmp").exists()


class TestGetApproved:
    def test_filters_approved_retweet(self):
        actions = [
            {"status": "approved", "action": "retweet"},
            {"status": "pending",  "action": "retweet"},
            {"status": "done",     "action": "retweet"},
        ]
        assert len(get_approved(actions)) == 1

    def test_filters_approved_quote(self):
        actions = [{"status": "approved", "action": "quote"}]
        assert len(get_approved(actions)) == 1

    def test_excludes_invalid_action_types(self):
        actions = [{"status": "approved", "action": "like"}]
        assert get_approved(actions) == []

    def test_empty_list(self):
        assert get_approved([]) == []
