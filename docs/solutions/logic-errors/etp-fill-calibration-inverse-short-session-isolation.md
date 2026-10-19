---
title: Live Execution Multi-Source Fallback, ETP Fill Calibration, and Inverse Short Spot Mechanics
date: 2026-10-19
category: logic-errors
module: trading_system/execution
problem_type: logic_error
component: position_monitor
symptoms:
  - "Demo account instant false stop-outs across 5 open positions on Trading 212"
  - "Inverse short ETPs liquidating winning trades as stop losses and placing invalid broker stop orders"
  - "Synthetic market data bypassing safety gates due to multi-threaded class-variable race condition"
  - "API request quota exhaustion from 24/7 continuous polling while global exchanges were closed"
  - "Flash crashes incorrectly triggering scale recalibration and erasing protective stop losses"
root_cause: underlying_etp_asymmetry
resolution_type: spot_geometry_alignment
severity: critical
tags:
  - leveraged-etps
  - trading212
  - execution-safeguards
  - inverse-etps
  - market-hours
  - timezone-scheduling
---

# Live Execution Multi-Source Fallback, ETP Fill Calibration, and Inverse Short Spot Mechanics

## Problem
During live execution against Trading 212 demo and production accounts, five interconnected execution defects compromised engine stability and capital preservation:
1. **Immediate False Stop Liquidation**: The system calculated technical indicators, stop loss, and take profit levels on high-priced underlying assets (e.g. NVDA @ $130, QQQ @ $500), but placed orders on European leveraged ETPs trading at different nominal scales (e.g. 3NVD @ $34.50, QQQ3 @ $185). The first price tick compared the $34.50 ETP price against the $123.50 underlying stop loss, immediately liquidating all newly opened positions.
2. **Inverse Short Direction Inversion**: In Trading 212 Invest/Stocks accounts, inverse ETPs (`QQQSl_EQ`, `3USSl_EQ`, `3SNVl_EQ`) are spot instruments bought long. When the underlying market falls, the inverse ETP increases in value. The monitor erroneously treated `direction="SHORT"` as a CFD short where price increases indicate losses, placing stop losses above entry and take profits below entry. As a result, winning trades were stopped out as losses, and broker stop orders were rejected.
3. **Synthetic Data Safety Bypass**: A process-global `MarketDataProvider.LAST_FETCH_WAS_SYNTHETIC` flag experienced race conditions in multi-threaded ingestion workers, allowing orders derived from synthetic candles to bypass the safety interceptor.
4. **Flash Crash Scale Erosion**: A preliminary >30% scale discrepancy recalibration lacked an initial-tick guard, causing subsequent flash crashes to be misinterpreted as scale errors, ratcheting stops lower and neutralizing risk controls.
5. **Session Unawareness & Quota Exhaustion**: Polling ran 24/7 regardless of exchange hours, burning ~5,760 requests/day against closed exchanges, triggering 429 rate limit penalties.

## Root Cause Analysis

### 1. Underlying vs. ETP Scale Discrepancy & GBX Pence Units
The screening pipeline evaluates liquid US and international underlyings (`QQQ`, `SPY`, `NVDA`, `TSM`). When routed to Trading 212, execution maps to leveraged ETPs listed on the London Stock Exchange (LSE) or European bourses.
- ETPs trade at independent NAV unit prices unrelated to the underlying's nominal quote.
- Many UK-listed ETPs quote in GBX (pence, e.g. 9173 pence = £91.73). Registering entry in pence while receiving live prices in pounds (or vice versa) created a 100x scale discrepancy.

### 2. Spot Buying of Inverse ETPs vs. Borrow-and-Sell CFD Shorting
In standard equity shorting (CFDs/Margin), shorting borrows shares and sells them; profit occurs when price drops.
On Trading 212 Invest/ISA accounts, short exposure is achieved exclusively via spot purchases of inverse ETPs:
- Position direction is economically bearish on the underlying index, but structurally LONG the ETP instrument.
- For the ETP itself, higher price = profit, lower price = loss.
- Stop losses must strictly sit below the ETP's entry price (`entry * (1 - stop_dist)`), and take profits above entry (`entry * (1 + tp_dist)`).

