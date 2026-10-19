---
title: Institutional Trading Expansion - Plan
type: feat
date: 2026-10-19
artifact_contract: ce-unified-plan/v1
product_contract_source: user-approved
execution: code
---

# Institutional Trading Expansion - Plan

## Goal Capsule

- **Objective**: Harden the 3x Leveraged Trading Engine to institutional operational standards by establishing server-side broker GTC Stop/Limit protection, zero-latency SSE live dashboard telemetry, crypto/commodity universe expansion with dynamic volatility-targeted position sizing, and a complete historical backtesting/benchmark harness.
- **Means**: Implement broker-native GTC Stop/Limit order execution at fill time (KTD1), SSE event streaming on localhost:8888 (KTD2), Crypto & Commodity 3x ETP universe with realized-volatility sizing (KTD3), and an event-driven backtest simulator (KTD4).
- **Authority Hierarchy**:
  1. Capital Preservation & Broker Safety Gates (never risk capital blindly or violate Trading 212 rate limits).
  2. Broker-Native Server-Side Protection (server-side GTC orders outrank software polling).
  3. Strict 3x Leverage Cap & Volatility Drag Elimination (Connors 3-RSI <= 4-day holding).
- **Stop Conditions**: Stop and report immediately if Trading 212 API rejects native stop orders, if socket streams leak file descriptors, or if historical simulations reveal unhedged volatility decay.
- **Execution Profile**: Full multi-unit implementation across broker client, risk manager, dashboard server, universe definitions, and backtester.

---

## Product Contract

### Problem Frame
The current trading engine successfully runs screening, multi-agent debate, risk management, and order entry, but suffers from four critical institutional bottlenecks:
1. **Software-Only Stop Polling**: If the local Python process terminates or network drops, open 3x positions have no resting GTC stop on Trading 212, exposing capital to uncapped tail risk.
2. **Dashboard Polling Latency**: The web UI relies on full-page reloads every 30 seconds, causing visual flicker and delayed signal visualization.
3. **Restricted Equities-Only Universe**: High-alpha 3x ETP asset classes (Cryptocurrency ETPs like 3x BTC/ETH, Commodities like 3x Gold/Silver/Oil) are omitted, and position sizing uses fixed equity fractions rather than realized-volatility parity.
4. **Lack of Backtest Benchmark Harness**: Strategy rules (Connors 3-RSI + ADX momentum) cannot be backtested locally against historical data without running live/paper cycles.

### Requirements

#### Broker-Native Server-Side Protection
- R1. **Native GTC Stop Placement**: Immediately upon fill of a market entry order, the engine must place a resting GTC Stop Loss order on Trading 212 (`stopPrice = card.stop_loss`).
- R2. **Native Limit Take-Profit Placement**: Concurrently place or reserve resting Limit Take Profit orders (`limitPrice = card.tp1`) where permitted by Trading 212 margin rules, storing order IDs in `Position`.
- R3. **Lifecycle Synchronization**: When `PositionMonitor` ratchets TSL or triggers a milestone trim, it must cancel/replace the resting broker stop order to guarantee server-side synchronization.

#### Real-Time SSE Telemetry
- R4. **Server-Sent Events Stream**: The embedded HTTP server on `localhost:8888` must expose an SSE endpoint (`/stream` or `/events`) broadcasting live position ticks, PnL updates, signal alerts, and log events.
- R5. **Flicker-Free UI Updates**: `signals_dashboard.html` must consume the SSE stream via standard browser `EventSource`, dynamically updating DOM cards and tables without triggering full-page browser reloads.

#### Universe & Volatility Sizing Expansion
- R6. **Crypto & Commodity 3x Pairs**: Add curated European-listed 3x/2x Crypto ETPs (3x Bitcoin `3BTC`, 3x Ethereum `3ETH`) and Commodities (3x Gold `3GOL`, 3x Silver `3SIL`, 3x Oil `3OIL`, 3x Copper `3CPR`) to `LeveragedUniverse`.
- R7. **Volatility-Targeted Position Sizing**: Implement dynamic volatility parity:
  $$S = \min\left(\text{MaxNotional}, \text{Equity} \times \frac{\text{TargetVol}}{3 \times \sigma_{\text{realized}}}\right)$$
  reducing position size when daily realized volatility spikes to eliminate beta slippage.

#### Historical Backtesting Engine
- R8. **Historical Simulator**: Implement an event-driven backtesting engine in `trading_system/backtest/engine.py` simulating 3-RSI mean reversion and ADX trend breakout setups against historical OHLCV candles.
- R9. **Performance & Benchmark Metrics**: Calculate Win Rate %, Profit Factor, Max Drawdown %, Sharpe Ratio, and benchmark performance comparison against 1x Buy & Hold.
- R10. **Backtest CLI**: Provide a CLI interface (`python3 -m trading_system.backtest.engine --ticker NVDA --start 2023-01-01 --end 2026-01-01`) producing structured terminal tables and JSON exports.

---

## Planning Contract

### Key Technical Decisions
- KTD1. **Asynchronous Order Chaining for GTC Stops** (session-settled: user-directed — chosen over synchronous blocking calls: avoids holding orchestrator thread while waiting for broker stop confirmation).
- KTD2. **Zero-Dependency SSE Protocol** (session-settled: user-directed — chosen over external WebSocket libraries like `websockets`: standard library `http.server` natively supports `text/event-stream` chunked streaming without external package dependencies).
- KTD3. **Parkinson / Close-to-Close Realized Volatility Estimator** (session-settled: user-directed — chosen over static ATR multipliers: dynamically adapts capital exposure to 20-day annualized realized volatility).
- KTD4. **Simulated Position Lifecyle Parity** (session-settled: user-directed — chosen over naive end-of-bar execution: faithfully executes the exact 3-tier exit logic — TP1 50% trim + break-even stop, TP2 25% trim + TSL ratchet, TP3 25% runner, and 4-day max duration).

