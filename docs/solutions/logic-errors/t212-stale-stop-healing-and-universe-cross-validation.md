---
title: Trading 212 Stale Stop Healing, Fallback Screening & Universe Cross-Validation
date: 2026-10-22
category: logic-errors
module: trading_system/execution
problem_type: logic_error
component: position_monitor
symptoms:
  - "Stale stop orders returning HTTP 400 'price too far from current market price' during broker resubmission"
  - "UK GBX pence scale mismatches distorting unrealized P&L calculations by 10x to 100x"
  - "Transformer NLI cold-start failures causing total signal starvation"
  - "Unverified speculative tickers causing broken order submissions on Trading 212"
root_cause: broker_rejection_drift
resolution_type: dynamic_recalibration
severity: high
tags:
  - stale-stop-loss
  - trading212
  - fallback-screener
  - currency-scaling
  - universe-validation
---

# Trading 212 Stale Stop Healing, Fallback Screening & Universe Cross-Validation

## Problem Context
In long-running autonomous trading engines on physical micro-runners (e.g., Raspberry Pi):
1. **Stale Stop Rejections (HTTP 400)**: If a protective stop order fails to place during initial entry or is cancelled, the locally persisted `stop_loss` in `active_trades.json` can become severely stale relative to current market price. When retried, Trading 212 returns `400 Bad Request: price is too far from current market price`, permanently blocking native broker stop placement.
2. **Currency Unit Scale Inconsistencies**: When positions in UK/London-listed assets are recorded, entry prices may occasionally be recorded in GBX pence while broker quotes are returned in EUR or GBP (a ~10x to 100x discrepancy), causing unrealized P&L calculations to distort dramatically (+1600%).
3. **NLI Screener Model Cold-Starts**: When running in headless environments where large transformer NLI models (ModernBERT / Laya) fail to load or yield 0 passing setups, the engine would emit 0 candidates, causing complete signal starvation.
4. **Speculative Tickers in Universe**: Manually adding leveraged ETPs without verifying against broker instrument metadata leads to broken order submissions.

## Root Cause & Mechanisms

### 1. Broker Rejection Handling (`_heal_stale_stop`)
Trading 212 enforces strict distance checks on Stop Orders relative to current bid/ask. Stored stop prices from previous sessions fail this check.
**Solution**: Catch HTTP 400 and "price too far" exceptions during `place_stop_order()`, dynamically query the live broker portfolio (`get_portfolio(force_refresh=True)`), and recompute a protective stop 2.5× ATR away from current live price in the protective direction. Reset `_last_stop_fail_time` to allow the monitor cycle to place the healed stop immediately.

### 2. Auto-Healing Currency Scale Mismatch
During `reconcile_with_broker()`, compare `pos.entry_price` with `broker_avg = bp.average_price`.
If `pos.entry_price / broker_avg > 5.0` or `< 0.20`, treat the broker average price as the single ground truth and update `pos.entry_price`, persisting state immediately.

### 3. Dual-Tier Fallback Screener
In `LayaScreener.scan_universe()` and `Orchestrator.screen_candidates()`, implement `TechnicalFallbackScreener`:
- Automatically triggers when `HAS_LAYA = False` OR when `LayaScreener` returns 0 candidates.
- Evaluates pre-computed technical indicators:
  - Bullish: `RSI < 65`, `ADX >= 20`, `close > EMA20`, `MACD line > MACD signal`
  - Bearish: `RSI > 35`, `ADX >= 20`, `close < EMA20`, `MACD line < MACD signal`
  - Setup score: `ADX > 30` -> Exceptional, `ADX > 25` -> Favorable, else Mediocre.
- Prevents 3x volatility decay by strictly filtering out consolidation regimes (`ADX < 20`).

### 4. Deterministic Universe Validation
All universe tickers in `CURATED_3X_PAIRS` and `CURRENCY_ALTERNATIVES` must be validated against `.t212_instruments_cache.json` using `scripts/validate_t212_tickers.py`.
- Enforces EUR first preference (101/120 pairs in EUR).
- Rejects unverified synthetic tickers before they reach production.

## Verification
- Verified 120 pairs with 0 invalid tickers against 17,143 tradeable T212 instruments.
- Unit tests in `tests/test_technical_fallback_screener.py` verify signal generation, choppy filtering, stop healing, and currency ratio correction.
- 129/129 test suite passes.
