#!/bin/bash
# run-engage.sh — Entry point for the x-engage analysis cron job.
# Called by OpenClaw cron every 4 hours.
# Analyzes top engaging posts from tweets_window.json and posts digest to Discord.
# Uses uv to run in the project virtual environment.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCK_FILE="$SCRIPT_DIR/.analyze.lock"

# ─── Anti-concurrency lock ───────────────────────────────────────────────────
acquire_lock() {
  if [ -f "$LOCK_FILE" ]; then
    pid=$(cat "$LOCK_FILE" 2>/dev/null)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      echo "[x-engage] Lock held by PID $pid — another instance running. Exiting." >&2
      return 1
    fi
    # Stale lock
    rm -f "$LOCK_FILE"
  fi
  echo $$ > "$LOCK_FILE"
  return 0
}

release_lock() {
  rm -f "$LOCK_FILE"
}

trap release_lock EXIT

# ─── Env loading ─────────────────────────────────────────────────────────────
if [ -f "$SCRIPT_DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$SCRIPT_DIR/.env" 2>/dev/null || true
  set +a
fi

# ─── Run ─────────────────────────────────────────────────────────────────────
echo "[x-engage] Starting engagement analysis at $(date -u '+%Y-%m-%dT%H:%M:%SZ')"

if ! acquire_lock; then
  exit 1
fi

# Use uv run python (project-managed venv)
uv run python "$SCRIPT_DIR/analyze.py"

exit_code=$?

echo "[x-engage] Analysis complete (exit $exit_code) at $(date -u '+%Y-%m-%dT%H:%M:%SZ')"

exit $exit_code
