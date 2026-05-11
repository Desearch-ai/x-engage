# Socialos runtime — X engagement plumbing (legacy x-engage repo)

Internal Socialos runtime plumbing for X engagement. The repository is still named `x-engage` for compatibility, but product/operator language should treat it as Socialos runtime: normalized source signals in, account-specific drafts/review rows out, approved X actions executed only after explicit approval.

1. **`analyze.py`** — Loads bounded source connectors (X-Monitor normalized Signal records by default), scores/qualifies source signals, applies Socialos account profile filters/styles, writes Social OS review rows, and can still post the legacy Discord digest.
2. **`execute_actions.py`** — Reads approved Social OS rows first, falls back to `pending_actions.json` only when Social OS has no approved rows, validates **explicit Mission Control approval**, and executes `retweet` / `quote` actions via isolated Playwright browser profiles.
3. **`post_tweet.py`** — Posts original tweets with the same approval contract.

---

## 🔒 Explicit Per-Post Approval Requirement

**Live execution requires explicit Mission Control per-post approval.** The system no longer accepts implied, batch, or chat-based approval for publishing.

### Approval Contract

For any live action (retweet, quote, or original post), the item MUST include:

| Field | Required | Description |
|-------|----------|-------------|
| `approval_status` | ✅ Yes | Must be exactly `"approved"` (case-insensitive) |
| `approval_url` | Conditional | URL to the MC approval message/link (required when `approved_at` is not present) |
| `approved_by` | ✅ Yes | Who approved (operator audit trail) |
| `approved_at` | Conditional | Required when `approval_url` is not present |

### Approval Validation Flow

```
Item enters queue → Check status='approved' + approval_status='approved' + approved_by + (approval_url or approved_at)
                                    ↓
                    ❌ REJECTED         ✅ PROCEED
                    (status =              ↓
                     approval_rejected)      Execute action
```

Items without valid approval are:
- Logged with rejection reason
- Marked as `approval_rejected` in the queue
- Never attempted for live execution

This ensures auditability — every live action can be traced back to an explicit human approval.

---

## What happens when you merge?

1. **Analysis runs automatically every 4 hours** via OpenClaw (re-enable around `bash run-engage.sh analyze`).
2. It reads configured Socialos source connectors; the default connector is X-Monitor normalized Signal records from `tweets_window.json` (legacy tweet-shaped windows still work).
3. Scores every tweet: `score = likes×3 + rts×5 + replies×2 + views×0.01 + quotes×4 + bookmarks×2`
4. Picks the top 10. Runs GPT-4o-mini on **top 3 only** (cost-efficient).
5. Generates 3 content ideas for @desearch_ai based on the patterns.
6. Posts a 6-message digest to **Discord `#x-alerts` (channel `1477727527618347340`)**.
7. Writes Social OS review rows plus secondary `pending_actions.json` fallback entries for the top routed signals, using an exclusive queue lock and atomic replace semantics.
8. Live execution remains a separate step, behind **explicit per-post approval** from Mission Control.

**After merging → analysis reports appear in Discord automatically. Live X account actions still require explicit MC approval first.**

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
[Actions: 🔄 RT as @cosmic_desearch | 💬 Quote | ⏭️ Skip]
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

# Install Playwright's Chromium browser (required for execute_actions.py and post_tweet.py)
uv run playwright install chromium
```

### Managed runtime contract (default path)

The Socialos runtime resolves normal behavior from the active `social_runtime_configs` row managed by Social OS. `x-engage` remains the legacy repo/internal service name for compatibility fields and scripts.

- **Accounts / lanes** come from `lane_routing`
- **Browser profile mapping** comes from `session_mappings`
- **Default post send-window** comes from `send_window`
- **Execution cadence metadata** comes from `check_interval_seconds`

Supported runtime sources, in order:

1. `X_ENGAGE_RUNTIME_PATH=/path/to/runtime.json` — explicit local override for tests / emergency recovery
2. `SOCIAL_OS_SUPABASE_URL` + `SOCIAL_OS_SUPABASE_KEY` (or `SOCIAL_OS_SUPABASE_ANON_KEY`)
3. `SUPABASE_URL` + `SUPABASE_ANON_KEY` / `SUPABASE_SERVICE_ROLE_KEY`
4. `VITE_SUPABASE_URL` + `VITE_SUPABASE_ANON_KEY`
5. `config.json` fallback only when no managed runtime source is available

`session_mappings` may contain either:
- an absolute / `~/...` profile path, or
- a logical session id like `brand-session`, which resolves under `X_ENGAGE_BROWSER_PROFILE_ROOT` (default: `~/.x-engage-browser`)

### First-time X.com login

`execute_actions.py` and `post_tweet.py` use **per-account persistent browser profiles** from the managed runtime contract first, then fall back to `config.json` only if the managed source is unavailable.
On the very first run for each account the browser will open to `x.com`. Log in to the correct account manually — the session is saved for all future runs.

### Runtime cadence

Recommended cadence:
- every 4h: `bash run-engage.sh analyze`
- operator review window after each digest
- explicit approval via Mission Control SocialPage
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


### Socialos review queue refill (`queue_refill.py`)

The Socialos runtime owns the review-queue generation contract. Social OS should call
this endpoint/command instead of inserting placeholder `social_posts` rows itself. The
contract is review-queue-only: it loads configured source connectors, applies the
normal account filter/route stage, writes source-rich Social OS review rows plus the
secondary `pending_actions.json` fallback, emits Socialos runtime telemetry, and never
executes live X actions.

One-shot CLI mode:

```bash
printf '%s' '{"source":"social-os-ui","mode":"review_queue_only","allow_live_actions":false,"requested_by":"social-os","requested_at":"2026-05-04T07:49:00Z"}' \
  | bash run-engage.sh queue-refill
