---
title: Session-Day Decay Trailing Stop Envelope & Volatility Drag Mitigation
date: 2026-10-30
artifact_contract: ce-unified-plan/v1
product_contract_source: ce-brainstorm
execution: code
---

## Goal Capsule

- **Objective:** Eliminate capital erosion caused by daily compounding volatility drag (beta slippage) on stagnant leveraged ETP positions by compressing the allowable trailing stop distance across holding sessions, forcing Break-Even or Zero-Alpha liquidation after 4 sessions of chop, while granting complete decay immunity to profitable runners (+3.0% gain) to let multi-week trend-following profits run.
- **Means:** Implement a session-day decay calculation in `PositionMonitor` that tightens the active ATR trailing stop multiplier by 0.35x ATR per completed exchange session during the 07:30 UTC daily rollover sync, updating Trading 212 native GTC stop orders before market open.
- **Product Authority:** Covers leveraged ETP position lifecycle management, stop-loss calculations, and broker stop synchronization. Upstream scanning, System 2 multi-agent debate, and portfolio circuit breakers are preserved context, not active scope.
- **Open Blockers:** None.

## Product Contract

**Product Contract Preservation:** Unchanged from `ce-brainstorm`. All stable IDs (R1–R12, F1–F3, AE1–AE4) and scope boundaries are preserved verbatim.

### Summary

The Session-Day Trailing Stop Envelope extends the 100% Pure Trailing Stop Loss engine with a time-decay dimension tailored for leveraged Exchange-Traded Products (3x and 5x). Active positions start with a standard 2.2x ATR noise buffer on Day 1. On each subsequent completed market trading session, the allowable ATR trailing distance compresses by 0.35x ATR. Positions stagnant after 4 sessions are ratcheted directly to Break-Even or evacuated before market close. Positions locking in the +1.5% profit cushion (+3.0% gain) graduate to Tenured Runner status with full decay immunity, preserving wide trailing stops for multi-week momentum runs.

### Problem Frame

Leveraged ETPs (e.g. 3LNV, 3TSL, QQQ3, 3USL) rebalance their debt/swap exposure daily at market close. In choppy, non-trending markets, daily compounding causes volatility drag (beta slippage): a 3x leveraged product held through 5 to 10 days of oscillating price action will lose value even if the underlying index ends flat.

Under the current 100% Pure TSL architecture, trailing stops move exclusively when price reaches a new high-water mark or crosses the +1.5% / +3.0% profit thresholds. If price chops sideways around entry for multiple sessions, the trailing stop remains static at 2.2x ATR below entry. The position continues accumulating rebalancing decay while tying up risk capital. A time-decay envelope is required to actively compress the allowable risk window as holding duration lengthens without prematurely choking valid breakout runners.

### Key Decisions

- **Session-Day Discrete Compression:** Compress trailing stop distance by 0.35x ATR per completed exchange trading day rather than calculating continuous intraday hourly decay. (session-settled: user-directed — chosen over continuous hourly decay: avoids intraday thrashing and aligns directly with European exchange daily rebalancing schedules). Governs R3, R4.
- **Decay Immunity for Established Runners:** Freeze time decay once a trade achieves +3.0% unrealized gain and locks in the +1.5% profit cushion. (session-settled: user-directed — chosen over uniform decay across all positions: allows multi-week trend runners to compound without premature session stop-outs). Governs R6, R7.
- **Daily Rollover Synchronization:** Calculate session decay and update broker native GTC stop orders at 07:30 UTC daily rollover before European market open. (session-settled: user-directed — chosen over closing auction liquidation: ensures resting GTC stop protection is live on Trading 212 before the 08:00 UTC opening auction). Governs R8, R9.
- **Pure Trailing Stop Loss Preservation:** Maintain single native GTC stop order execution on Trading 212 Invest without reintroducing static take-profit limit orders. (session-settled: user-approved — chosen over bracket orders: preserves API compatibility and prevents Trading 212 Error 400). Governs R1, R10.

### Requirements

#### Session Age & State Accounting

- R1. The system must record and persist the integer session age (`session_count: int`, initialized to `1` on entry date) for each active position in `active_trades.json`.
- R2. The session counter must increment by 1 only on completed exchange trading days where the market was open for regular trading, ignoring non-trading weekend days and official exchange holidays.

