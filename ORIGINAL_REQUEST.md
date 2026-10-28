# Original User Request

## 2026-10-28T15:15:58Z

# Quantitative Trading Strategy Alpha Overhaul & Systemic Architectural Harmonization

High-expectancy quantitative strategy overhaul and systemic architectural fix for the Trading 212 algorithmic engine: Prioritize adding a high-profit Multi-Regime Trading Strategy Ensemble (ADX regime classification combining 3-RSI Mean Reversion for chop with EMA/Donchian Trend Breakouts for rallies) with rigorous historical backtesting, followed by patching core architecture discrepancies (live ETP pricing scale, GBX/EUR currency normalization, and broker-anchored equity curve).

Working directory: /Users/pratikshah/Documents/antigravity/modest-bose
Integrity mode: demo

## Requirements

### R1. Multi-Regime Strategy Ensemble & Alpha Engine
- Implement an automated Market Regime Classifier in `trading_system/screener/` and `trading_system/agents/technical_agent.py`:
  - **Chop / Range Regime (ADX < 22):** Route to Fast 3-RSI Mean Reversion with Bollinger / Keltner Bands to capture rapid 1-3 day relief swings.
  - **Trending / Breakout Regime (ADX >= 25, 20 EMA > 50 EMA):** Route to Donchian Channel / EMA Momentum Breakout to ride multi-day trends in 3x leveraged equities and tech ETPs.
  - **High-Vol Erratic Regime (Realized Vol > 65% or ATR Spike):** Reduce position sizing or enforce strict cash preservation.
- Calibrate dynamic profit-taking: implement 3-tier milestone ratchets (+1.5R, +2.5R, +3.5R) and progressive trailing stops to aggressively lock in gains before leverage compounding decay or session-close cutoffs.

### R2. Quantitative Backtesting, Edge Verification & Walk-Forward Validation
- Extend `trading_system/backtest/engine.py` to evaluate the multi-regime ensemble against historical 1-year OHLCV candles across the 120-asset 3x universe.
- Measure and enforce quantifiable statistical edge: benchmark target of Sharpe ratio >= 1.5, Profit Factor >= 1.75, and Win Rate >= 60% on validated historical datasets.
- Ensure all strategy transitions pass regression testing with 0 regressions across the 192+ unit test suite.

### R3. Architectural Discrepancy & Currency Scale Patching
- Fix the **underlying vs. ETP pricing mismatch** in `trading_system/orchestrator.py` and `trading_system/agents/risk_manager.py`: before calculating `position_size_shares`, query the live price and currency of the actual execution instrument (`card.trading212_ticker`).
- Normalize **LSE GBX (pence) to GBP/EUR**: divide pence quotes by 100 before computing notional values, ensuring no position ever exceeds the configured 20% equity cap.
- Replace hardcoded synthetic starting equity (`base_equity = 10000.0`) in `trading_system/dashboard/server.py` with live broker ground truth from `client.get_account_cash().total` (€4,923.05).
- Sync updated code and services to the live Raspberry Pi environment.

## Acceptance Criteria

### Strategy Alpha & Expectancy
- [ ] Multi-regime classifier correctly distinguishes trending vs range-bound regimes and triggers appropriate strategy signals (3-RSI vs Breakout).
- [ ] Backtest engine produces simulated results with Sharpe ratio >= 1.5 and Profit Factor >= 1.75 on benchmark assets.
- [ ] Dynamic trailing stop ratchets lock in gains progressively when positions reach +1.5% and +3.0% unrealized profit.

### Architectural Precision & Execution Safety
- [ ] Position sizing calculation queries `card.trading212_ticker` quote and currency, verifying that 3GISl_EQ (7805p) sizes to ~10 shares (~€940) rather than 24 shares (~€2,256).
- [ ] GBX pence quotes are normalized to base EUR currency across order calculation, active trades tracking, and risk management.
- [ ] Live dashboard Portfolio Equity Curve and Session Trajectory dynamically mirror real Trading 212 account equity (€4,923.05) with 0 hardcoded fallback numbers.
- [ ] 100% of unit tests pass with zero errors.
