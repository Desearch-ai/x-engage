# x-engage — Engagement Analyzer + X Action Executor

Two-part system for Desearch AI's X/Twitter engagement workflow:

1. **`analyze.py`** — Reads x-monitor's sliding tweet window, scores posts, runs GPT-4o-mini deep-dive on top performers, generates @desearch_ai content ideas, and posts a digest to Discord **#x-alerts**.
2. **`execute_actions.py`** — Reads `pending_actions.json`, finds items Giga approved, and executes `retweet` / `quote` actions via Playwright browser automation on x.com.

---

## What happens when you merge?

1. **A cron job runs automatically every 4 hours** via OpenClaw (cron ID: `b046db40-f90c-4185-8fdd-d54fe6c552e0`).
2. It reads `tweets_window.json` from x-monitor (24h sliding window, ~100 tweets).
3. Scores every tweet: `score = likes×3 + rts×5 + replies×2 + views×0.01 + quotes×4 + bookmarks×2`
4. Picks the top 10. Runs GPT-4o-mini on **top 3 only** (cost-efficient).
5. Generates 3 content ideas for @desearch_ai based on the patterns.
6. Posts a 6-message digest to **Discord `#x-alerts` (channel `1477727527618347340`)**.
7. Writes `pending_actions.json` with the top 3 tweets for RT/Quote approval.

**After merging → engagement reports appear in Discord automatically, no action needed.**

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

`execute_actions.py` uses a **persistent browser profile** at `~/.x-engage-browser-profile/`.
On the very first run the browser will open to `x.com`. Log in manually — your session will be saved for all future runs.

### The cron job (already created)

The OpenClaw cron job `X Engage — Engagement Report (4h)` is already active.
Check it: `openclaw cron list | grep Engage`

---

## Usage

### Engagement Analyzer (`analyze.py`)

```bash
python3 analyze.py --dry-run    # Dry run: prints JSON, no Discord post
python3 analyze.py              # Live run: posts to Discord #x-alerts
```

Or via shell wrapper (used by cron):
```bash
bash run-engage.sh
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

```json
[{
  "tweet_id":     "123",
  "tweet_url":    "https://x.com/user/status/123",
  "tweet_text":   "...",
  "author":       "username",
  "action":       "pending | retweet | quote",
  "quote_text":   "(required for action=quote)",
  "status":       "pending | approved | done | skipped | failed",
  "account_id":   "personal",
  "account_label":"@cosmicquantum (personal)",
  "timestamp":    "2026-..."
}]
```

Set `action=retweet` or `action=quote` + `status=approved` to queue for execution.
After `execute_actions.py` runs, `status` becomes `done` (or `failed` with an `error` field).

---

## Config (`config.json`)

```json
{
  "x_monitor_window_path": "/Users/giga/projects/openclaw/x-monitor/tweets_window.json",
  "discord_channel_id": "1477727527618347340",
  "openai_model": "gpt-4o-mini",
  "top_n": 10,
  "top_deep_dive": 3,
  "trigger_interval_hours": 4,
  "x_accounts": [
    {
      "id": "personal",
      "label": "@cosmicquantum (personal)",
      "browser_profile": "~/.x-engage-browser/personal",
      "active": true
    }
  ],
  "active_account": "personal"
}
```

---

## Multi-Account Architecture

Config supports N accounts from day one. To add a second account:

```json
{
  "x_accounts": [
    { "id": "personal", "label": "@cosmicquantum (personal)", "active": true },
    { "id": "desearch", "label": "@desearch_ai (brand)", "active": false }
  ],
  "active_account": "personal"
}
```

Set `active_account` to switch which account's label appears in Discord buttons and `pending_actions.json`.

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
