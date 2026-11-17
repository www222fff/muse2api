#!/usr/bin/env bash
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

if [ -f data/gateway.pid ]; then
  PID=$(cat data/gateway.pid)
  if kill -0 "$PID" 2>/dev/null; then
    echo "Stopping muse2api gateway (PID $PID)..."
    kill "$PID" 2>/dev/null || true
  fi
  rm -f data/gateway.pid
fi

# Ensure port 18610 and any orphan gateway processes are cleared
pkill -9 -f "python -m muse2api" 2>/dev/null || true
lsof -ti :18610 | xargs kill -9 2>/dev/null || true
echo "Gateway stopped."
