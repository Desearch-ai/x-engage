#!/usr/bin/env python3
"""
Validation Wave Executor - requires explicit MC per-post approval.

Each post in the batch MUST carry approval metadata:
- approval_status: "approved" (explicit, not inferred)
- approval_url: URL of the MC approval message

Posts without valid approval are rejected at the door and never attempted.
This enforces the Social OS boundary where live execution requires
explicit per-post approval, not batch/chat approval or operator intent inference.
"""
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from post_tweet import post_tweet, validate_approval

BATCH_FILE = Path(__file__).parent / "validation_wave_1_batch.json"
RESULTS_FILE = Path(__file__).parent / "validation_wave_1_results.json"
LEDGER_FILE = Path(__file__).parent / "posted_ledger.json"


def _ledger_key(batch_id: str, order: int) -> str:
    return f"{batch_id}:{order}"


def load_ledger() -> dict:
    """Load the durable posted-tweet ledger. Returns a dict keyed by 'batch_id:order'."""
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

    print(f"=== Validation Wave Executor ===")
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

        print(f"[{order}/{len(batch)}] Processing: {handle} ({account})")
        print(f"  Text: {text[:80]}...")
        print(f"  Source: {item.get('source_label', 'unknown')}")
        print(f"  Pillar: {item.get('pillar', 'unknown')}")
        if send_window:
            print(f"  Window: {send_window}")

        # CRITICAL: Validate explicit MC approval before ANY live action
        approval_status = item.get("approval_status", "").strip().lower() if item.get("approval_status") else ""
        approval_url = item.get("approval_url", "").strip()
        approved_by = item.get("approved_by", "").strip()
        
        # Build item for approval validation (matching post_tweet's expected format)
        approval_item = {
            "approval_status": approval_status,
            "approval_url": approval_url,
            "approved_by": approved_by,
        }
        
        is_valid, reason = validate_approval(approval_item)
        print(f"  Approval: {reason}")
        
        if not is_valid:
            # REJECT at the door - do NOT attempt to post
            print(f"  ❌ BLOCKED - {reason}")
            results.append({
                "account_id": account,
                "text": text,
                "status": "approval_rejected",
                "error": reason,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "batch_id": item["batch_id"],
                "order": order,
                "account_handle": handle,
                "lane": item.get("lane"),
                "pillar": item.get("pillar"),
                "source_label": item.get("source_label"),
                "approval_status": approval_status,
                "approval_url": approval_url,
                "approved_by": approved_by,
            })
            print()
            continue

        # Dedup check: skip if already successfully posted (only for approved items)
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
                "lane": item.get("lane"),
                "pillar": item.get("pillar"),
                "source_label": item.get("source_label"),
                "approval_status": approval_status,
                "approval_url": approval_url,
                "approved_by": approved_by,
                "send_window": send_window,
                "ledger_key": key,
            })
            print()
            continue

        # Execute the post (post_tweet will also validate approval for live runs)
        result = await post_tweet(
            account, 
            text, 
            dry_run=dry_run, 
            send_window=send_window,
            approval_status=approval_status,
            approval_url=approval_url,
            approved_by=approved_by,
        )

        result["batch_id"] = item["batch_id"]
        result["order"] = order
        result["account_handle"] = handle
        result["lane"] = item.get("lane")
        result["pillar"] = item.get("pillar")
        result["source_label"] = item.get("source_label")
        result["approval_status"] = approval_status
        result["approval_url"] = approval_url
        result["approved_by"] = approved_by
        result["send_window"] = send_window

        # Record in ledger only on real successful posts
        if result["status"] == "posted":
            ledger[key] = {
                "posted_url": result.get("posted_url"),
                "external_post_id": result.get("external_post_id"),
                "timestamp": result["timestamp"],
                "approval_url": approval_url,
            }
            save_ledger(ledger)

        results.append(result)

        status = result["status"]
        status_icon = "✅" if status == "posted" else "❌" if status == "failed" else "⏭️"
        print(f"  {status_icon} Status: {status}")
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
    rejected = sum(1 for r in results if r["status"] == "approval_rejected")
    failed = sum(1 for r in results if r["status"] == "failed")

    print(f"\n=== Summary ===")
    print(
        f"Posted: {posted} | Dry-run: {dry_runs} | "
        f"Skipped (dup): {skipped} | Blocked (window): {blocked} | "
        f"Rejected (no approval): {rejected} | Failed: {failed}"
    )

    return failed == 0 and rejected == 0


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv or "--live" not in sys.argv
    success = asyncio.run(run_wave(dry_run=dry_run))
    sys.exit(0 if success else 1)
