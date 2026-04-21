"""Tests for execute_actions.py — approval validation and per-account browser profile resolution."""
import pytest
from pathlib import Path

# Import from execute_actions module
import execute_actions
from execute_actions import (
    get_browser_profile, 
    load_actions, 
    save_actions, 
    get_approved,
    validate_action_approval,
)


SAMPLE_CONFIG = {
    "x_accounts": [
        {
            "id": "personal",
            "label": "@cosmic_desearch (founder)",
            "handle": "cosmic_desearch",
            "lane": "founder",
            "browser_profile": "~/.x-engage-browser/personal",
        },
        {
            "id": "brand",
            "label": "@desearch_ai (brand)",
            "handle": "desearch_ai",
            "lane": "brand",
            "browser_profile": "~/.x-engage-browser/brand",
        },
    ]
}


# ── validate_action_approval ─────────────────────────────────────────────────

class TestValidateActionApproval:
    """Tests for explicit MC per-post approval validation."""

    def test_approved_with_all_fields(self):
        """Valid approval with all fields present."""
        item = {
            "approval_status": "approved",
            "approval_url": "https://discord.com/channels/.../1234567890",
            "approved_by": "Giga",
        }
        is_valid, reason = validate_action_approval(item)
        assert is_valid is True
        assert "Giga" in reason
        assert "discord.com" in reason

    def test_missing_approval_status(self):
        """Rejects when approval_status is missing."""
        item = {
            "approval_url": "https://discord.com/channels/.../1234567890",
        }
        is_valid, reason = validate_action_approval(item)
        assert is_valid is False
        assert "missing approval_status" in reason

    def test_approval_status_not_approved(self):
        """Rejects when approval_status is not 'approved'."""
        item = {
            "approval_status": "pending",
            "approval_url": "https://discord.com/channels/.../1234567890",
        }
        is_valid, reason = validate_action_approval(item)
        assert is_valid is False
        assert "pending" in reason

    def test_missing_approval_url(self):
        """Rejects when approval_url is missing (no provenance)."""
        item = {
            "approval_status": "approved",
            # approval_url missing
        }
        is_valid, reason = validate_action_approval(item)
        assert is_valid is False
        assert "missing approval_url" in reason

    def test_approval_status_case_insensitive(self):
        """Accepts 'Approved' (uppercase) as valid."""
        item = {
            "approval_status": "Approved",
            "approval_url": "https://discord.com/channels/.../1234567890",
        }
        is_valid, reason = validate_action_approval(item)
        assert is_valid is True

    def test_empty_approval_status(self):
        """Rejects empty approval_status."""
        item = {
            "approval_status": "",
            "approval_url": "https://discord.com/channels/.../1234567890",
        }
        is_valid, reason = validate_action_approval(item)
        assert is_valid is False

    def test_approved_by_optional(self):
        """approved_by is optional but warns if missing."""
        item = {
            "approval_status": "approved",
            "approval_url": "https://discord.com/channels/.../1234567890",
            # approved_by missing
        }
        is_valid, reason = validate_action_approval(item)
        assert is_valid is True  # Should still pass


# ── get_browser_profile ───────────────────────────────────────────────────────

class TestGetBrowserProfile:
    """Tests for per-account browser profile resolution."""

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
        monkeypatch.setattr(execute_actions, "PENDING_ACTIONS_PATH", tmp_path / "nonexistent.json")
        actions = load_actions()
        assert actions == []

    def test_save_then_load(self, tmp_path, monkeypatch):
        path = tmp_path / "pending.json"
        monkeypatch.setattr(execute_actions, "PENDING_ACTIONS_PATH", path)
        original = [{"tweet_id": "1", "action": "retweet", "status": "approved"}]
        save_actions(original)
        loaded = load_actions()
        assert loaded == original

    def test_save_is_atomic(self, tmp_path, monkeypatch):
        """save_actions writes a tmp file then replaces — no partial writes."""
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
