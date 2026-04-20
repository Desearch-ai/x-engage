#!/usr/bin/env python3
"""
Validation Wave 1 Executor
Runs the approved pilot batch end-to-end with full traceability.

Rerun safety:
  - A durable ledger (posted_ledger.json) records every successfully posted item
    by its (batch_id, order) key.  On rerun, items already in the ledger are
    skipped — they are never re-attempted.
  - Each item's send_window is enforced before any real publish attempt.  Items
    outside the window receive status='window_blocked' and are NOT added to the
    ledger so they will be retried in the next valid window.
"""
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from post_tweet import post_tweet

BATCH_FILE = Path(__file__).parent / "validation_wave_1_batch.json"
RESULTS_FILE = Path(__file__).parent / "validation_wave_1_results.json"
LEDGER_FILE = Path(__file__).parent / "posted_ledger.json"


def _ledger_key(batch_id: str, order: int) -> str:
    return f"{batch_id}:{order}"


def load_ledger() -> dict:
    """Load the durable posted-tweet ledger.  Returns a dict keyed by 'batch_id:order'."""
    if LEDGER_FILE.exists():
        try:
            return json.loads(LEDGER_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_ledger(ledger: dict) -> None:
    """Atomically write the ledger to disk."""
    tmp = LEDGER_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(ledger, indent=2, ensure_ascii=False))
    tmp.replace(LEDGER_FILE)


async def run_wave(dry_run=False):
    batch = json.loads(BATCH_FILE.read_text())
    ledger = load_ledger()
    results = []

    print(f"=== Validation Wave 1 ===")
    print(f"Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    print(f"Batch size: {len(batch)} posts")
    print(f"Ledger entries: {len(ledger)} already posted")
    print(f"Started: {datetime.now(timezone.utc).isoformat()}")
    print()

    for item in batch:
        order = item["order"]
        account = item["account_id"]
        text = item["text"]
        handle = item["account_handle"]
        send_window = item.get("send_window")
        key = _ledger_key(item["batch_id"], order)

        print(f"[{order}/4] Posting as {handle} ({account})...")
        print(f"  Text: {text[:80]}...")
        print(f"  Source: {item['source_label']}")
        print(f"  Pillar: {item['pillar']}")
        if send_window:
            print(f"  Window: {send_window}")

        # Dedup check: skip if already successfully posted
        if key in ledger and not dry_run:
            print(f"  SKIPPED — already posted (ledger key: {key})")
            results.append({
                "account_id": account,
                "text": text,
                "status": "skipped_duplicate",
                "posted_url": ledger[key].get("posted_url"),
                "external_post_id": ledger[key].get("external_post_id"),
                "error": None,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "batch_id": item["batch_id"],
                "order": order,
                "account_handle": handle,
                "lane": item["lane"],
                "pillar": item["pillar"],
                "source_label": item["source_label"],
                "approval_origin": item["approval_origin"],
                "approval_url": item["approval_url"],
                "send_window": send_window,
                "ledger_key": key,
            })
            print()
            continue

        result = await post_tweet(account, text, dry_run=dry_run, send_window=send_window)

        result["batch_id"] = item["batch_id"]
        result["order"] = order
        result["account_handle"] = handle
        result["lane"] = item["lane"]
        result["pillar"] = item["pillar"]
        result["source_label"] = item["source_label"]
        result["approval_origin"] = item["approval_origin"]
        result["approval_url"] = item["approval_url"]
        result["send_window"] = send_window

        # Record in ledger only on real successful posts
        if result["status"] == "posted":
            ledger[key] = {
                "posted_url": result.get("posted_url"),
                "external_post_id": result.get("external_post_id"),
                "timestamp": result["timestamp"],
            }
            save_ledger(ledger)

        results.append(result)

        status = result["status"]
        print(f"  Status: {status}")
        if result.get("error"):
            print(f"  Error: {result['error']}")
        if result.get("posted_url"):
            print(f"  URL: {result['posted_url']}")
        print()

    RESULTS_FILE.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print(f"Results saved to {RESULTS_FILE}")

    posted = sum(1 for r in results if r["status"] == "posted")
    dry_runs = sum(1 for r in results if r["status"] == "dry_run")
    skipped = sum(1 for r in results if r["status"] == "skipped_duplicate")
    blocked = sum(1 for r in results if r["status"] == "window_blocked")
    failed = sum(1 for r in results if r["status"] == "failed")

    print(f"\n=== Summary ===")
    print(
        f"Posted: {posted} | Dry-run: {dry_runs} | "
        f"Skipped (dup): {skipped} | Blocked (window): {blocked} | Failed: {failed}"
    )

    return failed == 0


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv or "--live" not in sys.argv
    success = asyncio.run(run_wave(dry_run=dry_run))
    sys.exit(0 if success else 1)
