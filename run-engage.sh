#!/bin/bash
# run-engage.sh — Entry point for the x-engage cron job
# Called by OpenClaw cron every 4 hours.
# Analyzes top engaging posts from tweets_window.json and posts digest to Discord.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON3="/usr/local/bin/python3"

# Load env from .env file if present (explicit, no shell sourcing needed)
if [ -f "$SCRIPT_DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$SCRIPT_DIR/.env" 2>/dev/null || true
  set +a
fi

echo "[x-engage] Starting engagement analysis at $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo "[x-engage] Python: $PYTHON3"

exec "$PYTHON3" "$SCRIPT_DIR/analyze.py"
