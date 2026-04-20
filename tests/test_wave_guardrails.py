"""Tests for the dedup ledger and send-window guardrails."""
import asyncio
import json
import pytest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import post_tweet as pt
import run_validation_wave as rw


# ── is_within_send_window ─────────────────────────────────────────────────────

class TestSendWindow:
    def _tbilisi_datetime(self, weekday: int, hour: int) -> datetime:
        """Build an aware datetime in Tbilisi TZ for the given weekday and hour."""
        tz = ZoneInfo("Asia/Tbilisi")
        # Find any date with the right weekday (start from 2026-04-20 = Monday)
        from datetime import timedelta
        base = datetime(2026, 4, 20, hour, 0, 0, tzinfo=tz)  # Monday
        delta = (weekday - base.weekday()) % 7
        return base + timedelta(days=delta)

    def test_inside_window_morning(self):
        # Tue morning Tbilisi — weekday=1, hour=9
        now = self._tbilisi_datetime(1, 9)
        ok, reason = pt.is_within_send_window("Tue-Thu morning Tbilisi", now=now)
        assert ok, reason

    def test_inside_window_afternoon(self):
        # Thu afternoon — weekday=3, hour=14
        now = self._tbilisi_datetime(3, 14)
        ok, reason = pt.is_within_send_window("Thu-Sat afternoon Tbilisi", now=now)
        assert ok, reason

    def test_outside_day(self):
        # Mon morning — outside Tue-Thu
        now = self._tbilisi_datetime(0, 9)
        ok, reason = pt.is_within_send_window("Tue-Thu morning Tbilisi", now=now)
        assert not ok
        assert "day" in reason.lower()

    def test_outside_hour_too_early(self):
        # Tue 6am — before morning window
        now = self._tbilisi_datetime(1, 6)
        ok, reason = pt.is_within_send_window("Tue-Thu morning Tbilisi", now=now)
        assert not ok
        assert "morning" in reason.lower()

    def test_outside_hour_too_late(self):
        # Wed 14:00 — after morning window ends at 12:00
        now = self._tbilisi_datetime(2, 14)
        ok, reason = pt.is_within_send_window("Tue-Thu morning Tbilisi", now=now)
        assert not ok

    def test_week_wrap_sat_mon_inside(self):
        # Sat afternoon inside Sat-Mon range
        now = self._tbilisi_datetime(5, 13)
        ok, reason = pt.is_within_send_window("Sat-Mon afternoon Tbilisi", now=now)
        assert ok, reason

    def test_week_wrap_sun_inside(self):
        # Sun afternoon inside Sat-Mon range
        now = self._tbilisi_datetime(6, 13)
        ok, reason = pt.is_within_send_window("Sat-Mon afternoon Tbilisi", now=now)
        assert ok, reason

    def test_week_wrap_mon_inside(self):
        # Mon afternoon inside Sat-Mon range
        now = self._tbilisi_datetime(0, 13)
        ok, reason = pt.is_within_send_window("Sat-Mon afternoon Tbilisi", now=now)
        assert ok, reason

    def test_week_wrap_outside(self):
        # Tue afternoon outside Sat-Mon range
        now = self._tbilisi_datetime(1, 13)
        ok, reason = pt.is_within_send_window("Sat-Mon afternoon Tbilisi", now=now)
        assert not ok

    def test_unparseable_window_allows(self):
        ok, reason = pt.is_within_send_window("anytime", now=datetime.now(timezone.utc))
        assert ok
        assert "unparseable" in reason.lower()


# ── post_tweet window_blocked path ───────────────────────────────────────────

class TestPostTweetWindowGuard:
    def _outside_window_datetime(self) -> datetime:
        """Return a datetime that is Mon 3am Tbilisi — outside every batch window."""
        tz = ZoneInfo("Asia/Tbilisi")
        return datetime(2026, 4, 20, 3, 0, 0, tzinfo=tz)  # Monday 03:00

    def test_blocks_outside_window(self):
        """post_tweet with a send_window outside current time returns window_blocked."""
        # Patch is_within_send_window to always return blocked
        with patch.object(pt, "is_within_send_window", return_value=(False, "outside test window")):
            result = asyncio.run(
                pt.post_tweet("personal", "test tweet", dry_run=False, send_window="Mon-Mon morning Tbilisi")
            )
        assert result["status"] == "window_blocked"
        assert "window" in result["error"].lower()

    def test_dry_run_bypasses_window(self):
        """dry_run should return dry_run status even if window check would block."""
        result = asyncio.run(
            pt.post_tweet("personal", "test tweet", dry_run=True, send_window="Tue-Thu morning Tbilisi")
        )
        assert result["status"] == "dry_run"

    def test_no_window_does_not_block(self):
        """When send_window is None, is_within_send_window is never called."""
        # playwright is lazy-imported, so when it's not installed the function returns
        # "failed" with a playwright error — we just verify the window check isn't invoked.
        with patch.object(pt, "is_within_send_window") as mock_check:
            result = asyncio.run(
                pt.post_tweet("personal", "test tweet", dry_run=False, send_window=None)
            )
        mock_check.assert_not_called()
        # Should fail at playwright import (not window check) — status is not window_blocked
        assert result["status"] != "window_blocked"


# ── Ledger helpers ────────────────────────────────────────────────────────────

