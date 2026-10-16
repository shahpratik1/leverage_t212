---
title: Schmitt Hysteresis Trailing Stop Ratchet Prevents Bid-Ask Bounce Liquidation on 3x ETPs
date: 2026-10-16
category: logic-errors
module: trading_system/execution
problem_type: logic_error
component: position_monitor
symptoms:
  - "Ratcheting Stop Loss to TP1 (+1.0R) upon touching TP1 caused immediate self-liquidation on the subsequent tick"
  - "On 3x daily leveraged ETPs, resting bid spreads (20-60 bps) triggered stop orders prematurely before any adverse trend reversal occurred"
  - "Short inverse ETP protective stop orders failed or doubled exposure due to positive quantity submission on Trading 212 Invest API"
root_cause: microstructure_friction
resolution_type: algorithmic_hysteresis
severity: high
tags:
  - trailing-stop-loss
  - schmitt-trigger
  - hysteresis
  - bid-ask-bounce
  - leveraged-etps
  - trading212
---

# Schmitt Hysteresis Trailing Stop Ratchet Prevents Bid-Ask Bounce Liquidation on 3x ETPs

## Problem
In automated milestone trailing stop systems, moving the protective Stop Loss directly to Target 1 (TP1, +1.0R) at the moment TP1 is hit creates an immediate false stop-out on 3x leveraged European ETPs (e.g. `QQQ3`, `3TSM`, `3NVD`). Positions that reached +1.0R gains were immediately closed at zero additional profit or slight loss on the very next market tick, destroying strategy expectancy and preventing capture of larger runner targets (TP2 +2.0R, TP3 +3.0R).

## Symptoms
- Position monitor logs showed trades hitting TP1, instantly logging `STOP_EXIT` within 1–2 ticks.
- Realized profit on the second tranche collapsed from expected +1.0R to ~0.0R due to immediate exit at the bid price.
- On inverse ETPs (`QQQ3S_EQ`, `3LIS`), protective stop orders submitted via the Trading 212 API either failed with validation errors or attempted to buy additional shares instead of selling held shares.

## Root Cause Analysis
Two distinct microstructure and broker mechanics caused this behavior:

1. **Roll (1984) Bid-Ask Bounce & Microstructure Flapping**:
   On London Stock Exchange (LSE) 3x leveraged ETPs, the typical bid-ask spread ranges from 20 to 60 bps (widening up to 100 bps during US market open or macroeconomic volatility). When a Long position reaches TP1, the tick represents an Ask execution or touch. The resting Bid price is immediately below TP1 by the spread width. If the active Stop Loss is ratcheted to $P_{\text{stop}} = \text{TP1}$, the resting bid is already below the stop price. The next tick (which may reflect a bid transaction) triggers the stop immediately, terminating the position prematurely.

2. **Trading 212 Invest / Stocks API Order Sign Convention**:
   Inverse ETPs (e.g., `QQQ3S_EQ`, `QQQS_EQ`) trade as long cash equities in Invest/ISA accounts (spot purchases), avoiding CFD borrow fees. To protect a short directional thesis implemented via inverse ETPs, the held shares must be SOLD to exit. In the Trading 212 API, all protective stops for equity holdings must submit a negative quantity (`quantity = -abs(remaining_shares)`). Submitting positive quantity treats the stop as an entry stop order to purchase more shares.

3. **Single-Tick Milestone Gap Surge**:
   When high-beta ETPs open with gaps or experience momentum spikes, a single price tick can surge past TP1 directly to TP2. If milestones are evaluated out of order or if state flags are not sequenced defensively, lower-priority handlers can execute after higher-priority handlers, demoting an elevated stop back to entry price Break-Even.

## Solution

### 1. Schmitt-Trigger Hysteresis Dead-Band
We implemented a two-stage ratcheting mechanism inspired by Schmitt triggers in electrical engineering:
- **At TP1 (+1.0R)**: Trim exactly 50% of original shares to lock in gains (`TP1_TRIM_PCT = 0.50`). Move the protective stop loss to Entry Price (**Break-Even**, 0.0R). This guarantees a completely risk-free trade while providing full breathing room for bid-ask noise. Arm Schmitt Hysteresis (`pos.tp1_armed_hysteresis = True`).
- **Escape Threshold ($P_{\text{escape}}$)**: Calculate a volatility-buffered escape boundary using beta-adjusted ATR:
  $$\text{Long: } P_{\text{escape}} = \text{TP1} + (0.5 \times \text{ATR})$$
  $$\text{Short: } P_{\text{escape}} = \text{TP1} - (0.5 \times \text{ATR})$$
- **Hysteresis Ratchet**: Only when market price firmly clears $P_{\text{escape}}$ into positive territory is the active stop floor elevated to TP1 (+1.0R). The hysteresis flag is then cleared, allowing standard dynamic trailing to trail the remaining runner.

### 2. Universal Negative Quantity for Cash Equity Stops
In `PositionMonitor.place_broker_stop()`:
```python
order_qty = -abs(pos.remaining_quantity)
```
This guarantees that protective stops for both long underlying ETPs and inverse short ETPs sell held shares rather than opening duplicate long exposure.

### 3. Defensive Milestone Ordering and Gap Guarantees
In `PositionMonitor.monitor_tick()`, checks evaluate in strict priority order:
1. Stop Loss & Trailing Stop Loss
2. TP3 (+3.0R full runner exit)
3. TP1 (+1.0R 50% trim + Breakeven + Arm Hysteresis)
4. TP2 (+2.0R 25% trim + Ratchet to TP1)
5. Schmitt Hysteresis Escape Check
6. Dynamic Trailing Ratchet

In `_handle_tp2_hit()`, `pos.tp1_hit = True`, `pos.tp1_armed_hysteresis = False`, and `pos.tp1_stop_elevated = True` are explicitly set, ensuring that price surges directly to TP2 never demote stops.

## Verification
- Comprehensive test suite in `tests/test_milestone_tsl_hysteresis.py` (11/11 tests passing):
  - In-band oscillation between TP1 and $P_{\text{escape}}$ holds stop at Break-Even without false triggering.
  - Pullback to entry price exits remaining shares at $0 net loss, preserving banked +0.50R profit.
  - Crossing $P_{\text{escape}}$ elevates broker stop to TP1.
  - Single-tick gap surges to TP2 cleanly trim 75% and lock stop at TP1.
  - Negative order quantities verified on broker mock for both long and short positions.
  - State persistence round-trip verified for all hysteresis fields.
