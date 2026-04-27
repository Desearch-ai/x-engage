#!/usr/bin/env python3
"""
Post original tweets via Playwright using existing browser profiles.

REQUIREMENTS FOR LIVE POSTING:
- Each post MUST carry explicit approval metadata from Mission Control
- Required fields for live execution:
  - approval_status: "approved" (not implied, not inferred)
  - approval_url: URL of the MC approval message/link
  - approved_by: who approved it
- Posts without explicit approval_status='approved' are rejected at the door

Usage:
    python3 post_tweet.py --account personal --text "Hello world"
    python3 post_tweet.py --account brand --text "New feature: ..."
"""

import asyncio
import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from runtime_loader import load_config

SCRIPT_DIR = Path(__file__).parent

# X.com selectors
SEL_COMPOSE = '[data-testid="tweetTextarea_0"]'
SEL_POST = '[data-testid="tweetButtonInline"]'
SEL_HOME = '[data-testid="primaryColumn"]'

# Send-window definitions
_DAY_MAP = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}
_TZ_MAP = {"Tbilisi": "Asia/Tbilisi"}
_PERIOD_HOURS = {"morning": (8, 12), "afternoon": (12, 18)}


def _parse_hhmm(value: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"(\d{2}):(\d{2})", value.strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return hour, minute


def _is_within_structured_send_window(send_window: dict, now: datetime | None = None) -> tuple[bool, str]:
    if now is None:
        now = datetime.now(timezone.utc)

    start = _parse_hhmm(str(send_window.get("start", "")))
    end = _parse_hhmm(str(send_window.get("end", "")))
    tz_name = str(send_window.get("tz", "UTC")).strip() or "UTC"
    if not start or not end:
        return True, f"unparseable send window {send_window!r} — allowing by default"

    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        return True, f"unknown timezone '{tz_name}' — allowing by default"

    local_now = now.astimezone(tz)
    current_minute = local_now.hour * 60 + local_now.minute
    start_minute = start[0] * 60 + start[1]
    end_minute = end[0] * 60 + end[1]

    if start_minute < end_minute:
        allowed = start_minute <= current_minute < end_minute
    else:
        allowed = current_minute >= start_minute or current_minute < end_minute

    if not allowed:
        return (
            False,
            f"outside send window ({start[0]:02d}:{start[1]:02d}–{end[0]:02d}:{end[1]:02d} {tz_name}); "
            f"current time: {local_now.strftime('%H:%M')}",
        )

    return True, f"within window ({local_now.strftime('%a %H:%M')} {tz_name})"


def is_within_send_window(send_window: str | dict, now: datetime | None = None) -> tuple[bool, str]:
    """
    Check whether `now` falls inside a send-window.

    Supports both:
    - managed runtime shape: {"start": "08:00", "end": "22:00", "tz": "Asia/Tbilisi"}
    - legacy human string: "Tue-Thu morning Tbilisi"
    """
    if isinstance(send_window, dict):
        return _is_within_structured_send_window(send_window, now=now)

    if now is None:
        now = datetime.now(timezone.utc)

    m = re.match(r"(\w+)-(\w+)\s+(\w+)\s+(\w+)", send_window.strip())
    if not m:
        return True, f"unparseable window '{send_window}' — allowing by default"

    day_start_str, day_end_str, period, tz_name = m.groups()

    tz_key = _TZ_MAP.get(tz_name, tz_name)
    try:
        tz = ZoneInfo(tz_key)
    except Exception:
        return True, f"unknown timezone '{tz_name}' — allowing by default"

    local_now = now.astimezone(tz)
    cur_day = local_now.weekday()  # 0=Mon … 6=Sun

    start_day = _DAY_MAP.get(day_start_str)
    end_day = _DAY_MAP.get(day_end_str)
    if start_day is None or end_day is None:
        return True, f"unknown day names '{day_start_str}'/'{day_end_str}' — allowing by default"

    if start_day <= end_day:
        day_ok = start_day <= cur_day <= end_day
    else:
        day_ok = cur_day >= start_day or cur_day <= end_day

    hour_range = _PERIOD_HOURS.get(period, (0, 24))
    hour_ok = hour_range[0] <= local_now.hour < hour_range[1]

    day_label = local_now.strftime("%a")
    time_label = local_now.strftime("%H:%M")

    if not day_ok:
        return (
            False,
            f"outside allowed days ({day_start_str}–{day_end_str}); "
            f"current day: {day_label} ({tz_name})",
        )
    if not hour_ok:
        return (
            False,
            f"outside {period} window "
            f"({hour_range[0]:02d}:00–{hour_range[1]:02d}:00 {tz_name}); "
            f"current time: {time_label}",
        )

    return True, f"within window ({day_label} {time_label} {tz_name})"


def resolve_send_window(send_window: str | dict | None) -> str | dict | None:
    if send_window is not None:
        return send_window
    cfg = load_config()
    return cfg.get("send_window")


def get_profile_path(account_id: str) -> Path:
    """Get browser profile path for account, preferring the managed runtime contract."""
    cfg = load_config()

    for acct in cfg.get("x_accounts", []):
        if acct.get("id") == account_id or acct.get("handle") == account_id:
            raw = acct.get("browser_profile", "")
            if raw:
                return Path(raw).expanduser().resolve()

    xengage_path = Path.home() / ".x-engage-browser" / account_id
    if xengage_path.exists():
        return xengage_path

    chrome_path = Path.home() / "Library/Application Support/Google/Chrome/Default"
    if chrome_path.exists():
        return chrome_path

    xengage_path.mkdir(parents=True, exist_ok=True)
    return xengage_path


def validate_approval(item: dict) -> tuple[bool, str]:
    """
    Validate that the post has explicit Mission Control approval.

    Required for LIVE execution:
    - approval_status must be "approved" (exact string match)
    - approval_url must be present (provenance link)
    - approved_by should be present but is optional

    Returns (is_valid, reason_string).
    """
    approval_status = (item.get("approval_status") or "").strip().lower()
    approval_url = (item.get("approval_url") or "").strip()
    approved_by = (item.get("approved_by") or "").strip()

    if not approval_status:
        return False, "missing approval_status field - live execution requires explicit MC per-post approval"

    if approval_status != "approved":
        return False, f"approval_status is '{approval_status}', not 'approved' - live execution blocked"

    if not approval_url:
        return False, "missing approval_url - cannot verify approval provenance for audit"

    if not approved_by:
        print("  [warn] approved_by not set - audit trail will be incomplete", file=sys.stderr)

    return True, f"approved by {approved_by or 'unknown'} (URL: {approval_url})"


async def post_tweet(
    account_id: str,
    text: str,
    dry_run: bool = False,
    send_window: str | dict | None = None,
    approval_status: str | None = None,
    approval_url: str | None = None,
    approved_by: str | None = None,
) -> dict:
    """Post an original tweet. Returns result dict.

    Args:
        account_id: Account identifier (e.g. 'personal', 'brand').
        text: Tweet text (truncated to 280 chars if longer).
        dry_run: If True, simulate without opening a browser.
        send_window: Optional explicit override. If omitted, the managed Social OS
            runtime contract is consulted before any fallback.
        approval_status: Explicit approval status from MC (required for live).
        approval_url: URL of approval (required for live).
        approved_by: Who approved (recommended for audit).
    """
    item = {
        "approval_status": approval_status,
        "approval_url": approval_url,
        "approved_by": approved_by,
    }

    if not dry_run:
        is_valid, reason = validate_approval(item)
        print(f"  Approval check: {reason}")
        if not is_valid:
            return {
                "account_id": account_id,
                "text": text[:280] if len(text) > 280 else text,
                "status": "approval_rejected",
                "error": reason,
                "approval_status": approval_status,
                "approval_url": approval_url,
                "approved_by": approved_by,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

    profile_dir = get_profile_path(account_id)

    print(f"Using profile: {profile_dir}")
    print(f"Profile exists: {profile_dir.exists()}")

    result = {
        "account_id": account_id,
        "text": text[:280] if len(text) > 280 else text,
        "status": "pending",
        "posted_url": None,
        "external_post_id": None,
        "error": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "approval_status": approval_status,
        "approval_url": approval_url,
        "approved_by": approved_by,
    }

    if dry_run:
        result["status"] = "dry_run"
        print(f"[dry-run] Would post: {text[:80]}...")
        return result

    effective_send_window = resolve_send_window(send_window)
    if effective_send_window:
        allowed, reason = is_within_send_window(effective_send_window)
        print(f"  Send-window check: {reason}")
        if not allowed:
            result["status"] = "window_blocked"
            result["error"] = f"Send-window blocked: {reason}"
            print(f"  BLOCKED — {reason}")
            return result

    try:
        from playwright.async_api import async_playwright
    except ModuleNotFoundError:
        result["status"] = "failed"
        result["error"] = "playwright not installed. Run: pip install playwright && playwright install chromium"
        return result

    async with async_playwright() as p:
        try:
            context = await p.chromium.launch_persistent_context(
                str(profile_dir),
                headless=False,
                channel="chrome",
                viewport={"width": 1280, "height": 800},
                locale="en-US",
                args=["--no-sandbox"],
            )
        except Exception as e:
            result["status"] = "failed"
            result["error"] = f"Browser launch failed: {e}"
            print(f"Error launching browser: {e}")
            return result

        page = context.pages[0] if context.pages else await context.new_page()

        try:
            await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)

            current_url = page.url.lower()
            if "login" in current_url or "signin" in current_url:
                result["status"] = "failed"
                result["error"] = f"Not logged in - please log in at {current_url}"
                print(f"Not logged in. URL: {page.url}")
                await context.close()
                return result

            print(f"Logged in, URL: {page.url}")

            try:
                await page.wait_for_selector(SEL_COMPOSE, timeout=15000)
            except Exception:
                await page.click('[data-testid="SideNav_NewTweet_Button"]')
                await page.wait_for_timeout(2000)
                await page.wait_for_selector(SEL_COMPOSE, timeout=10000)

            await page.fill(SEL_COMPOSE, text)
            print(f"Filled tweet text: {text[:50]}...")

            await page.click(SEL_POST)
            print("Clicked post button")

            await page.wait_for_timeout(5000)

            final_url = page.url
            print(f"Final URL: {final_url}")

            if "/status/" in final_url:
                result["posted_url"] = final_url
                parts = final_url.split("/status/")
                if len(parts) > 1:
                    result["external_post_id"] = parts[-1].split("?")[0]

            result["status"] = "posted"
            print(f"Successfully posted! URL: {result.get('posted_url')}")

        except Exception as e:
            result["status"] = "failed"
            result["error"] = str(e)
            print(f"Error posting: {e}")
            try:
                await page.screenshot(path=f"/tmp/post_error_{account_id}.png")
                result["screenshot"] = f"/tmp/post_error_{account_id}.png"
            except Exception:
                pass

        await context.close()

    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Post an original tweet")
    parser.add_argument("--account", default="personal", help="Account ID (personal/brand)")
    parser.add_argument("--text", required=True, help="Tweet text")
    parser.add_argument("--dry-run", action="store_true", help="Dry run only")
    parser.add_argument("--send-window", default=None, help="Send window spec, e.g. 'Tue-Thu morning Tbilisi'")
    parser.add_argument("--approval-status", default=None, help="Approval status from MC (required for live)")
    parser.add_argument("--approval-url", default=None, help="Approval URL from MC (required for live)")
    parser.add_argument("--approved-by", default=None, help="Who approved (recommended)")
    args = parser.parse_args()

    result = asyncio.run(post_tweet(
        args.account,
        args.text,
        args.dry_run,
        args.send_window,
        args.approval_status,
        args.approval_url,
        args.approved_by,
    ))
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["status"] in ("posted", "dry_run") else 1)
