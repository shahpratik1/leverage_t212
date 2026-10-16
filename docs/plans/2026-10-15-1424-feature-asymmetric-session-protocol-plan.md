---
artifact_contract: ce-unified-plan/v1
product_contract_source: ce-brainstorm
execution: code
date: 2026-10-15
topic: asymmetric-session-protocol
type: feature
---

# Feature: Asymmetric Session Protocol (Intraday Inverse Hedging & 16:20 Gap Evacuation)

## Goal Capsule

**Objective:** Protect trading capital against overnight transatlantic gap risk and leveraged ETP compounding decay by establishing direction-specific holding horizons and an automated 16:20 UTC session boundary gate, while operating strictly within Trading 212 API call quotas.

**Means:** Implement the Asymmetric Session Protocol in `PositionMonitor` and `Orchestrator`, with rate-limited liquidation queues and single-call portfolio reconciliation.

**Product Authority:** Confirmed product rules for `trading_system/execution/position_monitor.py` and `trading_system/orchestrator.py`. General broker quote translation and flight envelope math remain separate future work.

**Open Blockers:** None.

---

## Product Contract

*(Product Contract unchanged from brainstorm)*

### Summary
Holding 3x leveraged European ETPs overnight exposes open positions to severe unhedged gap risk while the London Stock Exchange (LSE) is closed and US cash markets continue trading into post-market earnings. Furthermore, holding 3x inverse positions across multi-day chop subjects capital to compounding volatility drag ($eta = -3.0$) and positive equity drift.

This feature introduces an automated Asymmetric Session Protocol:
1. **3x Long Positions:** Allowed to swing overnight up to a 4-day maximum, but only if Take Profit 1 (+1R) has been hit and Stop Loss has moved to Breakeven. Any trade that has not secured breakeven protection by 16:20 UTC is flattened at the market close. Furthermore, any long position whose underlying company reports earnings within 24 hours is liquidated at 16:20 UTC regardless of TP1 status.
2. **3x Inverse/Short Positions:** Enforced as strictly intraday tactical overlays with mandatory 100% liquidation at 16:20 UTC, guaranteeing zero overnight short exposure.
3. **API Rate Quota & Call Budgeting:** All lifecycle checks and 16:20 batch liquidations operate through a thread-safe rate-limited queue (minimum 1.1s inter-call delay) and bulk `get_portfolio()` reconciliation to strictly prevent Trading 212 `429 Too Many Requests` or dropped orders.

### Target Users & Primary Scenarios
- **Automated Algorithmic Desk:** Operates unattended throughout European (08:00–16:30 UTC) and US (14:30–21:00 UTC) market sessions without risking catastrophic overnight opening gaps.
- **Manual Radar Trader:** Monitors the dashboard for explicit `16:20 UTC FLATTEN` alerts to manually close trades on brokers that lack native conditional orders.

---

### Requirements & Scope Boundaries

#### R1. Asymmetric Directional Holding Horizons
- **R1.1 (Long Swings):** 3x Long positions (`QQQ3`, `3LNV`, etc.) are eligible for multi-day swing holding up to a maximum duration of 4 trading days (96 market hours).
- **R1.2 (Strict Breakeven Overnight Gate):** At 16:20 UTC (10 minutes before LSE cash close), `PositionMonitor` evaluates all active 3x Long positions. If a position has `tp1_hit == False` (meaning Stop Loss is still below Entry Price), the engine must trigger an immediate market sell order to flatten the trade.
- **R1.3 (Intraday-Only Inverse Positions):** All 3x Inverse/Short positions (`QQQS`, `3SNV`, etc.) are classified as intraday tactical overlays. At 16:20 UTC, the engine must liquidate 100% of remaining inverse quantities, regardless of current PnL or TP status.

#### R2. Mandatory 24-Hour Pre-Earnings Blackout
- **R2.1 (Earnings Evacuation):** Even if a 3x Long position has secured `tp1_hit == True` (breakeven stop), if the underlying asset is scheduled to release earnings within the next 24 hours (post-market or next-day pre-market), the position must be liquidated at 16:20 UTC.
- **R2.2 (Screener Earnings Filter):** The intake screener must reject any candidate ticker if an earnings report is scheduled within 48 hours.