#### Envelope Compression Mechanics

- R3. On Day 1 (entry session), the trailing stop must use the baseline noise buffer multiplier (2.20x ATR, clamped between 2.5% and 8.0% of price).
- R4. For non-runner positions on Day 2, Day 3, and Day 4, the allowable ATR multiplier must compress by 0.35x ATR per elapsed session:
  - Day 1: 2.20x ATR multiplier
  - Day 2: 1.85x ATR multiplier
  - Day 3: 1.50x ATR multiplier
  - Day 4+: 1.15x ATR multiplier (floor)
- R5. If a position reaches Day 4 (3 completed rollover cycles) with unrealized P&L between -2.0% and +1.5% (stagnant chop zone), the system must elevate the trailing stop directly to Entry Price (Break-Even), or trigger a `ZERO_ALPHA_EVACUATION` exit at 16:20 UTC before LSE close.

#### Runner Decay Immunity

- R6. When an active position achieves an unrealized gain of +3.0% or greater and activates the +1.5% profit cushion, the position must be marked as `is_runner: bool = True`.
- R7. Positions marked as `is_runner == True` must be exempt from session-day decay compression, retaining the full baseline 2.0x ATR trailing stop distance to allow multi-week trend-following runs.

#### Daily Rollover & Broker Synchronization

- R8. The `PositionMonitor.rollover_daily_stops()` routine at 07:30 UTC must recalculate the compressed trailing stop loss for each non-runner position using the formula:
  $$\text{Stop Distance} = \text{Peak Price} - (\text{ATR} \times \max(1.15, 2.20 - 0.35 \times (\text{session\_count} - 1)))$$
- R9. The updated stop-loss price must be synchronized to Trading 212 via `place_broker_stop()` as an updated GTC stop-loss order prior to 07:55 UTC.
- R10. The ratcheting invariant must be strictly enforced: a compressed stop loss price must only move in the favorable direction (higher for spot long positions) and must never loosen or drop below the previous session's resting stop price.

#### Telemetry & Alerting

- R11. When a position is closed due to the Day 4+ Break-Even ratchet or 16:20 UTC stagnancy exit, the system must log an alert with event type `ZERO_ALPHA_EVACUATION` and broadcast it to the dashboard and notification feeds.
- R12. The dashboard `/api/data` payload and position monitor cards must display the active `session_count` and the active effective ATR multiplier (e.g. `1.85x ATR (Day 2 Decay)`).

### Key Flows

- F1. Normal Stagnant Trade Compression Lifecycle
  - **Trigger:** A 3x ETP position is entered on Day 1 with 2.2x ATR stop. Price oscillates in a tight range (-0.5% to +0.8%).
  - **Actors:** `PositionMonitor`, Trading 212 API.
  - **Steps:**
    1. At Day 2 rollover (07:30 UTC), `session_count` increments to 2. Stop distance compresses to 1.85x ATR. Native broker stop ratchets up.
    2. At Day 3 rollover, `session_count` increments to 3. Stop distance compresses to 1.50x ATR. Native broker stop ratchets up.
    3. At Day 4 rollover, `session_count` increments to 4. Position remains under +1.5% profit.
    4. Stop loss is elevated directly to Entry Price (Break-Even).
    5. If price fails to break out and remains flat, the position is evacuated at 16:20 UTC before market close.
  - **Covered by:** R1, R2, R3, R4, R5, R8, R9, R10, R11.

- F2. High-Momentum Runner Immunity Transition
  - **Trigger:** A 3x ETP position surges +4.5% on Day 2 following an earnings beat on the underlying.
  - **Actors:** `PositionMonitor`, `RiskManager`, Trading 212 API.
  - **Steps:**
    1. During in-flight price polling, unrealized profit crosses +3.0%.
    2. Progressive stop triggers: Stop loss is moved to Entry Price + 1.5% (Cushion Locked).
    3. Position flag `is_runner` is set to `True`.
    4. At Day 3 and Day 4 rollover, the system detects `is_runner == True` and bypasses the 0.35x ATR decay compression.
    5. The trade continues trailing at 2.0x ATR below peak price, capturing an extended 10-day trend.
  - **Covered by:** R6, R7, R8, R10.

