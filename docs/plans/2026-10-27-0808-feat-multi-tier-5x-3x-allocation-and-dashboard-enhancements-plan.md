# Implementation Plan: Simultaneous 3x/5x Multi-Tier Leverage Allocation, Strategy Backtest Verification, and Telemetry Dashboard Upgrades

> **Plan Status:** Implementation-Ready  
> **Target Date:** 2026-10-27  
> **Author:** Antigravity / Compound Engineering (`ce-plan`)

---

## 1. Overview & Objectives

This implementation delivers four core enhancements requested for the quantitative trading engine:

1. **Simultaneous 3x & 5x Multi-Tier Leverage Allocation**:
   - Previously, when an asset had 5x leverage (e.g. `QQQ`, `SPY`, `GLD`, `TLT`), the system forced leverage to 5x exclusively, replacing 3x.
   - We update [`LeveragedUniverse`](file:///Users/pratikshah/Documents/antigravity/modest-bose/trading_system/data/leveraged_universe.py) and [`RiskManager`](file:///Users/pratikshah/Documents/antigravity/modest-bose/trading_system/agents/risk_manager.py) to support simultaneous multi-tier allocation: both `[3, 5]` multipliers are available on 5x assets.
   - Dynamic tier selection: High-conviction setups ($\ge 0.85$) with controlled volatility ($\le 35\%$) receive 5x leverage; standard conviction ($0.70 - 0.85$) or higher-volatility setups receive 3x leverage to protect against beta slippage.
2. **Strategy Improvement Agent Backtest Verification**:
   - Connect [`StrategyImprovementAgent`](file:///Users/pratikshah/Documents/antigravity/modest-bose/trading_system/agents/strategy_improvement_agent.py) with [`BacktestEngine`](file:///Users/pratikshah/Documents/antigravity/modest-bose/trading_system/backtest/engine.py).
   - When parameter tuning is recommended (e.g., broadening RSI3 threshold during low-volatility regimes), the agent automatically backtests candidate thresholds against cached candle data to verify Sharpe and win-rate improvement before recommending adoption.
3. **Dashboard Real-Time Telemetry & Equity Curve**:
   - In [`signals_dashboard.html`](file:///Users/pratikshah/Documents/antigravity/modest-bose/signals_dashboard.html) and [`server.py`](file:///Users/pratikshah/Documents/antigravity/modest-bose/trading_system/dashboard/server.py), add dynamic leverage tier badges (`3x` / `5x`), display live spread percentages, and introduce an SVG/Canvas equity curve tracking portfolio valuation over time.
4. **Production Verification on Raspberry Pi Runner**:
   - Deploy code to the Raspberry Pi 5 runner via [`deploy_to_pi.sh`](file:///Users/pratikshah/Documents/antigravity/modest-bose/scripts/deploy_to_pi.sh), execute a live scan & reconciliation cycle, and verify `/api/data` and `/api/live`.

---

## 2. Settled Decisions

- **Decision 1**: Both 3x and 5x leverage multipliers must be supported on assets with 5x instruments.
  - *Provenance*: User-directed.
  - *Rejected alternative*: Exclusive 5x-only replacement policy.
  - *Reason*: Enables proportional volatility-adjusted capital scaling, reserving 5x for the highest-conviction and lowest-drag opportunities.
- **Decision 2**: Leverage tier resolution is driven by conviction and realized volatility.
  - *Provenance*: User-directed.
  - *Rejected alternative*: Hardcoded fixed multiplier.
  - *Reason*: Leveraged ETPs suffer volatility drag (beta decay) during high-volatility chop; 3x is safer when volatility is elevated.
- **Decision 3**: Backtest verification must validate parameter changes before adoption.
  - *Provenance*: User-directed.
  - *Rejected alternative*: Unvalidated heuristic threshold adjustments.
  - *Reason*: Guarantees quantitative validity and prevents overfitting during market regime shifts.

---

## 3. Step-by-Step Implementation Work breakdown

### Phase 1: Multi-Tier Leverage Allocation Engine
- **File**: [`trading_system/data/leveraged_universe.py`](file:///Users/pratikshah/Documents/antigravity/modest-bose/trading_system/data/leveraged_universe.py)
  1. Update `get_available_leverage_multipliers(underlying: str) -> List[int]`:
     - Return `[3, 5]` when `pair.has_5x` is True.
     - Return `[3]` when standard 3x pair.
     - Return `[1]` for ordinary uncurated underlying.
  2. Update `get_execution_ticker(underlying, direction, leverage, ...)`:
     - If `leverage == 5` and `pair.has_5x`: return 5x ticker (`long_5x_ticker` / `short_5x_ticker`).
     - If `leverage == 3` (even if `pair.has_5x`): return 3x ticker (`long_3x_ticker` / `short_3x_ticker`).
     - If `leverage is None`: default to 5 if `has_5x`, else 3.
  3. Ensure `get_pair_for_etp` and `get_etp_direction` cleanly identify both 3x and 5x tickers.
- **File**: [`trading_system/agents/risk_manager.py`](file:///Users/pratikshah/Documents/antigravity/modest-bose/trading_system/agents/risk_manager.py)
  1. In `calculate_decision_card`:
     - Determine dynamic leverage:
       ```python
       has_5x = universe.has_5x_leverage(ticker, direction)
       realized_vol = metrics.get("realized_vol", 0.25)
       conviction = getattr(debate_outcome, "conviction_score", 0.5)
       
       if has_5x and getattr(settings, "ENABLE_5X_LEVERAGE", True):
           # Allocate 5x for high conviction with moderate volatility, else 3x
           if conviction >= 0.85 and realized_vol <= 0.35:
               resolved_leverage = 5
           else:
               resolved_leverage = 3
       else:
           resolved_leverage = 3
       ```
     - Resolve execution ticker using `resolved_leverage`.
     - Tag `card.leverage = resolved_leverage`.

### Phase 2: Strategy Improvement Agent Automated Backtesting
- **File**: [`trading_system/agents/strategy_improvement_agent.py`](file:///Users/pratikshah/Documents/antigravity/modest-bose/trading_system/agents/strategy_improvement_agent.py)
  1. Import `BacktestEngine` from `trading_system.backtest.engine`.
  2. In `analyze_performance()` or `evaluate_parameter_adjustment()`:
     - When starvation or suboptimal win-rate is detected, generate candidate parameter variants (e.g. `rsi3_oversold`: 20, 22, 25).
     - Run quick backtest on recent candle data across benchmark index (e.g. `QQQ` / `SPY`).
     - Only include recommendation if candidate win-rate $> 75\%$ and profit factor $> 1.5$.
     - Include backtest metrics (win-rate, total trades, profit factor) in recommendation dictionary.

### Phase 3: Dashboard UI Enhancements & Telemetry
- **File**: [`trading_system/dashboard/server.py`](file:///Users/pratikshah/Documents/antigravity/modest-bose/trading_system/dashboard/server.py)
  1. In `load_active_signals()`:
     - Include `"leverage": pos.get("leverage") or (5 if "5" in ticker else 3)` in signal dictionary.
     - Include `"spread_pct"` from live spread info.
  2. In `build_dashboard_data()`:
     - Compute historical equity points from `trades.jsonl` closed trades + current open P&L for equity curve plotting.
     - Return `"equity_history": [...]` in dashboard data payload.
- **File**: [`signals_dashboard.html`](file:///Users/pratikshah/Documents/antigravity/modest-bose/signals_dashboard.html)
  1. Add a visual `3x` / `5x` leverage pill badge on each active position card:
     - `5x`: Bold purple/amber gradient badge (`5x Leverage Shares`).
     - `3x`: Sleek indigo badge (`3x Daily Leveraged`).
  2. Add an SVG Equity Curve Sparkline card in the dashboard header showing session capital trajectory.

### Phase 4: Unit Testing & Verification
- Update existing tests and add new tests:
  - `tests/test_5x_leveraged_universe.py`: Test `get_available_leverage_multipliers` returning `[3, 5]`, and `get_execution_ticker(..., leverage=3)` on QQQ returning `QQQ3m_EQ` while `leverage=5` returns `5QQEl_EQ`.
  - `tests/test_strategy_improvement_agent.py`: Test backtest integration during starvation adaptation.
  - `tests/test_embedded_dashboard.py`: Test `equity_history` payload and `leverage` badge fields.
- Run complete test suite (`./venv/bin/python3 -m unittest discover tests`) - ensure 100% pass rate.

### Phase 5: Raspberry Pi Deployment & Live Runner Scan
- Run `./scripts/deploy_to_pi.sh`.
- SSH to the Raspberry Pi runner and trigger a live system scan and broker reconciliation cycle.
- Verify live dashboard at `http://192.168.68.50:8080` and verify JSON API response from `/api/data`.

---

## 4. Invariants & Safety Rules

- **Zero Regression**: All existing 187 tests must continue passing.
- **Deterministic Ticker Resolution**: Never return a 5x ticker when 3x is explicitly requested, and vice-versa.
- **Spot Security Mechanics**: All ETPs remain spot holdings ($Q > 0$).
- **GTC Native Stop**: Native server stop orders remain mandatory at fill time.
