# Runtime Orchestration

## Overview

x-engage is the active engagement layer. It reads the rolling tweet window from x-monitor, scores and analyzes top performers, and queues candidate actions for human review before any live X execution.

```
x-monitor/tweets_window.json
    ↓ (every 4h via run-engage.sh)
analyze.py
    ↓ Discord digest (#x-alerts)
    ↓ ranked candidates
pending_actions.json
    ↓ (human review — REQUIRED)
execute_actions.py
    ↓ Playwright browser automation
X.com (live retweet / quote)
```

## Stages

### Stage 1 — Analysis (auto, every 4h)

```bash
cd ~/projects/openclaw/x-engage && ./run-engage.sh
```

`run-engage.sh` uses `uv run python` for managed venv execution and acquires `.analyze.lock`.

What analyze.py does:
1. Reads `tweets_window.json` from x-monitor
2. Scores tweets by engagement (likes, retweets, views, replies)
3. Runs LLM deep-dive analysis on top 3 tweets
4. Generates 3 content ideas for @desearch_ai
5. Posts multi-message digest to `#x-alerts` (1477727527618347340)
6. Writes ranked candidates to `pending_actions.json`

### Stage 2 — Human review (required gate)

**Before running the executor, always review pending_actions.json.**

```bash
cat ~/projects/openclaw/x-engage/pending_actions.json | python3 -m json.tool | less
```

Each entry needs:
- `status: "approved"` — executor only picks up items with this status
- `action: "retweet"` or `"quote"` — retweet or quote tweet
- `quote_text` — required for `action: "quote"`

### Stage 3 — Execution (manual, human-gated)

Dry-run (always do this first):

```bash
cd ~/projects/openclaw/x-engage && ./run-executor.sh
# Output shows what WOULD execute — no browser opens
```

Live execution (after human review of pending_actions.json):

```bash
cd ~/projects/openclaw/x-engage && ./run-executor.sh --live
```

## Anti-concurrency locks

| File | Stage |
|------|-------|
| `.analyze.lock` | run-engage.sh (analyze.py) |
| `.executor.lock` | execute_actions.py |

Both are PID-based. Remove stale locks manually:

```bash
cd ~/projects/openclaw/x-engage
rm .analyze.lock .executor.lock
```

## Dry-run support

Both analyze.py and execute_actions.py support `--dry-run`:

```bash
cd ~/projects/openclaw/x-engage
uv run python analyze.py --dry-run
uv run python execute_actions.py --dry-run
```

## Rollback

If a script change causes issues:

1. Remove lock files
2. Restore previous versions:

   ```bash
   cd ~/projects/openclaw/x-engage
   git checkout HEAD~1 -- analyze.py execute_actions.py run-engage.sh
   ```
3. Verify pending_actions.json is valid:

   ```bash
   python3 -c "import json; json.load(open('pending_actions.json'))"
   ```

## Known operational constraints

- **No auto-cron for executor**: execute_actions.py should NOT be on auto-cron. Human review of pending_actions.json is always required before live execution.
- **Single browser profile**: The executor uses one browser profile path. Multi-account execution requires manual coordination.
- **X selector stability**: Executor uses DOM `data-testid` selectors. If X.com changes their UI, repost/quote actions will fail silently until selectors are updated.

## Env secrets

| Secret | Used by | Purpose |
|--------|---------|---------|
| `CEREBRAS_API_KEY` | analyze.py | LLM analysis |
| `DISCORD_BOT_TOKEN` | analyze.py, execute_actions.py | Discord digest + confirmation |
| `DESEARCH_API_KEY` | (upstream monitor) | Tweet collection |

All secrets are loaded from:
1. Environment variables
2. `~/.openclaw/openclaw.json` (Cerebras + Discord fallback)
3. `.env` file in x-engage/ (local override)
