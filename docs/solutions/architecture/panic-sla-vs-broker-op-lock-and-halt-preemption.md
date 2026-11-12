---
title: "Panic SLA vs _broker_op_lock Contention and Re-Epoch Halt Preemption"
date: 2026-11-12
category: architecture
module: trading_system/execution
problem_type: logic_error
component: position_monitor
severity: critical
symptoms:
  - "Wrapping place_broker_stop in _broker_op_lock causes panic_flatten_all to exceed its emergency latency budget (<0.12s) while a slow monitor_tick stop call is in flight"
  - "Aborting place_broker_stop mid-call on is_system_halted() breaks re_epoch_all_positions per-position preemption when a halt trips during the active position's broker call"
  - "Suppressing alert/cleanup exceptions with contextlib.suppress without importing contextlib raises NameError after orphan-stop cancellation or halt"
root_cause: concurrency
resolution_type: code_fix
tags:
  - position-monitor
  - panic-flatten
  - lock-ordering
  - re-epoch
  - halt-safety
---

# Panic SLA vs `_broker_op_lock` Contention and Re-Epoch Halt Preemption

## Problem
Three competing safety invariants govern `PositionMonitor` in [`position_monitor.py`](file:///Users/pratikshah/Documents/antigravity/modest-bose/trading_system/execution/position_monitor.py):

1. **Emergency panic responsiveness**: `panic_flatten_all` must flip `is_halted = True` immediately and liquidate positions without blocking on slow background `monitor_tick` broker HTTP calls.
2. **Orphan-stop prevention**: A background `place_broker_stop` call that began before panic must not leave a freshly created stop order resting on Trading 212 after `panic_flatten_all` halts the system.
3. **Deterministic batch Re-Epoch preemption**: During `re_epoch_all_positions`, if a halt triggers while migrating position `P1`, `P1` completes its authorized stop update under `_broker_op_lock` while subsequent position `P2` is preempted (`test_re_epoch_all_preempted_by_panic_halt`).

Wrapping `place_broker_stop` wholesale in `with self._broker_op_lock:` violates (1) because slow broker HTTP I/O during `monitor_tick` blocks `panic_flatten_all`. Conversely, aborting unconditionally inside `place_broker_stop` whenever `is_system_halted()` becomes `True` breaks (3).

## Root Cause
`re_epoch_position`, `re_epoch_all_positions`, `reconcile_with_broker`, and `panic_flatten_all` serialize structural broker mutations via `_broker_op_lock`, whereas high-frequency `monitor_tick` stop placements must remain non-blocking with respect to `_broker_op_lock` while still fencing against concurrent halts via `_state_lock`.

## Solution
In [`trading_system/execution/position_monitor.py`](file:///Users/pratikshah/Documents/antigravity/modest-bose/trading_system/execution/position_monitor.py):

1. Keep `place_broker_stop` **outside** `_broker_op_lock` so `monitor_tick` never blocks `panic_flatten_all`.
2. Add `_allow_while_halted: bool = False` to `place_broker_stop`:
   - Default (`False`, used by `monitor_tick`, `reconcile_with_broker`, and external callers): check `(not _allow_while_halted and self.is_system_halted()) or pos.is_closed` under `_state_lock` before cancelling/placing AND immediately after `place_stop_order` returns; if halted or closed mid-call, cancel the newly created orphan stop order via `self.t212.cancel_order(order_id)` and return `None`.
   - Pass `_allow_while_halted=True` ONLY from `re_epoch_position` (which already holds `_broker_op_lock` and checked `is_system_halted()` up front) and `resume_trading` (which intentionally re-arms stops before unhalting).
3. Ensure `reconcile_with_broker` also re-checks `if self.is_system_halted(): return summary` inside `_state_lock` immediately before committing an adopted broker position.

## Prevention
- Never acquire `_broker_op_lock` inside `place_broker_stop` or any `monitor_tick` hot path; verify both [`tests/test_position_monitor_epochs.py`](file:///Users/pratikshah/Documents/antigravity/modest-bose/tests/test_position_monitor_epochs.py) and [`tests/test_review_regressions.py`](file:///Users/pratikshah/Documents/antigravity/modest-bose/tests/test_review_regressions.py) (`test_concurrent_panic_save_state_and_monitor_tick_thread_safety`, `test_re_epoch_all_preempted_by_panic_halt`, and `test_heal_stale_stop_skips_rejected_price_and_respects_halt_and_panic_latency`) after any change to `PositionMonitor` locking.
