#!/usr/bin/env python3
"""
x-engage: X Action Executor

Reads pending_actions.json, finds items with status=approved AND explicit
MC approval (approval_status='approved' + approval_url), and executes
the requested action (retweet / quote tweet) via Playwright on x.com.

APPROVAL CONTRACT:
- Live execution REQUIRES explicit per-post approval from Mission Control
- Required fields:
  - status: "approved"
  - approval_status: "approved" (explicit, not implied)
  - approval_url: URL of approval (provenance for audit)
- Posts lacking approval_status='approved' are rejected at the door

Schema expected in pending_actions.json:
{
  "tweet_id":   "123",
  "tweet_url":  "https://x.com/user/status/123",
  "tweet_text": "...",
  "author":     "username",
  "action":     "retweet" | "quote",
  "quote_text": "...",          # required for action=quote
  "status":     "pending" | "approved" | "done" | "skipped" | "failed",
  "approval_status": "approved",   # REQUIRED for live execution
  "approval_url": "...",            # REQUIRED for live execution
  "approved_by": "...",             # recommended for audit
  "timestamp":  "2024-...",
  "executed_at": "..."              # set by this script after execution
}

Usage:
    uv run python execute_actions.py              # Execute all approved actions
    uv run python execute_actions.py --dry-run    # Print what would happen, no browsing
"""

import argparse
import asyncio
import fcntl
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
try:
    from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
except ModuleNotFoundError:
    async_playwright = None

    class PlaywrightTimeoutError(Exception):
        pass

# ─────────────────────────────────────────────
# Config & Constants
# ─────────────────────────────────────────────

load_dotenv()

SCRIPT_DIR = Path(__file__).parent
PENDING_ACTIONS_PATH = Path(
    os.environ.get("PENDING_ACTIONS_PATH", SCRIPT_DIR / "pending_actions.json")
)
PENDING_ACTIONS_LOCK_NAME = ".pending_actions.lock"
DISCORD_CHANNEL_ID = "1477727527618347340"
LOCK_FILE = SCRIPT_DIR / ".executor.lock"
_LEGACY_LOCK_HANDLE = None

_DEFAULT_BROWSER_PROFILE = Path.home() / ".x-engage-browser-profile" / "default"

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
# Approval validation
# ─────────────────────────────────────────────

def validate_action_approval(item: dict) -> tuple[bool, str]:
    """
    Validate that an action item has explicit Mission Control approval.
    
    Required for LIVE execution:
    - approval_status must be "approved" (exact string match)
    - approval_url must be present (provenance link)
    - approved_by is recommended but optional
    
    Returns (is_valid, reason_string).
    """
    approval_status = item.get("approval_status", "").strip().lower() if item.get("approval_status") else ""
    approval_url = item.get("approval_url", "").strip()
    approved_by = item.get("approved_by", "").strip()
    
    if not approval_status:
        return False, "missing approval_status - live execution requires explicit MC per-post approval"
    
    if approval_status != "approved":
        return False, f"approval_status is '{approval_status}', not 'approved' - live execution blocked"
    
    if not approval_url:
        return False, "missing approval_url - cannot verify approval provenance for audit"
    
    if not approved_by:
        print(f"  [warn] approved_by not set - audit trail will be incomplete", file=sys.stderr)
    
    return True, f"approved by {approved_by or 'unknown'} (URL: {approval_url})"


# ─────────────────────────────────────────────
# Account helpers
# ─────────────────────────────────────────────

def get_browser_profile(cfg: dict, account_id: str) -> Path:
    """
    Resolve the browser profile path for a given account_id.
    Expands ~ and returns an absolute Path.
    Falls back to _DEFAULT_BROWSER_PROFILE if account not found.
    """
    for acct in cfg.get("x_accounts", []):
        if acct.get("id") == account_id:
            raw = acct.get("browser_profile", "")
            if raw:
                return Path(raw).expanduser().resolve()
    return _DEFAULT_BROWSER_PROFILE


# ─────────────────────────────────────────────
# pending_actions.json helpers
# ─────────────────────────────────────────────

