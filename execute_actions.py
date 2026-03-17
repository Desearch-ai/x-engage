#!/usr/bin/env python3
"""
x-engage: X Action Executor
Reads pending_actions.json, finds items with status=approved, and executes
the requested action (retweet / quote tweet) via Playwright on x.com.

Schema expected in pending_actions.json:
{
  "tweet_id":   "123",
  "tweet_url":  "https://x.com/user/status/123",
  "tweet_text": "...",
  "author":     "username",
  "action":     "retweet" | "quote",
  "quote_text": "...",          # required for action=quote
  "status":     "pending" | "approved" | "done" | "skipped" | "failed",
  "timestamp":  "2024-...",
  "executed_at": "..."          # set by this script after execution
}

Usage:
    uv run python execute_actions.py              # Execute all approved actions
    uv run python execute_actions.py --dry-run    # Print what would happen, no browsing
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# ─────────────────────────────────────────────
# Config & Constants
# ─────────────────────────────────────────────

load_dotenv()

SCRIPT_DIR = Path(__file__).parent
PENDING_ACTIONS_PATH = Path(
    os.environ.get("PENDING_ACTIONS_PATH", SCRIPT_DIR / "pending_actions.json")
)
BROWSER_PROFILE_DIR = Path(
    os.environ.get("X_BROWSER_PROFILE", Path.home() / ".x-engage-browser-profile")
)
DISCORD_CHANNEL_ID = "1477727527618347340"

# Timeouts (ms)
PAGE_LOAD_TIMEOUT = 30_000    # 30s to load a tweet page
ELEMENT_TIMEOUT   = 20_000    # 20s to find an element
POST_ACTION_WAIT  = 2_000     # 2s cool-down after each action
BETWEEN_ACTIONS   = 3_000     # 3s between consecutive actions

# ─────────────────────────────────────────────
# x.com Playwright Selectors
# ─────────────────────────────────────────────
# These target stable data-testid attributes from the X.com React codebase.
# If X changes the DOM, update these selectors.

SEL_RETWEET_BTN     = '[data-testid="retweet"]'         # retweet/repost icon on the tweet
SEL_RETWEET_CONFIRM = '[data-testid="retweetConfirm"]'  # "Repost" button in popup
SEL_QUOTE_OPTION    = '[data-testid="quoteTweet"]'      # "Quote" option in the retweet popup
SEL_TWEET_TEXTAREA  = '[data-testid="tweetTextarea_0"]' # compose box after clicking Quote
SEL_TWEET_SUBMIT    = '[data-testid="tweetButtonInline"]'  # "Post" submit button

# ─────────────────────────────────────────────
# pending_actions.json helpers
# ─────────────────────────────────────────────

def load_actions() -> list[dict]:
    """Load pending_actions.json; returns [] if missing or unreadable."""
    if not PENDING_ACTIONS_PATH.exists():
        print(f"[executor] pending_actions.json not found at {PENDING_ACTIONS_PATH}", file=sys.stderr)
        return []
    try:
        return json.loads(PENDING_ACTIONS_PATH.read_text())
    except json.JSONDecodeError as exc:
        print(f"[executor] JSON parse error: {exc}", file=sys.stderr)
        return []


def save_actions(actions: list[dict]) -> None:
    """Write the (mutated) actions list back to disk atomically."""
    tmp = PENDING_ACTIONS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(actions, indent=2, ensure_ascii=False))
    tmp.replace(PENDING_ACTIONS_PATH)


def get_approved(actions: list[dict]) -> list[dict]:
    """Return items that are approved and have a valid action type."""
    return [
        a for a in actions
        if a.get("status") == "approved" and a.get("action") in ("retweet", "quote")
    ]

# ─────────────────────────────────────────────
# Discord
# ─────────────────────────────────────────────

def _discord_post(bot_token: str, content: str) -> None:
    """POST a single message to the x-alerts Discord channel."""
    if len(content) > 2000:
        content = content[:1997] + "…"
    url = f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages"
    headers = {
        "Authorization": f"Bot {bot_token}",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(url, headers=headers, json={"content": content}, timeout=10)
        if resp.ok:
            print(f"[discord] Posted confirmation (len={len(content)})")
        else:
            print(f"[discord] Failed: {resp.status_code} {resp.text[:200]}", file=sys.stderr)
    except requests.RequestException as exc:
        print(f"[discord] Request error: {exc}", file=sys.stderr)


def post_confirmation(bot_token: str, item: dict, success: bool, error_msg: str = "") -> None:
    """Send a success or failure confirmation to Discord after an action."""
    if not bot_token:
        return

    action_type = item.get("action", "?")
    author      = item.get("author", "?")
    tweet_url   = item.get("tweet_url", "")
    tweet_snip  = (item.get("tweet_text", "")[:120] or "").replace("\n", " ")
    now_str     = datetime.now(timezone.utc).strftime("%H:%M UTC")

    if success:
        if action_type == "retweet":
            header = f"✅ **Reposted** @{author}'s tweet"
        else:
            quote_snip = (item.get("quote_text", "")[:100] or "").replace("\n", " ")
            header = f"✅ **Quoted** @{author}'s tweet\n💬 _{quote_snip}_"
        lines = [
            header,
            f"> {tweet_snip}",
            f"🔗 <{tweet_url}>",
            f"🕐 {now_str}",
        ]
    else:
        lines = [
            f"❌ **{action_type.capitalize()} FAILED** for @{author}",
            f"> {tweet_snip}",
            f"🔗 <{tweet_url}>",
            f"⚠️ `{error_msg[:200]}`",
            f"🕐 {now_str}",
        ]

    _discord_post(bot_token, "\n".join(lines))

# ─────────────────────────────────────────────
# Browser helpers
# ─────────────────────────────────────────────

async def _wait_and_click(page, selector: str, description: str, timeout: int = ELEMENT_TIMEOUT) -> None:
    """Wait for `selector` to appear, then click it. Raises on timeout."""
    try:
        await page.wait_for_selector(selector, timeout=timeout)
        await page.click(selector)
    except PlaywrightTimeoutError:
        raise RuntimeError(f"Timed out waiting for '{description}' ({selector})")


async def _goto_tweet(page, url: str) -> None:
    """Navigate to a tweet URL and wait for the page to be interactive."""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
        # Give React a moment to fully hydrate
        await page.wait_for_timeout(2000)
    except PlaywrightTimeoutError:
        raise RuntimeError(f"Timed out loading tweet page: {url}")


async def execute_retweet(page, tweet_url: str) -> None:
    """
    Retweet / Repost a tweet.

    Flow:
      1. Navigate to tweet URL
      2. Click the retweet icon (↺)
      3. Click "Repost" in the popup to confirm
    """
    await _goto_tweet(page, tweet_url)

    # Step 1: Click the retweet icon
    await _wait_and_click(page, SEL_RETWEET_BTN, "Retweet icon")

    # Step 2: Confirm "Repost" in the popup
    await _wait_and_click(page, SEL_RETWEET_CONFIRM, "Repost confirm button")

    # Let the request settle
    await page.wait_for_timeout(POST_ACTION_WAIT)


async def execute_quote(page, tweet_url: str, quote_text: str) -> None:
    """
    Quote-tweet a tweet with pre-generated quote_text.

    Flow:
      1. Navigate to tweet URL
      2. Click the retweet icon (↺) → popup opens
      3. Click "Quote" option in the popup
      4. Wait for the compose textarea
      5. Fill in quote_text
      6. Click "Post" / submit button
    """
    await _goto_tweet(page, tweet_url)

    # Step 1: Open the retweet popup
    await _wait_and_click(page, SEL_RETWEET_BTN, "Retweet icon (for quote)")

    # Step 2: Choose "Quote" from the popup
    await _wait_and_click(page, SEL_QUOTE_OPTION, "Quote option")

    # Step 3: Wait for the compose textarea to appear
    try:
        await page.wait_for_selector(SEL_TWEET_TEXTAREA, timeout=ELEMENT_TIMEOUT)
    except PlaywrightTimeoutError:
        raise RuntimeError(f"Compose textarea did not appear ({SEL_TWEET_TEXTAREA})")

    # Step 4: Type the quote text (click first to ensure focus)
    await page.click(SEL_TWEET_TEXTAREA)
    await page.fill(SEL_TWEET_TEXTAREA, quote_text)

    # Step 5: Submit
    await _wait_and_click(page, SEL_TWEET_SUBMIT, "Post button")

    # Let the request settle
    await page.wait_for_timeout(POST_ACTION_WAIT)

# ─────────────────────────────────────────────
# Main executor
# ─────────────────────────────────────────────

async def run_executor(dry_run: bool = False) -> int:
    """
    Main entry point.
    Returns exit code: 0 = success (or nothing to do), 1 = one or more failures.
    """
    actions = load_actions()
    approved = get_approved(actions)

    if not approved:
        print("[executor] No approved actions found in pending_actions.json.")
        return 0

    print(f"[executor] Found {len(approved)} approved action(s):")
    for a in approved:
        print(f"  • [{a['action'].upper()}] @{a.get('author','?')} — {a['tweet_url']}")

    # ── Dry-run: print + exit ─────────────────────────────────────────────
    if dry_run:
        print("\n[dry-run] Actions that would be executed:")
        for a in approved:
            print(f"  → {a['action'].upper()} tweet {a['tweet_id']} by @{a.get('author','?')}")
            if a["action"] == "quote":
                qt = a.get("quote_text", "")
                print(f"     quote_text: {qt[:120]}{'…' if len(qt) > 120 else ''}")
        print("[dry-run] Done (no browser launched).")
        return 0

    # ── Live execution ────────────────────────────────────────────────────
    bot_token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not bot_token:
        print("[warn] DISCORD_BOT_TOKEN not set — Discord confirmations will be skipped.", file=sys.stderr)

    BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[executor] Using browser profile: {BROWSER_PROFILE_DIR}")

    any_failed = False

    async with async_playwright() as p:
        # Persistent context keeps X.com login across runs.
        # First time: user must log in manually in the opened browser window.
        context = await p.chromium.launch_persistent_context(
            str(BROWSER_PROFILE_DIR),
            headless=False,
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            args=["--no-sandbox"],
        )
        # Reuse the first tab if any, otherwise open a new one
        page = context.pages[0] if context.pages else await context.new_page()

        # Quick check: are we logged into X?
        try:
            await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
            await page.wait_for_timeout(2000)
            if "login" in page.url.lower() or "signin" in page.url.lower():
                print(
                    "\n⚠️  Not logged into X.com!\n"
                    "   The browser will remain open.\n"
                    "   Please log in manually, then re-run this script.\n",
                    file=sys.stderr,
                )
                await context.close()
                return 1
        except Exception as exc:
            print(f"[warn] Could not check X.com login status: {exc}", file=sys.stderr)

        for item in actions:
            # Only process items that are still approved (skip already-processed)
            if item.get("status") != "approved" or item.get("action") not in ("retweet", "quote"):
                continue

            tweet_id   = item.get("tweet_id", "?")
            tweet_url  = item.get("tweet_url", "")
            action_type = item.get("action")
            quote_text  = item.get("quote_text", "")
            author      = item.get("author", "?")

            print(f"\n[executor] ── Processing: {action_type.upper()} @{author} ({tweet_id}) ──")

            success    = False
            error_msg  = ""

            try:
                if action_type == "retweet":
                    await execute_retweet(page, tweet_url)
                elif action_type == "quote":
                    if not quote_text:
                        raise ValueError("action=quote requires a non-empty quote_text field")
                    await execute_quote(page, tweet_url, quote_text)

                # Mark as done
                item["status"]      = "done"
                item["executed_at"] = datetime.now(timezone.utc).isoformat()
                success = True
                print(f"[executor] ✓ {action_type} completed for tweet {tweet_id}")

            except Exception as exc:
                error_msg = str(exc)
                item["status"] = "failed"
                item["error"]  = error_msg
                item["failed_at"] = datetime.now(timezone.utc).isoformat()
                any_failed = True
                print(f"[executor] ✗ {action_type} FAILED for {tweet_id}: {error_msg}", file=sys.stderr)

            # Persist state after every action so partial progress is saved
            save_actions(actions)

            # Discord confirmation (success or failure)
            post_confirmation(bot_token, item, success, error_msg)

            # Polite delay between actions
            if any(
                a.get("status") == "approved" and a.get("action") in ("retweet", "quote")
                for a in actions
            ):
                await page.wait_for_timeout(BETWEEN_ACTIONS)

        await context.close()

    total   = len(approved)
    done    = sum(1 for a in actions if a.get("status") == "done")
    failed  = sum(1 for a in actions if a.get("status") == "failed")
    print(f"\n[executor] Summary: {done} done, {failed} failed out of {total} approved actions.")
    return 1 if any_failed else 0

# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="x-engage: Execute approved X actions (retweet / quote tweet) via Playwright"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print actions that would be executed without launching any browser",
    )
    args = parser.parse_args()

    try:
        exit_code = asyncio.run(run_executor(dry_run=args.dry_run))
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\n[executor] Interrupted by user.", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(f"[executor] Fatal error: {exc}", file=sys.stderr)
        sys.exit(1)
