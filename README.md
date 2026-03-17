# x-engage — Engagement Analyzer + X Action Executor

Two-part system for Desearch AI's X/Twitter engagement workflow:

1. **`analyze.py`** — Reads x-monitor's sliding tweet window, scores posts, runs GPT-4o-mini deep-dive on top performers, generates @desearch_ai content ideas, and posts a digest to Discord **#x-alerts**.
2. **`execute_actions.py`** — Reads `pending_actions.json`, finds items Giga approved, and executes `retweet` / `quote` actions via Playwright browser automation on x.com.

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

## Usage

```bash
# Dry run — print JSON, no Discord post
uv run python analyze.py --dry-run

# Full run — analyze + post to Discord
uv run python analyze.py
```

## Scoring Formula

```
score = likes×3 + retweets×5 + replies×2 + views×0.01 + quotes×4 + bookmarks×2
```

Example: likes=10, rts=5, replies=2, views=500, quotes=1 → **68**

## LLM Cost Optimization

GPT-4o-mini is called **only for the top-3 posts** (not all 10), keeping cost minimal.

## Action Executor

```bash
# Dry run — see what would be executed without opening any browser
uv run python execute_actions.py --dry-run

# Live run — open Chromium, perform approved actions, post Discord confirmations
uv run python execute_actions.py
```

### `pending_actions.json` schema

```json
[{
  "tweet_id":   "123",
  "tweet_url":  "https://x.com/user/status/123",
  "tweet_text": "...",
  "author":     "username",
  "action":     "retweet | quote",
  "quote_text": "(required for action=quote)",
  "status":     "pending | approved | done | skipped | failed",
  "timestamp":  "2024-..."
}]
```

Set `status=approved` on any item to queue it for execution.  
After `execute_actions.py` runs, the item's `status` becomes `done` (or `failed` with an `error` field).

## Output Files

- `pending_actions.json` — tweet queue managed by both scripts

## Config (`config.json`)

| Field | Description |
|---|---|
| `x_monitor_window_path` | Path to x-monitor `tweets_window.json` |
| `discord_channel_id` | Discord channel to post digest |
| `openai_model` | LLM model (default: `gpt-4o-mini`) |
| `top_n` | How many tweets to rank (default: 10) |
| `top_deep_dive` | How many get LLM deep-dive (default: 3) |
| `trigger_interval_hours` | How often to run (default: 4) |
| `pending_actions_path` | Output file for pending actions |
| `score_weights` | Per-metric score multipliers |

## Environment Variables

See `.env.example`:
- `OPENAI_API_KEY` — OpenAI API key
- `DISCORD_BOT_TOKEN` — Discord bot token
