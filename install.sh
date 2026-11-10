#!/bin/bash
# GoCharting Order Flow MCP Server - Automated Ubuntu Installer
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

echo "=========================================================="
echo "🚀 Installing GoCharting Order Flow MCP Server on AWS..."
echo "=========================================================="

# 1. Setup 2 GB Swap File to prevent memory issues
if [ ! -f /swapfile ]; then
    echo "📦 Configuring 2 GB Swap memory..."
    sudo fallocate -l 2G /swapfile
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile
    sudo swapon /swapfile
    echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
fi

# 2. Install System Dependencies
echo "📦 Installing system dependencies, Xvfb, noVNC, and Python..."
sudo apt-get update -y
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
    wget curl git python3 python3-pip python3-venv \
    xvfb x11vnc novnc websockify \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 fonts-liberation

sudo apt-get install -y libasound2t64 2>/dev/null || sudo apt-get install -y libasound2 2>/dev/null || true

# 3. Install Official Google Chrome
if ! command -v google-chrome &>/dev/null; then
    echo "🌐 Installing official Google Chrome..."
    wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
    sudo dpkg -i google-chrome-stable_current_amd64.deb || sudo apt-get -f install -y
    rm -f google-chrome-stable_current_amd64.deb
fi

# 4. Setup Python Virtual Environment
echo "🐍 Setting up Python virtual environment..."
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi
source .venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q

# 5. Create start_all.sh Runner
cat << 'RUNNER' > "$DIR/start_all.sh"
#!/bin/bash
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

echo "🛑 Cleaning previous processes..."
pkill -f "Xvfb :99" 2>/dev/null || true
pkill -f "websockify.*6080" 2>/dev/null || true
pkill -f "x11vnc.*99" 2>/dev/null || true
pkill -f "google-chrome.*9222" 2>/dev/null || true
pkill -f "python3.*server.py" 2>/dev/null || true
sleep 2

echo "🖥️ Starting Xvfb virtual display on :99..."
Xvfb :99 -screen 0 1920x1080x24 &
sleep 2

echo "👁️ Starting x11vnc and noVNC on port 6080..."
x11vnc -display :99 -forever -nopw -shared -bg
websockify --web /usr/share/novnc 6080 localhost:5900 &
sleep 1

echo "🌐 Starting Bot Google Chrome on port 9222..."
DISPLAY=:99 google-chrome \
    --no-sandbox \
    --disable-gpu \
    --disable-dev-shm-usage \
    --user-data-dir="$DIR/bot_chrome_profile" \
    --remote-debugging-port=9222 \
    --remote-allow-origins=* \
    --no-first-run \
    --no-default-browser-check \
    "https://gocharting.com/terminal/chart/yDoy4eLHt" &
sleep 4

echo "🚀 Starting GoCharting MCP Server on port 8765..."
nohup "$DIR/.venv/bin/python3" "$DIR/server.py" > "$DIR/server.log" 2>&1 &
sleep 2

PUBLIC_IP=$(curl -s ifconfig.me || echo "YOUR_SERVER_IP")
echo "=========================================================="
echo "✅ All services started successfully!"
echo "📡 MCP SSE Endpoint: http://${PUBLIC_IP}:8765/sse"
echo "🖥️ Web Remote Desktop: http://${PUBLIC_IP}:6080/vnc.html"
echo "=========================================================="
RUNNER

chmod +x "$DIR/start_all.sh"
chmod +x "$DIR/update.sh"
chmod +x "$DIR/test_suite.py"

# 6. Start services
echo "🚀 Launching all services..."
"$DIR/start_all.sh"
