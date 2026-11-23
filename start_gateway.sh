#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

# Clean up any existing instances first
./stop_gateway.sh 2>/dev/null || true

# Ensure directory exists and clean up stale locks if any
mkdir -p data/logs data/browser-profile
rm -f data/browser-profile/Singleton* 2>/dev/null || true

echo "Starting muse2api gateway on http://127.0.0.1:18610..."
nohup .venv/bin/python -m muse2api > data/logs/gateway.log 2>&1 &
PID=$!
echo $PID > data/gateway.pid
echo "Gateway running with PID: $PID"
echo "Logs: $DIR/data/logs/gateway.log"
echo "Dashboard: http://127.0.0.1:18610/dashboard"