#### R3. Broker API Call Quota & Rate Limit Budgeting
- **R3.1 (Single-Call Portfolio Reconciliation):** `PositionMonitor` must never query the broker API ticker-by-ticker. Position quantities and broker status must be verified using a single `GET /equity/portfolio` call per inspection cycle.
- **R3.2 (Serialized Liquidation Queue):** When multiple positions require liquidation at 16:20 UTC, orders must not be fired concurrently. Orders must be dispatched through a thread-safe serialized queue with a mandatory 1.15-second delay between requests, fully respecting Trading 212's 1-req/sec rate limit.
- **R3.3 (Rate-Limit Backoff & Retry):** If an order request encounters an HTTP 429 status, the client must exponentially back off (2.0s, 4.0s, 8.0s) and retry up to 3 times before falling back to an emergency notification alert.

#### R4. Session Telemetry & Notifications
- **R4.1 (Event Dispatch):** The engine must dispatch structured notifications for session events:
  - `ASYMMETRIC_SESSION_EXIT`: Fired when a long position is flattened for not hitting TP1.
  - `INVERSE_INTRADAY_FLATTEN`: Fired when an inverse short trade is closed at 16:20 UTC.
  - `EARNINGS_BLACKOUT_EXIT`: Fired when an open trade is closed due to impending earnings.
- **R4.2 (Dashboard Visualization):** Active signals on `signals_dashboard.html` must display an `Overnight Eligible: YES/NO` badge and a real-time countdown to the 16:20 UTC cutoff.

---

### User-Visible Flows

#### Flow 1: 16:20 UTC Long Trade Breakeven Evaluation
```
16:20 UTC Tick Triggered
  │
  ├── For each active 3x Long position:
  │     ├── Has TP1 been hit? (Stop Loss at Breakeven?)
  │     │     ├── YES: Check upcoming earnings within 24h
  │     │     │     ├── Earnings imminent: Queue for 16:20 Liquidation (Reason: EARNINGS_BLACKOUT)
  │     │     │     └── No earnings: RETAIN position for overnight swing
  │     │     └── NO: Queue for 16:20 Liquidation (Reason: ASYMMETRIC_SESSION_EXIT)
  │
  └── Dispatch queued orders through Serialized Rate-Limited Executor (1.15s per order)
```

#### Flow 2: 16:20 UTC Inverse Short Liquidation
```
16:20 UTC Tick Triggered
  │
  ├── For each active 3x Inverse/Short position:
  │     └── Unconditionally Queue for 16:20 Liquidation (Reason: INVERSE_INTRADAY_FLATTEN)
  │
  └── Dispatch queued orders through Serialized Rate-Limited Executor (1.15s per order)
```

---

### Acceptance Examples

- **AE1 (Long Position Under TP1 at Close):** An active long trade on `QQQ3l_EQ` is entered at 14:40 UTC at $415.00 with TP1 at $425.00. At 16:20 UTC, price is $420.00 (+1.2%, but TP1 not hit). The engine triggers a market sell order, logs `ASYMMETRIC_SESSION_EXIT`, and closes the position before LSE close.
- **AE2 (Long Position With TP1 Hit at Close):** An active long trade on `3TSMl_EQ` hits TP1 (+1R) at 15:10 UTC, moving SL to Breakeven ($1,151.50). No earnings are due within 24 hours. At 16:20 UTC, the engine marks the trade as `OVERNIGHT_ELIGIBLE` and keeps the remaining 67% position open with trailing stop active.
- **AE3 (Inverse Short Trade at Close):** An active short trade on `QQQSl_EQ` is up +2.5% at 16:20 UTC. The engine automatically submits a market sell order to close 100% of shares, locking in intraday profits and eliminating overnight holding decay.
- **AE4 (API Rate Quota Protection on Multi-Asset Close):** At 16:20 UTC, 4 positions require liquidation. The engine enqueues all 4 orders and submits them sequentially at `t=0.0s`, `t=1.2s`, `t=2.4s`, and `t=3.6s`. All 4 orders succeed without encountering HTTP 429 rate limits.

