#!/bin/bash
# run-executor.sh — Execute approved X actions from pending_actions.json.
#
# IMPORTANT: This script executes real X actions (retweets/quotes) on live accounts.
# It should only be run AFTER human review of pending_actions.json.
#
# Usage:
#   ./run-executor.sh           # Dry-run: shows what would be executed
#   ./run-executor.sh --live    # Live execution of approved actions
#
# Schedule: This is human-gated. Run manually or on a conservative schedule
#           (e.g., weekly) after reviewing pending_actions.json.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCK_FILE="$SCRIPT_DIR/.executor.lock"

# ─── Anti-concurrency lock ───────────────────────────────────────────────────
acquire_lock() {
  if [ -f "$LOCK_FILE" ]; then
    pid=$(cat "$LOCK_FILE" 2>/dev/null)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      echo "[executor] Lock held by PID $pid — another instance running. Exiting." >&2
      return 1
    fi
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

MODE="${1:-}"
if [ "$MODE" = "--live" ]; then
  echo "[executor] LIVE MODE — executing approved actions on X"
  echo "[executor] Press Ctrl+C now to abort if pending_actions.json has not been reviewed."
  sleep 3
  uv run python "$SCRIPT_DIR/execute_actions.py"
else
  echo "[executor] DRY-RUN mode — use --live to execute"
  uv run python "$SCRIPT_DIR/execute_actions.py" --dry-run
fi
