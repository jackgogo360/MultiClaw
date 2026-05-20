#!/bin/bash
set -e

cd "$(dirname "$0")"

# Kill any existing process on port 15800
lsof -ti:15800 | xargs kill -9 2>/dev/null || true

echo "Starting MultiClaw on http://localhost:15800"
exec uv run uvicorn multiclaw.server:app --host 0.0.0.0 --port 15800 --reload
