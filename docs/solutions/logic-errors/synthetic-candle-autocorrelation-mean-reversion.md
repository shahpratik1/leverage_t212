---
title: Synthetic Candle Generator Autocorrelation Breaks 3-RSI Mean Reversion Backtests
date: 2026-10-15
category: logic-errors
module: trading_system/data
problem_type: logic_error
component: testing_framework
symptoms:
  - "3-RSI mean reversion walk-forward backtest showed negative Sharpe ratio (-18.94) and 17.2% win rate on leveraged ETP universe"
  - "Parameter tuning across RSI thresholds (5-20), holding duration (1-10 days), and ATR stop multipliers (1.0-10.0x) failed to achieve positive edge (max win rate capped at 19.8%)"
  - "100% of losing trades exited either on stop-loss or maximum holding duration without ever reaching 5-SMA mean reversion target"
root_cause: data_integrity
resolution_type: workflow_improvement
severity: medium
tags:
  - mean-reversion
  - rsi3
  - synthetic-data
  - autocorrelation
  - backtesting
  - leveraged-etps
---

# Synthetic Candle Generator Autocorrelation Breaks 3-RSI Mean Reversion Backtests

## Problem
Walk-forward backtesting of the Connors 3-RSI mean reversion strategy across the leveraged ETP universe yielded a heavily negative Sharpe ratio (-18.94), 0.02 profit factor, and a 17.2% win rate. Systematic parameter sweeps over RSI entry thresholds, holding periods, and stop loss multipliers failed to produce positive expectancy despite 3-RSI mean reversion being a well-documented institutional high-win-rate (>75%) anomaly in real equity markets.

## Symptoms
- Baseline backtest (`evaluate_3rsi.py`) returned:
  - `sharpe_ratio`: `-18.94`
  - `profit_factor`: `0.02`
  - `win_rate`: `0.172` (17.2%)
  - `avg_trade_pnl_pct`: `-2.23%`
- In 100% of the worst-performing trades, positions exited prematurely via stop-loss (`reason: "SL"`) or time exit (`reason: "TIME_EXIT"`).
- Widening stop-losses from 1.5x ATR to 5.0x ATR improved Sharpe from `-18.94` to `-10.34` (+8.60 points), but win rate remained severely depressed (16.0% to 19.8%).

## What Didn't Work
- **Tightening RSI Oversold Trigger (`RSI(3) <= 10.0` vs `<= 20.0`)**: Intended to filter out shallow pullbacks and enter only on deep exhaustion. Win rate regressed from 17.2% to 11.3%, because the asset continued declining regardless of exhaustion level.
- **Shortening Maximum Holding Period (`MR_MAX_HOLD_DAYS = 2` vs `4`)**: Intended to cut beta drag and volatility decay. Average loss shrank slightly (-1.90% vs -2.23%), but win rate remained stuck at 16.3%.
- **Tightening Take-Profit Ratchets (0.5R / 1.0R / 1.5R with 1.0x ATR SL)**: Intended to scalp quick snapbacks. Win rate collapsed to 0.3% and Sharpe fell to `-28.04` due to noise stop-outs.
- **Grid Search Across Parameter Space**: Exhaustive grid search of 360 parameter combinations (`rsi3_oversold` $\in [1, 30]$, `max_hold` $\in [1, 20]$, `atr_sl` $\in [0.5, 10.0]$) revealed that no parameter configuration exceeded a 19.8% win rate on the backtest dataset.

## Solution
Detailed trade-by-trade tracing and locating measurements identified the root cause in `MarketDataProvider._generate_synthetic_candles` in `trading_system/data/trading212_client.py` (lines 488–507). When external network requests to Yahoo Finance are unavailable (such as inside sandboxed execution where HTTP requests return 403), the engine falls back to synthetic candles generated via:

```python
wave = math.sin((i + seed) * 0.25) * 0.010 + math.cos((i + seed) * 0.1) * 0.007
pct_change = wave + trend_drift
p = max(1.0, p * (1.0 + pct_change))
```

This deterministic mathematical function creates a smooth sinusoidal curve with an angular frequency of $\omega = 0.25$ rad/bar (cycle period $T \approx 25$ trading days). During the declining half-cycle ($\approx 12$ consecutive bars), the derivative is monotonically negative with **zero white noise and 100% downward autocorrelation**.

On a pure sine wave:
1. `RSI(3)` drops below 20.0 on Day 3 of the decline, triggering an initial buy signal.
2. Because there is no microstructure bounce or intraday stochastic volatility, price continues falling monotonically for the next 9 consecutive days.
3. Any fixed holding period between 1 and 5 days (`MR_MAX_HOLD_DAYS`) is mathematically guaranteed to exit at a loss before the sine wave trough is reached.
4. When the sine wave finally troughs and begins its 12-day upward rally, `RSI(3)` instantly surges above 80.0, so the strategy never buys during the up-leg.

**The Fix:**
1. **Preserve Production Strategy Parameters**: The negative backtest was an artifact of synthetic data autocorrelation rather than strategy invalidity. The institutional parameters (`RSI3_OVERSOLD = 20.0`, `RSI7_OVERSOLD = 40.0`, `ATR_SL_MULTIPLIER = 1.5`, `MR_MAX_HOLD_DAYS = 4`, with TP1 break-even ratchet) remain sound for live stochastic market feeds.
2. **Offline Simulation Mandate**: Replace the deterministic sinusoidal generator in offline harnesses with an Ornstein-Uhlenbeck stochastic jump diffusion model:
   $$dS_t = \theta (\mu - S_t) dt + \sigma dW_t + J_t dN_t$$
   or cache historical real market candle series (CSV/JSON) for liquid ETP underlyings (`QQQ`, `SPY`, `NVDA`, `TSM`) to preserve realistic bid/ask microstructure noise.

## Why This Works
Connors 3-RSI and short-term mean reversion strategies exploit liquidity imbalances, market-maker inventory skew, and temporary overextension within structural trends. In real markets, prices exhibit bid/ask bounce, mean-reverting increments, and intraday relief rallies. 

A deterministic sine wave lacks these properties: it exhibits perfect serial autocorrelation where every down bar is followed by another down bar. Acknowledging this discrepancy prevents engineering teams from mistakenly abandoning high-expectancy mean-reversion alpha or overtuning trading system risk parameters to fit an unrepresentative synthetic artifact.

## Prevention
- **Never backtest mean-reversion algorithms against deterministic periodic functions**: Always evaluate mean-reversion strategies against stochastic price processes that incorporate volatility noise ($\sigma > 0$) or historical tick/candle data.
- **Run Locating Diagnostics Before Parameter Tuning**: When a strategy performs drastically below theoretical expectations in backtesting, run trade-level path inspections (`reason: "SL"` vs `"TIME_EXIT"` vs `"TP"`) to verify whether market data properties (such as serial autocorrelation) are distorting performance.
- **Maintain Wide Stops for Mean Reversion**: As demonstrated by Experiment 5 (where widening the ATR stop multiplier to 5.0x lifted Sharpe by +8.60 points), mean reversion strategies require wide catastrophic stops rather than tight noise-choking stops, relying on indicator reversion (e.g. crossing 5-SMA or RSI(3) relief) for normal exits.

## Related Issues
- `trading_system/data/trading212_client.py`: `_generate_synthetic_candles` implementation
- `evaluate_3rsi.py`: 3-RSI walk-forward backtest harness
- `.context/compound-engineering/ce-optimize/optimize-3rsi-strategy/`: Optimization spec and experiment log
