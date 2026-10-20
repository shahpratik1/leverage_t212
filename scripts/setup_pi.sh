#!/usr/bin/env bash
set -e

APP_DIR="/home/shahpratik11/modest-bose"
SERVICE_NAME="trading-bot"

echo "========================================================"
echo "Setting up Trading 212 Engine on Raspberry Pi..."
echo "========================================================"

cd "$APP_DIR"

# 1. Ensure Python Virtual Environment exists
if [ ! -d "$APP_DIR/venv" ]; then
    echo "Creating virtual environment at $APP_DIR/venv..."
    python3 -m venv "$APP_DIR/venv"
fi

# 2. Activate and install dependencies
source "$APP_DIR/venv/bin/activate"
pip install --upgrade pip

echo "Installing trading engine dependencies..."
# Core quantitative & data dependencies
pip install -r "$APP_DIR/trading_system/requirements.txt"

# 3. Setup systemd service
echo "Configuring systemd service..."
sudo cp "$APP_DIR/scripts/trading-bot.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

echo "========================================================"
echo "✅ Setup Complete!"
echo "Service status:"
sudo systemctl status "$SERVICE_NAME" --no-pager
echo "========================================================"
echo "Dashboard running at: http://$(hostname -I | awk '{print $1}'):8080"