- F3. Weekend and Holiday Rollover Accounting
  - **Trigger:** Friday market close transitions into Monday session.
  - **Actors:** `PositionMonitor`, `MarketDataProvider`.
  - **Steps:**
    1. Saturday and Sunday 07:30 UTC checks detect closed exchange calendar and skip session incrementation.
    2. Monday 07:30 UTC rollover verifies the next trading session and increments `session_count` by exactly 1.
    3. Stop order is verified on Trading 212 without duplicate decay penalty over non-trading days.
  - **Covered by:** R1, R2, R8.

### Acceptance Examples

- AE1. Day 2 Stop Compression Calculation
  - **Covers:** R3, R4, R8, R10
  - **Given:** A position in `QQQ3l_EQ` entered at €100.00 with ATR = €2.00, initial stop at €95.60 (2.2x ATR), peak price €101.00.
  - **When:** 07:30 UTC daily rollover executes for Day 2 (`session_count = 2`).
  - **Then:** Effective multiplier becomes $2.20 - (0.35 \times 1) = 1.85$. Compressed distance is $1.85 \times €2.00 = €3.70$. Recommended stop price is $€101.00 - €3.70 = €97.30$. Since €97.30 > €95.60, the resting GTC stop on Trading 212 is ratcheted up to €97.30.

- AE2. Day 4 Stagnancy Evacuation
  - **Covers:** R5, R8, R11
  - **Given:** A position in `3LNVl_EQ` entered at €50.00 has hovered between €49.50 and €50.50 for 3 days. `session_count` reaches 4.
  - **When:** Daily rollover runs on Day 4 with current price at €50.20 (+0.4% unrealized gain).
  - **Then:** The position is classified as stagnant. Stop loss is ratcheted to €50.00 (Break-Even). At 16:20 UTC, if unstopped and gain remains < +1.5%, the position is closed via market order and an alert `ZERO_ALPHA_EVACUATION` is broadcast.

- AE3. Runner Immunity Preservation
  - **Covers:** R6, R7, R8
  - **Given:** A position in `GPTSl_EQ` entered at $33.50 hits peak price $38.00 (+13.4% gain, Cushion locked). `session_count` is 5.
  - **When:** Daily rollover executes for Day 5 with ATR = $1.20.
  - **Then:** `is_runner` is `True`. The 0.35x decay penalty is suppressed. Trailing stop is calculated using full 2.0x ATR distance: $\$38.00 - (2.0 \times \$1.20) = \$35.60$. The stop does not compress to the 1.15x floor.

- AE4. Stop Invariant Non-Loosening Guard
  - **Covers:** R10
  - **Given:** A position has an existing resting stop of €80.00 due to an earlier intraday wick. A newly calculated decayed stop yields €78.50.
  - **When:** `rollover_daily_stops()` compares the candidate stop against `pos.trailing_stop_loss`.
  - **Then:** The candidate stop (€78.50) is rejected because it is lower than the active stop. The active stop remains locked at €80.00.

### Scope Boundaries

#### In Scope

- Adding `session_count: int` and `is_runner: bool` fields to `TrackedPosition` dataclass with state serialization in `active_trades.json`.
- Modifying `PositionMonitor.rollover_daily_stops()` to evaluate exchange calendar sessions and calculate the compressed ATR envelope.
- Modifying `RiskManager.calculate_trailing_stop()` to accept dynamic session decay multipliers.
- Emitting `ZERO_ALPHA_EVACUATION` alert events in telemetry, logger, and `/api/data` dashboard payload.
- Comprehensive unit and E2E regression tests covering multi-day decay steps, runner exemption, and calendar filtering.

#### Out of Scope / Deferred

- Continuous intraday hourly decay: Deferred to avoid excessive broker API order modifications.
- Multi-broker routing (IBKR/Alpaca): Deferred; Trading 212 Invest remains the sole execution target.
- Machine-learning predictive decay models (GARCH/LSTM): Deferred in favor of deterministic session-day arithmetic.
- Intraday tick streaming WebSockets: Deferred due to Trading 212 REST API constraints.

### Success Criteria