### 3. Global Mutable Flag Race Condition
`MarketDataProvider.LAST_FETCH_WAS_SYNTHETIC` was stored as a single class attribute. Worker threads fetching 12 assets in parallel overwrote this flag concurrently. If Asset A failed and fell back to synthetic data (`True`), but Asset B subsequently succeeded via live data (`False`), the safety gate allowed Asset A's trade to execute live on the broker.

### 4. Continuous Recalibration Vulnerability
Checking `if abs(current_price - pos.entry_price) / pos.entry_price > 0.30` on every tick without state persistence meant that any rapid >30% adverse market movement was misidentified as an underlying-vs-ETP scale error, resetting entry to the crashed price instead of executing the stop loss.

## Implementation Details

### 1. Broker Fill Price Calibration & Initial-Tick Guard
In `PositionMonitor.register_new_trade`:
- When real broker fill price (`entry_order.fill_price` or portfolio average price) is received, entry, stop loss, take profits, and ATR are dynamically rescaled to the fill price.
- `TrackedPosition` carries `scale_calibrated: bool = False`. If registered with `fill_price`, `scale_calibrated = True` immediately.
- In `monitor_tick`, scale recalibration executes only on the initial tick (`not getattr(pos, "scale_calibrated", False)`), preventing loss-erasing adjustments during live crashes.

### 2. Economic Geometry-Based Short Resolution
Rather than fragile ticker or direction string matching, `PositionMonitor` resolves CFD shorts vs. spot ETPs using price geometry:
```python
is_short = (pos.tp1 < pos.entry_price) if (pos.tp1 > 0 and pos.entry_price > 0) else (
    pos.direction.upper() == "SHORT" and not (
        pos.ticker.endswith("_EQ") or pos.ticker.endswith(".L") or pos.direction.upper().endswith("_ETP")
    )
)
```
- For spot inverse ETPs, `tp1 > entry_price`: price rising is recognized as profit, stops sit safely below entry, and broker sell stop orders place valid resting orders.
- For generic CFD shorts, `tp1 < entry_price`: price falling is recognized as profit.

### 3. Per-Ticker Synthetic Isolation
In `MarketDataProvider`:
- Added `SYNTHETIC_TICKERS: set = set()`.
- Added `MarketDataProvider.is_ticker_synthetic(symbol: str) -> bool`.
- Worker threads populate `metrics["is_synthetic"]`, which threads through to `DecisionCard.is_synthetic`.
- `Orchestrator.execute_trade` gates live execution on `card.is_synthetic or MarketDataProvider.is_ticker_synthetic(card.ticker)`.

### 4. Multi-Source Ingestion Cascade
Added a 5-tier fallback cascade before generating synthetic candles:
1. `yfinance` history (if installed)
2. Raw Yahoo Finance Chart API (`query1.finance.yahoo.com/v8/finance/chart/`)
3. Stooq CSV endpoint (`stooq.com/q/d/l/?s=...`)
4. Polygon.io REST API (if `POLYGON_API_KEY` configured)
5. Finnhub REST API (if `FINNHUB_API_KEY` configured)
6. Tagged synthetic fallback (dry-run only)

### 5. Multi-Exchange Timezone Awareness & Power-Save Polling
In `Trading212Client`:
- Added `get_market_session_info()`, `is_market_open()`, and `is_any_market_open()`.
- Maps LSE (`08:00–16:30 UTC`), US cash (`14:30–21:00 UTC`), Xetra, Euronext, and Tokyo, with weekend shutdown (`weekday >= 5`).
- Pre-execution gate refuses live order submission when the exchange is closed.
- Continuous loops in `orchestrator.py` and `server.py` default empty position checks to `["QQQ3l_EQ", "SPY_US_EQ"]`, entering low-frequency 60s sleep during closed hours and preventing API exhaustion while remaining responsive to open US sessions.

## Verification
- Comprehensive unit tests in `tests/test_live_execution_safeguards.py` verify:
  1. Synthetic data strictly blocks live orders while permitting dry-run simulations.
  2. Inverse short ETPs calibrate with `stop_loss < entry < tp1`.
  3. Subsequent flash crashes trigger `STOP_EXIT` without recalibration.
  4. GBX pence quotes are normalized to base currency in both directions.
  5. Multi-exchange market hours correctly determine open/closed states across LSE, US, and weekends.
- All 60 repository unit tests pass with zero regressions.