```

HTTP mode for `SOCIAL_QUEUE_REFILL_URL` deployments:

```bash
bash run-engage.sh queue-refill-server --host 127.0.0.1 --port 8788
# POST http://127.0.0.1:8788/queue-refill
```

Request body:

| Field | Required | Description |
|-------|----------|-------------|
| `source` | ✅ | Expected caller, usually `social-os-ui`. |
| `mode` | ✅ | Must be `review_queue_only`. |
| `allow_live_actions` | ✅ | Must be `false`; requests with `true` are rejected before analysis. |
| `requested_by` | ✅ | Operator or service initiating the refill. |
| `requested_at` | ✅ | ISO timestamp used for telemetry and `next_run_at` calculation. |
| `trigger` | Optional | `manual` (default) or `scheduled`. |
| `run_id` | Optional | Stable caller-provided run id; otherwise the runtime generates one. |
| `skip_llm` | Optional | Defaults to `true` for safe/local queue refill. |

Normalized response includes `ok`, `status`, `message`, `created`, `refreshed`,
`skipped`, `run_id`, and either `next_run_at` or `schedule_not_configured`. Counts
come from Socialos runtime-created review rows; Social OS placeholder rows do not
count as success.

### Source connector contract

`source_connectors.py` provides the bounded input layer. Configured connectors return the analyzer-friendly signal shape and report non-fatal connector errors in `source_report`; one bad source does not block other sources.

Supported now:
- `x_monitor_signal` / `x-monitor`: reads a list (or `{ "signals": [...] }`) containing either legacy tweet-shaped records or Task #1613 normalized Signal records:

```json
{
  "signal_id": "uuid",
  "type": "mention|keyword|list",
  "source": "x-monitor",
  "timestamp": "ISO8601",
  "content": {"text": "raw tweet text", "author": "@handle", "url": "tweet URL"},
  "context": {"matched_term": "string", "watchlist_name": "string"},
  "route_hints": {"channel": "#x-monitor", "priority": 1, "lanes": ["brand"]}
}
```

- `markdown`: fixture/future connector for local markdown/API-style inputs. It emits `source=markdown` and route hints such as `markdown/brand` so downstream drafts do not pretend to be X-Monitor signals.

Connector polling is bounded to the configured paths for each run; the runtime does not fetch per UI render/request.

### Account profile filters/styles

Account routing is no longer limited to `desearch_ai` and `cosmicquantum`. The runtime applies filters in this order:
1. Socialos account profile embedded on the account (`filters`, `style`, `tone_rules`)
2. Managed/runtime `account_filters` keyed by handle/id/lane
3. Built-in defaults for `@desearch_ai`, `@cosmicquantum`, and `@MOkradze`
4. Generic fallback (`min_confidence=0.55`) for future accounts

`@MOkradze` is first-class for routing/style defaults. If Social OS has not configured its browser session, execution remains blocked by the existing per-account session/profile checks and approval gates rather than by analysis.

### Action Executor (`execute_actions.py`)

```bash
# Dry run — see what would be executed without opening any browser
uv run python execute_actions.py --dry-run

# Live run — requires explicit MC approval for each item
uv run python execute_actions.py
```

### Original Tweet Poster (`post_tweet.py`)

```bash
# Dry run
uv run python post_tweet.py --account personal --text "Hello world" --dry-run

# Live — requires explicit approval via CLI args or item metadata
uv run python post_tweet.py --account personal --text "Hello world" \
  --approval-status approved \
  --approval-url "https://discord.com/channels/.../1234567890" \
  --approved-by "Giga"