class TestLedger:
    def test_load_missing_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rw, "LEDGER_FILE", tmp_path / "no_ledger.json")
        assert rw.load_ledger() == {}

    def test_save_then_load_roundtrip(self, tmp_path, monkeypatch):
        path = tmp_path / "ledger.json"
        monkeypatch.setattr(rw, "LEDGER_FILE", path)
        data = {"validation-wave-1:1": {"posted_url": "https://x.com/x/status/1", "external_post_id": "1", "timestamp": "2026-04-20T00:00:00+00:00"}}
        rw.save_ledger(data)
        loaded = rw.load_ledger()
        assert loaded == data

    def test_save_is_atomic(self, tmp_path, monkeypatch):
        path = tmp_path / "ledger.json"
        monkeypatch.setattr(rw, "LEDGER_FILE", path)
        rw.save_ledger({"k": "v"})
        assert path.exists()
        assert not (tmp_path / "ledger.json.tmp").exists()

    def test_ledger_key_format(self):
        assert rw._ledger_key("validation-wave-1", 3) == "validation-wave-1:3"


# ── run_wave dedup integration ────────────────────────────────────────────────

class TestRunWaveDedup:
    @pytest.fixture
    def batch_file(self, tmp_path):
        batch = [
            {
                "batch_id": "test-wave",
                "order": 1,
                "account_id": "personal",
                "account_handle": "@test",
                "lane": "founder",
                "pillar": "test",
                "text": "Hello world",
                "source_label": "test",
                "approval_origin": "test",
                "approval_url": "https://example.com",
                "send_window": None,
            }
        ]
        f = tmp_path / "batch.json"
        f.write_text(json.dumps(batch))
        return f

    def test_skips_already_posted_item(self, tmp_path, batch_file, monkeypatch):
        """Items in the ledger must be skipped without calling post_tweet."""
        ledger_file = tmp_path / "ledger.json"
        results_file = tmp_path / "results.json"
        monkeypatch.setattr(rw, "BATCH_FILE", batch_file)
        monkeypatch.setattr(rw, "LEDGER_FILE", ledger_file)
        monkeypatch.setattr(rw, "RESULTS_FILE", results_file)

        # Pre-populate the ledger with order=1
        rw.save_ledger({"test-wave:1": {"posted_url": "https://x.com/x/status/999", "external_post_id": "999", "timestamp": "2026-01-01T00:00:00+00:00"}})

        call_count = {"n": 0}

        async def mock_post(account_id, text, dry_run=False, send_window=None):
            call_count["n"] += 1
            return {"account_id": account_id, "text": text, "status": "posted", "posted_url": None, "external_post_id": None, "error": None, "timestamp": "2026-01-01T00:00:00+00:00"}

        monkeypatch.setattr(rw, "post_tweet", mock_post)

        asyncio.run(rw.run_wave(dry_run=False))

        assert call_count["n"] == 0, "post_tweet should not be called for already-posted items"
        results = json.loads(results_file.read_text())
        assert results[0]["status"] == "skipped_duplicate"

    def test_first_run_posts_and_records(self, tmp_path, batch_file, monkeypatch):
        """First run with empty ledger should post and add item to ledger."""
        ledger_file = tmp_path / "ledger.json"
        results_file = tmp_path / "results.json"
        monkeypatch.setattr(rw, "BATCH_FILE", batch_file)
        monkeypatch.setattr(rw, "LEDGER_FILE", ledger_file)
        monkeypatch.setattr(rw, "RESULTS_FILE", results_file)

        async def mock_post(account_id, text, dry_run=False, send_window=None):
            return {"account_id": account_id, "text": text, "status": "posted", "posted_url": "https://x.com/x/status/42", "external_post_id": "42", "error": None, "timestamp": "2026-01-01T00:00:00+00:00"}

        monkeypatch.setattr(rw, "post_tweet", mock_post)

        asyncio.run(rw.run_wave(dry_run=False))

        ledger = rw.load_ledger()
        assert "test-wave:1" in ledger
        assert ledger["test-wave:1"]["external_post_id"] == "42"

    def test_window_blocked_not_added_to_ledger(self, tmp_path, batch_file, monkeypatch):
        """window_blocked items must NOT be added to the ledger so they can retry."""
        ledger_file = tmp_path / "ledger.json"
        results_file = tmp_path / "results.json"
        monkeypatch.setattr(rw, "BATCH_FILE", batch_file)
        monkeypatch.setattr(rw, "LEDGER_FILE", ledger_file)
        monkeypatch.setattr(rw, "RESULTS_FILE", results_file)

        async def mock_post(account_id, text, dry_run=False, send_window=None):
            return {"account_id": account_id, "text": text, "status": "window_blocked", "posted_url": None, "external_post_id": None, "error": "Send-window blocked: outside allowed days", "timestamp": "2026-01-01T00:00:00+00:00"}

        monkeypatch.setattr(rw, "post_tweet", mock_post)

        asyncio.run(rw.run_wave(dry_run=False))

        ledger = rw.load_ledger()
        assert "test-wave:1" not in ledger

    def test_dry_run_does_not_modify_ledger(self, tmp_path, batch_file, monkeypatch):
        """Dry runs must never write to the ledger."""
        ledger_file = tmp_path / "ledger.json"
        results_file = tmp_path / "results.json"
        monkeypatch.setattr(rw, "BATCH_FILE", batch_file)
        monkeypatch.setattr(rw, "LEDGER_FILE", ledger_file)
        monkeypatch.setattr(rw, "RESULTS_FILE", results_file)

        async def mock_post(account_id, text, dry_run=False, send_window=None):
            return {"account_id": account_id, "text": text, "status": "dry_run", "posted_url": None, "external_post_id": None, "error": None, "timestamp": "2026-01-01T00:00:00+00:00"}

        monkeypatch.setattr(rw, "post_tweet", mock_post)

        asyncio.run(rw.run_wave(dry_run=True))

        assert not ledger_file.exists()
