#!/bin/bash
set -e

BACKEND_PORT=15800
FRONTEND_PORT=5173

echo "=== Stopping MultiClaw ==="

# Try PID files first
if [ -f /tmp/multiclaw-backend.pid ]; then
    kill "$(cat /tmp/multiclaw-backend.pid)" 2>/dev/null && echo "[backend] Stopped (PID file)" || true
    rm -f /tmp/multiclaw-backend.pid
fi

if [ -f /tmp/multiclaw-frontend.pid ]; then
    kill "$(cat /tmp/multiclaw-frontend.pid)" 2>/dev/null && echo "[frontend] Stopped (PID file)" || true
    rm -f /tmp/multiclaw-frontend.pid
fi

# Fallback: kill by port
lsof -ti:$BACKEND_PORT | xargs kill 2>/dev/null && echo "[backend] Stopped (port $BACKEND_PORT)" || true
lsof -ti:$FRONTEND_PORT | xargs kill 2>/dev/null && echo "[frontend] Stopped (port $FRONTEND_PORT)" || true

echo "Done."
