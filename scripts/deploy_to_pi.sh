#!/usr/bin/env bash
set -e

# Auto-discover reachable Raspberry Pi host if none provided
if [ -n "$1" ]; then
    PI_DEST="$1"
else
    echo "🔍 Probing for active Raspberry Pi host on local network..."
    CANDIDATES=("shahpratik11@192.168.178.54" "trading212-pi" "shahpratik11@trading212-pi.local" "shahpratik11@192.168.68.50" "shahpratik11@192.168.68.61")
    FOUND=""
    for cand in "${CANDIDATES[@]}"; do
        if ssh -o ConnectTimeout=2 -o StrictHostKeyChecking=no "$cand" "true" 2>/dev/null; then
            FOUND="$cand"
            echo "✅ Reachable host detected: $FOUND"
            break
        fi
    done
    if [ -z "$FOUND" ]; then
        echo "❌ Error: Could not connect to any Raspberry Pi host (${CANDIDATES[*]})."
        echo "Please specify host manually: ./scripts/deploy_to_pi.sh user@<IP>"
        exit 1
    fi
    PI_DEST="$FOUND"
fi
REMOTE_DIR="/home/shahpratik11/modest-bose"

echo "========================================================"
echo "🚀 Deploying to Raspberry Pi ($PI_DEST)..."
echo "========================================================"

# 1. Create remote directory if missing
ssh "$PI_DEST" "mkdir -p $REMOTE_DIR"

# 2. Sync .env credentials and risk settings to Pi
echo "Syncing .env configuration to Pi..."
scp .env "$PI_DEST:$REMOTE_DIR/.env"

# 3. Sync recalibrated active_trades.json to Pi
echo "Syncing recalibrated active_trades.json to Pi..."
scp active_trades.json "$PI_DEST:$REMOTE_DIR/active_trades.json"

# 4. Sync application code (excluding runtime state, logs, and git)
echo "Syncing code files..."
rsync -avz --delete \
  --exclude '.git' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude '.DS_Store' \
  --exclude 'active_trades.json' \
  --exclude 'trades.jsonl' \
  --exclude 'notifications.jsonl' \
  --exclude '*.log' \
  --exclude '.env' \
  --exclude 'venv' \
  --exclude '.t212_rate_limit.json' \
  ./ "$PI_DEST:$REMOTE_DIR/"

# 5. Make scripts executable on Pi
ssh "$PI_DEST" "chmod +x $REMOTE_DIR/scripts/*.sh"

# 6. Run setup or restart service
ssh "$PI_DEST" << 'EOF'
  set -e
  cd /home/shahpratik11/modest-bose
  if [ ! -d "venv" ] || [ ! -f "/etc/systemd/system/trading-bot.service" ]; then
      ./scripts/setup_pi.sh
  else
      echo "Updating Python dependencies..."
      ./venv/bin/pip install -r trading_system/requirements.txt
      echo "Restarting trading-bot service..."
      sudo systemctl restart trading-bot
      sudo systemctl status trading-bot --no-pager
  fi
EOF

echo "========================================================"
echo "🎉 Deployment Successful!"
DASH_HOST=$(echo "$PI_DEST" | sed 's/.*@//')
echo "Permanent URL (never changes): http://trading212-pi.local:8080"
echo "Direct IP URL:               http://${DASH_HOST}:8080"
echo "========================================================"
