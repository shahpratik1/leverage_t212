#!/usr/bin/env bash
set -e

echo "⏳ Watching for Raspberry Pi to boot up on the network..."
CANDIDATES=("trading212-pi" "shahpratik11@trading212-pi.local" "shahpratik11@192.168.178.54" "shahpratik11@192.168.68.50" "shahpratik11@192.168.68.61")

while true; do
    for cand in "${CANDIDATES[@]}"; do
        if ssh -o ConnectTimeout=2 -o StrictHostKeyChecking=no "$cand" "true" 2>/dev/null; then
            echo "🎉 Reachable host detected: $cand. Starting deployment..."
            ./scripts/deploy_to_pi.sh "$cand"
            exit 0
        fi
    done
    sleep 5
done
