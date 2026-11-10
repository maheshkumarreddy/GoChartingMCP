#!/bin/bash
# Dedicated Chrome Browser for GoCharting Order Flow MCP
# Completely isolated from your personal Chrome profile.

PROFILE_DIR="$HOME/Desktop/GoChartingMCP/bot_chrome_profile"
CHROME_APP="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

echo "============================================================"
echo "🚀 Launching Dedicated Bot Chrome for GoCharting..."
echo "📁 Profile directory: $PROFILE_DIR"
echo "🌐 Remote debugging port: 9222 (for automated control)"
echo "============================================================"

mkdir -p "$PROFILE_DIR"

"$CHROME_APP" \
  --user-data-dir="$PROFILE_DIR" \
  --remote-debugging-port=9222 \
  --remote-allow-origins=* \
  --no-first-run \
  --no-default-browser-check \
  "https://gocharting.com" &

echo "✅ Bot Chrome launched. Your personal Chrome remains completely separate!"

