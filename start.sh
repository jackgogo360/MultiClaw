#!/bin/bash
set -e

cd "$(dirname "$0")"

# Kill any existing process on port 15800
lsof -ti:15800 | xargs kill -9 2>/dev/null || true

echo "Starting MultiClaw on http://localhost:15800"
uv run uvicorn multiclaw.server:app --host 0.0.0.0 --port 15800 --reload &
SERVER_PID=$!

LOG_FILE="$HOME/.multiclaw/logs/multiclaw.log"

echo "Waiting for log file..."
for _ in $(seq 1 30); do
    if [ -f "$LOG_FILE" ]; then
        break
    fi
    sleep 0.5
done

echo "Tailing $LOG_FILE — Ctrl-C to stop watching (server keeps running)"
tail -50f "$LOG_FILE" || true
