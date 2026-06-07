#!/bin/bash
set -e

cd "$(dirname "$0")"

# Ports
BACKEND_PORT=15800
FRONTEND_PORT=5173
FRONTEND_URL="http://localhost:$FRONTEND_PORT"

# Kill any existing processes on our ports
lsof -ti:$BACKEND_PORT | xargs kill -9 2>/dev/null || true
lsof -ti:$FRONTEND_PORT | xargs kill -9 2>/dev/null || true

echo "=== Starting MultiClaw (dev mode) ==="

# Start backend
echo "[backend] Starting on http://localhost:$BACKEND_PORT"
uv run uvicorn multiclaw.server:app --host 0.0.0.0 --port $BACKEND_PORT --reload &
BACKEND_PID=$!
echo "[backend] PID: $BACKEND_PID"

# Start frontend Vite dev server
echo "[frontend] Starting on http://localhost:$FRONTEND_PORT"
cd frontend
npx vite --host 0.0.0.0 --port $FRONTEND_PORT &
FRONTEND_PID=$!
cd ..
echo "[frontend] PID: $FRONTEND_PID"

# Save PIDs for stop.sh
echo $BACKEND_PID > /tmp/multiclaw-backend.pid
echo $FRONTEND_PID > /tmp/multiclaw-frontend.pid

echo ""
echo "=== MultiClaw is running ==="
echo "  Frontend: $FRONTEND_URL"
echo "  Backend:  http://localhost:$BACKEND_PORT"
echo "  Logs:     ~/.multiclaw/logs/multiclaw.log"

# Auto-open browser
sleep 2
open "$FRONTEND_URL" 2>/dev/null || true

echo ""
echo "=== Tailing backend log in 3 seconds ==="
sleep 3
tail -f ~/.multiclaw/logs/multiclaw.log
