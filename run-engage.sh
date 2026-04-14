#!/bin/bash
# run-engage.sh — Safe runtime entry point for the x-engage pipeline
# Default cron-safe mode: analyze only. Live execution stays behind an explicit gate.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
MODE="${1:-analyze}"
shift || true

# Load env from .env file if present
if [ -f "$SCRIPT_DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$SCRIPT_DIR/.env" 2>/dev/null || true
  set +a
fi

run_python() {
  if command -v uv >/dev/null 2>&1; then
    uv run python "$@"
  else
    /usr/local/bin/python3 "$@"
  fi
}

echo "[x-engage] Mode: $MODE"
echo "[x-engage] Started: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"

case "$MODE" in
  analyze)
    run_python "$SCRIPT_DIR/analyze.py" "$@"
    ;;
  analyze-dry-run)
    run_python "$SCRIPT_DIR/analyze.py" --dry-run --skip-llm "$@"
    ;;
  execute-dry-run)
    run_python "$SCRIPT_DIR/execute_actions.py" --dry-run "$@"
    ;;
  execute-live)
    if [ "${X_ENGAGE_ENABLE_LIVE_EXECUTION:-0}" != "1" ]; then
      echo "[x-engage] Refusing live execution. Export X_ENGAGE_ENABLE_LIVE_EXECUTION=1 to continue." >&2
      exit 1
    fi
    run_python "$SCRIPT_DIR/execute_actions.py" "$@"
    ;;
  full-dry-run)
    run_python "$SCRIPT_DIR/analyze.py" --dry-run --skip-llm
    run_python "$SCRIPT_DIR/execute_actions.py" --dry-run
    ;;
  *)
    echo "Usage: bash run-engage.sh [analyze|analyze-dry-run|execute-dry-run|execute-live|full-dry-run]" >&2
    exit 1
    ;;
esac
