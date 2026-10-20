#!/usr/bin/env bash
set -e

PI_DEST="${1:-trading212-pi}"
REMOTE_DIR="/home/shahpratik11/modest-bose"

echo "========================================================"
echo "🚀 Deploying to Raspberry Pi ($PI_DEST)..."
echo "========================================================"

# 1. Create remote directory if missing
ssh "$PI_DEST" "mkdir -p $REMOTE_DIR"

# 2. If .env does not exist on remote Pi, copy it over once
if ! ssh "$PI_DEST" "[ -f $REMOTE_DIR/.env ]"; then
    echo "Copying initial .env credentials to Pi..."
    scp .env "$PI_DEST:$REMOTE_DIR/.env"
fi

# 3. If active_trades.json does not exist on remote Pi, copy current state
if ! ssh "$PI_DEST" "[ -f $REMOTE_DIR/active_trades.json ]"; then
    echo "Copying initial active_trades.json to Pi..."
    scp active_trades.json "$PI_DEST:$REMOTE_DIR/active_trades.json"
fi

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
echo "Dashboard is live at: http://192.168.68.61:8080"
echo "========================================================"
