# x-engage — Engagement Analyzer + X Action Executor

Two-part system for Desearch AI's X/Twitter engagement workflow:

1. **`analyze.py`** — Reads x-monitor's sliding tweet window, scores posts, runs GPT-4o-mini deep-dive on top performers, generates @desearch_ai content ideas, and posts a digest to Discord **#x-alerts**.
2. **`execute_actions.py`** — Reads `pending_actions.json`, finds items Giga approved, and executes `retweet` / `quote` actions via Playwright browser automation on x.com.

---

## What happens when you merge?

1. **Analysis runs automatically every 4 hours** via OpenClaw (re-enable around `bash run-engage.sh analyze`).
2. It reads `tweets_window.json` from x-monitor (24h sliding window, ~100 tweets).
3. Scores every tweet: `score = likes×3 + rts×5 + replies×2 + views×0.01 + quotes×4 + bookmarks×2`
4. Picks the top 10. Runs GPT-4o-mini on **top 3 only** (cost-efficient).
5. Generates 3 content ideas for @desearch_ai based on the patterns.
6. Posts a 6-message digest to **Discord `#x-alerts` (channel `1477727527618347340`)**.
7. Writes `pending_actions.json` with the top 3 tweets for RT/Quote approval, using an exclusive queue lock and atomic replace semantics.
8. Live execution remains a separate step, behind manual approval and an explicit env gate.

**After merging → analysis reports appear in Discord automatically. Live X account actions still require approval first.**

---

## Discord Output

```
📊 Engagement Report | Top 10 posts • 2026-04-02 10:00 UTC
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#1 @steipete · score 6400 · ❤️1.2K 🔄55 👁️143.6K
> I never use plan mode...
#2 @openclaw · score 2606 · ❤️386 🔄61 👁️49.8K
> OpenClaw 2026.4.1 🦞...
...
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🔍 Top 3 Deep Dive
[Detailed card per post: tweet text + engagement breakdown + LLM analysis]
[Actions: 🔄 RT as @cosmicquantum | 💬 Quote | ⏭️ Skip]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
💡 Content Ideas for @desearch_ai
[3 ideas based on top-performer patterns]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

## Setup

```bash
cd ~/projects/openclaw/x-engage
cp .env.example .env
# Fill in OPENAI_API_KEY and DISCORD_BOT_TOKEN in .env
uv sync

# Install Playwright's Chromium browser (required for execute_actions.py)
uv run playwright install chromium
```

### First-time X.com login

`execute_actions.py` uses **per-account persistent browser profiles** defined in `config.json` (`browser_profile` field per account, e.g. `~/.x-engage-browser/personal` and `~/.x-engage-browser/brand`).
On the very first run for each account the browser will open to `x.com`. Log in to the correct account manually — the session is saved for all future runs.

### Runtime cadence

Recommended cadence:
- every 4h: `bash run-engage.sh analyze`
- operator review window after each digest
- optional/manual validation: `bash run-engage.sh execute-dry-run`
- live execution only when explicitly approved: `X_ENGAGE_ENABLE_LIVE_EXECUTION=1 bash run-engage.sh execute-live`

Do not bundle analysis and live execution into one unattended cron.

---

## Usage

### Engagement Analyzer (`analyze.py`)

```bash
python3 analyze.py --dry-run    # Dry run: prints JSON, no Discord post
python3 analyze.py              # Live run: posts to Discord #x-alerts
```

Or via the safe shell wrapper:
```bash
bash run-engage.sh analyze
bash run-engage.sh analyze-dry-run
bash run-engage.sh execute-dry-run
X_ENGAGE_ENABLE_LIVE_EXECUTION=1 bash run-engage.sh execute-live
```

### Action Executor (`execute_actions.py`)

```bash
# Dry run — see what would be executed without opening any browser
uv run python execute_actions.py --dry-run