```

---

## Files

| File | Purpose |
|------|---------|
| `analyze.py` | Socialos source analyzer: load connectors, score, filter/route, create review rows, optionally post digest |
| `execute_actions.py` | Action executor: RT/Quote approved tweets via Playwright |
| `post_tweet.py` | Post original tweets with explicit MC approval |
| `run_validation_wave.py` | Validation batch executor (requires approval in batch file) |
| `run-engage.sh` | Shell wrapper for cron (loads .env, calls python3 analyze.py) |
| `config.json` | Local repo fallback for paths, model, scoring weights, and emergency account metadata |
| `runtime_loader.py` | Shared Socialos runtime contract loader, account profile projection, telemetry, and fallback merger |
| `.env` | API keys (not in git) |
| `.env.example` | Template for keys |
| `pending_actions.json` | Secondary local fallback queue; Social OS rows are primary for execution |

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
  "tweet_id":       "123",
  "tweet_url":      "https://x.com/user/status/123",
  "tweet_text":     "...",
  "author":         "username",
  "score":          650.0,
  "action":         "pending | retweet | quote",
  "quote_text":     "(required for action=quote)",
  "status":         "pending | approved | done | skipped | failed | approval_rejected",
  "account_id":     "personal",
  "account_handle": "cosmic_desearch",
  "lane":           "founder | brand",
  "action_types":   ["retweet", "quote"],
  "source":         "x-engage-analyzer",
  "category":       "ai",
  "timestamp":      "2026-...",
  "approval_status": "approved",        // REQUIRED for live execution
  "approval_url":    "https://discord.com/...",  // REQUIRED unless approved_at is present
  "approved_by":     "Giga",            // REQUIRED for audit
  "approved_at":     "2026-..."         // REQUIRED unless approval_url is present
}]
```

Deduplication key is `(tweet_id, account_id)` — re-running `analyze.py` never adds duplicates.

Set `action=retweet` or `action=quote` + `status=approved` + `approval_status=approved` + `approved_by=<operator>` + (`approval_url=<MC approval URL>` or `approved_at=<timestamp>`) to queue for execution.
After `execute_actions.py` runs, `status` becomes `done` (or `failed` with an `error` field).

---

## Config (`config.json`)

`config.json` is now **fallback-only** for runtime account/browser settings. The normal operator control path is the managed Socialos runtime contract.

Keep these fields in `config.json` for repo-local behavior and recovery:

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
      "label": "@cosmic_desearch (founder)",
      "handle": "cosmic_desearch",
      "lane": "founder",
      "browser_profile": "~/.x-engage-browser/personal",
      "min_confidence": 0.7,
      "action_types": ["retweet", "quote"]
    }
  ]
}
```

When managed runtime is available, this runtime will:
- replace `x_accounts` from `lane_routing` + `session_mappings`
- use the managed `send_window` for `post_tweet.py` when `--send-window` is omitted
- keep local-only fields like `x_monitor_window_path`, scoring weights, and Discord channel id

Use `X_ENGAGE_RUNTIME_PATH` only as an explicit emergency override or in tests. Day-to-day account/style/routing edits should happen in Social OS, not in this repo.

---

## Multi-Account Architecture

`analyze.py` generates one `pending_actions.json` entry **per tweet × account**. All accounts in the managed runtime `lane_routing` projection are processed — there is no `active_account` toggle.

`execute_actions.py` groups approved actions by `account_id` and opens a **separate Chromium browser context** per account (each with its own runtime-resolved browser profile), so sessions never cross-contaminate. It now claims the shared queue lock before execution and validates explicit MC approval before any live action.

To add a new account in the normal path: update `lane_routing` + `session_mappings` in the Social OS runtime panel. Keep `config.json` account entries only for fallback metadata / recovery.

---

## Relationship to X-Monitor and Socialos

```
X-Monitor source service       Socialos runtime (this repo)
────────────────────────       ───────────────────────────
monitor.py                →    source_connectors.py
  ↓ detects X signals            ↓ normalizes Signal contract
  ↓ writes normalized records    ↓ analyze.py scores + routes
  ↓ posts raw alerts             ↓ account profile filters/styles
                                ↓ writes Social OS review rows
                                ↓ pending_actions.json fallback
                                       ↓
                              execute_actions.py (manual trigger, requires MC approval)
                                ↓ reads approved Social OS rows first
                                ↓ validates approval provenance
                                ↓ RT/Quote via isolated Playwright session
```

X-Monitor remains the X signal intelligence service. Socialos owns accounts, tone/style, approvals, scheduling, regeneration, and execution. This repo is the internal Socialos runtime bridge for X actions; future source connectors can feed markdown/API sources without labeling them as X-Monitor.

---

## GitHub

Repository: https://github.com/Desearch-ai/x-engage
