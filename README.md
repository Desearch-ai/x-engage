# x-engage — Engagement Analyzer + Discord Reporter

Reads the x-monitor `tweets_window.json` (24h sliding window), scores posts, runs GPT-4o-mini analysis on the top performers, generates content ideas for @desearch_ai, and posts a digest to Discord **#x-alerts**.

## Setup

```bash
cd ~/projects/openclaw/x-engage
cp .env.example .env
# Fill in OPENAI_API_KEY and DISCORD_BOT_TOKEN in .env
uv sync
```

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

## Output Files

- `pending_actions.json` — top-3 tweets needing human approval (`retweet` / `quote` / `skip`)

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