# Live run — open Chromium, perform approved actions, post Discord confirmations
uv run python execute_actions.py
```

---

## Files

| File | Purpose |
|------|---------|
| `analyze.py` | Engagement analyzer: score, analyze, post digest to Discord |
| `execute_actions.py` | Action executor: RT/Quote approved tweets via Playwright |
| `run-engage.sh` | Shell wrapper for cron (loads .env, calls python3 analyze.py) |
| `config.json` | Settings (paths, model, scoring weights, accounts) |
| `.env` | API keys (not in git) |
| `.env.example` | Template for keys |
| `pending_actions.json` | Tweet queue managed by both scripts |

---

## Scoring Formula

```
score = likes×3 + retweets×5 + replies×2 + views×0.01 + quotes×4 + bookmarks×2
```

Example: likes=10, rts=5, replies=2, views=500, quotes=1 → **68**

GPT-4o-mini is called **only for the top-3 posts** (not all 10), keeping cost minimal.

---

## `pending_actions.json` schema

Each entry represents one **tweet × account** pair. The same tweet appears once per account.

```json
[{
  "tweet_id":     "123",
  "tweet_url":    "https://x.com/user/status/123",
  "tweet_text":   "...",
  "author":       "username",
  "score":        650.0,
  "action":       "pending | retweet | quote",
  "quote_text":   "(required for action=quote)",
  "status":       "pending | approved | done | skipped | failed",
  "account_id":   "personal",
  "account_label":"@cosmicquantum (personal)",
  "lane":         "founder | brand",
  "action_types": ["retweet", "quote"],
  "source":       "x-engage-analyzer",
  "category":     "ai",
  "timestamp":    "2026-..."
}]
```

Deduplication key is `(tweet_id, account_id)` — re-running `analyze.py` never adds duplicates.

Set `action=retweet` or `action=quote` + `status=approved` to queue for execution.
After `execute_actions.py` runs, `status` becomes `done` (or `failed` with an `error` field).

---

## Config (`config.json`)

```json
{
  "x_monitor_window_path": "/path/to/x-monitor/tweets_window.json",
  "discord_channel_id": "1477727527618347340",
  "openai_model": "gpt-4o-mini",
  "top_n": 10,
  "top_deep_dive": 3,
  "trigger_interval_hours": 4,
  "pending_actions_path": "pending_actions.json",
  "score_weights": { "likes": 3, "retweets": 5, "replies": 2, "views": 0.01, "quotes": 4, "bookmarks": 2 },
  "x_accounts": [
    {
      "id": "personal",
      "label": "@cosmicquantum (personal)",
      "lane": "founder",
      "browser_profile": "~/.x-engage-browser/personal",
      "min_confidence": 0.7,
      "action_types": ["retweet", "quote"]
    },
    {
      "id": "brand",
      "label": "@desearch_ai (brand)",
      "lane": "brand",
      "browser_profile": "~/.x-engage-browser/brand",
      "min_confidence": 0.8,
      "action_types": ["quote"]
    }
  ]
}
```

---

## Multi-Account Architecture

`analyze.py` generates one `pending_actions.json` entry **per tweet × account**. All accounts in `x_accounts` are processed — there is no `active_account` toggle.

`execute_actions.py` groups approved actions by `account_id` and opens a **separate Chromium browser context** per account (each with its own `browser_profile`), so sessions never cross-contaminate. It now claims the shared queue lock before execution and marks each item as `executing` before a live browser action, so crashes remain visible instead of silently re-running the same approval.

To add a new account: append an entry to `x_accounts` with its own `id`, `lane`, `browser_profile`, and `action_types`. No code changes required.

---

## Relationship to x-monitor

```
x-monitor (every 2h)          x-engage (every 4h)
─────────────────────          ────────────────────
monitor.py                →    analyze.py
  ↓ fetches tweets               ↓ reads tweets_window.json
  ↓ deduplicates                 ↓ scores + ranks top 10
  ↓ writes tweets_window.json    ↓ GPT-4o-mini analyzes top 3
  ↓ posts raw tweets to          ↓ generates content ideas
    Discord #x-alerts            ↓ posts digest to Discord #x-alerts
                                 ↓ writes pending_actions.json
                                        ↓
                               execute_actions.py (manual trigger)
                                 ↓ reads approved items
                                 ↓ RT/Quote via Playwright
                                 ↓ posts confirmations to Discord
```

Both services post to Discord `#x-alerts` (`1477727527618347340`):
- x-monitor: real-time tweet alerts (raw, unanalyzed)
- x-engage: engagement analysis digest with LLM insights + action executor

---

## GitHub

Repository: https://github.com/Desearch-ai/x-engage