- SC1. 100% of open non-runner positions held for 2+ trading sessions exhibit tightened stop distances aligned with the $0.35 \times \text{session}$ compression formula.
- SC2. 0% of positions with unrealized profit exceeding +3.0% suffer premature time-decay stop-outs; all tenured runners maintain full 2.0x ATR trailing room.
- SC3. Zero Trading 212 API Error 400 rejections during rollover stop updates.
- SC4. 100% test pass rate across existing unit and E2E regression test suites (606+ tests).

### Outstanding Questions

#### Deferred to Planning

- OQ1. Should exchange trading calendars be fetched dynamically via pandas_market_calendars or validated via an offline list of LSE/NYSE holiday dates to maintain Raspberry Pi offline resilience? (Recommendation: lightweight offline holiday table with fallback to weekday checks).
- OQ2. Should the `ZERO_ALPHA_EVACUATION` at 16:20 UTC use the existing `_liquidate_asymmetric_session_positions` pipeline or a dedicated handler? (Recommendation: integrate cleanly into the 16:20 UTC asymmetric session loop).

---

## Planning Contract

### High-Level Technical Design

```
+---------------------------------------------------------------------------------------------------+
|                                 SESSION-DAY DECAY TRAILING STOP ENVELOPE                          |
|                                                                                                   |
|   07:30 UTC Daily Rollover Cycle (PositionMonitor)                                                |
|   +-------------------------------------------------------------------------------------------+   |
|   | 1. Exchange Session Evaluation:                                                           |   |
|   |    Check date vs offline calendar. If regular trading day and date != last_session_date:  |   |
|   |    pos.session_count += 1, pos.last_session_date = today                                  |   |
|   +---------------------------------------------+---------------------------------------------+   |
|                                                 |                                                 |
|                                                 v                                                 |
|   +---------------------------------------------+---------------------------------------------+   |
|   | 2. Runner & Gain Check:                                                                   |   |
|   |    Is pos.is_runner == True (Unrealized Gain >= +3.0% with Cushion Locked)?               |   |
|   |    [YES] -> Multiplier = 2.00x ATR (Decay Frozen, Multi-Week Trend Ride)                  |   |
|   |    [NO]  -> Multiplier = max(1.15, 2.20 - 0.35 * (pos.session_count - 1))                 |   |
|   +---------------------------------------------+---------------------------------------------+   |
|                                                 |                                                 |
|                                                 v                                                 |
|   +---------------------------------------------+---------------------------------------------+   |
|   | 3. Day 4+ Stagnancy Gate:                                                                 |   |
|   |    Is pos.session_count >= 4 AND unrealized_pnl < +1.5%?                                  |   |
|   |    [YES] -> Elevate recommended stop directly to pos.entry_price (Break-Even)             |   |
|   |             Schedule for 16:20 UTC ZERO_ALPHA_EVACUATION if still flat                    |   |
|   |    [NO]  -> recommended_sl = pos.highest_price - (pos.atr * Multiplier)                   |   |
|   +---------------------------------------------+---------------------------------------------+   |
|                                                 |                                                 |
|                                                 v                                                 |
|   +---------------------------------------------+---------------------------------------------+   |
|   | 4. Broker Stop Ratchet & Invariant:                                                       |   |
|   |    Assert recommended_sl > pos.trailing_stop_loss (Never loosen stop)                     |   |
|   |    Assert recommended_sl < pos.current_price (Never place stop above market)              |   |
|   |    Execute: self.place_broker_stop(pos, recommended_sl) via Trading 212 API               |   |
|   +-------------------------------------------------------------------------------------------+   |
+---------------------------------------------------------------------------------------------------+
```

### Key Technical Decisions