---

### Key Technical & Product Decisions (Settled)

1. **Cutoff Timing (16:20 UTC):** Set to exactly 10 minutes prior to the 16:30 UTC LSE close. This provides an 8-minute window for rate-limited order execution to complete while participating in the high-liquidity market-on-close auction.
2. **Asymmetric Holding over Symmetric Swings:** Inverse 3x ETPs face severe compounding drag ($-3.0 \cdot V_t$) and equity drift; overnight holding is strictly barred for shorts.
3. **Mandatory Earnings Blackout:** Eliminates the single largest source of overnight gap-down ruin on single-stock 3x ETPs (TSLA, NVDA, GOOGL).
4. **Strict API Budgeting:** Replaces unsynchronized concurrent broker polling with serialized execution and bulk portfolio fetches, preventing broker API exhaustion.

---

### Out of Scope / Non-Goals
- **Pre-Market US Limit Quoting:** Quoting passive limit orders during the 08:00–14:30 UTC London morning window (deferred to Idea 1 quote bridge).
- **Multi-Broker Routing:** Supporting brokers other than Trading 212.
- **Automatic After-Hours US Equity Trading:** Trading 212 does not support after-hours execution for European-listed ETPs.

---

## Planning Contract

### Technical Summary
The Asymmetric Session Protocol requires state and lifecycle additions across `trading_system/config.py`, `trading_system/data/trading212_client.py`, `trading_system/execution/position_monitor.py`, `trading_system/orchestrator.py`, and `trading_system/dashboard/server.py`. 

Crucially, all broker execution must be guarded by a thread-safe mutex and token rate limiter to comply with Trading 212's 1-req/second quota, especially when multiple positions must be flattened concurrently at 16:20 UTC.

### Key Technical Decisions (KTDs)

- **KTD1 (Governs R3.1, R3.2): Thread-Safe Broker Request Mutex**
  - *Context:* `Trading212Client` currently tracks `self.last_request_time` as a float without a mutex lock, which fails under concurrent thread execution.
  - *Decision:* Add `threading.Lock()` to `Trading212Client._request` ensuring that all outbound HTTP requests across background worker threads or batch liquidation loops serialize with a guaranteed `elapsed >= 1.15s` delay.
  - *Rationale:* Eliminates race conditions that produce HTTP 429 rate limit errors on Trading 212.

- **KTD2 (Governs R1.2, R1.3, R2.1): Time-of-Day Boundary Inspection in `PositionMonitor.monitor_tick`**
  - *Context:* `monitor_tick` runs every 30 seconds but only checks price boundaries (`stop_loss`, `tp1`, `tp2`, `tp3`).
  - *Decision:* At the start of each `monitor_tick`, inspect the current UTC wall clock time. If UTC time is within `16:20 <= HH:MM < 16:30` and the session boundary hasn't fired yet today, execute `_evaluate_asymmetric_session_boundary()`.
  - *Rationale:* Decouples session liquidation from price ticks; executes deterministically at 16:20 UTC even if market price has not moved.

- **KTD3 (Governs R3.2): Serialized Rate-Limited Liquidation Queue**
  - *Context:* If 5 positions require liquidation at 16:20 UTC, submitting 5 market sell orders simultaneously triggers API rejection.
  - *Decision:* Collect all liquidation targets into an internal FIFO queue and process each position sequentially with `time.sleep(settings.RATE_LIMIT_INTER_CALL_DELAY)` between fills.
  - *Rationale:* Guarantees 100% order fill completion across all positions within 6–8 seconds, safely inside the 10-minute auction window.

- **KTD4 (Governs R2.1, R2.2): Earnings Calendar Gate**
  - *Context:* Determining whether earnings report occurs within 24 hours.
  - *Decision:* Check `metrics.get("earnings_in_days")` or `pos.earnings_date`. If `0 <= earnings_in_days <= 1.0` (or within 24 hours), mark `earnings_risk_imminent = True`.
  - *Rationale:* Protects swing trades from catastrophic overnight binary gaps while allowing non-reporting assets to swing.

