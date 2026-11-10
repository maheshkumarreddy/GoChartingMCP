#!/bin/bash
# GoCharting MCP - Seamless Cloud Updater
# Pulls latest code changes and gracefully restarts only the Python MCP server
# (Chrome and GoCharting remain open and undisturbed)

set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

echo "🔄 Pulling latest changes from Git..."
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git pull || echo "⚠️ Git pull skipped or not tracking remote yet."
fi

echo "📦 Checking Python dependencies..."
source .venv/bin/activate
pip install -r requirements.txt -q

echo "🧪 Running self-test..."
python3 test_suite.py

echo "🔄 Restarting Python MCP Server..."
pkill -f "python3.*server.py" 2>/dev/null || true
sleep 1
nohup "$DIR/.venv/bin/python3" "$DIR/server.py" > "$DIR/server.log" 2>&1 &

echo "✅ Update complete! Server running on port 8765."