def acquire_lock() -> bool:
    """Backward-compatible lock helper retained for existing tests/tooling."""
    global _LEGACY_LOCK_HANDLE
    if _LEGACY_LOCK_HANDLE is not None:
        return False
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    handle = LOCK_FILE.open("w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return False
    handle.write(str(os.getpid()))
    handle.flush()
    _LEGACY_LOCK_HANDLE = handle
    return True


def release_lock() -> None:
    global _LEGACY_LOCK_HANDLE
    if _LEGACY_LOCK_HANDLE is None:
        try:
            LOCK_FILE.unlink()
        except FileNotFoundError:
            pass
        return
    _LEGACY_LOCK_HANDLE.close()
    _LEGACY_LOCK_HANDLE = None
    try:
        LOCK_FILE.unlink()
    except FileNotFoundError:
        pass


def acquire_queue_lock(queue_path: Path):
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = queue_path.parent / PENDING_ACTIONS_LOCK_NAME
    handle = lock_path.open("w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise RuntimeError(f"pending_actions queue busy, lock held at {lock_path}")
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


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
    """
    Return items that are approved and have a valid action type.
    
    NOTE: This only filters by status='approved' and valid action types.
    Full approval validation (approval_status + approval_url) is done
    at execution time via validate_action_approval().
    """
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
        # Include approval provenance for audit
        if item.get("approval_url"):
            lines.append(f"📋 Approval: {item['approval_url']}")
    else:
        lines = [
            f"❌ **{action_type.capitalize()} FAILED** for @{author}",
            f"> {tweet_snip}",
            f"🔗 <{tweet_url}>",
            f"⚠️ `{error_msg[:200]}`",
            f"🕐 {now_str}",
        ]
        if item.get("approval_url"):
            lines.append(f"📋 Approval: {item['approval_url']}")

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
    Groups approved actions by account_id and opens a separate browser context
    per account using its configured profile directory.
    
    CRITICAL: Validates explicit MC approval (approval_status='approved' + approval_url)
    before any live execution. Items without valid approval are rejected at the door.
    """
    try:
        from analyze import load_config
        cfg = load_config()
    except Exception:
        cfg = {}

    lock_handle = acquire_queue_lock(PENDING_ACTIONS_PATH)
    try:
        actions = load_actions()
        approved = get_approved(actions)

        if not approved:
            print("[executor] No approved actions found in pending_actions.json.")
            return 0

        # Filter out items without explicit MC approval (for both dry-run and live)
        # In dry-run mode, we show what would be rejected; in live mode, we reject
        explicitly_approved = []
        rejected_for_approval = []
        
        for a in approved:
            is_valid, reason = validate_action_approval(a)
            if is_valid:
                explicitly_approved.append(a)
            else:
                rejected_for_approval.append((a, reason))

        print(f"[executor] Found {len(approved)} approved action(s) total:")
        for a in approved:
            acct = a.get("account_id", "?")
            print(f"  • [{a['action'].upper()}] @{a.get('author','?')} ({acct}) — {a['tweet_url']}")

        if rejected_for_approval:
            print(f"\n[executor] ⚠️ {len(rejected_for_approval)} item(s) rejected (no explicit MC approval):")
            for a, reason in rejected_for_approval:
                print(f"  ❌ @{a.get('author','?')} ({a.get('account_id','?')}): {reason}")

        if not explicitly_approved:
            print("\n[executor] No items with valid explicit MC approval. Exiting.")
            if not dry_run:
                # Mark rejected items in the queue
                for a, reason in rejected_for_approval:
                    a["status"] = "approval_rejected"
                    a["error"] = reason
                    a["rejected_at"] = datetime.now(timezone.utc).isoformat()
                save_actions(actions)
            return 0 if dry_run else 1

        print(f"\n[executor] ✓ {len(explicitly_approved)} item(s) with valid explicit MC approval:")
        for a in explicitly_approved:
            acct = a.get("account_id", "?")
            approval_url = a.get("approval_url", "")
            print(f"  ✅ [{a['action'].upper()}] @{a.get('author','?')} ({acct})")
            print(f"      Approval: {approval_url[:60]}..." if len(approval_url) > 60 else f"      Approval: {approval_url}")

        if dry_run:
            print("\n[dry-run] Actions that would be executed:")
            for a in explicitly_approved:
                acct = a.get("account_id", "?")
                print(f"  → {a['action'].upper()} tweet {a['tweet_id']} by @{a.get('author','?')} as {acct}")
                if a["action"] == "quote":
                    qt = a.get("quote_text", "")
                    print(f"     quote_text: {qt[:120]}{'…' if len(qt) > 120 else ''}")
                print(f"     approval_url: {a.get('approval_url', 'NONE')}")
            print("[dry-run] Done (no browser launched).")
            return 0

        if async_playwright is None:
            raise RuntimeError("playwright is not installed. Run `uv run playwright install chromium` after syncing dependencies.")

        bot_token = os.environ.get("DISCORD_BOT_TOKEN", "")
        if not bot_token:
            print("[warn] DISCORD_BOT_TOKEN not set — Discord confirmations will be skipped.", file=sys.stderr)

        any_failed = False
        account_groups: dict[str, list[dict]] = {}
        for item in actions:
            if item.get("status") != "approved" or item.get("action") not in ("retweet", "quote"):
                continue
            # Only include items with valid explicit approval
            is_valid, _ = validate_action_approval(item)
            if not is_valid:
                continue
            acct_id = item.get("account_id", "default")
            account_groups.setdefault(acct_id, []).append(item)

        async with async_playwright() as p:
            for acct_id, acct_items in account_groups.items():
                profile_dir = get_browser_profile(cfg, acct_id)
                profile_dir.mkdir(parents=True, exist_ok=True)
                print(f"\n[executor] Account '{acct_id}' — profile: {profile_dir}")

                context = await p.chromium.launch_persistent_context(
                    str(profile_dir),
                    headless=False,
                    viewport={"width": 1280, "height": 800},
                    locale="en-US",
                    args=["--no-sandbox"],
                )
                page = context.pages[0] if context.pages else await context.new_page()

                try:
                    await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
                    await page.wait_for_timeout(2000)
                    if "login" in page.url.lower() or "signin" in page.url.lower():
                        print(
                            f"\n⚠️  Account '{acct_id}' is not logged into X.com!\n"
                            "   The browser will remain open.\n"
                            "   Please log in manually, then re-run this script.\n",
                            file=sys.stderr,
                        )
                        await context.close()
                        any_failed = True
                        continue
                except Exception as exc:
                    print(f"[warn] Could not check X.com login status for '{acct_id}': {exc}", file=sys.stderr)

                for item in acct_items:
                    tweet_id = item.get("tweet_id", "?")
                    tweet_url = item.get("tweet_url", "")
                    action_type = item.get("action")
                    quote_text = item.get("quote_text", "")
                    author = item.get("author", "?")

                    print(f"\n[executor] ── Processing: {action_type.upper()} @{author} ({tweet_id}) as '{acct_id}' ──")
                    print(f"  Approval: {item.get('approval_url', 'NONE')}")

                    success = False
                    error_msg = ""

                    try:
                        item["status"] = "executing"
                        item["execution_started_at"] = datetime.now(timezone.utc).isoformat()
                        save_actions(actions)

                        if action_type == "retweet":
                            await execute_retweet(page, tweet_url)
                        elif action_type == "quote":
                            if not quote_text:
                                raise ValueError("action=quote requires a non-empty quote_text field")
                            await execute_quote(page, tweet_url, quote_text)

                        item["status"] = "done"
                        item["executed_at"] = datetime.now(timezone.utc).isoformat()
                        success = True
                        print(f"[executor] ✓ {action_type} completed for tweet {tweet_id}")
                    except Exception as exc:
                        error_msg = str(exc)
                        item["status"] = "failed"
                        item["error"] = error_msg
                        item["failed_at"] = datetime.now(timezone.utc).isoformat()
                        any_failed = True
                        print(f"[executor] ✗ {action_type} FAILED for {tweet_id}: {error_msg}", file=sys.stderr)

                    save_actions(actions)
                    post_confirmation(bot_token, item, success, error_msg)

                    remaining = any(
                        a.get("status") == "approved" and a.get("action") in ("retweet", "quote")
                        for a in acct_items
                    )
                    if remaining:
                        await page.wait_for_timeout(BETWEEN_ACTIONS)

                await context.close()

        # Handle rejected items in the queue
        for a, reason in rejected_for_approval:
            a["status"] = "approval_rejected"
            a["error"] = reason
            a["rejected_at"] = datetime.now(timezone.utc).isoformat()
        save_actions(actions)

        total = len(explicitly_approved)
        done = sum(1 for a in actions if a.get("status") == "done")
        failed = sum(1 for a in actions if a.get("status") == "failed")
        rejected = len(rejected_for_approval)
        print(f"\n[executor] Summary: {done} done, {failed} failed, {rejected} rejected (no approval) out of {len(approved)} total approved actions.")
        return 1 if any_failed else 0
    finally:
        lock_handle.close()

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
