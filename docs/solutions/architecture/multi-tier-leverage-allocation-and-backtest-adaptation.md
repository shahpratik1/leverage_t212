# Multi-Tier 3x & 5x Leverage Allocation and Strategy Backtest Adaptation

## Problem
1. **Binary 5x Replacement Limitation**: Previously, assets supporting 5x leverage (`QQQ`, `SPY`, `GLD`, `TLT`) were forced to 5x exclusively, replacing 3x. During elevated market volatility, holding 5x leverage incurs significant volatility drag (beta decay). A flexible multi-tier allocation strategy was needed to allow allocating 3x or 5x based on setup conviction and market regime volatility.
2. **Unvalidated Heuristic Parameter Adaptation**: The `StrategyImprovementAgent` recommended threshold adjustments during low-volatility starvation without quantitative backtest verification.
3. **Frontend Telemetry Gap**: The signals dashboard lacked visual leverage tier pills (`3x` vs `5x`) and historical session equity curve tracking.

## Solution & Architecture
1. **Dynamic Multi-Tier Multipliers**:
   - In `LeveragedUniverse`, updated `get_available_leverage_multipliers()` to return `[3, 5]` when `pair.has_5x` is True.
   - In `RiskManager` and `Orchestrator`, high-conviction setups ($\ge 0.85$) with moderate volatility ($\le 35\%$) receive 5x leverage; standard setups ($0.70 - 0.85$) or higher-volatility setups receive 3x leverage.
2. **Automated Backtest Verification**:
   - Added `verify_parameter_adaptation()` to `StrategyImprovementAgent`, running simulations over historical candles via `BacktestEngine` to confirm win-rate ($\ge 70\%$) and profit factor ($\ge 1.2$) before emitting adaptive recommendations.
3. **Telemetry & Dashboard Enhancements**:
   - In `server.py`, included `leverage` tier and `equity_history` points tracking session capital trajectory.
   - In `signals_dashboard.html`, added bold `5x Institutional` and `3x Daily` visual pills.

## Verification
- 188 unit tests passing with zero regressions across `test_5x_leveraged_universe.py`, `test_strategy_improvement_agent.py`, `test_embedded_dashboard.py`, and the full suite.
- Deployed to Raspberry Pi runner and verified via `/api/data`.
