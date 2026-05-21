#!/bin/bash
set -e

echo "Stopping MultiClaw on port 15800..."
lsof -ti:15800 | xargs kill 2>/dev/null && echo "Done." || echo "No process found on port 15800."
