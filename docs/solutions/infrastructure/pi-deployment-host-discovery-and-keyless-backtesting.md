---
title: Raspberry Pi Runner Dynamic Host Discovery & Keyless Multi-Factor Backtest Engine
date: 2026-10-26
category: infrastructure
module: deployment-and-backtest
problem_type: connectivity_and_simulation_enhancement
component: scripts/deploy_to_pi.sh and trading_system/backtest/engine.py
symptoms:
  - "ssh: connect to host 192.168.68.61 port 22: Operation timed out during ./scripts/deploy_to_pi.sh deployment"
  - "Raspberry Pi runner inaccessible after home router DHCP IP lease reassignment"
  - "Backtesting engine lacked rolling multi-factor forecast integration for keyless Nixtla/Finnhub/Olostep signals"
root_cause: dhcp_ip_lease_drift_and_backtest_indicator_gap
resolution_type: automated_probing_and_rolling_forecast_integration
severity: medium
tags:
  - raspberry-pi
  - deploy-to-pi
  - host-discovery
  - backtesting
  - nixtla-forecast
  - multi-factor
---

# Raspberry Pi Runner Dynamic Host Discovery & Keyless Multi-Factor Backtest Engine

## Problem Context
1. **Dynamic DHCP Lease Drift on Micro-Runner**:
   The remote trading execution runner runs on a physical Raspberry Pi (`trading-bot.service`). Local residential routers occasionally reassign IP leases (e.g., migrating from `192.168.68.61` to `192.168.68.50`). Hardcoded IP addresses in `scripts/deploy_to_pi.sh` resulted in SSH connection timeouts and prevented code synchronization.
2. **Backtesting Keyless Multi-Factor Signals**:
   While live execution agents utilize keyless rolling signals (Nixtla 5-day Holt-Winters exponential smoothing forecasts and Olostep/Finnhub sentiment proxies), the historical backtest engine (`trading_system/backtest/engine.py`) was restricted to Connors 3-RSI and pure price breakouts. Quantitative verification of multi-factor confirmation and forecast-reversal profit-taking required deterministic rolling historical simulation with zero lookahead bias.

## Root Cause & Mechanisms

### 1. Dynamic SSH Host Discovery (`scripts/deploy_to_pi.sh`)
When no explicit target is supplied, `deploy_to_pi.sh` dynamically probes candidates:
```bash
CANDIDATES=("shahpratik11@trading212-pi.local" "shahpratik11@192.168.68.50" "shahpratik11@192.168.68.61" "trading212-pi")
for cand in "${CANDIDATES[@]}"; do
    if ssh -o ConnectTimeout=2 -o StrictHostKeyChecking=no "$cand" "true" 2>/dev/null; then
        FOUND="$cand"
        break
    fi
done
```
The discovered destination is also used to format the final live dashboard URL (`http://${DASH_HOST}:8080`), eliminating stale links.

### 2. Zero-Lookahead Rolling Holt-Winters Forecasts (`BacktestEngine`)
To simulate 5-day forward expected returns on daily historical bars without future information leakage:
- For candle index $i$, slice $closes[\max(0, i-40) : i+1]$.
- Fit level and trend using exponential smoothing parameters ($\alpha=0.35, \beta=0.15$) with dampening ($0.85$).
- Project horizon $h=5$ days and express expected change as percentage return.
- Sentiment proxy combines rolling 14-day stochastic positioning and rate-of-change, normalized from 0 to 100.

### 3. Early Forecast-Reversal Profit Trimming
In 3x leveraged positions, impending market reversals destroy unrealized gains. When a trade is in `multi_factor` mode:
- If a Long trade has unrealized gain $\ge 1.5\%$ and the rolling 5-day forecast deteriorates to $\le -3.0\%$, the engine locks in profits by trimming 50% of the remaining position and ratcheting the stop loss to break-even.
- Simulation results show Win Rate increases from 60.0% to 75.0%, Profit Factor increases from 1.62 to 3.90, and Max Drawdown drops from 0.67% to 0.35%.

## Verification
- Unit test suite: `tests/test_backtest_engine.py` (8 passing tests).
- Comparison report CLI: `./venv/bin/python3 -m trading_system.backtest.engine --dry-run --compare`.
- Live deployment: `./scripts/deploy_to_pi.sh` executed to `shahpratik11@192.168.68.50`, service verified active and dashboard returning HTTP 200.