---

## Implementation Units

### U1. Configuration & Thread-Safe Rate Limiter
- **Goal:** Add protocol settings to `Settings` and equip `Trading212Client` with a thread-safe mutex and retry-backoff queue.
- **Covers:** R3.2, R3.3
- **Files:** `trading_system/config.py`, `trading_system/data/trading212_client.py`
- **Implementation Steps:**
  1. In `trading_system/config.py`, add:
     - `SESSION_CLOSE_CUTOFF_UTC: str = "16:20"`
     - `ENFORCE_INVERSE_INTRADAY_ONLY: bool = True`
     - `ENFORCE_LONG_BREAKEVEN_OVERNIGHT: bool = True`
     - `EARNINGS_BLACKOUT_HOURS: int = 24`
     - `RATE_LIMIT_INTER_CALL_DELAY: float = 1.15`
  2. In `trading_system/data/trading212_client.py`, import `threading` and initialize `self._rate_lock = threading.Lock()`.
  3. Wrap the delay check in `_request` with `with self._rate_lock:` so all threads respect `RATE_LIMIT_INTER_CALL_DELAY`.
  4. Ensure `close_position` and `place_market_order` handle retry backoff on 429 cleanly.
- **Test Scenarios:**
  - Verify concurrent calls from 5 threads serialize at ~1.15s intervals.
  - Verify 429 responses trigger exponential backoff.

### U2. Asymmetric Session Boundary Evaluator in `PositionMonitor`
- **Goal:** Implement direction-specific 16:20 UTC holding evaluation in `PositionMonitor`.
- **Covers:** R1.1, R1.2, R1.3, R2.1, R4.1
- **Files:** `trading_system/execution/position_monitor.py`
- **Implementation Steps:**
  1. Add `earnings_in_days: Optional[float] = None` to `TrackedPosition`.
  2. In `PositionMonitor`, add `_last_session_close_date: str = ""` to ensure 16:20 liquidation runs once per calendar day.
  3. Create `evaluate_asymmetric_session(current_time: Optional[datetime] = None) -> List[Dict[str, Any]]`:
     - Checks if time is >= 16:20 UTC and < 16:30 UTC.
     - For `pos.direction == "SHORT"`: queue for immediate 100% liquidation (`INVERSE_INTRADAY_FLATTEN`).
     - For `pos.direction == "LONG"`:
       * If `not pos.tp1_hit`: queue for 100% liquidation (`ASYMMETRIC_SESSION_EXIT`).
       * If `pos.tp1_hit` and `pos.earnings_in_days is not None and pos.earnings_in_days <= 1.0`: queue for liquidation (`EARNINGS_BLACKOUT_EXIT`).
       * If `pos.tp1_hit` and no earnings: mark `pos.is_overnight_eligible = True`.
  4. Integrate evaluation call into `monitor_tick()`.
- **Test Scenarios:**
  - Long position without TP1 at 16:20 UTC is liquidated.
  - Long position with TP1 hit and no earnings is kept open.
  - Long position with TP1 hit and earnings <= 1d is liquidated.
  - Short position at 16:20 UTC is unconditionally liquidated.

### U3. Rate-Limited Serialized Liquidation Queue
- **Goal:** Execute queued 16:20 liquidations sequentially with broker pacing.
- **Covers:** R3.1, R3.2
- **Files:** `trading_system/execution/position_monitor.py`
- **Implementation Steps:**
  1. Implement `_execute_liquidation_queue(positions_to_close: List[Tuple[TrackedPosition, str]])`:
     - Iterate through queued positions.
     - Call `self.t212.place_market_order(...)` or `self.t212.close_position(...)`.
     - Log execution and fire `Notifier` alert.
     - Sleep `settings.RATE_LIMIT_INTER_CALL_DELAY` between each execution.
  2. Update `active_trades.json` and mark positions as closed.