---

## Implementation Units

### U1. Native Broker GTC Stop & Limit Orders
- **Goal**: Implement server-side GTC Stop Loss order placement and replacement in `Trading212Client`, `Orchestrator`, and `PositionMonitor`.
- **Files**: `trading_system/data/trading212_client.py`, `trading_system/orchestrator.py`, `trading_system/execution/position_monitor.py`
- **Approach**:
  - Add `place_stop_order(ticker, quantity, stop_price, time_validity="GTC")` and `cancel_order(order_id)` to `Trading212Client`.
  - In `Orchestrator.execute_trade`: when entry order fills, invoke `place_stop_order` and store `broker_stop_order_id` on the `Position` record.
  - In `PositionMonitor._handle_tp1_hit` and `_handle_tp2_hit`: update the broker resting stop order to break-even or ratcheted TSL.

### U2. Server-Sent Events (SSE) Live Telemetry Stream
- **Goal**: Upgrade embedded server with `/stream` SSE endpoint and integrate reactive frontend client in `signals_dashboard.html`.
- **Files**: `trading_system/dashboard/server.py`, `signals_dashboard.html`
- **Approach**:
  - Implement a thread-safe `SSEBroadcaster` managing client response queues.
  - In `CustomHandler.do_GET`: handle `/stream` by emitting `text/event-stream` headers and looping with `broadcaster.subscribe()`.
  - Whenever `PositionMonitor.monitor_tick()` or `Orchestrator` generates signals or executes trades, broadcast JSON payloads (`event: tick`, `event: trade`, `event: alert`).
  - In `signals_dashboard.html`: attach `new EventSource('/stream')` to update signal cards, position table, and countdowns in real time without refreshing the page.

### U3. Crypto & Commodity 3x Universe + Volatility-Targeted Sizing
- **Goal**: Expand curated pairs with liquid crypto and commodity ETPs, and calculate volatility-adjusted position size.
- **Files**: `trading_system/data/leveraged_universe.py`, `trading_system/agents/risk_manager.py`, `trading_system/config.py`
- **Approach**:
  - Add `BTC` (3BTC / 3BTS), `ETH` (3ETH / 3ETS), `GOLD` (3GOL / 3GOS), `SILVER` (3SIL / 3SIS), `OIL` (3OIL / 3OIS), `COPPER` (3CPR / 3CPS) to `CURATED_3X_PAIRS`.
  - In `RiskManager`: add `calculate_volatility_targeted_size(equity, annualized_vol, target_vol=0.20, leverage=3)` to scale position sizes inversely with realized volatility.
  - Add configuration knobs in `trading_system/config.py`: `ENABLE_CRYPTO_3X`, `ENABLE_COMMODITIES_3X`, `TARGET_ANNUAL_VOLATILITY`.

### U4. Historical Backtesting Engine & Benchmark CLI
- **Goal**: Build high-performance event-driven backtesting engine and standalone CLI harness.
- **Files**: `trading_system/backtest/engine.py`, `trading_system/backtest/__init__.py`, `trading_system/orchestrator.py`
- **Approach**:
  - Create `BacktestEngine` accepting OHLCV candles, initial capital, and strategy rules.
  - Simulate daily screening, entry triggers (Connors 3-RSI dip buy, ADX momentum breakout), and intraday/daily high-low tracking for TP1/TP2/TP3 and Stop Loss.
  - Support benchmark comparison against baseline asset.
  - Add CLI arguments in `orchestrator.py`: `--backtest`, `--backtest-symbol`, `--backtest-start`, `--backtest-end`.

### U5. Test Suite & Verification
- **Goal**: Add dedicated unit tests for all 4 pillars and verify zero regressions.
- **Files**: `tests/test_broker_native_orders.py`, `tests/test_dashboard_sse.py`, `tests/test_crypto_commodity_vol_sizing.py`, `tests/test_backtest_engine.py`
- **Approach**:
  - Mock broker API calls to test stop order creation, cancellation, and replacement.
  - Test SSE broadcaster subscribe, publish, and client disconnect handling.
  - Test volatility-targeted sizing formula under low, normal, and extreme volatility regimes.
  - Test backtest engine with synthetic historical candles to verify trade metrics computation.

---

## Verification Contract

1. **Unit Test Suite**:
   ```bash
   python3 -m unittest discover tests
   ```
   All existing 65 tests + new tests must pass (100% green).
2. **Backtest CLI Smoke Test**:
   ```bash
   python3 -m trading_system.backtest.engine --symbol QQQ --dry-run
   ```
3. **SSE Telemetry Test**:
   ```bash
   python3 -c "from trading_system.dashboard.server import SSEBroadcaster; b = SSEBroadcaster(); q = b.subscribe(); b.publish('test', {'status': 'ok'}); print(q.get_nowait())"
   ```

---

## Definition of Done

- [ ] All 5 Implementation Units completed and verified.
- [ ] Broker GTC Stop orders placed on entry and updated on TSL ratchets.
- [ ] SSE telemetry active on `localhost:8888/stream` and consumed by `signals_dashboard.html`.
- [ ] Crypto & Commodity 3x pairs active with volatility-targeted sizing.
- [ ] Historical backtester executable via CLI with performance metrics table.
- [ ] All unit tests pass with 0 errors and 0 regressions.
- [ ] Clean code adhering to PEP 8, typed annotations, and robust error handling.
