#!/bin/bash
# GoCharting MCP - Seamless Cloud Updater
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

echo "=========================================================="
echo "🔄 Updating GoCharting MCP Server to v2.2.1..."
echo "=========================================================="

# 1. Ensure git remote uses HTTPS so it doesn't fail on missing SSH keys
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "🌐 Setting HTTPS remote origin..."
    git remote set-url origin https://github.com/maheshkumarreddy/GoChartingMCP.git 2>/dev/null || true
    echo "📥 Fetching and resetting to origin/main..."
    git fetch origin main
    git reset --hard origin/main
fi

# 2. Check virtualenv & dependencies
echo "📦 Verifying dependencies..."
if [ -d "$DIR/.venv" ]; then
    source "$DIR/.venv/bin/activate"
    pip install -r requirements.txt -q
fi

# 3. Aggressively kill any old running server processes on port 8765
echo "🛑 Terminating any existing processes on port 8765..."
fuser -k 8765/tcp 2>/dev/null || true
pkill -9 -f "server.py" 2>/dev/null || true
sleep 2

# 4. Launch new server
echo "🚀 Starting new GoCharting MCP Server v2.2.1..."
nohup "$DIR/.venv/bin/python3" "$DIR/server.py" > "$DIR/server.log" 2>&1 &
sleep 3

# 5. Verify local health
echo "🩺 Verifying server status..."
curl -s http://127.0.0.1:8765/health || echo "⚠️ Server starting up..."
echo ""
echo "=========================================================="
echo "✅ Server successfully updated and running on port 8765!"
echo "=========================================================="