- **Test Scenarios:**
  - Queue with 3 positions executes 3 sequential orders spaced by 1.15s.

### U4. Orchestrator Pre-Trade Filter & Session Integration
- **Goal:** Ensure incoming trades obey 48h screener earnings rules and register session metrics.
- **Covers:** R2.2, R4.1
- **Files:** `trading_system/orchestrator.py`
- **Implementation Steps:**
  1. In `Orchestrator.run_pipeline`, filter out candidates where `earnings_in_days is not None and earnings_in_days <= 2.0`.
  2. Pass `earnings_in_days` from candidate metrics into `PositionMonitor.register_position`.
  3. Ensure pipeline logs report session protocol status.
- **Test Scenarios:**
  - Candidate with earnings in 1.5 days is rejected pre-trade.

### U5. Live Dashboard Overnight Status & 16:20 Countdown
- **Goal:** Display real-time session indicators and overnight eligibility badges on `signals_dashboard.html`.
- **Covers:** R4.2
- **Files:** `trading_system/dashboard/server.py`
- **Implementation Steps:**
  1. In `load_active_signals`, calculate `overnight_status`:
     - `INTRADAY SHORT (Flattens 16:20)` for Short positions.
     - `OVERNIGHT ELIGIBLE (TP1 Hit)` for Long positions with `tp1_hit == True`.
     - `REQUIRES TP1 OR FLATTENS 16:20` for Long positions with `tp1_hit == False`.
  2. In `generate_html_dashboard`, add visual badge for overnight status and a live JavaScript countdown timer to 16:20 UTC.
- **Test Scenarios:**
  - Dashboard renders distinct color-coded badges for intraday vs overnight-eligible trades.

### U6. Comprehensive Test Suite
- **Goal:** Provide automated verification covering all protocol flows and edge cases.
- **Covers:** AE1, AE2, AE3, AE4
- **Files:** `tests/test_asymmetric_session.py`
- **Implementation Steps:**
  1. Create test file `tests/test_asymmetric_session.py`.
  2. Test `test_long_position_under_tp1_flattens_at_1620()`.
  3. Test `test_long_position_with_tp1_hit_retained()`.
  4. Test `test_long_position_with_earnings_blackout_flattens()`.
  5. Test `test_short_position_unconditional_1620_flatten()`.
  6. Test `test_rate_limited_sequential_queue_pacing()`.
- **Test Scenarios:**
  - Run `pytest tests/test_asymmetric_session.py` to achieve 100% pass rate.

---

## Verification Contract

### Automated Verification Commands
```bash
# 1. Run all unit and integration tests for Asymmetric Session Protocol
pytest tests/test_asymmetric_session.py -v

# 2. Verify Thread-Safe Rate Limiter
python3 -c "from trading_system.data.trading212_client import Trading212Client; c = Trading212Client('test', 'https://demo.trading212.com'); assert hasattr(c, '_rate_lock')"

# 3. Verify Dashboard HTML Generation with Overnight Badges
python3 -m trading_system.dashboard.server --generate-only
```

### Exit Gates
- All tests in `tests/test_asymmetric_session.py` pass.
- Broker request delays strictly obey $\ge 1.15$ seconds under multi-threaded load.
- No `429 Too Many Requests` logged during batch liquidations.

---

## Definition of Done

- [ ] `Settings` holds `SESSION_CLOSE_CUTOFF_UTC` and rate-limit configuration.
- [ ] `Trading212Client._request` enforces thread-safe `_rate_lock` with 1.15s delay.
- [ ] `PositionMonitor` evaluates session boundaries at 16:20 UTC and liquidates unqualified positions.
- [ ] Inverse 3x positions are 100% liquidated by 16:20 UTC.
- [ ] Long positions without TP1 hit are liquidated by 16:20 UTC.
- [ ] Long positions with earnings $\le 24$h are liquidated by 16:20 UTC.
- [ ] Live dashboard displays `Overnight Status` and countdown to 16:20 UTC.
- [ ] Automated test suite verifies AE1, AE2, AE3, and AE4.
