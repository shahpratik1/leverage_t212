---
title: Leveraged ETP Active Trade Reconciliation with Underlying Market Indicators and Dual-Trigger Broker Execution
date: 2026-10-26
category: logic-errors
module: trading_system/execution
problem_type: logic_error
component: position_monitor
symptoms:
  - "Untracked or adopted broker positions on Trading 212 assigned arbitrary 2.5% fixed ATR stop loss"
  - "Premature stop-outs on 3x/5x leveraged ETPs due to unscaled underlying asset volatility calculations"
  - "Orchestrator fetched active trade underlyings metrics without propagating them to position reconciliation"
  - "Lack of leading market signal exits (Nixtla 5-day forecast reversals, Connors 3-RSI exhaustion) on active broker trades"
root_cause: unscaled_leverage_volatility_and_decoupled_reconciliation
resolution_type: leverage_scaled_atr_and_dual_trigger_execution
severity: high
tags:
  - leveraged-etps
  - market-data-reconciliation
  - trading212
  - atr-scaling
  - dual-trigger-exits
  - nixtla-forecasting
---

# Leveraged ETP Active Trade Reconciliation with Underlying Market Indicators and Dual-Trigger Broker Execution

## Problem
When trades were adopted from the Trading 212 broker portfolio or reconciled during execution cycles:
1. **Unscaled Default Volatility**: `PositionMonitor.reconcile_with_broker` lacked access to real-time market data for the position's underlying asset, defaulting to a static `2.5%` ATR estimate. Because 3x and 5x leveraged ETPs fluctuate by 3x to 5x the underlying percentage move, a 2.5% stop distance was unrealistically tight, triggering noise stop-outs on ordinary market fluctuations.
2. **Decoupled Data Pipeline**: `Orchestrator.fetch_active_trades_metrics` computed live indicators and ATR for active trade underlyings, but this market intelligence was decoupled from `reconcile_with_broker`, preventing newly adopted or existing positions from benefiting from live market volatility.
3. **Absence of Leading Forecast & Reversal Trims**: Active positions only exited when hard prices touched static levels, ignoring high-conviction market signals such as Nixtla 5-day forecast reversals or Connors 3-RSI overbought/oversold exhaustion.

## Root Cause
- `calculate_exit_levels` calculated stop and target distances purely from absolute ATR without scaling for the ETP's leverage multiplier (3x or 5x).
- `reconcile_with_broker` did not accept `active_metrics`, adopting untracked broker positions with `atr=None`.
- `evaluate_active_trade_signals` lacked integration with Nixtla quantitative 5-day price return projections.

## Solution & Architecture
1. **Leverage-Scaled ATR Engine**:
   - `calculate_exit_levels` dynamically resolves the instrument leverage factor (`leverage_factor = 3.0` for 3x ETPs, `5.0` for 5x ETPs).
   - Scales underlying market volatility: `effective_atr = round(entry * underlying_atr_pct * leverage_factor, 4)`.
   - Clamps stop loss distance between a 2.5% floor (absorbing bid-ask spread) and an 8.0% ceiling (protecting against drawdown).
2. **Integrated Broker Reconciliation**:
   - `Orchestrator.fetch_active_trades_metrics` queries daily candles, hourly candles, technical indicators, and Nixtla 5-day price projections for active trade underlyings.
   - Passes `active_metrics` directly into `reconcile_with_broker(adopt_untracked=True, active_metrics=active_metrics)`, ensuring newly adopted positions immediately receive quantitative ATR, SL, TP1, TP2, TP3, and forecast levels.
3. **Dual-Trigger Execution**:
   - **Broker-Side Protection**: Places native GTC Stop Loss and TP1 Limit orders on Trading 212 server to guard against network dropouts.
   - **Algorithmic Signal Exits**: Monitors Connors 3-RSI overbought peaks (>= 75.0) and Nixtla 5-day expected return reversals (e.g. projection turning <= -3.0% on Longs) to execute partial profit trims (TP1/TP2) and protect accumulated gains before market turns.

## Verification
- Unit test `test_calculate_exit_levels_with_leverage_and_underlying_atr`: Verified 3x scaling (1% underlying -> 3% ETP ATR) and 5x scaling with 8.0% max stop clamping.
- Unit test `test_reconciliation_adopts_with_live_market_metrics`: Verified that broker adoption computes correct scaled ATR and attaches Nixtla forecast targets.
- Unit test `test_evaluate_active_trade_signals_nixtla_bearish_reversal_long` and `test_evaluate_active_trade_signals_nixtla_target_achieved`: Verified signal-driven automated trims.
- Full 175-test unit suite passing green.
