#!/usr/bin/env bash
set -e

PI_DEST="${1:-trading212-pi}"
REMOTE_DIR="/home/shahpratik11/modest-bose"

echo "📥 Fetching live trading state and dashboard from $PI_DEST..."

scp "$PI_DEST:$REMOTE_DIR/active_trades.json" ./active_trades.json
scp "$PI_DEST:$REMOTE_DIR/signals_dashboard.html" ./signals_dashboard.html
scp "$PI_DEST:$REMOTE_DIR/notifications.jsonl" ./notifications.jsonl 2>/dev/null || true
scp "$PI_DEST:$REMOTE_DIR/trading_bot.log" ./pi_trading_bot.log 2>/dev/null || true

echo "✅ Live state synchronized to local workspace."