- KTD1. **Deterministic Offline Holiday Calendar over Remote API:** Implement an offline set of fixed market holidays (New Year's, Good Friday, Easter Monday, Early May, Spring Bank, Summer Bank, Christmas, Boxing Day for LSE, plus US federal holidays for dual-listed underlyings) combined with weekday checking (`weekday() < 5`). Governs R2. (session-settled: user-directed — chosen over dynamic market calendar APIs: guarantees 100% offline edge runner reliability on Raspberry Pi without external network dependencies).
- KTD2. **In-Memory and On-Disk State Schema Evolution:** Add `session_count: int = 1`, `is_runner: bool = False`, and `last_session_date: str = ""` directly to the `TrackedPosition` dataclass. The existing `known_fields = {f.name for f in fields(TrackedPosition)}` reflection in `load_state` will automatically populate defaults for legacy state entries without requiring database migration or breaking `active_trades.json`. Governs R1.
- KTD3. **Rollover Non-Gain Stagnancy Branching:** In `PositionMonitor.rollover_daily_stops()`, eliminate the early `if not has_gain: continue` guard. Instead, branch into two distinct paths:
  1. *Gain Path (Peak > Entry):* Evaluates profit-cushion ratchet and chandelier trailing stop with decay multiplier.
  2. *Stagnancy Path (Peak <= Entry):* Evaluates session decay compression and elevates to Break-Even on Day 4+. Governs R4, R5, R8.
- KTD4. **Unified 16:20 UTC Asymmetric Loop Evacuation:** Integrate the Day 4+ Zero-Alpha liquidation into `PositionMonitor._liquidate_asymmetric_session_positions()`. Positions reaching Day 4 with P&L < +1.5% will be marked for session close liquidation along with inverse ETPs, emitting `ZERO_ALPHA_EVACUATION` in alerts. Governs R5, R11.

### System-Wide Impact

- **Broker API Calls:** Exactly one stop modification request per active position during the 07:30 UTC rollover. Well within Trading 212's 30 req/min rate limit.
- **Micro-Runner CPU & Memory:** The offline holiday check and mathematical multiplier take `<0.1ms` per position, adding zero perceptible latency to the 15-second polling loop.
- **State File Compatibility:** `active_trades.json` is preserved atomically. Older versions of the bot can read the file safely, and running tests will not invalidate existing state.

### Assumptions

- Exchange trading days follow regular LSE / Western European holiday schedules. Half-day trading sessions (e.g. Christmas Eve) count as a full completed session for rebalancing purposes.
- Positions opened intraday start at `session_count = 1`. Their first rollover occurs on the following calendar day at 07:30 UTC (`session_count = 2`).

---

## Implementation Units

### U1. State Schema & Position Dataclass Accounting

- **Goal:** Update `TrackedPosition` dataclass with `session_count`, `is_runner`, and `last_session_date` fields with backwards-compatible defaults, and ensure atomic state serialization.
- **Requirements:** R1, R2.
- **Dependencies:** None.
- **Files:**
  - `trading_system/execution/position_monitor.py`
  - `tests/test_position_monitor.py`
- **Approach:**
  1. Add fields to `TrackedPosition`:
     ```python
     session_count: int = 1
     is_runner: bool = False
     last_session_date: str = ""
     ```
  2. Ensure `load_state()` retains `session_count` and `is_runner` when reading `active_trades.json`.
  3. Ensure `register_new_trade()` initializes `session_count = 1` and `last_session_date` to today's UTC date string (`YYYY-MM-DD`).
- **Patterns to follow:** Existing `known_fields` dataclass reflection in `PositionMonitor.load_state()`.
- **Test scenarios:**
  - Test scenario 1: New position registration initializes `session_count = 1`, `is_runner = False`, and `last_session_date` matching current date.
  - Test scenario 2: Deserializing legacy state missing `session_count` defaults to `1` without throwing `KeyError`.
  - Test scenario 3: Serializing active positions to JSON retains `session_count` and `is_runner` accurately across save/load cycles.

### U2. Dynamic Session Decay ATR Compression in RiskManager

- **Goal:** Add session-day decay calculation logic to `RiskManager`, supporting dynamic ATR multiplier compression and runner exemption.
- **Requirements:** R3, R4, R6, R7.
- **Dependencies:** None.
- **Files:**
  - `trading_system/agents/risk_manager.py`
  - `tests/test_risk_manager.py`
- **Approach:**
  1. Add helper method `get_decay_atr_multiplier(session_count: int, is_runner: bool = False) -> float`:
     - If `is_runner`: return `2.00`
     - Else: return `round(max(1.15, 2.20 - 0.35 * max(0, session_count - 1)), 2)`
  2. Update `calculate_trailing_stop` to accept `session_count: int = 1` and `is_runner: bool = False`, scaling distance by the decayed multiplier:
     ```python
     effective_mult = self.get_decay_atr_multiplier(session_count=session_count, is_runner=is_runner)
     distance = max(effective_mult * atr, peak_price * 0.02)
     ```
- **Patterns to follow:** Existing `tsl_atr_multiplier` calculation in `RiskManager.calculate_trailing_stop()`.
- **Test scenarios:**
  - Test scenario 1: Day 1 (`session_count = 1`) returns `2.20x` ATR. Covers AE1.
  - Test scenario 2: Day 2 returns `1.85x`, Day 3 returns `1.50x`, Day 4 returns `1.15x`, Day 5+ floors at `1.15x`. Covers AE1.
  - Test scenario 3: When `is_runner = True`, Day 4 returns full `2.00x` ATR without decay compression. Covers AE3.

### U3. PositionMonitor Rollover Calculation & In-Flight Broker Stop Sync

- **Goal:** Update `PositionMonitor.rollover_daily_stops()` to increment session counts on trading days, compress trailing stops for both gaining and stagnant trades, and ratchet resting GTC broker stops.
- **Requirements:** R4, R5, R6, R7, R8, R9, R10.
- **Dependencies:** U1, U2.
- **Files:**
  - `trading_system/execution/position_monitor.py`
  - `tests/test_broker_native_orders.py`
- **Approach:**
  1. Implement `is_exchange_trading_day(dt: datetime) -> bool`: returns True if Monday-Friday and date not in offline holiday list.
  2. In `rollover_daily_stops()`:
     - Check if today is a trading day and `pos.last_session_date != today_str`:
       increment `pos.session_count += 1` and update `pos.last_session_date = today_str`.
     - Check runner status: if unrealized P&L >= +3.0% or cushion locked, set `pos.is_runner = True`.
     - Calculate decayed ATR distance using `RiskManager.get_decay_atr_multiplier()`.
     - For stagnant positions on Day 4+ (`pos.session_count >= 4` and P&L < +1.5%), set `recommended_sl = pos.entry_price`.
     - Clamp `recommended_sl` below `cur_px` (cur_px * 0.985 guard) if `cur_px > 0`.
     - Ratchet stop if `recommended_sl > pos.trailing_stop_loss`.
     - Call `self.place_broker_stop(pos, stop_price=recommended_sl)`.
- **Patterns to follow:** Existing `rollover_daily_stops()` stop clamping and broker placement in `PositionMonitor`.
- **Test scenarios:**
  - Test scenario 1: Saturday/Sunday execution does not increment `session_count` or advance decay. Covers F3.
  - Test scenario 2: Gaining position on Day 2 compresses trail from 2.2x to 1.85x ATR and ratchets broker stop upward. Covers AE1.
  - Test scenario 3: Stagnant position on Day 4 elevates stop directly to Break-Even (`pos.entry_price`). Covers AE2.
  - Test scenario 4: Stop invariant prevents stop from loosening if candidate decayed stop is lower than active stop. Covers AE4.

### U4. Day 4 Stagnancy Evacuation & 16:20 UTC Asymmetric Loop Integration

- **Goal:** Integrate automated Day 4+ zero-alpha stagnancy liquidation into the 16:20 UTC session risk handler.
- **Requirements:** R5, R11.
- **Dependencies:** U1, U3.
- **Files:**
  - `trading_system/execution/position_monitor.py`
  - `tests/test_position_monitor.py`
- **Approach:**
  1. In `_liquidate_asymmetric_session_positions()`:
     - Check active spot positions:
       ```python
       is_day4_stagnant = (
           pos.session_count >= 4
           and not pos.is_runner
           and gain_pct < 1.5
       )
       ```
     - If `is_day4_stagnant`:
       liquidate position via broker market order, cancel resting stop, and emit `ZERO_ALPHA_EVACUATION` notification.
- **Patterns to follow:** Existing inverse ETP 16:20 UTC liquidation in `_liquidate_asymmetric_session_positions()`.
- **Test scenarios:**
  - Test scenario 1: Position on Day 4 with +0.5% gain is liquidated at 16:20 UTC, logging `ZERO_ALPHA_EVACUATION`. Covers AE2.
  - Test scenario 2: Position on Day 4 with +3.5% gain (`is_runner = True`) is held overnight without liquidation. Covers AE3.
  - Test scenario 3: Position on Day 2 with -0.5% gain is held overnight (decay continues without early liquidation). Covers F1.

### U5. Telemetry, Dashboard API & Alert Feeds

- **Goal:** Expose session age, decay status, and runner flags in `/api/data` payload, update dashboard UI badges, and add `ZERO_ALPHA_EVACUATION` alerts.
- **Requirements:** R11, R12.
- **Dependencies:** U1, U3.
- **Files:**
  - `trading_system/dashboard/server.py`
  - `signals_dashboard.html`
  - `trading_system/agents/notifier.py`
  - `tests/test_dashboard_equity_history.py`
- **Approach:**
  1. In `trading_system/dashboard/server.py`:
     Include `session_count`, `is_runner`, and `decay_multiplier` in active signals JSON.
  2. In `signals_dashboard.html`:
     Render session pill badge: `Day {session_count} ({multiplier}x ATR)` on position cards.
  3. In `trading_system/agents/notifier.py`:
     Support `ZERO_ALPHA_EVACUATION` event format with clear rationale.
- **Patterns to follow:** Existing pure TSL status badges in `signals_dashboard.html` and `server.py`.
- **Test scenarios:**
  - Test scenario 1: `/api/data` JSON includes `session_count`, `is_runner`, and formatted status text.
  - Test scenario 2: `notify_tsl` correctly dispatches `ZERO_ALPHA_EVACUATION` event message.

### U6. Verification Test Suite & Regression Hardening

- **Goal:** Create comprehensive dedicated unit tests for session-decay envelope and run full regression suite.
- **Requirements:** All (R1–R12).
- **Dependencies:** U1–U5.
- **Files:**
  - `tests/test_session_decay_tsl.py`
  - `tests/test_broker_native_orders.py`
- **Approach:**
  1. Build `tests/test_session_decay_tsl.py` with mock clock and multi-day simulated trading sessions.
  2. Verify all edge cases: weekend roll, holiday skip, runner breakout, stagnancy evacuation, stop invariant.
  3. Run full unit and E2E test suites (`python3 -m unittest discover tests` and `bash run_e2e_tests.sh`).
- **Patterns to follow:** Existing `tests/test_broker_native_orders.py` test fixtures.
- **Test scenarios:**
  - Test scenario 1: Full multi-day simulation tests 4-day lifecycle from entry to evacuation. Covers F1.
  - Test scenario 2: Runner transition scenario verifies immunity and multi-week trailing. Covers F2.
  - Test scenario 3: All 405 existing unit tests + 201 E2E tests pass with 0 regressions.

---

## Verification Contract

### Automated Test Suites

Execute following test commands in sequence:
1. `python3 -m unittest tests/test_session_decay_tsl.py` — Verifies new decay envelope and stagnancy evacuation logic.
2. `python3 -m unittest discover tests` — Verifies full unit test suite (405+ tests).
3. `bash run_e2e_tests.sh` — Verifies all 4 tiers of opaque-box E2E scenarios (201 tests).

### Acceptance Criteria Verification

- [ ] `session_count` increments exclusively on completed exchange business days (ignoring weekends/holidays).
- [ ] Non-runner positions compress by 0.35x ATR per session (Day 1: 2.20x, Day 2: 1.85x, Day 3: 1.50x, Day 4: 1.15x).
- [ ] Positions with gain >= +3.0% lock Cushion and are marked `is_runner = True`, freezing decay at 2.0x ATR.
- [ ] Stagnant positions on Day 4+ elevate stop to Break-Even and evacuate at 16:20 UTC before market close.
- [ ] Stops never loosen (`should_ratchet` strictly enforces favorable direction).
- [ ] Full regression passes with 0 failures, 0 errors.

---

## Definition of Done

1. **Code Implementation:** All 6 implementation units (U1–U6) are cleanly implemented in Python.
2. **Schema Safety:** `active_trades.json` persists new attributes without breaking backwards compatibility.
3. **Execution Safety:** Zero Trading 212 API Error 400 exceptions during daily rollover or in-flight stop updates.
4. **Verification:** 100% test pass rate across unit and E2E test suites (606+ passing tests).
5. **Production Deployment:** Cleanly deployable to Raspberry Pi 5 micro-runner with active service restart verification.
