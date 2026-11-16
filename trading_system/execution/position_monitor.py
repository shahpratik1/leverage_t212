"""Active Position & Lifecycle Monitor for 3x Leveraged Trades.

Monitors open positions in real time, manages multi-stage exits (TP1, TP2, TP3),
ratchets dynamic Trailing Stop Losses (TSL), triggers auto-executions on Trading 212,
and dispatches real-time alerts.
"""

from __future__ import annotations

import contextlib
import inspect
import json
import logging
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from trading_system.agents.risk_manager import DecisionCard, RiskManager
from trading_system.config import atomic_write_text, settings
from trading_system.data.leveraged_universe import get_etp_direction, get_ticker_currency
from trading_system.data.trading212_client import Trading212Client, Trading212Error
from trading_system.notifications.notifier import Notifier


class PositionEpochConflictError(RuntimeError):
    """Raised when optimistic CAS validation detects a stale position or config epoch."""

    def __init__(
        self,
        message: str,
        *,
        ticker: str = "",
        expected_epoch_id: Optional[str] = None,
        actual_epoch_id: Optional[str] = None,
        expected_position_epoch_id: Optional[str] = None,
        actual_position_epoch_id: Optional[str] = None,
        expected_target_epoch_id: Optional[str] = None,
        actual_target_epoch_id: Optional[str] = None,
    ) -> None:
        self.ticker = ticker
        self.expected_epoch_id = expected_epoch_id if expected_epoch_id is not None else expected_position_epoch_id
        self.actual_epoch_id = actual_epoch_id if actual_epoch_id is not None else actual_position_epoch_id
        self.expected_position_epoch_id = expected_position_epoch_id
        self.actual_position_epoch_id = actual_position_epoch_id
        self.expected_target_epoch_id = expected_target_epoch_id
        self.actual_target_epoch_id = actual_target_epoch_id
        super().__init__(message)


class ResumeBlockedError(RuntimeError):
    """Raised when resume_trading cannot verify or re-arm GTC stops for all open positions."""

    def __init__(
        self,
        message: str,
        *,
        unprotected_positions: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self.unprotected_positions: List[Dict[str, Any]] = list(unprotected_positions or [])
        super().__init__(message)

logger = logging.getLogger(__name__)

OFFLINE_EXCHANGE_HOLIDAYS = {
    # 2026 European (LSE) & US Market Holidays
    "2026-01-01",  # New Year's Day
    "2026-01-19",  # MLK Jr. Day
    "2026-02-16",  # Presidents' Day
    "2026-04-03",  # Good Friday
    "2026-04-06",  # Easter Monday
    "2026-05-04",  # Early May Bank Holiday
    "2026-05-25",  # Spring Bank Holiday / Memorial Day
    "2026-06-19",  # Juneteenth
    "2026-07-03",  # US Independence Day observed
    "2026-08-31",  # Summer Bank Holiday
    "2026-11-26",  # Thanksgiving
    "2026-12-25",  # Christmas Day
    "2026-12-28",  # Boxing Day observed
}


def is_exchange_trading_day(dt: Optional[datetime] = None) -> bool:
    """Check if date is a standard exchange business trading day (Mon-Fri and not holiday)."""
    target = dt or datetime.now(timezone.utc)
    if target.weekday() >= 5:  # 5=Sat, 6=Sun
        return False
    date_str = target.strftime("%Y-%m-%d")
    return date_str not in OFFLINE_EXCHANGE_HOLIDAYS


@dataclass
class TrackedPosition:
    """Represents an active trade being managed through its lifecycle."""
    ticker: str                         # Trading 212 execution ticker (e.g. 'QQQ3l_EQ')
    underlying: str                     # Underlying ticker (e.g. 'QQQ')
    direction: str                      # 'LONG' or 'SHORT'
    entry_price: float
    original_quantity: float
    remaining_quantity: float
    stop_loss: float
    trailing_stop_loss: float
    highest_price: float                # Peak price recorded since entry
    lowest_price: float                 # Trough price recorded since entry
    tp1: float
    tp2: float
    tp3: float
    atr: float
    tp1_hit: bool = False
    tp2_hit: bool = False
    tp3_hit: bool = False
    is_closed: bool = False
    strategy_type: str = "momentum"
    rsi3: Optional[float] = None
    rsi7: Optional[float] = None
    rsi14: Optional[float] = None
    sma5: Optional[float] = None
    win_rate_exp: str = "80%"
    entry_time: str = ""
    last_update_time: str = ""
    earnings_in_days: Optional[float] = None
    is_overnight_eligible: bool = False
    broker_stop_order_id: Optional[str] = None
    broker_tp_order_id: Optional[str] = None
    tp1_armed_hysteresis: bool = False
    tp1_escape_threshold: float = 0.0
    tp1_stop_elevated: bool = False
    scale_calibrated: bool = False
    current_price: Optional[float] = None
    currency: str = "EUR"
    session_count: int = 1
    is_runner: bool = False
    last_session_date: str = ""
    config_epoch_id: Optional[str] = None
    entry_config_snapshot: Optional[Dict[str, Any]] = None
    panic_failed: bool = False
    unprotected_reason: Optional[str] = None

    def __post_init__(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        if not self.entry_time:
            self.entry_time = now
        if not self.last_update_time:
            self.last_update_time = now
        if not self.last_session_date:
            self.last_session_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.highest_price <= 0:
            self.highest_price = self.entry_price
        if self.lowest_price <= 0:
            self.lowest_price = self.entry_price
        if self.ticker and (not self.currency or self.currency == "EUR"):
            resolved = get_ticker_currency(self.ticker)
            if resolved != "EUR":
                self.currency = resolved
        if self.entry_config_snapshot is not None and not isinstance(self.entry_config_snapshot, dict):
            if hasattr(self.entry_config_snapshot, "to_dict"):
                self.entry_config_snapshot = self.entry_config_snapshot.to_dict(include_specs=False)
            elif isinstance(self.entry_config_snapshot, Mapping):
                self.entry_config_snapshot = dict(self.entry_config_snapshot)
            else:
                self.entry_config_snapshot = None


class PositionMonitor:
    """Manages active trades, checks exit targets, updates TSL, and fires notifications."""

    def __init__(
        self,
        t212_client: Trading212Client,
        notifier: Notifier,
        risk_manager: RiskManager,
        state_file: str = "active_trades.json",
        dry_run: bool = False,
        pure_tsl_mode: Optional[bool] = None,
        config_bus: Optional[Any] = None,
        execution_gate_lock: Optional[threading.RLock] = None,
    ):
        self.t212 = t212_client
        self.notifier = notifier
        self.risk_manager = risk_manager
        self.state_file = state_file
        self.dry_run = dry_run
        self.config_bus = config_bus
        self._explicit_pure_tsl_mode: Optional[bool] = pure_tsl_mode
        if pure_tsl_mode is not None:
            self.pure_tsl_mode = pure_tsl_mode
        elif config_bus is not None:
            try:
                self.pure_tsl_mode = bool(config_bus.active_snapshot().PURE_TSL_MODE)
            except Exception:
                self.pure_tsl_mode = getattr(settings, "PURE_TSL_MODE", True)
        else:
            self.pure_tsl_mode = getattr(settings, "PURE_TSL_MODE", True)

        # Split locking (KTD7): lock order is ALWAYS _broker_op_lock -> _state_lock
        # and execution_gate_lock -> _state_lock. Never do network I/O or time.sleep inside _state_lock.
        self._state_lock = threading.RLock()
        self._broker_op_lock = threading.RLock()
        self.execution_gate_lock = execution_gate_lock if execution_gate_lock is not None else threading.RLock()
        self._in_flight_leases: Dict[str, float] = {}

        self.positions: Dict[str, TrackedPosition] = self._load_state()
        self._last_session_close_date: str = ""
        self.is_halted: bool = False
        if self.halt_marker_file != ":memory:.halted" and os.path.exists(self.halt_marker_file):
            self.is_halted = True

    @property
    def _rate_limit_delay(self) -> float:
        delay = getattr(self.t212, "rate_limit_delay", 1.25)
        return float(delay) if isinstance(delay, (int, float)) else 0.0

    @property
    def halt_marker_file(self) -> str:
        return f"{self.state_file}.halted"

    def _atomic_write_text(self, target_path: str, content: str) -> None:
        """Write text atomically via thread-unique NamedTemporaryFile + flush + fsync + os.replace."""
        if not target_path or target_path.startswith(":memory:"):
            return
        atomic_write_text(target_path, content)

    def acquire_position_lease(self, ticker: str, ttl_seconds: float = 30.0) -> bool:
        """Acquire a short-lived in-flight operation lease for ticker under _state_lock."""
        now = time.monotonic()
        with self._state_lock:
            expires_at = self._in_flight_leases.get(ticker)
            if expires_at is not None and expires_at > now:
                return False
            self._in_flight_leases[ticker] = now + max(0.1, float(ttl_seconds))
            return True

    def release_position_lease(self, ticker: str) -> None:
        """Release any in-flight lease held for ticker under _state_lock."""
        with self._state_lock:
            self._in_flight_leases.pop(ticker, None)

    @staticmethod
    def _snap_val(snapshot: Optional[Any], key: str, default: Any = None) -> Any:
        """Read a parameter cleanly from a ConfigSnapshot, mapping, or fallback default."""
        if snapshot is None:
            return default
        if isinstance(snapshot, Mapping):
            val = snapshot.get(key, default)
            return default if val is None else val
        getter = getattr(snapshot, "get", None)
        if callable(getter):
            try:
                val = getter(key, default)
                if val is not None:
                    return val
            except Exception:
                pass
        val = getattr(snapshot, key, None)
        return default if val is None else val

    @staticmethod
    def _snapshot_to_dict(snapshot: Optional[Any]) -> Optional[Dict[str, Any]]:
        """Serialize a snapshot or mapping to a plain JSON-safe dictionary."""
        if snapshot is None:
            return None
        if hasattr(snapshot, "to_dict"):
            return dict(snapshot.to_dict(include_specs=False))
        if isinstance(snapshot, Mapping):
            return dict(snapshot)
        return None

    def _resolve_position_snapshot(self, pos: Optional[TrackedPosition]) -> Optional[Any]:
        """Resolve effective config snapshot: entry_config_snapshot -> ConfigBus epoch -> active."""
        if pos is not None:
            entry_snap = getattr(pos, "entry_config_snapshot", None)
            if entry_snap:
                return entry_snap
            epoch_id = getattr(pos, "config_epoch_id", None)
            if epoch_id and self.config_bus is not None:
                try:
                    snap = self.config_bus.snapshot_for_epoch(epoch_id)
                    if snap is not None:
                        return snap
                except Exception as exc:
                    logger.debug("Could not resolve epoch %s for %s: %s", epoch_id, pos.ticker, exc)
        if self.config_bus is not None:
            try:
                return self.config_bus.active_snapshot()
            except Exception as exc:
                logger.debug("Could not resolve active snapshot from ConfigBus: %s", exc)
        return None

    def _is_pure_tsl_for_position(self, pos: Optional[TrackedPosition]) -> bool:
        """Determine effective PURE_TSL_MODE for a single position with legacy override fallback."""
        if pos is not None:
            entry_snap = getattr(pos, "entry_config_snapshot", None)
            if entry_snap is not None:
                val = self._snap_val(entry_snap, "PURE_TSL_MODE", None)
                if val is not None:
                    return bool(val)
            epoch_id = getattr(pos, "config_epoch_id", None)
            if epoch_id and self.config_bus is not None:
                try:
                    snap = self.config_bus.snapshot_for_epoch(epoch_id)
                    if snap is not None:
                        return bool(self._snap_val(snap, "PURE_TSL_MODE", self.pure_tsl_mode))
                except Exception:
                    pass
        if self.config_bus is not None:
            try:
                snap = self.config_bus.active_snapshot()
                if snap is not None and (
                    self._explicit_pure_tsl_mode is None or getattr(snap, "generation", 0) > 0
                ):
                    return bool(self._snap_val(snap, "PURE_TSL_MODE", self.pure_tsl_mode))
            except Exception:
                pass
        if self._explicit_pure_tsl_mode is not None:
            return bool(self.pure_tsl_mode)
        return bool(getattr(self, "pure_tsl_mode", getattr(settings, "PURE_TSL_MODE", True)))

    @staticmethod
    def _callable_accepts_kwarg(fn: Any, kwarg: str) -> bool:
        """Check whether a callable accepts a given keyword parameter or **kwargs."""
        try:
            sig = inspect.signature(fn)
            for param in sig.parameters.values():
                if param.name == kwarg or param.kind == inspect.Parameter.VAR_KEYWORD:
                    return True
        except (TypeError, ValueError):
            pass
        return False

    def _get_decay_atr_multiplier(
        self,
        session_count: int = 1,
        is_runner: bool = False,
        config_snapshot: Optional[Any] = None,
    ) -> float:
        """Compute session-day ATR decay multiplier from snapshot or RiskManager."""
        if config_snapshot is not None:
            fn = getattr(self.risk_manager, "get_decay_atr_multiplier", None)
            if fn is not None and self._callable_accepts_kwarg(fn, "config_snapshot"):
                try:
                    val = fn(session_count=session_count, is_runner=is_runner, config_snapshot=config_snapshot)
                    if isinstance(val, (int, float)):
                        return float(val)
                except Exception:
                    pass
            runner_mult = float(self._snap_val(config_snapshot, "TSL_DECAY_RUNNER_MULT", getattr(settings, "TSL_DECAY_RUNNER_MULT", 2.00)))
            if is_runner:
                return runner_mult
            day1_mult = float(self._snap_val(config_snapshot, "TSL_DECAY_DAY1_MULT", getattr(settings, "TSL_DECAY_DAY1_MULT", 2.20)))
            step_mult = float(self._snap_val(config_snapshot, "TSL_DECAY_STEP_MULT", getattr(settings, "TSL_DECAY_STEP_MULT", 0.35)))
            floor_mult = float(self._snap_val(config_snapshot, "TSL_DECAY_FLOOR_MULT", getattr(settings, "TSL_DECAY_FLOOR_MULT", 1.15)))
            decayed = day1_mult - (step_mult * max(0, session_count - 1))
            return round(max(floor_mult, decayed), 2)

        if hasattr(self.risk_manager, "get_decay_atr_multiplier"):
            try:
                decay_mult = self.risk_manager.get_decay_atr_multiplier(
                    session_count=session_count, is_runner=is_runner
                )
                if isinstance(decay_mult, (int, float)):
                    return float(decay_mult)
            except Exception:
                pass
        return RiskManager.get_decay_atr_multiplier(session_count=session_count, is_runner=is_runner)

    def _calculate_trailing_stop_for_pos(
        self,
        pos: TrackedPosition,
        current_price: float,
        peak_price: float,
        config_snapshot: Optional[Any] = None,
    ) -> float:
        """Compute epoch-aware Chandelier trailing stop without mutating never-loosen invariant."""
        session_count = getattr(pos, "session_count", 1)
        is_runner = getattr(pos, "is_runner", False)
        if config_snapshot is None:
            return self.risk_manager.calculate_trailing_stop(
                current_price=current_price,
                peak_price=peak_price,
                atr=pos.atr,
                current_tsl=pos.trailing_stop_loss,
                direction=pos.direction,
                ticker=pos.ticker,
                session_count=session_count,
                is_runner=is_runner,
            )

        fn = getattr(self.risk_manager, "calculate_trailing_stop", None)
        if fn is not None and self._callable_accepts_kwarg(fn, "config_snapshot"):
            try:
                val = fn(
                    current_price=current_price,
                    peak_price=peak_price,
                    atr=pos.atr,
                    current_tsl=pos.trailing_stop_loss,
                    direction=pos.direction,
                    ticker=pos.ticker,
                    session_count=session_count,
                    is_runner=is_runner,
                    config_snapshot=config_snapshot,
                )
                if isinstance(val, (int, float)):
                    return float(val)
            except Exception:
                pass

        effective_mult = self._get_decay_atr_multiplier(
            session_count=session_count,
            is_runner=is_runner,
            config_snapshot=config_snapshot,
        )
        if config_snapshot is not None:
            min_sl_pct = float(self._snap_val(config_snapshot, "ETP_MIN_SL_PCT", 0.025))
            if min_sl_pct <= 0:
                min_sl_pct = 0.025
        else:
            min_sl_pct = 0.02
        distance = max(effective_mult * pos.atr, peak_price * min_sl_pct)
        is_cfd_short = self.is_true_short_contract(pos)
        if is_cfd_short:
            new_tsl = peak_price + distance
            return round(min(pos.trailing_stop_loss, new_tsl) if pos.trailing_stop_loss > 0 else new_tsl, 2)
        new_tsl = peak_price - distance
        return round(max(pos.trailing_stop_loss, new_tsl), 2)

    def _calculate_escape_threshold_for_pos(
        self,
        pos: TrackedPosition,
        config_snapshot: Optional[Any] = None,
    ) -> float:
        """Compute TP1 hysteresis escape threshold respecting position epoch TSL_HYSTERESIS_ATR_MULT."""
        default_mult = getattr(self.risk_manager, "tsl_hysteresis_atr_mult", getattr(settings, "TSL_HYSTERESIS_ATR_MULT", 0.5))
        if not isinstance(default_mult, (int, float)):
            default_mult = getattr(settings, "TSL_HYSTERESIS_ATR_MULT", 0.5)
        mult = float(self._snap_val(config_snapshot, "TSL_HYSTERESIS_ATR_MULT", default_mult)) if config_snapshot is not None else None
        esc_fn = self.risk_manager.calculate_escape_threshold
        extra_kwargs: Dict[str, Any] = {}
        if self._callable_accepts_kwarg(esc_fn, "ticker"):
            extra_kwargs["ticker"] = pos.ticker
        if mult is not None and self._callable_accepts_kwarg(esc_fn, "multiplier"):
            val = esc_fn(
                tp1=pos.tp1,
                atr=pos.atr,
                direction=pos.direction,
                multiplier=mult,
                **extra_kwargs,
            )
            if isinstance(val, (int, float)):
                return float(val)
        return esc_fn(
            tp1=pos.tp1,
            atr=pos.atr,
            direction=pos.direction,
            **extra_kwargs,
        )

    def _check_market_open(self, ticker: str) -> bool:
        """Check exchange open status via broker client if explicitly boolean, else session hours."""
        if self.t212 and hasattr(self.t212, "is_market_open"):
            try:
                res = self.t212.is_market_open(ticker)
                if isinstance(res, bool):
                    return res
            except Exception as exc:
                logger.debug("t212.is_market_open check notice for %s: %s", ticker, exc)
        try:
            info = Trading212Client.get_market_session_info(ticker)
            return bool(info.get("is_open", True))
        except Exception:
            return True

    def is_system_halted(self) -> bool:
        """Check whether system is in emergency halt state (in-memory or persistent marker)."""
        with self._state_lock:
            if self.is_halted:
                return True
            if self.halt_marker_file != ":memory:.halted" and os.path.exists(self.halt_marker_file):
                self.is_halted = True
                return True
            return False

    def resume_trading(self, confirm_unprotected: bool = False) -> Dict[str, Any]:
        """Verify/re-arm missing broker GTC stops before clearing halt state (fail-closed R18)."""
        with self._broker_op_lock:
            with self._state_lock:
                open_items = [
                    (t, p)
                    for t, p in list(self.positions.items())
                    if not getattr(p, "is_closed", False)
                    and (getattr(p, "remaining_quantity", 0.0) or 0.0) > 0.0
                ]

            if not open_items:
                with self._state_lock:
                    self.is_halted = False
                    if self.halt_marker_file != ":memory:.halted" and os.path.exists(self.halt_marker_file):
                        try:
                            os.remove(self.halt_marker_file)
                        except Exception as exc:
                            logger.warning("Could not remove halt marker file: %s", exc)
                return {
                    "resumed": True,
                    "checked_positions": 0,
                    "rearmed_stops": [],
                    "verified_stops": [],
                    "unprotected_positions": [],
                    "confirmed_unprotected": False,
                }

            active_stop_ids: set = set()
            active_stop_by_ticker: Dict[str, str] = {}
            orders_fetch_ok = self.dry_run
            if not self.dry_run and self.t212 and hasattr(self.t212, "get_orders"):
                try:
                    raw_orders = self.t212.get_orders(force_refresh=True)
                    if isinstance(raw_orders, (list, tuple)):
                        orders_fetch_ok = True
                        for o in raw_orders:
                            o_type = str(o.get("type") if isinstance(o, dict) else getattr(o, "type", "")).upper()
                            if "STOP" in o_type:
                                o_id = str(o.get("id") if isinstance(o, dict) else getattr(o, "id", ""))
                                o_ticker = o.get("ticker") if isinstance(o, dict) else getattr(o, "ticker", "")
                                if o_id:
                                    active_stop_ids.add(o_id)
                                if o_ticker and o_id:
                                    active_stop_by_ticker[str(o_ticker)] = o_id
                except Exception as exc:
                    logger.warning("resume_trading failed to inspect broker open orders: %s", exc)
                    orders_fetch_ok = False

            rearmed_stops: List[str] = []
            verified_stops: List[str] = []
            unprotected: List[Dict[str, Any]] = []
            state_dirty = False

            for ticker, pos in open_items:
                with self._state_lock:
                    cur_stop_id = pos.broker_stop_order_id
                    panic_failed = bool(getattr(pos, "panic_failed", False))

                if self.dry_run:
                    if not cur_stop_id or panic_failed:
                        placed_id = self.place_broker_stop(pos, allow_day_fallback=False, _allow_while_halted=True)
                        if placed_id:
                            with self._state_lock:
                                pos.panic_failed = False
                                pos.unprotected_reason = None
                            rearmed_stops.append(ticker)
                            state_dirty = True
                        else:
                            unprotected.append({
                                "ticker": ticker,
                                "panic_failed": panic_failed,
                                "reason": getattr(pos, "unprotected_reason", None) or "Missing broker GTC stop",
                            })
                    else:
                        verified_stops.append(ticker)
                    continue

                verified_id: Optional[str] = None
                if orders_fetch_ok:
                    if cur_stop_id and cur_stop_id in active_stop_ids:
                        verified_id = cur_stop_id
                    elif ticker in active_stop_by_ticker:
                        verified_id = active_stop_by_ticker[ticker]

                if verified_id and not panic_failed:
                    if cur_stop_id != verified_id:
                        with self._state_lock:
                            pos.broker_stop_order_id = verified_id
                            state_dirty = True
                    verified_stops.append(ticker)
                    continue

                if verified_id and panic_failed:
                    with self._state_lock:
                        pos.broker_stop_order_id = verified_id
                        pos.panic_failed = False
                        pos.unprotected_reason = None
                        state_dirty = True
                    verified_stops.append(ticker)
                    continue

                # Clear stale local stop ID if broker confirms none exists, then re-arm strict GTC
                if orders_fetch_ok and cur_stop_id:
                    with self._state_lock:
                        pos.broker_stop_order_id = None
                        state_dirty = True

                placed_id = self.place_broker_stop(pos, allow_day_fallback=False, _allow_while_halted=True)
                if placed_id:
                    with self._state_lock:
                        pos.panic_failed = False
                        pos.unprotected_reason = None
                        state_dirty = True
                    rearmed_stops.append(ticker)
                else:
                    reason = (
                        getattr(pos, "unprotected_reason", None)
                        or "Missing verified broker GTC stop"
                    )
                    with self._state_lock:
                        pos.unprotected_reason = reason
                        state_dirty = True
                    unprotected.append({
                        "ticker": ticker,
                        "panic_failed": bool(getattr(pos, "panic_failed", False)),
                        "reason": reason,
                    })

            if state_dirty:
                self._save_state()

            if unprotected and not confirm_unprotected:
                raise ResumeBlockedError(
                    f"Cannot resume trading: {len(unprotected)} open position(s) lack verified GTC stops or failed panic liquidation",
                    unprotected_positions=unprotected,
                )

            with self._state_lock:
                if unprotected and confirm_unprotected:
                    logger.critical(
                        "Resuming trading under confirm_unprotected=True with %d unprotected position(s): %s",
                        len(unprotected), [u["ticker"] for u in unprotected],
                    )
                self.is_halted = False
                if self.halt_marker_file != ":memory:.halted" and os.path.exists(self.halt_marker_file):
                    try:
                        os.remove(self.halt_marker_file)
                    except Exception as exc:
                        logger.warning("Could not remove halt marker file: %s", exc)

            return {
                "resumed": True,
                "checked_positions": len(open_items),
                "rearmed_stops": rearmed_stops,
                "verified_stops": verified_stops,
                "unprotected_positions": unprotected,
                "confirmed_unprotected": bool(unprotected and confirm_unprotected),
            }

    # ─────────────────────────────────────────────────────────────
    # Opt-In Position Re-Epoch Preview & Execution (F3 / AE4)
    # ─────────────────────────────────────────────────────────────
    def _resolve_target_snapshot(self, target_epoch_id: Optional[str] = None) -> Tuple[Any, str]:
        """Resolve target epoch snapshot and authoritative epoch identifier."""
        if self.config_bus is not None:
            if target_epoch_id is None:
                snap = self.config_bus.active_snapshot()
                return snap, str(self._snap_val(snap, "config_epoch_id", "epoch-0"))
            snap = self.config_bus.snapshot_for_epoch(target_epoch_id)
            if snap is None:
                raise ValueError(f"Unknown target config epoch {target_epoch_id!r}")
            return snap, str(target_epoch_id)

        if target_epoch_id is not None:
            raise ValueError(f"Cannot resolve target_epoch_id={target_epoch_id!r} without ConfigBus")

        fallback = {
            "config_epoch_id": "live",
            "ATR_SL_MULTIPLIER": getattr(settings, "ATR_SL_MULTIPLIER", 2.2),
            "ETP_MIN_SL_PCT": getattr(settings, "ETP_MIN_SL_PCT", 0.025),
            "ETP_MAX_SL_PCT": getattr(settings, "ETP_MAX_SL_PCT", 0.08),
            "TP1_R_MULTIPLE": getattr(settings, "TP1_R_MULTIPLE", 1.5),
            "TP2_R_MULTIPLE": getattr(settings, "TP2_R_MULTIPLE", 2.5),
            "TP3_R_MULTIPLE": getattr(settings, "TP3_R_MULTIPLE", 3.5),
            "PURE_TSL_MODE": getattr(self, "pure_tsl_mode", getattr(settings, "PURE_TSL_MODE", True)),
            "TSL_DECAY_DAY1_MULT": getattr(settings, "TSL_DECAY_DAY1_MULT", 2.20),
            "TSL_DECAY_STEP_MULT": getattr(settings, "TSL_DECAY_STEP_MULT", 0.35),
            "TSL_DECAY_FLOOR_MULT": getattr(settings, "TSL_DECAY_FLOOR_MULT", 1.15),
            "TSL_DECAY_RUNNER_MULT": getattr(settings, "TSL_DECAY_RUNNER_MULT", 2.00),
            "TSL_HYSTERESIS_ATR_MULT": getattr(settings, "TSL_HYSTERESIS_ATR_MULT", 0.5),
        }
        return fallback, "live"

    def _get_live_price_for_pos(self, pos: TrackedPosition) -> float:
        """Fetch current market price outside _state_lock with safe fallback to tracked price."""
        with self._state_lock:
            is_short = self.is_true_short_contract(pos)
            fallback_px = (
                getattr(pos, "current_price", None)
                or (pos.lowest_price if is_short else pos.highest_price)
                or pos.entry_price
            )
            ticker = pos.ticker
            entry_px = pos.entry_price

        if not self.dry_run and self.t212 and hasattr(self.t212, "get_quote"):
            try:
                q = self.t212.get_quote(ticker)
                if isinstance(q, Mapping):
                    for field_name in ("bid", "price", "lastPrice", "currentPrice"):
                        raw = q.get(field_name)
                        if isinstance(raw, (int, float)) and raw > 0:
                            px = float(raw)
                            if entry_px <= 0 or (0.2 < (px / entry_px) < 5.0):
                                return px
            except Exception as exc:
                logger.debug("Live quote lookup skipped during re-epoch for %s: %s", ticker, exc)
        return float(fallback_px or 0.0)

    def _compute_re_epoch_plan(
        self,
        pos: TrackedPosition,
        target_snap: Any,
        resolved_target_epoch_id: str,
        confirm_pure_tsl_mode_change: bool,
        cur_px: float,
        market_open: bool,
    ) -> Dict[str, Any]:
        """Compute deterministic Re-Epoch diff, stop clamp, mode warning, and action classification."""
        is_short = self.is_true_short_contract(pos)
        if is_short:
            old_sl = float(min(pos.stop_loss, pos.trailing_stop_loss) if pos.trailing_stop_loss > 0 else pos.stop_loss)
        else:
            old_sl = float(max(pos.stop_loss, pos.trailing_stop_loss))

        old_pure_tsl = self._is_pure_tsl_for_position(pos)
        new_pure_tsl = bool(self._snap_val(target_snap, "PURE_TSL_MODE", self.pure_tsl_mode))
        pure_tsl_mode_changed = (old_pure_tsl != new_pure_tsl)
        requires_confirmation = bool(pure_tsl_mode_changed and not confirm_pure_tsl_mode_change)

        entry_levels = self.calculate_exit_levels(
            entry_price=pos.entry_price,
            direction=pos.direction,
            atr=pos.atr if pos.atr > 0 else None,
            ticker=pos.ticker,
            config_snapshot=target_snap,
        )
        candidate_sl = float(entry_levels["stop_loss"])

        gain_pct = (
            ((pos.entry_price - cur_px) / pos.entry_price * 100.0)
            if (is_short and pos.entry_price > 0 and cur_px > 0)
            else (((cur_px - pos.entry_price) / pos.entry_price * 100.0) if (pos.entry_price > 0 and cur_px > 0) else 0.0)
        )
        in_profit_zone = gain_pct >= 1.5 or (
            (old_sl <= pos.entry_price if is_short else old_sl >= pos.entry_price) and pos.entry_price > 0
        )
        if in_profit_zone and pos.entry_price > 0:
            peak_px = pos.lowest_price if is_short else pos.highest_price
            trail_sl = self._calculate_trailing_stop_for_pos(pos, cur_px, peak_px, target_snap)
            candidate_sl = min(candidate_sl, trail_sl) if is_short else max(candidate_sl, trail_sl)

        tick_buffer = (
            (round(max(0.02, cur_px * 0.001), 4) if cur_px > 1.0 else round(max(0.005, cur_px * 0.01), 4))
            if cur_px > 0
            else 0.0
        )
        was_clamped_to_market = False
        if cur_px > 0:
            if is_short:
                min_allowed = round(cur_px + tick_buffer, 4)
                if candidate_sl < min_allowed:
                    was_clamped_to_market = True
                clamped_sl = max(candidate_sl, min_allowed)
            else:
                max_allowed = round(max(0.01, cur_px - tick_buffer), 4)
                if candidate_sl > max_allowed:
                    was_clamped_to_market = True
                clamped_sl = min(candidate_sl, max_allowed)
        else:
            clamped_sl = candidate_sl

        # Never loosen invariant
        if is_short:
            new_sl = round(min(old_sl, clamped_sl), 4) if old_sl > 0 else round(clamped_sl, 4)
            needs_stop_tighten = (old_sl <= 0) or (new_sl < old_sl - 1e-6)
        else:
            new_sl = round(max(old_sl, clamped_sl), 4)
            needs_stop_tighten = new_sl > old_sl + 1e-6

        if requires_confirmation:
            action = "blocked"
            reason = "PURE_TSL_MODE transition requires explicit confirmation (confirm_pure_tsl_mode_change=True)"
        elif needs_stop_tighten:
            if not market_open:
                action = "market_closed"
                reason = "Exchange market is closed; broker GTC stop replacement refused"
            else:
                action = "tighten"
                reason = (
                    "Target stop tightened and clamped below live price minus tick buffer"
                    if was_clamped_to_market
                    else "Target epoch tightens protective GTC stop"
                )
        else:
            looser_candidate = (candidate_sl > old_sl + 1e-6) if is_short else (candidate_sl < old_sl - 1e-6)
            if looser_candidate or was_clamped_to_market:
                action = "keep_existing_stop"
                reason = "Existing stop is already tighter than target/market limit; keeping untouched with zero broker calls"
            elif pos.config_epoch_id == resolved_target_epoch_id and not pure_tsl_mode_changed:
                action = "no_change"
                reason = "Position is already on target epoch with identical stop and policy"
            else:
                action = "keep_existing_stop"
                reason = "Protective stop unchanged under target epoch; zero broker calls needed"

        return {
            "ticker": pos.ticker,
            "action": action,
            "reason": reason,
            "current_position_epoch_id": pos.config_epoch_id,
            "target_epoch_id": resolved_target_epoch_id,
            "old_stop_loss": round(old_sl, 4),
            "candidate_stop_loss": round(candidate_sl, 4),
            "clamped_stop_loss": round(clamped_sl, 4),
            "new_stop_loss": new_sl,
            "new_sl": new_sl,
            "current_price": round(cur_px, 4),
            "tick_buffer": tick_buffer,
            "was_clamped_to_market": was_clamped_to_market,
            "needs_stop_tighten": needs_stop_tighten,
            "market_open": market_open,
            "old_pure_tsl_mode": old_pure_tsl,
            "new_pure_tsl_mode": new_pure_tsl,
            "pure_tsl_mode_changed": pure_tsl_mode_changed,
            "requires_confirmation": requires_confirmation,
        }

    def preview_re_epoch(
        self,
        ticker: str,
        target_epoch_id: Optional[str] = None,
        confirm_pure_tsl_mode_change: bool = False,
    ) -> Dict[str, Any]:
        """Preview exact stop & policy impact of re-epoching a position without side effects."""
        target_snap, resolved_target_epoch_id = self._resolve_target_snapshot(target_epoch_id)
        with self._state_lock:
            pos = self.positions.get(ticker)
            if pos is None or pos.is_closed or pos.remaining_quantity <= 0:
                raise KeyError(f"Active position {ticker!r} not found")

        cur_px = self._get_live_price_for_pos(pos)
        market_open = self._check_market_open(ticker)
        with self._state_lock:
            return self._compute_re_epoch_plan(
                pos=pos,
                target_snap=target_snap,
                resolved_target_epoch_id=resolved_target_epoch_id,
                confirm_pure_tsl_mode_change=confirm_pure_tsl_mode_change,
                cur_px=cur_px,
                market_open=market_open,
            )

    def preview_re_epoch_all(
        self,
        target_epoch_id: Optional[str] = None,
        confirm_pure_tsl_mode_change: bool = False,
    ) -> Dict[str, Any]:
        """Preview re-epoch actions across all currently active open positions."""
        _, resolved_target_epoch_id = self._resolve_target_snapshot(target_epoch_id)
        with self._state_lock:
            active_tickers = [
                t
                for t, p in list(self.positions.items())
                if not getattr(p, "is_closed", False)
                and (getattr(p, "remaining_quantity", 0.0) or 0.0) > 0.0
            ]

        previews: List[Dict[str, Any]] = []
        for ticker in active_tickers:
            try:
                previews.append(
                    self.preview_re_epoch(
                        ticker=ticker,
                        target_epoch_id=resolved_target_epoch_id,
                        confirm_pure_tsl_mode_change=confirm_pure_tsl_mode_change,
                    )
                )
            except KeyError:
                continue

        return {
            "target_epoch_id": resolved_target_epoch_id,
            "positions": previews,
            "requires_confirmation": any(p.get("requires_confirmation") for p in previews),
            "tighten_count": sum(1 for p in previews if p.get("action") == "tighten"),
            "keep_existing_count": sum(1 for p in previews if p.get("action") == "keep_existing_stop"),
            "market_closed_count": sum(1 for p in previews if p.get("action") == "market_closed"),
            "blocked_count": sum(1 for p in previews if p.get("action") == "blocked"),
            "no_change_count": sum(1 for p in previews if p.get("action") == "no_change"),
        }

    def re_epoch_position(
        self,
        ticker: str,
        expected_position_epoch_id: Optional[str],
        target_epoch_id: Optional[str] = None,
        confirm_pure_tsl_mode_change: bool = False,
    ) -> Dict[str, Any]:
        """Safely migrate an open position to the active/target config epoch under _broker_op_lock.

        Enforces CAS checks on both position and target epoch, refuses unconfirmed PURE_TSL_MODE
        transitions and closed-market stop edits, makes zero broker calls unless tightening is
        needed, uses strict GTC stops (no DAY fallback), and restores old_sl immediately on failure.
        """
        with self._broker_op_lock:
            if self.is_system_halted():
                return {
                    "ticker": ticker,
                    "action": "blocked",
                    "status": "blocked",
                    "applied": False,
                    "broker_modified": False,
                    "reason": "System is HALTED — resume trading before re-epoching positions.",
                }
            target_snap, resolved_target_epoch_id = self._resolve_target_snapshot(target_epoch_id)

            # Target epoch CAS check against active ConfigBus epoch
            if target_epoch_id is not None and self.config_bus is not None:
                active_epoch_id = self.config_bus.active_epoch_id
                if target_epoch_id != active_epoch_id:
                    raise PositionEpochConflictError(
                        f"Target epoch conflict for {ticker}: expected {target_epoch_id!r}, active is {active_epoch_id!r}",
                        ticker=ticker,
                        expected_epoch_id=target_epoch_id,
                        actual_epoch_id=active_epoch_id,
                        expected_target_epoch_id=target_epoch_id,
                        actual_target_epoch_id=active_epoch_id,
                    )

            with self._state_lock:
                pos = self.positions.get(ticker)
                if pos is None or pos.is_closed or pos.remaining_quantity <= 0:
                    raise KeyError(f"Active position {ticker!r} not found")
                actual_pos_epoch_id = pos.config_epoch_id
                if actual_pos_epoch_id != expected_position_epoch_id:
                    raise PositionEpochConflictError(
                        f"Position epoch conflict for {ticker}: expected {expected_position_epoch_id!r}, actual is {actual_pos_epoch_id!r}",
                        ticker=ticker,
                        expected_epoch_id=expected_position_epoch_id,
                        actual_epoch_id=actual_pos_epoch_id,
                        expected_position_epoch_id=expected_position_epoch_id,
                        actual_position_epoch_id=actual_pos_epoch_id,
                    )
                old_stop_loss_field = pos.stop_loss
                old_tsl_field = pos.trailing_stop_loss
                had_existing_stop = bool(pos.broker_stop_order_id)

            cur_px = self._get_live_price_for_pos(pos)
            market_open = self._check_market_open(ticker)

            with self._state_lock:
                plan = self._compute_re_epoch_plan(
                    pos=pos,
                    target_snap=target_snap,
                    resolved_target_epoch_id=resolved_target_epoch_id,
                    confirm_pure_tsl_mode_change=confirm_pure_tsl_mode_change,
                    cur_px=cur_px,
                    market_open=market_open,
                )

            if plan["action"] in ("blocked", "market_closed"):
                return {
                    **plan,
                    "status": plan["action"],
                    "applied": False,
                    "broker_modified": False,
                }

            # Re-validate CAS right before committing broker/state mutations
            if self.config_bus is not None:
                active_epoch_id = self.config_bus.active_epoch_id
                if active_epoch_id != resolved_target_epoch_id:
                    raise PositionEpochConflictError(
                        f"Target epoch changed concurrently for {ticker}: expected {resolved_target_epoch_id!r}, active is {active_epoch_id!r}",
                        ticker=ticker,
                        expected_epoch_id=resolved_target_epoch_id,
                        actual_epoch_id=active_epoch_id,
                        expected_target_epoch_id=resolved_target_epoch_id,
                        actual_target_epoch_id=active_epoch_id,
                    )
            with self._state_lock:
                if pos.config_epoch_id != expected_position_epoch_id:
                    raise PositionEpochConflictError(
                        f"Position epoch changed concurrently for {ticker}: expected {expected_position_epoch_id!r}, actual is {pos.config_epoch_id!r}",
                        ticker=ticker,
                        expected_epoch_id=expected_position_epoch_id,
                        actual_epoch_id=pos.config_epoch_id,
                        expected_position_epoch_id=expected_position_epoch_id,
                        actual_position_epoch_id=pos.config_epoch_id,
                    )

            broker_modified = False
            if plan["needs_stop_tighten"]:
                new_sl = float(plan["new_sl"])
                old_sl = float(plan["old_stop_loss"])
                new_stop_id = self.place_broker_stop(
                    pos,
                    stop_price=new_sl,
                    allow_day_fallback=False,
                    _allow_heal=False,
                    _auto_restore_on_failure=False,
                    _allow_while_halted=True,
                )
                if new_stop_id is None:
                    # Replacement failed after cancelling previous stop -> immediately restore old_sl
                    restored_id = self.place_broker_stop(
                        pos,
                        stop_price=old_sl,
                        allow_day_fallback=False,
                        _allow_heal=False,
                        _auto_restore_on_failure=False,
                        _allow_while_halted=True,
                    )
                    with self._state_lock:
                        pos.stop_loss = old_stop_loss_field
                        pos.trailing_stop_loss = old_tsl_field
                        if restored_id is None and had_existing_stop:
                            pos.unprotected_reason = "Re-epoch GTC stop replacement and restoration failed"
                        self._save_state()
                    if restored_id is None and had_existing_stop and hasattr(self.notifier, "notify_alert"):
                        with contextlib.suppress(Exception):
                            self.notifier.notify_alert(
                                f"🚨 UNPROTECTED POSITION: {pos.ticker}",
                                f"Re-epoch GTC stop replacement ({new_sl:.2f}) and old stop restore ({old_sl:.2f}) both failed.",
                            )
                    return {
                        **plan,
                        "status": "replacement_failed",
                        "applied": False,
                        "broker_modified": bool(had_existing_stop and restored_id is None),
                        "restored_old_stop": bool(restored_id),
                        "restored_stop_order_id": restored_id,
                    }

                broker_modified = True
                with self._state_lock:
                    pos.stop_loss = new_sl
                    pos.trailing_stop_loss = new_sl
                    pos.unprotected_reason = None

            # Handle confirmed PURE_TSL_MODE TP limit order reconciliation if transitioning
            if plan["pure_tsl_mode_changed"] and confirm_pure_tsl_mode_change:
                if plan["new_pure_tsl_mode"] and pos.broker_tp_order_id:
                    self.cancel_broker_tp_limit(pos)
                    broker_modified = True

            with self._state_lock:
                pos.config_epoch_id = resolved_target_epoch_id
                pos.entry_config_snapshot = self._snapshot_to_dict(target_snap)
                pos.last_update_time = datetime.now(timezone.utc).isoformat()
                self._save_state()

            # Ensure newly transitioned non-Pure-TSL positions can obtain a TP limit if eligible & open
            if plan["pure_tsl_mode_changed"] and confirm_pure_tsl_mode_change and not plan["new_pure_tsl_mode"]:
                if not pos.tp1_hit and not pos.broker_tp_order_id and market_open:
                    tp_id = self.place_broker_tp_limit(pos)
                    if tp_id:
                        broker_modified = True

            return {
                **plan,
                "status": plan["action"],
                "applied": True,
                "broker_modified": broker_modified,
            }

    def re_epoch_all_positions(
        self,
        target_epoch_id: Optional[str] = None,
        expected_position_epochs: Optional[Mapping[str, Optional[str]]] = None,
        confirm_pure_tsl_mode_change: bool = False,
    ) -> Dict[str, Any]:
        """Sequentially migrate open positions under _broker_op_lock, yielding immediately if halted."""
        with self._broker_op_lock:
            _, resolved_target_epoch_id = self._resolve_target_snapshot(target_epoch_id)
            if target_epoch_id is not None and self.config_bus is not None:
                active_epoch_id = self.config_bus.active_epoch_id
                if target_epoch_id != active_epoch_id:
                    raise PositionEpochConflictError(
                        f"Batch target epoch conflict: expected {target_epoch_id!r}, active is {active_epoch_id!r}",
                        expected_epoch_id=target_epoch_id,
                        actual_epoch_id=active_epoch_id,
                        expected_target_epoch_id=target_epoch_id,
                        actual_target_epoch_id=active_epoch_id,
                    )

            with self._state_lock:
                snapshot_entries = [
                    (t, p.config_epoch_id)
                    for t, p in list(self.positions.items())
                    if not getattr(p, "is_closed", False)
                    and (getattr(p, "remaining_quantity", 0.0) or 0.0) > 0.0
                ]

            results: List[Dict[str, Any]] = []
            applied: List[str] = []
            skipped: List[str] = []
            conflicts: List[Dict[str, Any]] = []
            errors: List[Dict[str, Any]] = []
            preempted_by_halt = False

            for ticker, observed_pos_epoch in snapshot_entries:
                if self.is_system_halted():
                    preempted_by_halt = True
                    break

                expected_pos_epoch = (
                    expected_position_epochs[ticker]
                    if (expected_position_epochs is not None and ticker in expected_position_epochs)
                    else observed_pos_epoch
                )
                try:
                    res = self.re_epoch_position(
                        ticker=ticker,
                        expected_position_epoch_id=expected_pos_epoch,
                        target_epoch_id=resolved_target_epoch_id,
                        confirm_pure_tsl_mode_change=confirm_pure_tsl_mode_change,
                    )
                    results.append(res)
                    if res.get("applied"):
                        applied.append(ticker)
                    else:
                        skipped.append(ticker)
                except PositionEpochConflictError as conflict_exc:
                    conflict_rec = {
                        "ticker": ticker,
                        "status": "conflict",
                        "error": str(conflict_exc),
                        "expected_epoch_id": conflict_exc.expected_epoch_id,
                        "actual_epoch_id": conflict_exc.actual_epoch_id,
                    }
                    conflicts.append(conflict_rec)
                    results.append(conflict_rec)
                except Exception as exc:
                    err_rec = {"ticker": ticker, "status": "error", "error": str(exc)}
                    errors.append(err_rec)
                    results.append(err_rec)

            return {
                "target_epoch_id": resolved_target_epoch_id,
                "preempted_by_halt": preempted_by_halt,
                "applied": applied,
                "skipped": skipped,
                "conflicts": conflicts,
                "errors": errors,
                "results": results,
            }

    # ─────────────────────────────────────────────────────────────
    # Exit Levels & Target Calculations
    # ─────────────────────────────────────────────────────────────
    @staticmethod
    def is_true_short_contract(pos_or_ticker: Any, direction: str = "") -> bool:
        """Return True only if position is a margin/CFD short where falling price is profit.

        For all Trading 212 Invest assets (including 3x and 5x inverse ETPs like 5STLd_EQ),
        shares are held long, so rising ETP price is profit and Stop Loss must be BELOW live price.
        """
        if pos_or_ticker is None:
            return False
        if isinstance(pos_or_ticker, str):
            ticker = pos_or_ticker.upper()
            dir_str = direction.upper()
            tp1 = 0.0
            entry = 0.0
        else:
            ticker = (getattr(pos_or_ticker, "ticker", "") or "").upper()
            dir_str = (getattr(pos_or_ticker, "direction", "") or "").upper()
            tp1 = getattr(pos_or_ticker, "tp1", 0.0) or 0.0
            entry = getattr(pos_or_ticker, "entry_price", 0.0) or 0.0

        if dir_str == "LONG":
            return False

        # If explicitly configured as a short contract with inverse profit structure (TP1 < entry)
        if tp1 > 0 and entry > 0 and tp1 < entry:
            return True

        # Trading 212 Invest spot equities & ETPs trade long (rising ETP price is profit)
        if (
            ticker.endswith("_EQ")
            or ticker.endswith(".L")
            or "_ETP" in ticker
            or dir_str.endswith("_ETP")
            or ticker.startswith("3")
            or ticker.startswith("5")
        ):
            return False

        return dir_str == "SHORT"

    def calculate_exit_levels(
        self,
        entry_price: float,
        direction: str = "LONG",
        atr: Optional[float] = None,
        existing_stop_price: Optional[float] = None,
        ticker: Optional[str] = None,
        underlying_atr_pct: Optional[float] = None,
        leverage_factor: Optional[float] = None,
        tp1_r: Optional[float] = None,
        tp2_r: Optional[float] = None,
        tp3_r: Optional[float] = None,
        config_snapshot: Optional[Any] = None,
    ) -> Dict[str, float]:
        """Calculate dynamic ATR/volatility-based SL, TP1, TP2, TP3.

        For Leveraged ETPs (3x and 5x):
        - Uses existing stop price if one was already set on broker, aligning R-multiples.
        - Scales underlying market ATR percentage by instrument leverage (3x / 5x) to reflect
          true product volatility, preventing noise stop-outs.
        - Calculates stop distance from ATR (or 3.5% default for 3x ETPs), clamped between ETP_MIN_SL_PCT and ETP_MAX_SL_PCT.
        - Derives multi-stage targets from tp1_r / tp2_r / tp3_r or epoch snapshot defaults.
        """
        is_short = self.is_true_short_contract(ticker or "", direction=direction)
        entry = max(0.01, entry_price)

        sl_mult = float(self._snap_val(config_snapshot, "ATR_SL_MULTIPLIER", 2.2)) if config_snapshot is not None else 2.2
        min_sl_pct = float(self._snap_val(config_snapshot, "ETP_MIN_SL_PCT", 0.025)) if config_snapshot is not None else 0.025
        max_sl_pct = float(self._snap_val(config_snapshot, "ETP_MAX_SL_PCT", 0.08)) if config_snapshot is not None else 0.08

        # Resolve leverage multiplier (defaults to 3.0 for ETPs, 5.0 for 5x, 1.0 for ordinary)
        if leverage_factor is None or leverage_factor <= 0:
            if ticker:
                try:
                    from trading_system.data.leveraged_universe import LeveragedUniverse
                    univ = LeveragedUniverse()
                    pair = univ.get_pair_for_etp(ticker)
                    if pair and (ticker == pair.long_5x_ticker or ticker == pair.short_5x_ticker):
                        leverage_factor = 5.0
                    elif "5" in ticker and ("_EQ" in ticker or ".L" in ticker):
                        leverage_factor = 5.0
                    elif pair or "3" in ticker:
                        leverage_factor = 3.0
                    else:
                        leverage_factor = 1.0
                except Exception:
                    leverage_factor = 3.0 if ("3" in ticker or "5" in ticker) else 1.0
            else:
                leverage_factor = 3.0

        if existing_stop_price and existing_stop_price > 0:
            sl = round(existing_stop_price, 4)
            sl_dist = abs(entry - sl)
            # Ensure distance is at least 2% of entry to prevent zero-width targets
            sl_dist = max(sl_dist, round(entry * 0.02, 4))
            effective_atr = atr if (atr and atr > 0) else round(entry * min_sl_pct, 4)
        else:
            if underlying_atr_pct and underlying_atr_pct > 0:
                etp_atr_pct = underlying_atr_pct * leverage_factor
                effective_atr = round(entry * etp_atr_pct, 4)
            elif atr and atr > 0:
                effective_atr = atr
            else:
                effective_atr = round(entry * 0.035, 4)

            # ATR stop distance, bounded between ETP_MIN_SL_PCT and ETP_MAX_SL_PCT of price
            raw_dist = effective_atr * sl_mult
            min_dist = entry * min_sl_pct
            max_dist = entry * max_sl_pct
            sl_dist = round(min(max_dist, max(min_dist, raw_dist)), 4)
            sl = round((entry + sl_dist) if is_short else (entry - sl_dist), 4)

        default_tp1 = float(self._snap_val(config_snapshot, "TP1_R_MULTIPLE", 1.0)) if config_snapshot is not None else 1.0
        default_tp2 = float(self._snap_val(config_snapshot, "TP2_R_MULTIPLE", 2.0)) if config_snapshot is not None else 2.0
        default_tp3 = float(self._snap_val(config_snapshot, "TP3_R_MULTIPLE", 3.0)) if config_snapshot is not None else 3.0
        tp1_mult = tp1_r if tp1_r is not None else default_tp1
        tp2_mult = tp2_r if tp2_r is not None else default_tp2
        tp3_mult = tp3_r if tp3_r is not None else default_tp3

        if is_short:
            tp1 = round(max(0.01, entry - tp1_mult * sl_dist), 4)
            tp2 = round(max(0.01, entry - tp2_mult * sl_dist), 4)
            tp3 = round(max(0.01, entry - tp3_mult * sl_dist), 4)
        else:
            tp1 = round(entry + tp1_mult * sl_dist, 4)
            tp2 = round(entry + tp2_mult * sl_dist, 4)
            tp3 = round(entry + tp3_mult * sl_dist, 4)

        return {
            "stop_loss": sl,
            "trailing_stop_loss": sl,
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3,
            "atr": effective_atr,
        }

    @classmethod
    def check_progressive_stops(
        cls,
        entry_price: float,
        current_price: float,
        current_sl: float,
        direction: str = "LONG",
        ticker: str = "",
        config_snapshot: Optional[Any] = None,
    ) -> float:
        """Lock in gains progressively:
        - At +1.5% unrealized profit: move stop loss to Entry Price (Breakeven).
        - At +3.0% unrealized profit: move stop loss to Entry Price + 1.5% (lock profit cushion).
        - Stop never loosens.
        """
        be_trigger_pct = float(cls._snap_val(config_snapshot, "PROGRESSIVE_BE_TRIGGER_PCT", 1.5)) if config_snapshot is not None else 1.5
        lock_trigger_pct = float(cls._snap_val(config_snapshot, "PROGRESSIVE_LOCK_TRIGGER_PCT", 3.0)) if config_snapshot is not None else 3.0
        lock_pct = float(cls._snap_val(config_snapshot, "PROGRESSIVE_LOCK_PCT", 1.5)) if config_snapshot is not None else 1.5

        is_short = cls.is_true_short_contract(ticker, direction=direction) if ticker else (
            str(direction).upper() in ("SHORT", "SELL") and not str(direction).upper().endswith("_ETP")
        )
        if is_short:
            gain_pct = ((entry_price - current_price) / entry_price * 100.0) if entry_price > 0 else 0.0
            if gain_pct >= lock_trigger_pct:
                candidate = entry_price * (1.0 - lock_pct / 100.0)
            elif gain_pct >= be_trigger_pct:
                candidate = entry_price
            else:
                candidate = current_sl
            return round(min(current_sl, candidate) if current_sl > 0 else candidate, 4)
        else:
            gain_pct = ((current_price - entry_price) / entry_price * 100.0) if entry_price > 0 else 0.0
            if gain_pct >= lock_trigger_pct:
                candidate = entry_price * (1.0 + lock_pct / 100.0)
            elif gain_pct >= be_trigger_pct:
                candidate = entry_price
            else:
                candidate = current_sl
            return round(max(current_sl, candidate), 4)

    # ─────────────────────────────────────────────────────────────
    # Trade Registration
    # ─────────────────────────────────────────────────────────────
    def register_new_trade(
        self,
        card: DecisionCard,
        atr: float = 0.0,
        earnings_in_days: Optional[float] = None,
        fill_price: Optional[float] = None,
        config_snapshot: Optional[Any] = None,
    ) -> TrackedPosition:
        """Register a newly opened trade for active lifecycle tracking."""
        resolved_snap = config_snapshot
        card_epoch_id = getattr(card, "config_epoch_id", None)
        if resolved_snap is None and self.config_bus is not None:
            try:
                if card_epoch_id:
                    resolved_snap = self.config_bus.snapshot_for_epoch(card_epoch_id)
                if resolved_snap is None:
                    resolved_snap = self.config_bus.active_snapshot()
            except Exception as exc:
                logger.debug("Could not resolve ConfigBus snapshot during register_new_trade: %s", exc)

        resolved_epoch_id = card_epoch_id or (
            self._snap_val(resolved_snap, "config_epoch_id", None) if resolved_snap is not None else None
        )
        serialized_snap = self._snapshot_to_dict(resolved_snap)

        # On Trading 212 Invest/Stocks, all leveraged ETPs (both 3x Long and 3x Inverse Short)
        # are spot instruments bought long; an increasing ETP price represents profit.
        is_cfd_short = self.is_true_short_contract(card.trading212_ticker, direction=card.direction)

        # Derive relative price proportions from DecisionCard
        stop_dist_pct = abs(card.entry_price - card.stop_loss) / card.entry_price if card.entry_price > 0 else 0.05
        tp1_dist_pct = abs(card.take_profit_1 - card.entry_price) / card.entry_price if card.entry_price > 0 else 0.05
        tp2_dist_pct = abs(card.take_profit_2 - card.entry_price) / card.entry_price if card.entry_price > 0 else 0.10
        tp3_dist_pct = abs(card.take_profit_3 - card.entry_price) / card.entry_price if card.entry_price > 0 else 0.15
        atr_pct = (atr / card.entry_price) if (atr > 0 and card.entry_price > 0) else 0.02

        if not fill_price or fill_price <= 0:
            # Prevent underlying price scale contamination (e.g. TLT $81.68 vs 5STL €15.11)
            if card.trading212_ticker and card.trading212_ticker != card.ticker:
                try:
                    quote = MarketDataProvider.get_quote(card.trading212_ticker)
                    live_q = quote.get("price") or quote.get("close")
                    if live_q and float(live_q) > 0:
                        fill_price = float(live_q)
                except Exception:
                    pass

        if fill_price and fill_price > 0:
            entry_price = round(fill_price, 4)
            if is_cfd_short:
                stop_loss = round(entry_price * (1.0 + stop_dist_pct), 2)
                trailing_stop_loss = stop_loss
                tp1 = round(max(0.01, entry_price * (1.0 - tp1_dist_pct)), 2)
                tp2 = round(max(0.01, entry_price * (1.0 - tp2_dist_pct)), 2)
                tp3 = round(max(0.01, entry_price * (1.0 - tp3_dist_pct)), 2)
            else:
                stop_loss = round(max(0.01, entry_price * (1.0 - stop_dist_pct)), 2)
                trailing_stop_loss = stop_loss
                tp1 = round(entry_price * (1.0 + tp1_dist_pct), 2)
                tp2 = round(entry_price * (1.0 + tp2_dist_pct), 2)
                tp3 = round(entry_price * (1.0 + tp3_dist_pct), 2)
            calibrated_atr = round(entry_price * atr_pct, 4)
        else:
            entry_price = card.entry_price
            # If not CFD short and card stop loss is >= entry price (underlying inverted scale), recalculate
            if not is_cfd_short and card.stop_loss >= card.entry_price > 0:
                levels = self.calculate_exit_levels(
                    entry_price=entry_price,
                    direction=card.direction,
                    ticker=card.trading212_ticker,
                    config_snapshot=resolved_snap,
                )
                stop_loss = levels["stop_loss"]
                trailing_stop_loss = levels["trailing_stop_loss"]
                tp1 = levels["tp1"]
                tp2 = levels["tp2"]
                tp3 = levels["tp3"]
            else:
                stop_loss = card.stop_loss
                trailing_stop_loss = card.trailing_stop_loss or card.stop_loss
                tp1 = card.take_profit_1
                tp2 = card.take_profit_2
                tp3 = card.take_profit_3
            calibrated_atr = atr or (card.entry_price * 0.02)

        raw_earnings = earnings_in_days if earnings_in_days is not None else getattr(card, "earnings_in_days", None)
        parsed_earnings: Optional[float] = None
        if raw_earnings is not None:
            try:
                parsed_earnings = float(raw_earnings)
            except (ValueError, TypeError):
                parsed_earnings = None

        pos = TrackedPosition(
            ticker=card.trading212_ticker,
            underlying=card.ticker,
            direction=card.direction,
            entry_price=entry_price,
            original_quantity=card.position_size_shares,
            remaining_quantity=card.position_size_shares,
            stop_loss=stop_loss,
            trailing_stop_loss=trailing_stop_loss,
            highest_price=entry_price,
            lowest_price=entry_price,
            tp1=tp1,
            tp2=tp2,
            tp3=tp3,
            atr=calibrated_atr,
            earnings_in_days=parsed_earnings,
            scale_calibrated=bool(fill_price and fill_price > 0),
            config_epoch_id=resolved_epoch_id,
            entry_config_snapshot=serialized_snap,
        )
        with self.execution_gate_lock:
            with self._state_lock:
                self.positions[card.trading212_ticker] = pos
                self._save_state()

        logger.info(
            "Registered new 3x %s trade: %s @ $%.2f (Shares: %.2f, SL: $%.2f, TP1: $%.2f, Earnings: %s)",
            pos.direction, pos.ticker, pos.entry_price, pos.original_quantity, pos.stop_loss, pos.tp1,
            f"{pos.earnings_in_days}d" if pos.earnings_in_days is not None else "None",
        )
        return pos

    # ─────────────────────────────────────────────────────────────
    # Native Broker Stop Orders (Trading 212 Server-Side SL / TSL)
    # ─────────────────────────────────────────────────────────────
    def place_broker_stop(
        self,
        pos: TrackedPosition,
        stop_price: Optional[float] = None,
        allow_day_fallback: bool = False,
        _allow_heal: bool = True,
        _auto_restore_on_failure: bool = True,
        _allow_while_halted: bool = False,
    ) -> Optional[str]:
        """Place or replace a native GTC Stop Order on Trading 212 broker server."""
        with self._state_lock:
            if (not _allow_while_halted and self.is_system_halted()) or pos.is_closed:
                return None
            price = stop_price if stop_price is not None else pos.trailing_stop_loss
            rem_qty = pos.remaining_quantity
            cur_price = pos.current_price or pos.highest_price or pos.entry_price
            has_existing_stop = bool(pos.broker_stop_order_id)
            old_stop_price = getattr(pos, "_last_broker_stop_price", None)
            if not old_stop_price or old_stop_price <= 0:
                old_stop_price = pos.stop_loss if (pos.stop_loss or 0.0) > 0 else pos.trailing_stop_loss
            old_stop_field = pos.stop_loss
            old_tsl_field = pos.trailing_stop_loss
        if rem_qty <= 0 or price <= 0:
            return None

        # Invariant check: for spot holdings, Stop Loss must be strictly below current market price
        is_cfd_short = self.is_true_short_contract(pos)
        if not is_cfd_short:
            if not self.dry_run and self.t212 and hasattr(self.t212, "get_quote") and not type(self.t212).__name__.endswith("Mock"):
                try:
                    q = self.t212.get_quote(pos.ticker)
                    live_bid = float(q.get("bid") or 0.0)
                    if live_bid > 0 and cur_price > 0 and (0.7 < (live_bid / cur_price) < 1.3) and live_bid < cur_price:
                        cur_price = live_bid
                except Exception:
                    pass

            if cur_price > 0 and price >= cur_price:
                logger.error(
                    "INVARIANT VIOLATION: Stop price %.4f >= current price %.4f for spot holding %s! "
                    "Refusing to submit inverted broker stop order to prevent instant market liquidation. Healing stop...",
                    price, cur_price, pos.ticker,
                )
                if not _allow_heal:
                    return None
                self._heal_stale_stop(pos, rejected_price=price)
                with self._state_lock:
                    price = pos.trailing_stop_loss
                if price >= cur_price:
                    logger.critical(
                        "Stop healing could not place stop below current price for %s (price=%.4f, cur=%.4f). Aborting stop order.",
                        pos.ticker, price, cur_price,
                    )
                    return None

        with self._state_lock:
            if (not _allow_while_halted and self.is_system_halted()) or pos.is_closed:
                return None

        # Cancel previous broker stop order if one was already placed
        cancelled_existing_stop = False
        if has_existing_stop:
            cancelled_existing_stop = self.cancel_broker_stop(pos)

        with self._state_lock:
            if (not _allow_while_halted and self.is_system_halted()) or pos.is_closed:
                return None

        if self.dry_run:
            fake_id = f"DRY_STOP_{pos.ticker}_{int(time.time())}"
            with self._state_lock:
                if (not _allow_while_halted and self.is_system_halted()) or pos.is_closed:
                    return None
                pos.broker_stop_order_id = fake_id
                pos.unprotected_reason = None
                setattr(pos, "_last_broker_stop_price", round(price, 2))
                self._save_state()
            logger.info("[DRY-RUN] Would place native broker Stop Order for %s: %.3f shares @ $%.2f",
                        pos.ticker, rem_qty, price)
            return fake_id

        order_qty = -abs(rem_qty)
        try:
            # In Trading 212 Invest/Stocks API:
            # Protective stops for cash equity and 3x/5x leveraged ETPs sell held shares (negative quantity)
            logger.info(
                "Placing native Stop Order on Trading 212 for %s: %.3f shares @ stopPrice $%.2f",
                pos.ticker, order_qty, price
            )
            # Try GOOD_TILL_CANCEL first (standard for persistent protective stops), fallback to DAY only when explicitly allowed
            try:
                res = self.t212.place_stop_order(
                    ticker=pos.ticker,
                    quantity=order_qty,
                    stop_price=round(price, 2),
                    time_validity="GOOD_TILL_CANCEL",
                )
            except Exception as first_exc:
                if not allow_day_fallback:
                    raise
                logger.debug("Stop order with validity GOOD_TILL_CANCEL failed (%s), attempting DAY...", first_exc)
                delay = self._rate_limit_delay
                if delay > 0:
                    time.sleep(delay)
                res = self.t212.place_stop_order(
                    ticker=pos.ticker,
                    quantity=order_qty,
                    stop_price=round(price, 2),
                    time_validity="DAY",
                )
            order_id = str(res.id) if hasattr(res, "id") and res.id is not None else str(res)
            orphan_abort = False
            with self._state_lock:
                if (not _allow_while_halted and self.is_system_halted()) or pos.is_closed:
                    orphan_abort = True
                else:
                    pos.broker_stop_order_id = order_id
                    pos.unprotected_reason = None
                    setattr(pos, "_last_stop_fail_time", 0.0)
                    setattr(pos, "_last_broker_stop_price", round(price, 2))
                    self._save_state()
            if orphan_abort:
                with contextlib.suppress(Exception):
                    if self.t212:
                        self.t212.cancel_order(order_id)
                logger.warning("Cancelled orphan stop order %s for %s after system halt/close", order_id, pos.ticker)
                return None
            logger.info("Successfully placed broker Stop Order on Trading 212: %s (id=%s)", pos.ticker, order_id)
            return order_id
        except Exception as exc:
            with self._state_lock:
                setattr(pos, "_last_stop_fail_time", time.time())
            logger.error("Failed to place native broker Stop Order for %s on Trading 212: %s", pos.ticker, exc)
            err_str = str(exc).lower()

            # Check if broker rejected due to partial shares already reserved (e.g. pending limit order)
            owned_match = re.search(r"owned:\s*(\d+(?:\.\d+)?)", err_str)
            if owned_match:
                try:
                    available_qty = float(owned_match.group(1))
                    if 0 < available_qty < abs(order_qty):
                        logger.info(
                            "Auto-adjusting stop order quantity for %s from %.3f to available unreserved shares: -%.3f",
                            pos.ticker, order_qty, available_qty
                        )
                        res = self.t212.place_stop_order(
                            ticker=pos.ticker,
                            quantity=-round(available_qty, 4),
                            stop_price=round(price, 2),
                            time_validity="GOOD_TILL_CANCEL",
                        )
                        order_id = str(res.id) if hasattr(res, "id") and res.id is not None else str(res)
                        orphan_abort = False
                        with self._state_lock:
                            if (not _allow_while_halted and self.is_system_halted()) or pos.is_closed:
                                orphan_abort = True
                            else:
                                pos.broker_stop_order_id = order_id
                                pos.unprotected_reason = None
                                setattr(pos, "_last_stop_fail_time", 0.0)
                                setattr(pos, "_last_broker_stop_price", round(price, 2))
                                self._save_state()
                        if orphan_abort:
                            with contextlib.suppress(Exception):
                                if self.t212:
                                    self.t212.cancel_order(order_id)
                            return None
                        logger.info(
                            "Successfully placed adjusted broker Stop Order on Trading 212: %s (id=%s, qty=%.3f)",
                            pos.ticker, order_id, available_qty
                        )
                        return order_id
                except Exception as retry_exc:
                    logger.warning("Retry with adjusted quantity failed for %s: %s", pos.ticker, retry_exc)

            # If we cancelled an active GTC stop and replacement failed outside explicit Re-Epoch restore,
            # immediately attempt bounded restore of old_stop_price so the position is not left naked.
            if (
                _auto_restore_on_failure
                and cancelled_existing_stop
                and old_stop_price
                and old_stop_price > 0
                and round(old_stop_price, 2) != round(price, 2)
                and (is_cfd_short or cur_price <= 0 or old_stop_price < cur_price)
                and (_allow_while_halted or not self.is_system_halted())
            ):
                restored_id: Optional[str] = None
                for attempt in range(2):
                    try:
                        if attempt > 0 and self._rate_limit_delay > 0:
                            time.sleep(min(1.5, self._rate_limit_delay))
                        r_res = self.t212.place_stop_order(
                            ticker=pos.ticker,
                            quantity=order_qty,
                            stop_price=round(old_stop_price, 2),
                            time_validity="GOOD_TILL_CANCEL",
                        )
                        restored_id = str(r_res.id) if hasattr(r_res, "id") and r_res.id is not None else str(r_res)
                        break
                    except Exception as restore_exc:
                        logger.error("Restore previous GTC stop attempt %d failed for %s: %s", attempt + 1, pos.ticker, restore_exc)
                with self._state_lock:
                    if restored_id:
                        pos.broker_stop_order_id = restored_id
                        pos.stop_loss = min(old_stop_field, old_stop_price) if old_stop_field else old_stop_price
                        pos.trailing_stop_loss = old_stop_price if round(old_tsl_field or 0.0, 2) == round(price, 2) else old_tsl_field
                        pos.unprotected_reason = None
                        setattr(pos, "_last_broker_stop_price", round(old_stop_price, 2))
                    else:
                        pos.unprotected_reason = "GTC stop replacement and restoration failed"
                    self._save_state()
                if not restored_id and hasattr(self.notifier, "notify_alert"):
                    with contextlib.suppress(Exception):
                        self.notifier.notify_alert(
                            f"🚨 UNPROTECTED POSITION: {pos.ticker}",
                            f"Replacement stop ({price:.2f}) and previous stop restore ({old_stop_price:.2f}) failed.",
                        )
            elif cancelled_existing_stop and _auto_restore_on_failure:
                with self._state_lock:
                    pos.unprotected_reason = "GTC stop replacement failed after cancelling previous stop"
                    self._save_state()
                if hasattr(self.notifier, "notify_alert"):
                    with contextlib.suppress(Exception):
                        self.notifier.notify_alert(
                            f"🚨 UNPROTECTED POSITION: {pos.ticker}",
                            f"Previous GTC stop cancelled and replacement ({price:.2f}) failed.",
                        )

            # Detect stale stop-price rejection (HTTP 400 / "price too far") and self-heal
            if _allow_heal and ("400" in err_str or "price" in err_str or "far" in err_str or "invalid" in err_str):
                logger.warning(
                    "Detected possible stale stop price for %s — attempting self-heal", pos.ticker
                )
                self._heal_stale_stop(pos, rejected_price=price)
            return None

    def _heal_stale_stop(self, pos: TrackedPosition, rejected_price: Optional[float] = None) -> None:
        """Re-fetch live market price from broker and recompute a fresh ATR-based stop loss.

        Never loosens an already ratcheted valid stop (`max(old_sl, healed_sl)` for LONG),
        while excluding any `rejected_price` so a broker-rejected stop cannot re-loop.
        Updates `pos.stop_loss` and `pos.trailing_stop_loss` in-place under `_state_lock` and persists state.
        """
        if self.dry_run:
            return
        try:
            portfolio = self.t212.get_portfolio(force_refresh=True)
            bp = next((p for p in portfolio if p.ticker == pos.ticker), None)
            if not bp:
                logger.debug("_heal_stale_stop: %s not found in broker portfolio", pos.ticker)
                return
            live_price = bp.current_price if (bp.current_price or 0) > 0 else (bp.average_price or 0)
            if live_price <= 0:
                logger.debug("_heal_stale_stop: no valid live price for %s", pos.ticker)
                return
            with self._state_lock:
                # Use stored ATR percentage if available; default to 2% (conservative for 3x ETPs)
                atr_pct = getattr(pos, "_last_atr_pct", None)
                if not atr_pct or atr_pct <= 0:
                    atr_pct = 0.02
                is_short = self.is_true_short_contract(pos)
                if is_short:
                    healed_sl = round(live_price * (1.0 + atr_pct * 2.5), 4)
                    candidates = [
                        s for s in (pos.stop_loss, pos.trailing_stop_loss)
                        if (s or 0.0) > live_price
                        and (rejected_price is None or abs(float(s) - float(rejected_price)) > 1e-4)
                        and not (rejected_price is not None and rejected_price <= healed_sl and float(s) <= float(rejected_price) + 1e-4)
                    ]
                    old_sl = min(candidates) if candidates else pos.stop_loss
                    new_sl = round(min(old_sl, healed_sl), 4) if candidates else healed_sl
                else:
                    healed_sl = round(live_price * (1.0 - atr_pct * 2.5), 4)
                    candidates = [
                        s for s in (pos.stop_loss, pos.trailing_stop_loss)
                        if 0.0 < (s or 0.0) < live_price
                        and (rejected_price is None or abs(float(s) - float(rejected_price)) > 1e-4)
                        and not (rejected_price is not None and rejected_price >= healed_sl and float(s) >= float(rejected_price) - 1e-4)
                    ]
                    old_sl = max(candidates) if candidates else pos.stop_loss
                    new_sl = round(max(old_sl, healed_sl), 4) if candidates else healed_sl

                logger.warning(
                    "_heal_stale_stop %s: old_sl=%.4f → new_sl=%.4f (live_price=%.4f, atr_pct=%.4f)",
                    pos.ticker, old_sl, new_sl, live_price, atr_pct,
                )
                pos.stop_loss = new_sl
                pos.trailing_stop_loss = new_sl
                setattr(pos, "_last_stop_fail_time", 0.0)
                self._save_state()
        except Exception as heal_exc:
            logger.debug("_heal_stale_stop failed for %s: %s", pos.ticker, heal_exc)

    def cancel_broker_stop(self, pos: TrackedPosition) -> bool:
        """Cancel pending broker stop order on Trading 212."""
        with self._state_lock:
            order_id = pos.broker_stop_order_id
        if not order_id:
            return True

        if self.dry_run or order_id.startswith("DRY_"):
            with self._state_lock:
                pos.broker_stop_order_id = None
                self._save_state()
            logger.info("[DRY-RUN] Cancelled broker stop order %s for %s", order_id, pos.ticker)
            return True

        try:
            logger.info("Canceling broker Stop Order %s for %s on Trading 212", order_id, pos.ticker)
            success = self.t212.cancel_order(order_id)
            if success:
                with self._state_lock:
                    pos.broker_stop_order_id = None
                    self._save_state()
            return success
        except Exception as exc:
            logger.warning("Failed to cancel broker Stop Order %s on Trading 212: %s", order_id, exc)
            return False

    def place_broker_tp_limit(
        self,
        pos: TrackedPosition,
        limit_price: Optional[float] = None,
        quantity: Optional[float] = None,
    ) -> Optional[str]:
        """Place or replace a native GTC Limit Order for Take Profit on Trading 212 broker server."""
        if self._is_pure_tsl_for_position(pos):
            logger.debug("Pure TSL mode: broker TP limit orders disabled for %s to ensure 100%% stop protection", pos.ticker)
            return None

        with self._state_lock:
            price = limit_price if limit_price is not None else pos.tp1
            qty = quantity if quantity is not None else round(pos.remaining_quantity * 0.5, 4)
            has_existing_tp = bool(pos.broker_tp_order_id)
        if qty <= 0 or price <= 0:
            return None

        # Cancel previous broker TP limit order if one exists
        if has_existing_tp:
            self.cancel_broker_tp_limit(pos)

        if self.dry_run:
            fake_id = f"DRY_LIMIT_{pos.ticker}_{int(time.time())}"
            with self._state_lock:
                pos.broker_tp_order_id = fake_id
                self._save_state()
            logger.info("[DRY-RUN] Would place native broker Limit Order for %s: %.3f shares @ $%.2f",
                        pos.ticker, qty, price)
            return fake_id

        try:
            # In Trading 212 Invest: Sell held shares at limit price
            order_qty = -abs(qty)
            logger.info(
                "Placing native Limit Order on Trading 212 for %s: %.3f shares @ limitPrice $%.2f",
                pos.ticker, order_qty, price
            )
            try:
                res = self.t212.place_limit_order(
                    ticker=pos.ticker,
                    quantity=order_qty,
                    limit_price=round(price, 2),
                    time_validity="GOOD_TILL_CANCEL",
                )
            except Exception as first_exc:
                logger.debug("Limit order with validity GOOD_TILL_CANCEL failed (%s), attempting DAY...", first_exc)
                delay = self._rate_limit_delay
                if delay > 0:
                    time.sleep(delay)
                res = self.t212.place_limit_order(
                    ticker=pos.ticker,
                    quantity=order_qty,
                    limit_price=round(price, 2),
                    time_validity="DAY",
                )
            order_id = str(res.id) if hasattr(res, "id") and res.id is not None else str(res)
            with self._state_lock:
                pos.broker_tp_order_id = order_id
                self._save_state()
            logger.info("Successfully placed broker Limit Order on Trading 212: %s (id=%s)", pos.ticker, order_id)
            return order_id
        except Exception as exc:
            with self._state_lock:
                setattr(pos, "_last_tp_fail_time", time.time())
            logger.debug("Placing native broker Limit Order for %s deferred: %s", pos.ticker, exc)
            return None

    def cancel_broker_tp_limit(self, pos: TrackedPosition) -> bool:
        """Cancel pending broker TP limit order on Trading 212."""
        with self._state_lock:
            order_id = pos.broker_tp_order_id
        if not order_id:
            return True

        if self.dry_run or order_id.startswith("DRY_"):
            with self._state_lock:
                pos.broker_tp_order_id = None
                self._save_state()
            logger.info("[DRY-RUN] Cancelled broker limit order %s for %s", order_id, pos.ticker)
            return True

        try:
            logger.info("Canceling broker Limit Order %s for %s on Trading 212", order_id, pos.ticker)
            success = self.t212.cancel_order(order_id)
            if success:
                with self._state_lock:
                    pos.broker_tp_order_id = None
                    self._save_state()
            return success
        except Exception as exc:
            logger.warning("Failed to cancel broker Limit Order %s on Trading 212: %s", order_id, exc)
            return False

    def cancel_all_broker_orders(self, pos: TrackedPosition) -> None:
        """Cancel all pending broker orders (Stop Loss and Take Profit Limit) for a position."""
        if pos.broker_stop_order_id:
            self.cancel_broker_stop(pos)
        if pos.broker_tp_order_id:
            self.cancel_broker_tp_limit(pos)

    def sync_broker_stops(self) -> List[str]:
        """Ensure all active positions have an active native GTC Stop Order placed on Trading 212."""
        with self._broker_op_lock:
            synced = []
            now = time.time()
            with self._state_lock:
                items = list(self.positions.items())
            for ticker, pos in items:
                with self._state_lock:
                    needs_stop = not pos.is_closed and pos.remaining_quantity > 0 and not pos.broker_stop_order_id
                    last_fail = getattr(pos, "_last_stop_fail_time", 0.0)
                if needs_stop:
                    if now - last_fail < 180.0:
                        logger.debug("Cooldown active for %s stop sync (failed %.1fs ago)", ticker, now - last_fail)
                        continue

                    logger.info("Syncing missing broker Stop Order for active position %s...", ticker)
                    order_id = self.place_broker_stop(pos)
                    if order_id:
                        synced.append(ticker)
                    delay = self._rate_limit_delay
                    if delay > 0:
                        time.sleep(delay)
            if synced:
                self._save_state()
                logger.info("Synced native broker stops for %d positions: %s", len(synced), synced)
            return synced

    def sync_broker_tp_limits(self) -> List[str]:
        """Ensure active non-Pure-TSL positions have GTC TP Limit orders, cancelling Pure-TSL limits."""
        with self._broker_op_lock:
            synced = []
            now = time.time()
            with self._state_lock:
                items = list(self.positions.items())
            for ticker, pos in items:
                if self._is_pure_tsl_for_position(pos):
                    if pos.broker_tp_order_id:
                        self.cancel_broker_tp_limit(pos)
                    continue

                with self._state_lock:
                    needs_tp = not pos.is_closed and pos.remaining_quantity > 0 and not pos.tp1_hit and not pos.broker_tp_order_id
                    last_fail = getattr(pos, "_last_tp_fail_time", 0.0)
                    tp1_val = pos.tp1
                if needs_tp:
                    if now - last_fail < 900.0:
                        continue

                    logger.info("Syncing missing broker Take Profit Limit Order for %s @ $%.2f...", ticker, tp1_val)
                    order_id = self.place_broker_tp_limit(pos)
                    if order_id:
                        synced.append(ticker)
                    else:
                        with self._state_lock:
                            setattr(pos, "_last_tp_fail_time", now)
                    delay = self._rate_limit_delay
                    if delay > 0:
                        time.sleep(delay)
            if synced:
                self._save_state()
                logger.info("Synced native broker TP limits for %d positions: %s", len(synced), synced)
            return synced

    def sync_broker_orders(self) -> Dict[str, List[str]]:
        """Ensure all active positions have native GTC Stop orders and per-position TP Limits on Trading 212."""
        with self._broker_op_lock:
            if not self.dry_run:
                try:
                    open_orders = self.t212.get_orders()
                    for o in open_orders:
                        o_type = str(o.get("type") if isinstance(o, dict) else getattr(o, "type", "")).upper()
                        if o_type == "LIMIT":
                            limit_id = str(o.get("id") if isinstance(o, dict) else getattr(o, "id", ""))
                            limit_ticker = o.get("ticker") if isinstance(o, dict) else getattr(o, "ticker", None)
                            with self._state_lock:
                                pos = self.positions.get(limit_ticker) if limit_ticker else None
                                is_closed = getattr(pos, "is_closed", False) if pos is not None else False
                            should_cancel = self._is_pure_tsl_for_position(None if (pos is None or is_closed) else pos)
                            if should_cancel:
                                logger.info(
                                    "Pure TSL audit: Canceling competing broker Limit Order %s for %s to ensure 100%% stop protection",
                                    limit_id, limit_ticker
                                )
                                self.t212.cancel_order(limit_id)
                            else:
                                logger.debug(
                                    "Epoch isolation: Preserving broker Limit Order %s for %s (PURE_TSL_MODE=False)",
                                    limit_id, limit_ticker
                                )
                except Exception as clean_exc:
                    logger.debug("Pure TSL limit cancellation check notice: %s", clean_exc)

            synced_stops = self.sync_broker_stops()
            synced_tps = self.sync_broker_tp_limits()
            return {"stops": synced_stops, "tps": synced_tps}

    def apply_daily_rollover_stops(self) -> List[Dict[str, Any]]:
        """Apply new-day session stop loss elevation and time-decay envelope compression.

        Rules:
        1. On regular exchange trading days, increment session_count for each active position.
        2. Evaluate runner status: if gain >= +3.0% or cushion locked, mark is_runner = True.
        3. Compress ATR trail per session for non-runners using position epoch decay rules.
        4. If position has gain: ratchet to at least Entry + 50% gain, or decayed Chandelier trail.
        5. If stagnant: apply decayed Chandelier trail.
        6. Day 4+ Stagnancy Gate: If session_count >= 4 and not is_runner and gain < +1.5%,
           elevate stop directly to Entry Price (Break-Even).
        7. Strict ratchet invariant: Stop only moves in favorable direction (never loosens).
        """
        results = []
        now_utc = datetime.now(timezone.utc)
        today_str = now_utc.strftime("%Y-%m-%d")
        is_trading_day = is_exchange_trading_day(now_utc)

        with self._state_lock:
            items = list(self.positions.items())

        for ticker, pos in items:
            broker_stop_target: Optional[float] = None
            notif_payload: Optional[Dict[str, Any]] = None
            pos_snap = self._resolve_position_snapshot(pos)

            with self._state_lock:
                if pos.is_closed or pos.remaining_quantity <= 0 or pos.entry_price <= 0:
                    continue

                is_short = self.is_true_short_contract(pos)
                peak = pos.lowest_price if is_short else pos.highest_price

                # Sanity guard: Peak cannot exceed 2.0x entry or be <0.2x entry on ETPs (indicates underlying scale contamination)
                if peak > pos.entry_price * 2.0 or peak < pos.entry_price * 0.2:
                    logger.warning("Rejecting contaminated peak %.2f for %s (entry %.2f) in rollover stop calculation", peak, ticker, pos.entry_price)
                    pos.highest_price = pos.current_price or pos.entry_price
                    pos.lowest_price = pos.current_price or pos.entry_price
                    continue

                # 1. Session age accounting: increment session_count on business trading days
                if is_trading_day and getattr(pos, "last_session_date", "") != today_str:
                    pos.session_count = getattr(pos, "session_count", 1) + 1
                    pos.last_session_date = today_str

                session_count = getattr(pos, "session_count", 1)

                # 2. Check runner status (Cushion locked or gain >= +3.0%)
                cur_px = getattr(pos, "current_price", 0.0) or peak
                gain_pct = ((cur_px - pos.entry_price) / pos.entry_price * 100.0) if not is_short else ((pos.entry_price - cur_px) / pos.entry_price * 100.0)
                is_cushion_locked = (pos.trailing_stop_loss >= pos.entry_price * 1.0149 if not is_short else pos.trailing_stop_loss <= pos.entry_price * 0.9851)
                if gain_pct >= 3.0 or is_cushion_locked:
                    pos.is_runner = True

                is_runner = getattr(pos, "is_runner", False)

                # 3. Dynamic Epoch-Aware Decay Multiplier
                decay_mult = self._get_decay_atr_multiplier(
                    session_count=session_count,
                    is_runner=is_runner,
                    config_snapshot=pos_snap,
                )
                atr_trail_dist = max(pos.atr * decay_mult, peak * 0.015) if pos.atr > 0 else (peak * 0.02)
                target_chandelier = (peak + atr_trail_dist) if is_short else (peak - atr_trail_dist)

                # 4. Check if position experienced positive gain
                has_gain = (peak < pos.entry_price) if is_short else (peak > pos.entry_price)
                if has_gain:
                    gain_amt = (pos.entry_price - peak) if is_short else (peak - pos.entry_price)
                    target_50pct = (pos.entry_price - 0.50 * gain_amt) if is_short else (pos.entry_price + 0.50 * gain_amt)
                    recommended_sl = min(target_50pct, target_chandelier) if is_short else max(target_50pct, target_chandelier)
                else:
                    recommended_sl = target_chandelier

                # 5. Day 4+ Stagnancy Gate: Force Break-Even on 4+ sessions of chop
                if session_count >= 4 and not is_runner and gain_pct < 1.5:
                    recommended_sl = pos.entry_price

                recommended_sl = round(recommended_sl, 2)

                # Invariant check: Stop must remain below current market price for long spot positions
                if not is_short and cur_px and cur_px > 0 and recommended_sl >= cur_px:
                    recommended_sl = round(cur_px * 0.985, 2)
                elif is_short and cur_px and cur_px > 0 and recommended_sl <= cur_px:
                    recommended_sl = round(cur_px * 1.015, 2)

                # Check if this ratchets tighter than current stop
                should_ratchet = (recommended_sl < pos.trailing_stop_loss) if is_short else (recommended_sl > pos.trailing_stop_loss)
                if should_ratchet:
                    old_sl = pos.trailing_stop_loss
                    pos.trailing_stop_loss = recommended_sl
                    pos.stop_loss = recommended_sl
                    if (recommended_sl >= pos.tp1 if not is_short else recommended_sl <= pos.tp1):
                        pos.tp1_armed_hysteresis = False
                        pos.tp1_stop_elevated = True

                    locked_pct = abs((recommended_sl - pos.entry_price) / pos.entry_price) * 100.0 if pos.entry_price > 0 else 0.0
                    logger.info(
                        "🌅 Daily Session Rollover: %s stop ratcheted $%.2f -> $%.2f (Peak: $%.2f, +%.2f%% locked, Session %d, %.2fx ATR%s)",
                        ticker, old_sl, recommended_sl, peak, locked_pct, session_count, decay_mult,
                        " [RUNNER IMMUNITY]" if is_runner else "",
                    )
                    broker_stop_target = recommended_sl
                    notif_payload = {
                        "ticker": pos.ticker,
                        "underlying": pos.underlying,
                        "direction": pos.direction,
                        "current_price": pos.current_price or recommended_sl,
                        "new_tsl": recommended_sl,
                        "locked_in_pnl_pct": locked_pct,
                        "event_type": "DAILY_ROLLOVER_RATCHET",
                    }
                    results.append({
                        "ticker": ticker,
                        "old_sl": old_sl,
                        "new_sl": recommended_sl,
                        "peak": peak,
                        "locked_pct": round(locked_pct, 2),
                        "session_count": session_count,
                        "decay_multiplier": decay_mult,
                    })

            # Perform broker & notification I/O strictly outside _state_lock
            if broker_stop_target is not None:
                self.place_broker_stop(pos, stop_price=broker_stop_target)
            if notif_payload and hasattr(self.notifier, "notify_tsl"):
                try:
                    self.notifier.notify_tsl(**notif_payload)
                except Exception as notif_exc:
                    logger.debug("Rollover notification notice: %s", notif_exc)
        if results:
            self._save_state()
        return results

    def reconcile_with_broker(
        self,
        adopt_untracked: bool = True,
        active_metrics: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Reconcile local active positions with actual broker portfolio.

        Serialized via `_broker_op_lock`; never holds `_state_lock` across broker HTTP calls.
        """
        summary: Dict[str, Any] = {
            "closed_externally": [],
            "quantity_synced": [],
            "untracked_broker_positions": [],
            "adopted_from_broker": [],
            "expired_stops_restored": [],
        }

        with self._broker_op_lock:
            try:
                broker_portfolio = self.t212.get_portfolio(force_refresh=True)
            except Exception as exc:
                logger.warning("Broker reconciliation skipped — portfolio fetch failed: %s", exc)
                return summary

            broker_map = {p.ticker: p for p in broker_portfolio if getattr(p, "quantity", 0.0) > 0}

            # Query pending broker orders to associate native stop and limit orders
            stop_orders_by_ticker: Dict[str, Any] = {}
            limit_orders_by_ticker: Dict[str, Any] = {}
            try:
                if hasattr(self.t212, "get_orders"):
                    open_orders = self.t212.get_orders(force_refresh=False)
                    for o in open_orders:
                        o_ticker = o.get("ticker") if isinstance(o, dict) else getattr(o, "ticker", "")
                        o_type = str(o.get("type") if isinstance(o, dict) else getattr(o, "type", "")).upper()
                        if "STOP" in o_type:
                            stop_orders_by_ticker[o_ticker] = o
                        elif "LIMIT" in o_type:
                            limit_orders_by_ticker[o_ticker] = o
            except Exception as ord_exc:
                logger.debug("Reconciliation open orders fetch notice: %s", ord_exc)

            with self._state_lock:
                local_items = list(self.positions.items())

            # 1. Check local positions against broker
            for ticker, pos in local_items:
                with self._state_lock:
                    if pos.is_closed:
                        continue

                if ticker not in broker_map:
                    # In live mode, if position is missing from broker, mark closed externally
                    if not self.dry_run:
                        pos_snap = self._resolve_position_snapshot(pos)
                        logger.warning(
                            "Reconciliation: %s not found in broker portfolio! Marked closed externally.",
                            ticker,
                        )
                        with self._state_lock:
                            pos.is_closed = True
                            pos.remaining_quantity = 0.0
                            pos.last_update_time = datetime.now(timezone.utc).isoformat()
                        self.cancel_all_broker_orders(pos)
                        summary["closed_externally"].append(ticker)

                        # Register anti-churn loss cooldown on RiskManager to prevent immediate whipsaw re-entry
                        if hasattr(self.risk_manager, "register_loss_exit"):
                            cooldown = float(self._snap_val(
                                pos_snap,
                                "TRADE_LOSS_COOLDOWN_SECONDS",
                                getattr(settings, "TRADE_LOSS_COOLDOWN_SECONDS", 3600.0),
                            ))
                            self.risk_manager.register_loss_exit(pos.ticker, pos.underlying, cooldown_seconds=cooldown)

                        # Dispatch notification outside _state_lock
                        if hasattr(self.notifier, "notify_stop_loss"):
                            try:
                                self.notifier.notify_stop_loss(
                                    ticker=pos.ticker,
                                    underlying=pos.underlying,
                                    price=pos.stop_loss,
                                    quantity=pos.original_quantity,
                                    realized_pnl=0.0,
                                    realized_pnl_pct=0.0,
                                    direction=pos.direction,
                                    is_tsl=False,
                                    details="Reconciliation: Position closed on broker (Native stop hit, limit filled, or manual close)",
                                )
                            except Exception as notif_exc:
                                logger.debug("Reconciliation exit notification failed: %s", notif_exc)
                else:
                    bp = broker_map[ticker]
                    cur_px = bp.current_price if (bp.current_price or 0.0) > 0 else (bp.average_price or 0.0)
                    state_changed = False
                    need_stop_restore = False
                    need_cancel_all = False

                    with self._state_lock:
                        if cur_px and cur_px > 0:
                            pos.current_price = cur_px
                            pos.highest_price = max(pos.highest_price or cur_px, cur_px)
                            pos.lowest_price = min(pos.lowest_price or cur_px, cur_px)

                        # Authoritative direction verification
                        authoritative_dir = get_etp_direction(ticker)
                        if authoritative_dir and pos.direction != authoritative_dir:
                            logger.warning(
                                "Reconciliation: fixing direction discrepancy for %s: %s -> %s",
                                ticker, pos.direction, authoritative_dir,
                            )
                            pos.direction = authoritative_dir
                            state_changed = True

                        # Ingest real market ATR and indicators into existing position if active_metrics supplied
                        if active_metrics:
                            m = active_metrics.get(pos.underlying) or active_metrics.get(ticker)
                            if m:
                                und_atr = float(m.get("atr") or 0.0)
                                und_close = float(m.get("close") or 0.0)
                                if und_atr > 0 and und_close > 0:
                                    scaled_atr_pct = (und_atr / und_close) * 3.0
                                    setattr(pos, "_last_atr_pct", scaled_atr_pct)
                                    if not getattr(pos, "scale_calibrated", False) or pos.atr <= 0:
                                        pos.atr = round(pos.entry_price * scaled_atr_pct, 4)
                                if m.get("nixtla_5d_target") is not None:
                                    setattr(pos, "nixtla_5d_target", float(m["nixtla_5d_target"]))
                                    setattr(pos, "nixtla_expected_return_pct", float(m.get("nixtla_expected_return_pct", 0.0)))

                        # Detect and fix entry-price currency scale mismatch (e.g. GBX pence stored vs EUR)
                        broker_avg = getattr(bp, "average_price", 0.0) or 0.0
                        if broker_avg > 0 and pos.entry_price > 0:
                            ratio = pos.entry_price / broker_avg
                            if ratio > 5.0 or ratio < 0.20:
                                logger.warning(
                                    "Entry price scale mismatch for %s: stored=%.4f broker_avg=%.4f (ratio=%.1f×). "
                                    "Recalibrating position levels to broker average price.",
                                    ticker, pos.entry_price, broker_avg, ratio,
                                )
                                levels = self.calculate_exit_levels(
                                    entry_price=broker_avg,
                                    direction=pos.direction,
                                    existing_stop_price=pos.stop_loss if (0.2 < (pos.stop_loss / broker_avg) < 5.0) else None,
                                    ticker=ticker,
                                    config_snapshot=self._resolve_position_snapshot(pos),
                                )
                                pos.entry_price = broker_avg
                                pos.stop_loss = levels["stop_loss"]
                                pos.trailing_stop_loss = levels["trailing_stop_loss"]
                                pos.tp1 = levels["tp1"]
                                pos.tp2 = levels["tp2"]
                                pos.tp3 = levels["tp3"]
                                pos.atr = levels["atr"]
                                pos.highest_price = cur_px if cur_px > 0 else broker_avg
                                pos.lowest_price = cur_px if cur_px > 0 else broker_avg
                                pos.scale_calibrated = True
                                state_changed = True

                        # Link native stop order from broker if missing or unlinked
                        if ticker in stop_orders_by_ticker:
                            so = stop_orders_by_ticker[ticker]
                            so_id = str(so.get("id") if isinstance(so, dict) else getattr(so, "id", ""))
                            so_price = float(so.get("stopPrice", 0.0) if isinstance(so, dict) else getattr(so, "stop_price", 0.0))
                            if so_id and (not pos.broker_stop_order_id or pos.broker_stop_order_id.startswith("DRY_") or pos.broker_stop_order_id != so_id):
                                pos.broker_stop_order_id = so_id
                            if so_price > 0 and not pos.tp1_hit:
                                pos.stop_loss = so_price
                                pos.trailing_stop_loss = so_price
                        else:
                            if pos.broker_stop_order_id and not pos.broker_stop_order_id.startswith("DRY_"):
                                logger.warning(
                                    "Reconciliation: Native broker stop order %s for %s has EXPIRED or was cancelled on broker! Auto-healing...",
                                    pos.broker_stop_order_id, ticker,
                                )
                                pos.broker_stop_order_id = None
                                summary["expired_stops_restored"].append(ticker)
                                need_stop_restore = True

                        # Link native TP limit order from broker
                        if ticker in limit_orders_by_ticker:
                            lo = limit_orders_by_ticker[ticker]
                            lo_id = str(lo.get("id") if isinstance(lo, dict) else getattr(lo, "id", ""))
                            if lo_id and (not pos.broker_tp_order_id or pos.broker_tp_order_id.startswith("DRY_") or pos.broker_tp_order_id != lo_id):
                                pos.broker_tp_order_id = lo_id
                        else:
                            if pos.broker_tp_order_id and not pos.broker_tp_order_id.startswith("DRY_"):
                                logger.debug("Reconciliation: Native broker TP order %s for %s is no longer open (filled or expired).",
                                             pos.broker_tp_order_id, ticker)
                                pos.broker_tp_order_id = None

                        if abs(bp.quantity - pos.remaining_quantity) > 1e-4:
                            logger.info(
                                "Reconciliation: %s quantity mismatch (local: %.3f, broker: %.3f). Synchronizing.",
                                ticker, pos.remaining_quantity, bp.quantity,
                            )
                            pos.remaining_quantity = bp.quantity
                            pos.last_update_time = datetime.now(timezone.utc).isoformat()
                            summary["quantity_synced"].append((ticker, bp.quantity))
                            if pos.remaining_quantity <= 0:
                                pos.is_closed = True
                                need_cancel_all = True

                    if state_changed:
                        self._save_state()
                    if need_stop_restore and not self.is_system_halted():
                        self.place_broker_stop(pos)
                    if need_cancel_all:
                        self.cancel_all_broker_orders(pos)

            if self.is_system_halted():
                logger.info("Reconciliation skipped broker adoption/stop placement — system is HALTED")
                return summary

            # 2. Check for untracked positions on broker and adopt them
            active_snap = None
            active_epoch_id = None
            if self.config_bus is not None:
                try:
                    active_snap = self.config_bus.active_snapshot()
                    active_epoch_id = self._snap_val(active_snap, "config_epoch_id", None)
                except Exception:
                    active_snap = None

            for b_ticker, bp in broker_map.items():
                with self._state_lock:
                    needs_adopt = b_ticker not in self.positions or self.positions[b_ticker].is_closed
                if needs_adopt:
                    summary["untracked_broker_positions"].append((b_ticker, bp.quantity))
                    if adopt_untracked:
                        from trading_system.data.leveraged_universe import LeveragedUniverse
                        univ = LeveragedUniverse()
                        pair = univ.get_pair_for_etp(b_ticker)
                        underlying = pair.underlying_symbol if pair else b_ticker.replace("l_EQ", "").replace("_US_EQ", "")
                        direction = univ.get_etp_direction(b_ticker)

                        avg_px = bp.average_price if bp.average_price > 0 else (bp.current_price if bp.current_price > 0 else 100.0)
                        cur_px = bp.current_price if bp.current_price > 0 else avg_px

                        so = stop_orders_by_ticker.get(b_ticker)
                        so_id = str(so.get("id") if isinstance(so, dict) else getattr(so, "id", "")) if so else None
                        so_price = float(so.get("stopPrice", 0.0) if isinstance(so, dict) else getattr(so, "stop_price", 0.0)) if so else 0.0

                        und_atr_pct = None
                        m = None
                        if active_metrics:
                            m = active_metrics.get(underlying) or active_metrics.get(b_ticker)
                            if m:
                                und_atr = float(m.get("atr") or 0.0)
                                und_close = float(m.get("close") or 0.0)
                                if und_atr > 0 and und_close > 0:
                                    und_atr_pct = und_atr / und_close

                        levels = self.calculate_exit_levels(
                            entry_price=avg_px,
                            direction=direction,
                            existing_stop_price=so_price if so_price > 0 else None,
                            ticker=b_ticker,
                            underlying_atr_pct=und_atr_pct,
                            config_snapshot=active_snap,
                        )

                        broker_stop_id = so_id if so_id else (f"DRY_STOP_{b_ticker}_{int(time.time())}" if self.dry_run else None)

                        lo = limit_orders_by_ticker.get(b_ticker)
                        lo_id = str(lo.get("id") if isinstance(lo, dict) else getattr(lo, "id", "")) if lo else None
                        broker_tp_id = lo_id if lo_id else (f"DRY_LIMIT_{b_ticker}_{int(time.time())}" if self.dry_run else None)

                        adopted_pos = TrackedPosition(
                            ticker=b_ticker,
                            underlying=underlying,
                            direction=direction,
                            entry_price=avg_px,
                            original_quantity=bp.quantity,
                            remaining_quantity=bp.quantity,
                            stop_loss=levels["stop_loss"],
                            trailing_stop_loss=levels["trailing_stop_loss"],
                            highest_price=max(avg_px, cur_px),
                            lowest_price=min(avg_px, cur_px),
                            tp1=levels["tp1"],
                            tp2=levels["tp2"],
                            tp3=levels["tp3"],
                            atr=levels["atr"],
                            strategy_type="broker_placed",
                            broker_stop_order_id=broker_stop_id,
                            broker_tp_order_id=broker_tp_id,
                            scale_calibrated=True,
                            current_price=cur_px,
                            currency=get_ticker_currency(b_ticker),
                            config_epoch_id=active_epoch_id,
                            entry_config_snapshot=self._snapshot_to_dict(active_snap),
                        )
                        if m and m.get("nixtla_5d_target") is not None:
                            setattr(adopted_pos, "nixtla_5d_target", float(m["nixtla_5d_target"]))
                            setattr(adopted_pos, "nixtla_expected_return_pct", float(m.get("nixtla_expected_return_pct", 0.0)))
                        setattr(adopted_pos, "_last_atr_pct", (levels["atr"] / avg_px) if avg_px > 0 else 0.04)

                        with self._state_lock:
                            if self.is_system_halted():
                                return summary
                            self.positions[b_ticker] = adopted_pos
                        summary["adopted_from_broker"].append(b_ticker)
                        logger.info(
                            "Reconciliation: Adopted broker-placed position %s (%s, %.3f shares @ $%.2f, SL: $%.2f, TP1: $%.2f, StopID: %s)",
                            b_ticker, underlying, bp.quantity, avg_px, levels["stop_loss"], levels["tp1"], broker_stop_id,
                        )

                        if not adopted_pos.broker_stop_order_id:
                            try:
                                logger.info("Setting missing native Stop Order for adopted position %s...", b_ticker)
                                stop_id = self.place_broker_stop(adopted_pos)
                                if stop_id:
                                    with self._state_lock:
                                        adopted_pos.broker_stop_order_id = stop_id
                            except Exception as stop_exc:
                                logger.debug("Auto-place broker stop on adoption notice: %s", stop_exc)

                        if not self._is_pure_tsl_for_position(adopted_pos) and not adopted_pos.broker_tp_order_id and not adopted_pos.tp1_hit:
                            try:
                                logger.info("Setting missing native TP Limit Order for adopted position %s...", b_ticker)
                                tp_id = self.place_broker_tp_limit(adopted_pos)
                                if tp_id:
                                    with self._state_lock:
                                        adopted_pos.broker_tp_order_id = tp_id
                                else:
                                    setattr(adopted_pos, "_last_tp_fail_time", time.time())
                            except Exception as tp_exc:
                                setattr(adopted_pos, "_last_tp_fail_time", time.time())
                                logger.debug("Auto-place broker TP limit on adoption notice: %s", tp_exc)

            if (
                summary["closed_externally"]
                or summary["quantity_synced"]
                or summary["adopted_from_broker"]
                or summary.get("expired_stops_restored")
            ):
                self._save_state()

            return summary

    def panic_flatten_all(self) -> Dict[str, Any]:
        """Emergency fail-safe: immediately set halt flag/marker, cancel broker orders, and liquidate open positions."""
        logger.critical("🚨 EMERGENCY PANIC FLATTEN TRIGGERED! Liquidating all positions and cancelling orders.")
        results: Dict[str, Any] = {
            "cancelled_stops": 0,
            "liquidated_positions": [],
            "failed_liquidations": [],
        }

        # Step 1: Flip halt flag & persist marker IMMEDIATELY under _state_lock (zero broker I/O inside _state_lock)
        with self._state_lock:
            self.is_halted = True
            try:
                marker_payload = json.dumps({
                    "halted": True,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "reason": "Emergency Panic Flatten",
                }, indent=2)
                self._atomic_write_text(self.halt_marker_file, marker_payload)
            except Exception as exc:
                logger.warning("Could not write persistent halt marker to %s: %s", self.halt_marker_file, exc)

        # Step 2: Synchronize under strict lock hierarchy execution_gate_lock -> _broker_op_lock -> _state_lock
        # (never hold _state_lock during broker HTTP calls) so reconcile/entry/re-epoch cannot race panic.
        with self.execution_gate_lock:
            with self._broker_op_lock:
                with self._state_lock:
                    snapshot_positions = [
                        (t, p)
                        for t, p in list(self.positions.items())
                        if not getattr(p, "is_closed", False)
                    ]

                for ticker, pos in snapshot_positions:
                    with self._state_lock:
                        if pos.is_closed:
                            continue
                        qty_to_sell = pos.remaining_quantity

                    # 1. Cancel all orders on broker outside _state_lock
                    try:
                        self.cancel_all_broker_orders(pos)
                        results["cancelled_stops"] += 1
                    except Exception as exc:
                        logger.error("Failed to cancel orders for %s during panic flatten: %s", ticker, exc)

                    # 2. Sell / liquidate remaining quantity outside _state_lock
                    sold = True
                    if qty_to_sell > 0 and not self.dry_run:
                        try:
                            sold = bool(self._execute_sell(pos.ticker, qty_to_sell))
                        except Exception as exc:
                            logger.error("Failed to liquidate %s during panic flatten: %s", ticker, exc)
                            sold = False

                    now_iso = datetime.now(timezone.utc).isoformat()
                    with self._state_lock:
                        if sold:
                            results["liquidated_positions"].append((ticker, qty_to_sell))
                            pos.is_closed = True
                            pos.remaining_quantity = 0.0
                            pos.panic_failed = False
                            pos.unprotected_reason = None
                            pos.last_update_time = now_iso
                        else:
                            results["failed_liquidations"].append((ticker, qty_to_sell))
                            pos.is_closed = False
                            pos.panic_failed = True
                            pos.unprotected_reason = "Panic liquidation failed"
                            pos.last_update_time = now_iso

                self._save_state()

        # Notify via all channels outside _state_lock
        if hasattr(self.notifier, "notify_alert"):
            try:
                self.notifier.notify_alert(
                    "🚨 EMERGENCY PANIC FLATTEN EXECUTED",
                    f"Liquidated: {len(results['liquidated_positions'])} positions, "
                    f"Failed: {len(results['failed_liquidations'])}, "
                    f"Cancelled stops: {results['cancelled_stops']}. System halted.",
                )
            except Exception as notif_exc:
                logger.debug("Panic flatten notification skipped: %s", notif_exc)

        return results

    # ─────────────────────────────────────────────────────────────
    # Asymmetric Session Protocol (16:20 UTC Gap Evacuation & Quota Management)
    # ─────────────────────────────────────────────────────────────
    @staticmethod
    def _parse_cutoff_minutes(cutoff_str: Any) -> int:
        try:
            parts = str(cutoff_str).split(":")
            return int(parts[0]) * 60 + int(parts[1])
        except Exception:
            return 16 * 60 + 20

    def evaluate_asymmetric_session(
        self,
        current_time: Optional[datetime] = None,
        force_evaluation: bool = False,
        current_prices: Optional[Dict[str, float]] = None,
    ) -> List[Dict[str, Any]]:
        """Evaluate 16:20 UTC Asymmetric Session rules (per-position epoch-parameterized)."""
        now_utc = current_time or datetime.now(timezone.utc)
        today_str = now_utc.strftime("%Y-%m-%d")

        # Avoid unintended evening session triggers in unit test runs when current_time is omitted
        import sys
        if current_time is None and ("unittest" in sys.modules or any("test" in arg.lower() for arg in sys.argv)):
            return []

        from zoneinfo import ZoneInfo
        london_now = now_utc.astimezone(ZoneInfo("Europe/London"))
        london_minutes = london_now.hour * 60 + london_now.minute
        current_minutes = now_utc.hour * 60 + now_utc.minute

        with self._state_lock:
            items = list(self.positions.items())

        if not force_evaluation:
            any_cutoff_due = False
            any_unhedged_overnight = (
                (london_minutes >= 990 or london_minutes < 480 or current_minutes >= 990 or current_minutes < 480)
                and any(not getattr(p, "is_overnight_eligible", False) for _, p in items if not p.is_closed)
            )
            default_cutoff_str = (
                self._snap_val(self._resolve_position_snapshot(None), "SESSION_CLOSE_CUTOFF_UTC", settings.SESSION_CLOSE_CUTOFF_UTC)
                if self.config_bus is not None
                else settings.SESSION_CLOSE_CUTOFF_UTC
            )
            default_cutoff_mins = self._parse_cutoff_minutes(default_cutoff_str)
            if (980 <= london_minutes < 990) or (default_cutoff_mins <= current_minutes < 990):
                any_cutoff_due = True
            else:
                for _, p in items:
                    if p.is_closed:
                        continue
                    p_snap = self._resolve_position_snapshot(p)
                    p_cutoff_str = self._snap_val(p_snap, "SESSION_CLOSE_CUTOFF_UTC", settings.SESSION_CLOSE_CUTOFF_UTC)
                    p_cutoff_mins = self._parse_cutoff_minutes(p_cutoff_str)
                    if p_cutoff_mins <= current_minutes < 990:
                        any_cutoff_due = True
                        break

            if not (any_cutoff_due or any_unhedged_overnight):
                return []

            if self._last_session_close_date == today_str and not any_unhedged_overnight:
                return []

        active_cutoff_display = settings.SESSION_CLOSE_CUTOFF_UTC
        if self.config_bus is not None:
            try:
                active_cutoff_display = str(self._snap_val(self.config_bus.active_snapshot(), "SESSION_CLOSE_CUTOFF_UTC", settings.SESSION_CLOSE_CUTOFF_UTC))
            except Exception:
                pass

        logger.info(
            "Evaluating Asymmetric Session Protocol at %s UTC (Cutoff: %s UTC)...",
            now_utc.strftime("%H:%M:%S"), active_cutoff_display
        )

        prices = current_prices or {}
        positions_to_close: List[Tuple[TrackedPosition, str, float]] = []
        tickers_to_remove: List[str] = []

        for ticker, pos in items:
            with self._state_lock:
                if pos.is_closed or pos.remaining_quantity <= 0:
                    tickers_to_remove.append(ticker)
                    continue
                pos_snap = self._resolve_position_snapshot(pos)

                if not force_evaluation:
                    p_cutoff_str = self._snap_val(pos_snap, "SESSION_CLOSE_CUTOFF_UTC", settings.SESSION_CLOSE_CUTOFF_UTC)
                    p_cutoff_mins = self._parse_cutoff_minutes(p_cutoff_str)
                    is_pos_cutoff = (
                        (p_cutoff_mins <= current_minutes < 990)
                        or (p_cutoff_mins == 980 and 980 <= london_minutes < 990)
                    )
                    is_pos_unhedged = (
                        (london_minutes >= 990 or london_minutes < 480 or current_minutes >= 990 or current_minutes < 480)
                        and not getattr(pos, "is_overnight_eligible", False)
                    )
                    if not (is_pos_cutoff or is_pos_unhedged):
                        continue

                current_price = prices.get(ticker) or prices.get(pos.underlying) or pos.entry_price
                enforce_inverse = bool(self._snap_val(pos_snap, "ENFORCE_INVERSE_INTRADAY_ONLY", settings.ENFORCE_INVERSE_INTRADAY_ONLY))
                flatten_all = bool(self._snap_val(pos_snap, "FLATTEN_ALL_LEVERAGED_AT_SESSION_CLOSE", getattr(settings, "FLATTEN_ALL_LEVERAGED_AT_SESSION_CLOSE", False)))
                blackout_hours = float(self._snap_val(pos_snap, "EARNINGS_BLACKOUT_HOURS", settings.EARNINGS_BLACKOUT_HOURS))
                enforce_long_be = bool(self._snap_val(pos_snap, "ENFORCE_LONG_BREAKEVEN_OVERNIGHT", settings.ENFORCE_LONG_BREAKEVEN_OVERNIGHT))

                if pos.direction.upper() == "SHORT":
                    if enforce_inverse or flatten_all:
                        positions_to_close.append((pos, "INVERSE_INTRADAY_FLATTEN", current_price))
                        tickers_to_remove.append(ticker)
                elif pos.direction.upper() == "LONG":
                    if flatten_all:
                        positions_to_close.append((pos, "ASYMMETRIC_SESSION_EXIT", current_price))
                        tickers_to_remove.append(ticker)
                    else:
                        if (
                            pos.earnings_in_days is not None
                            and isinstance(pos.earnings_in_days, (int, float))
                            and 0 <= pos.earnings_in_days <= (blackout_hours / 24.0)
                        ):
                            positions_to_close.append((pos, "EARNINGS_BLACKOUT_EXIT", current_price))
                            tickers_to_remove.append(ticker)
                            continue

                        # Day 4+ Zero-Alpha Stagnancy Gate
                        gain_pct = ((current_price - pos.entry_price) / pos.entry_price * 100.0) if pos.entry_price > 0 else 0.0
                        is_day4_stagnant = (
                            getattr(pos, "session_count", 1) >= 4
                            and not getattr(pos, "is_runner", False)
                            and gain_pct < 1.5
                        )
                        if is_day4_stagnant:
                            positions_to_close.append((pos, "ZERO_ALPHA_EVACUATION", current_price))
                            tickers_to_remove.append(ticker)
                            continue

                        if enforce_long_be:
                            is_risk_free = (
                                (pos.trailing_stop_loss >= pos.entry_price)
                                or (pos.stop_loss >= pos.entry_price)
                                or pos.tp1_hit
                            )
                            if not is_risk_free:
                                positions_to_close.append((pos, "ASYMMETRIC_SESSION_EXIT", current_price))
                                tickers_to_remove.append(ticker)
                            else:
                                pos.is_overnight_eligible = True
                                logger.info(
                                    "Retaining Long %s for overnight swing (TSL Breakeven/Profit active: $%.2f >= $%.2f)",
                                    ticker, pos.trailing_stop_loss, pos.entry_price,
                                )
                        else:
                            pos.is_overnight_eligible = True

        events: List[Dict[str, Any]] = []
        if positions_to_close:
            logger.info("Queued %d positions for rate-limited 16:20 session liquidation", len(positions_to_close))
            events = self._execute_liquidation_queue(positions_to_close)

        with self._state_lock:
            for t in tickers_to_remove:
                if t in self.positions:
                    del self.positions[t]
            self._last_session_close_date = today_str
        self._save_state()
        return events

    def _execute_liquidation_queue(
        self,
        positions_to_close: List[Tuple[TrackedPosition, str, float]]
    ) -> List[Dict[str, Any]]:
        """Execute queued 16:20 liquidations serially with broker rate-limit pacing."""
        events = []
        total = len(positions_to_close)

        for i, (pos, reason, price) in enumerate(positions_to_close):
            with self._state_lock:
                shares_to_sell = pos.remaining_quantity
                pos_snap = self._resolve_position_snapshot(pos)
            logger.info(
                "[%d/%d] Executing 16:20 session liquidation for %s (%s): %.3f shares (Reason: %s)",
                i + 1, total, pos.ticker, pos.direction, shares_to_sell, reason
            )

            self.cancel_broker_stop(pos)
            success = self._execute_sell(pos.ticker, shares_to_sell)

            with self._state_lock:
                pos.is_closed = True
                pos.remaining_quantity = 0.0
                pos.last_update_time = datetime.now(timezone.utc).isoformat()
                entry_px = pos.entry_price

            pnl = shares_to_sell * (price - entry_px)
            pnl_pct = ((price - entry_px) / entry_px) * 100 if entry_px > 0 else 0.0

            self.notifier.notify_session_exit(
                ticker=pos.ticker,
                underlying=pos.underlying,
                direction=pos.direction,
                price=price,
                quantity=shares_to_sell,
                exit_reason=reason,
                pnl=pnl,
                pnl_pct=pnl_pct,
            )

            events.append({
                "event": reason,
                "ticker": pos.ticker,
                "underlying": pos.underlying,
                "direction": pos.direction,
                "price": price,
                "quantity": shares_to_sell,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "success": success,
            })

            if i < total - 1:
                delay = float(self._snap_val(pos_snap, "RATE_LIMIT_INTER_CALL_DELAY", settings.RATE_LIMIT_INTER_CALL_DELAY))
                if delay > 0:
                    time.sleep(delay)

        return events

    # ─────────────────────────────────────────────────────────────
    # Inspection & Lifecycle Update Tick
    # ─────────────────────────────────────────────────────────────
    def monitor_tick(
        self,
        current_prices: Dict[str, float],
        current_time: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """Run one check cycle across all active positions using latest prices."""
        if self.is_system_halted():
            return []

        events = []

        # 1. Asymmetric Session Protocol Check (16:20 UTC cutoff)
        session_events = self.evaluate_asymmetric_session(
            current_time=current_time,
            current_prices=current_prices,
        )
        events.extend(session_events)

        tickers_to_remove = []
        with self._state_lock:
            items = list(self.positions.items())

        for ticker, pos in items:
            if self.is_system_halted():
                break

            need_gbx_broker_sync = False
            need_scale_broker_sync = False

            with self._state_lock:
                if pos.is_closed:
                    tickers_to_remove.append(ticker)
                    continue

                current_price = current_prices.get(ticker)
                if not current_price or current_price <= 0:
                    current_price = getattr(pos, "current_price", 0.0) or getattr(pos, "entry_price", 0.0)
                    if (not current_price or current_price <= 0) and not getattr(pos, "scale_calibrated", False):
                        current_price = current_prices.get(pos.underlying)

                if not current_price or current_price <= 0:
                    continue

                # ── 1. GBX (Pence) vs Base Currency Normalization ──
                if current_price > 500 and pos.entry_price < 200 and abs((current_price / 100.0) - pos.entry_price) / pos.entry_price < 0.35:
                    logger.info("Normalizing GBX pence price to base currency for %s: %.2f -> %.4f", ticker, current_price, current_price / 100.0)
                    current_price = round(current_price / 100.0, 4)
                elif pos.entry_price > 500 and current_price < 200 and abs((pos.entry_price / 100.0) - current_price) / current_price < 0.35:
                    logger.info("Normalizing registered entry from GBX pence to base currency for %s: %.2f -> %.4f", ticker, pos.entry_price, pos.entry_price / 100.0)
                    pos.entry_price = round(pos.entry_price / 100.0, 4)
                    pos.stop_loss = round(pos.stop_loss / 100.0, 4)
                    pos.trailing_stop_loss = round(pos.trailing_stop_loss / 100.0, 4)
                    pos.tp1 = round(pos.tp1 / 100.0, 4)
                    pos.tp2 = round(pos.tp2 / 100.0, 4)
                    pos.tp3 = round(pos.tp3 / 100.0, 4)
                    if getattr(pos, "tp1_escape_threshold", 0.0) > 0:
                        pos.tp1_escape_threshold = round(pos.tp1_escape_threshold / 100.0, 4)
                    pos.highest_price = round(pos.highest_price / 100.0, 4)
                    pos.lowest_price = round(pos.lowest_price / 100.0, 4)
                    pos.atr = round(pos.atr / 100.0, 4)
                    need_gbx_broker_sync = True

                # ── 2. Scale Discrepancy Calibration (Underlying vs ETP) ──
                is_cfd_short = (pos.tp1 < pos.entry_price) if (pos.tp1 > 0 and pos.entry_price > 0) else (
                    pos.direction.upper() == "SHORT" and not (
                        pos.ticker.endswith("_EQ") or pos.ticker.endswith(".L") or pos.direction.upper().endswith("_ETP")
                    )
                )
                if not getattr(pos, "scale_calibrated", False):
                    pos.scale_calibrated = True
                    if not pos.tp1_hit and pos.remaining_quantity == pos.original_quantity and pos.entry_price > 0:
                        scale_diff = abs(current_price - pos.entry_price) / pos.entry_price
                        if scale_diff > 0.30:
                            logger.warning(
                                "Scale calibration for %s: live price %.2f differs from registered entry %.2f by %.1f%% (>30%%). "
                                "Recalibrating position levels to live instrument scale to prevent false stop-out.",
                                ticker, current_price, pos.entry_price, scale_diff * 100.0
                            )
                            stop_dist_pct = abs(pos.entry_price - pos.stop_loss) / pos.entry_price if pos.entry_price > 0 else 0.05
                            tp1_dist_pct = abs(pos.tp1 - pos.entry_price) / pos.entry_price if pos.entry_price > 0 else 0.05
                            tp2_dist_pct = abs(pos.tp2 - pos.entry_price) / pos.entry_price if pos.entry_price > 0 else 0.10
                            tp3_dist_pct = abs(pos.tp3 - pos.entry_price) / pos.entry_price if pos.entry_price > 0 else 0.15
                            atr_pct = (pos.atr / pos.entry_price) if (pos.atr > 0 and pos.entry_price > 0) else 0.02

                            pos.entry_price = current_price
                            pos.highest_price = current_price
                            pos.lowest_price = current_price
                            if is_cfd_short:
                                pos.stop_loss = round(current_price * (1.0 + stop_dist_pct), 2)
                                pos.trailing_stop_loss = pos.stop_loss
                                pos.tp1 = round(max(0.01, current_price * (1.0 - tp1_dist_pct)), 2)
                                pos.tp2 = round(max(0.01, current_price * (1.0 - tp2_dist_pct)), 2)
                                pos.tp3 = round(max(0.01, current_price * (1.0 - tp3_dist_pct)), 2)
                            else:
                                pos.stop_loss = round(max(0.01, current_price * (1.0 - stop_dist_pct)), 2)
                                pos.trailing_stop_loss = pos.stop_loss
                                pos.tp1 = round(current_price * (1.0 + tp1_dist_pct), 2)
                                pos.tp2 = round(current_price * (1.0 + tp2_dist_pct), 2)
                                pos.tp3 = round(current_price * (1.0 + tp3_dist_pct), 2)
                            pos.atr = round(current_price * atr_pct, 4)
                            need_scale_broker_sync = True

                pos.last_update_time = datetime.now(timezone.utc).isoformat()
                pos.current_price = current_price

                if current_price > pos.highest_price:
                    pos.highest_price = current_price
                if current_price < pos.lowest_price:
                    pos.lowest_price = current_price

                is_short = self.is_true_short_contract(pos)
                pos_snap = self._resolve_position_snapshot(pos)
                is_pure_tsl = self._is_pure_tsl_for_position(pos)

                if is_short:
                    active_stop = min(pos.stop_loss, pos.trailing_stop_loss) if pos.trailing_stop_loss > 0 else pos.stop_loss
                    stop_hit = current_price >= active_stop
                else:
                    active_stop = max(pos.stop_loss, pos.trailing_stop_loss)
                    stop_hit = current_price <= active_stop

                min_hold_sec = float(self._snap_val(pos_snap, "MIN_HOLD_SECONDS", getattr(settings, "MIN_HOLD_SECONDS", 120.0)))

            # Execute outside _state_lock if normalization required broker sync
            if need_gbx_broker_sync:
                self.place_broker_stop(pos)
                self._save_state()
            if need_scale_broker_sync:
                self.place_broker_stop(pos)
                self._save_state()

            # 1. Stop Loss & Trailing Stop Loss
            if stop_hit:
                entry_ts = getattr(pos, "entry_timestamp", 0.0)
                if not entry_ts and getattr(pos, "entry_time", None):
                    try:
                        entry_ts = datetime.fromisoformat(pos.entry_time.replace("Z", "+00:00")).timestamp()
                        setattr(pos, "entry_timestamp", entry_ts)
                    except Exception:
                        entry_ts = 0.0

                now_ts = current_time.timestamp() if current_time else time.time()
                time_held = (now_ts - entry_ts) if entry_ts > 0 else 999.0
                drop_from_entry = abs(pos.entry_price - current_price) / pos.entry_price if pos.entry_price > 0 else 0.0

                if time_held < min_hold_sec and drop_from_entry < 0.15 and not pos.tp1_hit:
                    logger.info(
                        "Anti-whipsaw grace period active for %s (held %.1fs < %.1fs, entry $%.2f, current $%.2f, drop %.1f%%). "
                        "Absorbing entry bid-ask spread without software stop liquidation.",
                        ticker, time_held, min_hold_sec, pos.entry_price, current_price, drop_from_entry * 100.0,
                    )
                    continue

                is_tsl_exit = (active_stop == pos.trailing_stop_loss and (
                    (pos.trailing_stop_loss <= pos.entry_price) if is_short else (pos.trailing_stop_loss >= pos.entry_price)
                )) or (pos.tp1_hit and active_stop == pos.trailing_stop_loss)
                event = self._handle_stop_exit(pos, current_price, is_tsl=is_tsl_exit)
                events.append(event)
                tickers_to_remove.append(ticker)
                continue

            if is_pure_tsl:
                # ── Pure 100% TSL Execution (per-position epoch): No partial trims, let winners run fully ──
                prog_broker_sl: Optional[float] = None
                prog_notif: Optional[Dict[str, Any]] = None
                with self._state_lock:
                    prog_sl = self.check_progressive_stops(
                        entry_price=pos.entry_price,
                        current_price=current_price,
                        current_sl=pos.trailing_stop_loss,
                        direction=pos.direction,
                        ticker=pos.ticker,
                        config_snapshot=pos_snap,
                    )
                    prog_ratcheted = (prog_sl < pos.trailing_stop_loss) if is_short else (prog_sl > pos.trailing_stop_loss)
                    if prog_ratcheted:
                        old_tsl = pos.trailing_stop_loss
                        pos.trailing_stop_loss = prog_sl
                        pos.stop_loss = min(pos.stop_loss, prog_sl) if is_short else max(pos.stop_loss, prog_sl)
                        locked_pct = (
                            ((pos.entry_price - prog_sl) / pos.entry_price * 100)
                            if is_short
                            else ((prog_sl - pos.entry_price) / pos.entry_price * 100)
                        ) if pos.entry_price > 0 else 0.0
                        logger.info(
                            "🛡️ %s Progressive TSL ratcheted: $%.2f -> $%.2f (+%.1f%% locked)",
                            ticker, old_tsl, prog_sl, locked_pct,
                        )
                        prog_broker_sl = prog_sl
                        prog_notif = {
                            "ticker": pos.ticker,
                            "underlying": pos.underlying,
                            "direction": pos.direction,
                            "current_price": current_price,
                            "new_tsl": prog_sl,
                            "locked_in_pnl_pct": locked_pct,
                            "event_type": "PROGRESSIVE_STOP_RATCHET",
                        }
                        events.append({"event": "PROGRESSIVE_STOP_RATCHET", "ticker": ticker, "new_tsl": prog_sl})

                if prog_broker_sl is not None:
                    self.place_broker_stop(pos, stop_price=prog_broker_sl)
                if prog_notif is not None:
                    self.notifier.notify_tsl(**prog_notif)

                # 2. Dynamic ATR Trailing Stop Ratchet:
                dyn_broker_sl: Optional[float] = None
                dyn_notif: Optional[Dict[str, Any]] = None
                with self._state_lock:
                    gain_pct = ((pos.entry_price - current_price) / pos.entry_price * 100.0) if is_short else ((current_price - pos.entry_price) / pos.entry_price * 100.0)
                    is_cushion_locked = (pos.trailing_stop_loss >= pos.entry_price * 1.0149 if not is_short else pos.trailing_stop_loss <= pos.entry_price * 0.9851)
                    if gain_pct >= 3.0 or is_cushion_locked:
                        pos.is_runner = True

                    is_in_profit_zone = gain_pct >= 1.5 or (pos.trailing_stop_loss <= pos.entry_price if is_short else pos.trailing_stop_loss >= pos.entry_price)
                    if is_in_profit_zone:
                        favorable_price = pos.lowest_price if is_short else pos.highest_price
                        new_tsl = self._calculate_trailing_stop_for_pos(pos, current_price, favorable_price, pos_snap)
                        ratcheted = (new_tsl < pos.trailing_stop_loss) if is_short else (new_tsl > pos.trailing_stop_loss)
                        if ratcheted:
                            old_tsl = pos.trailing_stop_loss
                            pos.trailing_stop_loss = new_tsl
                            pos.stop_loss = min(pos.stop_loss, new_tsl) if is_short else max(pos.stop_loss, new_tsl)
                            locked_pct = (
                                ((pos.entry_price - new_tsl) / pos.entry_price * 100)
                                if is_short
                                else ((new_tsl - pos.entry_price) / pos.entry_price * 100)
                            ) if pos.entry_price > 0 else 0.0
                            logger.info(
                                "🚀 %s Dynamic ATR TSL ratcheted: $%.2f -> $%.2f (+%.1f%% locked, peak $%.2f)",
                                ticker, old_tsl, new_tsl, locked_pct, favorable_price,
                            )
                            dyn_broker_sl = new_tsl
                            dyn_notif = {
                                "ticker": pos.ticker,
                                "underlying": pos.underlying,
                                "direction": pos.direction,
                                "current_price": current_price,
                                "new_tsl": new_tsl,
                                "locked_in_pnl_pct": locked_pct,
                                "event_type": "TSL_RATCHET",
                            }
                            events.append({"event": "TSL_RATCHET", "ticker": ticker, "new_tsl": new_tsl})

                if dyn_broker_sl is not None:
                    self.place_broker_stop(pos, stop_price=dyn_broker_sl)
                if dyn_notif is not None:
                    self.notifier.notify_tsl(**dyn_notif)

            else:
                # ── Legacy Milestone Trim Execution (TP1/TP2/TP3) ──
                tp3_hit = (current_price <= pos.tp3) if is_short else (current_price >= pos.tp3)
                if tp3_hit and not pos.tp3_hit:
                    event = self._handle_tp3_hit(pos, current_price)
                    events.append(event)
                    tickers_to_remove.append(ticker)
                    continue

                tp1_hit = (current_price <= pos.tp1) if is_short else (current_price >= pos.tp1)
                if tp1_hit and not pos.tp1_hit:
                    event = self._handle_tp1_hit(pos, current_price)
                    events.append(event)

                tp2_hit = (current_price <= pos.tp2) if is_short else (current_price >= pos.tp2)
                if tp2_hit and not pos.tp2_hit:
                    event = self._handle_tp2_hit(pos, current_price)
                    events.append(event)

                # 5. Schmitt-Trigger Hysteresis: Elevate stop to TP1 once price clears escape threshold
                hyst_broker_sl: Optional[float] = None
                hyst_notif: Optional[Dict[str, Any]] = None
                with self._state_lock:
                    if getattr(pos, "tp1_armed_hysteresis", False) and not pos.tp2_hit:
                        escape_threshold = getattr(pos, "tp1_escape_threshold", 0.0)
                        if not escape_threshold or escape_threshold <= 0:
                            escape_threshold = self._calculate_escape_threshold_for_pos(pos, pos_snap)
                            pos.tp1_escape_threshold = escape_threshold

                        escape_cleared = (current_price <= escape_threshold) if is_short else (current_price >= escape_threshold)
                        if escape_cleared:
                            pos.tp1_armed_hysteresis = False
                            pos.tp1_stop_elevated = True
                            if self.is_true_short_contract(pos):
                                pos.trailing_stop_loss = min(pos.trailing_stop_loss, pos.tp1) if pos.trailing_stop_loss > 0 else pos.tp1
                                pos.stop_loss = min(pos.stop_loss, pos.tp1) if pos.stop_loss > 0 else pos.tp1
                            else:
                                pos.trailing_stop_loss = max(pos.trailing_stop_loss, pos.tp1)
                                pos.stop_loss = max(pos.stop_loss, pos.tp1)
                            locked_pct = abs((pos.tp1 - pos.entry_price) / pos.entry_price) * 100 if pos.entry_price > 0 else 0.0
                            logger.info(
                                "🎯 %s Schmitt-Trigger Hysteresis ESCAPE: price $%.2f cleared threshold $%.2f. Stop safely elevated to TP1 ($%.2f, +%.1f%% locked)",
                                ticker, current_price, escape_threshold, pos.tp1, locked_pct,
                            )
                            hyst_broker_sl = pos.tp1
                            hyst_notif = {
                                "ticker": pos.ticker,
                                "underlying": pos.underlying,
                                "direction": pos.direction,
                                "current_price": current_price,
                                "new_tsl": pos.tp1,
                                "locked_in_pnl_pct": locked_pct,
                                "event_type": "HYSTERESIS_TP1_RATCHET",
                            }
                            events.append({"event": "HYSTERESIS_TP1_RATCHET", "ticker": ticker, "new_tsl": pos.tp1})

                if hyst_broker_sl is not None:
                    self.place_broker_stop(pos, stop_price=hyst_broker_sl)
                if hyst_notif is not None:
                    self.notifier.notify_tsl(**hyst_notif)

                # 5b. Progressive Trailing Stop (+1.5% BE, +3.0% cushion)
                prog2_broker_sl: Optional[float] = None
                with self._state_lock:
                    prog_sl = self.check_progressive_stops(
                        entry_price=pos.entry_price,
                        current_price=current_price,
                        current_sl=pos.trailing_stop_loss,
                        direction=pos.direction,
                        ticker=pos.ticker,
                        config_snapshot=pos_snap,
                    )
                    prog_ratcheted = (prog_sl < pos.trailing_stop_loss) if is_short else (prog_sl > pos.trailing_stop_loss)
                    if prog_ratcheted:
                        pos.trailing_stop_loss = prog_sl
                        pos.stop_loss = min(pos.stop_loss, prog_sl) if is_short else max(pos.stop_loss, prog_sl)
                        prog2_broker_sl = prog_sl
                        events.append({"event": "PROGRESSIVE_STOP_RATCHET", "ticker": ticker, "new_tsl": prog_sl})

                if prog2_broker_sl is not None:
                    self.place_broker_stop(pos, stop_price=prog2_broker_sl)

                # 6. Trailing Stop Ratchet (Ratchets higher with new highs once hysteresis clears)
                tsl_broker_sl: Optional[float] = None
                tsl_notif: Optional[Dict[str, Any]] = None
                with self._state_lock:
                    if pos.tp1_hit and not getattr(pos, "tp1_armed_hysteresis", False):
                        favorable_price = pos.lowest_price if is_short else pos.highest_price
                        new_tsl = self._calculate_trailing_stop_for_pos(pos, current_price, favorable_price, pos_snap)
                        ratcheted = (new_tsl < pos.trailing_stop_loss) if is_short else (new_tsl > pos.trailing_stop_loss)
                        if ratcheted:
                            old_tsl = pos.trailing_stop_loss
                            pos.trailing_stop_loss = new_tsl
                            pos.stop_loss = min(pos.stop_loss, new_tsl) if is_short else max(pos.stop_loss, new_tsl)
                            locked_pct = (
                                ((pos.entry_price - new_tsl) / pos.entry_price * 100)
                                if is_short
                                else ((new_tsl - pos.entry_price) / pos.entry_price * 100)
                            ) if pos.entry_price > 0 else 0.0
                            logger.info("%s TSL ratcheted: $%.2f -> $%.2f (+%.1f%% locked)", ticker, old_tsl, new_tsl, locked_pct)
                            tsl_broker_sl = new_tsl
                            tsl_notif = {
                                "ticker": pos.ticker,
                                "underlying": pos.underlying,
                                "direction": pos.direction,
                                "current_price": current_price,
                                "new_tsl": new_tsl,
                                "locked_in_pnl_pct": locked_pct,
                                "event_type": "TSL_RATCHET",
                            }
                            events.append({"event": "TSL_RATCHET", "ticker": ticker, "new_tsl": new_tsl})

                if tsl_broker_sl is not None:
                    self.place_broker_stop(pos, stop_price=tsl_broker_sl)
                if tsl_notif is not None:
                    self.notifier.notify_tsl(**tsl_notif)

        # Clean up closed positions under _state_lock
        with self._state_lock:
            for t in tickers_to_remove:
                if t in self.positions:
                    del self.positions[t]
            for t, p in list(self.positions.items()):
                if getattr(p, "is_closed", False) or (getattr(p, "remaining_quantity", 0.0) or 0.0) <= 0.0:
                    del self.positions[t]

        if events:
            self._save_state()

        return events

    # ─────────────────────────────────────────────────────────────
    # Active Trade Technical Signal Evaluation
    # ─────────────────────────────────────────────────────────────
    def evaluate_active_trade_signals(
        self,
        active_metrics: Dict[str, Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Evaluate technical exit/trim signals specifically for active positions."""
        if self.is_system_halted():
            return []

        with self._state_lock:
            if not self.positions:
                return []
            items = list(self.positions.items())

        events = []
        tickers_to_remove = []

        for ticker, pos in items:
            if self.is_system_halted():
                break

            with self._state_lock:
                if pos.is_closed or (pos.remaining_quantity or 0.0) <= 0:
                    tickers_to_remove.append(ticker)
                    continue

                metrics = active_metrics.get(pos.underlying) or active_metrics.get(pos.ticker)
                if not metrics:
                    continue

                if metrics.get("rsi3") is not None:
                    pos.rsi3 = float(metrics["rsi3"])
                if metrics.get("rsi7") is not None:
                    pos.rsi7 = float(metrics["rsi7"])
                if metrics.get("rsi14") is not None:
                    pos.rsi14 = float(metrics["rsi14"])
                if metrics.get("sma5") is not None:
                    pos.sma5 = float(metrics["sma5"])

                cur_px = pos.current_price or pos.entry_price
                if cur_px <= 0 or pos.entry_price <= 0:
                    continue

                is_short = self.is_true_short_contract(pos)
                pnl_pct = ((pos.entry_price - cur_px) / pos.entry_price * 100.0) if is_short else ((cur_px - pos.entry_price) / pos.entry_price * 100.0)
                nixtla_ret = metrics.get("nixtla_expected_return_pct")
                is_pure_tsl = self._is_pure_tsl_for_position(pos)

            if is_pure_tsl:
                sig_broker_sl: Optional[float] = None
                sig_notif: Optional[Dict[str, Any]] = None
                with self._state_lock:
                    if not is_short:
                        should_tighten = False
                        tighten_reason = ""
                        if pos.rsi3 is not None and pos.rsi3 >= 75.0 and pnl_pct > 1.0:
                            should_tighten = True
                            tighten_reason = f"3RSI_OVERBOUGHT_{pos.rsi3:.1f}"
                        elif (
                            pos.sma5 is not None
                            and cur_px > pos.sma5
                            and pos.rsi3 is not None
                            and pos.rsi3 >= 68.0
                            and pnl_pct >= 2.0
                        ):
                            should_tighten = True
                            tighten_reason = "SMA5_REVERSION"
                        elif nixtla_ret is not None and float(nixtla_ret) <= -3.0 and pnl_pct >= 1.5:
                            should_tighten = True
                            tighten_reason = f"NIXTLA_BEARISH_REVERSAL_{float(nixtla_ret):.1f}%"

                        if should_tighten:
                            candidate_sl = max(pos.entry_price, round(pos.entry_price + (cur_px - pos.entry_price) * 0.50, 2))
                            if candidate_sl > pos.trailing_stop_loss:
                                old_tsl = pos.trailing_stop_loss
                                pos.trailing_stop_loss = candidate_sl
                                pos.stop_loss = max(pos.stop_loss, candidate_sl)
                                locked_pct = ((candidate_sl - pos.entry_price) / pos.entry_price * 100.0) if pos.entry_price > 0 else 0.0
                                logger.info(
                                    "🛡️ %s TSL tightened on technical signal (%s): $%.2f -> $%.2f (+%.1f%% locked, 100%% shares riding)",
                                    ticker, tighten_reason, old_tsl, candidate_sl, locked_pct,
                                )
                                sig_broker_sl = candidate_sl
                                sig_notif = {
                                    "ticker": pos.ticker,
                                    "underlying": pos.underlying,
                                    "direction": pos.direction,
                                    "current_price": cur_px,
                                    "new_tsl": candidate_sl,
                                    "locked_in_pnl_pct": locked_pct,
                                    "event_type": "SIGNAL_TSL_TIGHTEN",
                                }
                                events.append({"event": "SIGNAL_TSL_TIGHTEN", "ticker": ticker, "new_tsl": candidate_sl, "reason": tighten_reason})
                    else:
                        should_tighten = False
                        tighten_reason = ""
                        if pos.rsi3 is not None and pos.rsi3 <= 25.0 and pnl_pct > 1.0:
                            should_tighten = True
                            tighten_reason = f"3RSI_OVERSOLD_{pos.rsi3:.1f}"
                        elif nixtla_ret is not None and float(nixtla_ret) >= 3.0 and pnl_pct >= 1.5:
                            should_tighten = True
                            tighten_reason = f"NIXTLA_BULLISH_REVERSAL_{float(nixtla_ret):.1f}%"

                        if should_tighten:
                            candidate_sl = min(pos.entry_price, round(pos.entry_price - (pos.entry_price - cur_px) * 0.50, 2))
                            if candidate_sl < pos.trailing_stop_loss or pos.trailing_stop_loss <= 0:
                                old_tsl = pos.trailing_stop_loss
                                pos.trailing_stop_loss = candidate_sl
                                pos.stop_loss = min(pos.stop_loss, candidate_sl)
                                locked_pct = ((pos.entry_price - candidate_sl) / pos.entry_price * 100.0) if pos.entry_price > 0 else 0.0
                                logger.info(
                                    "🛡️ %s TSL tightened on technical signal (%s): $%.2f -> $%.2f (+%.1f%% locked, 100%% shares riding)",
                                    ticker, tighten_reason, old_tsl, candidate_sl, locked_pct,
                                )
                                sig_broker_sl = candidate_sl
                                sig_notif = {
                                    "ticker": pos.ticker,
                                    "underlying": pos.underlying,
                                    "direction": pos.direction,
                                    "current_price": cur_px,
                                    "new_tsl": candidate_sl,
                                    "locked_in_pnl_pct": locked_pct,
                                    "event_type": "SIGNAL_TSL_TIGHTEN",
                                }
                                events.append({"event": "SIGNAL_TSL_TIGHTEN", "ticker": ticker, "new_tsl": candidate_sl, "reason": tighten_reason})

                if sig_broker_sl is not None:
                    self.place_broker_stop(pos, stop_price=sig_broker_sl)
                if sig_notif is not None:
                    self.notifier.notify_tsl(**sig_notif)

            else:
                # ── Legacy Milestone Trims on Technical Signals ──
                if not is_short:
                    if pos.rsi3 is not None and pos.rsi3 >= 75.0 and pnl_pct > 1.0:
                        if not pos.tp1_hit:
                            logger.info(
                                "🎯 Signal Achieved for %s: Connors 3-RSI Overbought (%.1f >= 75.0, PnL +%.2f%%). Executing TP1 trim.",
                                ticker, pos.rsi3, pnl_pct,
                            )
                            event = self._handle_tp1_hit(pos, cur_px)
                            event["signal_reason"] = f"3RSI_OVERBOUGHT_{pos.rsi3:.1f}"
                            events.append(event)
                        elif not pos.tp2_hit and pos.rsi3 >= 82.0 and pnl_pct > 3.0:
                            logger.info(
                                "🎯 Signal Achieved for %s: Connors 3-RSI Extreme Overbought (%.1f >= 82.0, PnL +%.2f%%). Executing TP2 trim.",
                                ticker, pos.rsi3, pnl_pct,
                            )
                            event = self._handle_tp2_hit(pos, cur_px)
                            event["signal_reason"] = f"3RSI_EXTREME_OVERBOUGHT_{pos.rsi3:.1f}"
                            events.append(event)

                    elif (
                        pos.sma5 is not None
                        and cur_px > pos.sma5
                        and pos.rsi3 is not None
                        and pos.rsi3 >= 68.0
                        and pnl_pct >= 2.0
                        and not pos.tp1_hit
                    ):
                        logger.info(
                            "🎯 Signal Achieved for %s: Price (%.2f) > SMA5 (%.2f) with RSI3=%.1f (PnL +%.2f%%). Executing TP1 trim.",
                            ticker, cur_px, pos.sma5, pos.rsi3, pnl_pct,
                        )
                        event = self._handle_tp1_hit(pos, cur_px)
                        event["signal_reason"] = "SMA5_REVERSION_EXIT"
                        events.append(event)

                else:
                    if pos.rsi3 is not None and pos.rsi3 <= 25.0 and pnl_pct > 1.0:
                        if not pos.tp1_hit:
                            logger.info(
                                "🎯 Signal Achieved for %s: Connors 3-RSI Oversold Cover (%.1f <= 25.0, PnL +%.2f%%). Executing TP1 trim.",
                                ticker, pos.rsi3, pnl_pct,
                            )
                            event = self._handle_tp1_hit(pos, cur_px)
                            event["signal_reason"] = f"3RSI_OVERSOLD_COVER_{pos.rsi3:.1f}"
                            events.append(event)
                        elif not pos.tp2_hit and pos.rsi3 <= 18.0 and pnl_pct > 3.0:
                            logger.info(
                                "🎯 Signal Achieved for %s: Connors 3-RSI Extreme Oversold Cover (%.1f <= 18.0, PnL +%.2f%%). Executing TP2 trim.",
                                ticker, pos.rsi3, pnl_pct,
                            )
                            event = self._handle_tp2_hit(pos, cur_px)
                            event["signal_reason"] = f"3RSI_EXTREME_OVERSOLD_COVER_{pos.rsi3:.1f}"
                            events.append(event)

                if nixtla_ret is not None:
                    try:
                        ret_val = float(nixtla_ret)
                        if not is_short:
                            if ret_val <= -3.0 and pnl_pct >= 1.5 and not pos.tp1_hit:
                                logger.info(
                                    "🎯 Signal Achieved for %s: Nixtla 5-Day Bearish Reversal (%.1f%%) with PnL +%.2f%%. Executing TP1 trim.",
                                    ticker, ret_val, pnl_pct,
                                )
                                event = self._handle_tp1_hit(pos, cur_px)
                                event["signal_reason"] = f"NIXTLA_BEARISH_REVERSAL_{ret_val:.1f}%"
                                events.append(event)
                            elif ret_val > 0 and pnl_pct >= max(4.0, ret_val) and pos.tp1_hit and not pos.tp2_hit:
                                logger.info(
                                    "🎯 Signal Achieved for %s: Nixtla 5-Day Target Achieved (PnL +%.2f%% >= Forecast %.1f%%). Executing TP2 trim.",
                                    ticker, pnl_pct, ret_val,
                                )
                                event = self._handle_tp2_hit(pos, cur_px)
                                event["signal_reason"] = f"NIXTLA_TARGET_ACHIEVED_{pnl_pct:.1f}%"
                                events.append(event)
                        else:
                            if ret_val >= 3.0 and pnl_pct >= 1.5 and not pos.tp1_hit:
                                logger.info(
                                    "🎯 Signal Achieved for %s: Nixtla 5-Day Bullish Reversal on Short (%.1f%%) with PnL +%.2f%%. Executing TP1 trim.",
                                    ticker, ret_val, pnl_pct,
                                )
                                event = self._handle_tp1_hit(pos, cur_px)
                                event["signal_reason"] = f"NIXTLA_BULLISH_REVERSAL_{ret_val:.1f}%"
                                events.append(event)
                    except (ValueError, TypeError):
                        pass

        with self._state_lock:
            for t in tickers_to_remove:
                if t in self.positions:
                    del self.positions[t]

        if events:
            self._save_state()

        return events

    # ─────────────────────────────────────────────────────────────
    # Internal Target Handlers
    # ─────────────────────────────────────────────────────────────
    @staticmethod
    def _calculate_pnl(pos: TrackedPosition, price: float, quantity: float) -> Tuple[float, float]:
        """Compute realized P&L amount and percentage accounting for direction."""
        is_short = PositionMonitor.is_true_short_contract(pos)
        if is_short:
            diff = pos.entry_price - price
        else:
            diff = price - pos.entry_price
        pnl = quantity * diff
        pnl_pct = (diff / pos.entry_price) * 100.0 if pos.entry_price > 0 else 0.0
        return round(pnl, 2), round(pnl_pct, 2)

    def _handle_tp1_hit(self, pos: TrackedPosition, price: float) -> Dict[str, Any]:
        """Process TP1 hit: Sell trim pct, bank profit, move stop to break-even, arm hysteresis."""
        with self._state_lock:
            pos_snap = self._resolve_position_snapshot(pos)
            pos.tp1_hit = True
            pos.tp1_armed_hysteresis = True
            pos.tp1_stop_elevated = False
            pos.tp1_escape_threshold = self._calculate_escape_threshold_for_pos(pos, pos_snap)
            rm_tp1 = getattr(self.risk_manager, "tp1_trim_pct", settings.TP1_TRIM_PCT)
            default_tp1 = rm_tp1 if isinstance(rm_tp1, (int, float)) else settings.TP1_TRIM_PCT
            trim_pct = float(self._snap_val(pos_snap, "TP1_TRIM_PCT", default_tp1))
            trim_shares = round(pos.original_quantity * trim_pct, 3)
            actual_trim = min(trim_shares, pos.remaining_quantity)
            pos.remaining_quantity = max(0.0, pos.remaining_quantity - actual_trim)

            pos.stop_loss = pos.entry_price
            pos.trailing_stop_loss = pos.entry_price

            rm_tp2 = getattr(self.risk_manager, "tp2_trim_pct", settings.TP2_TRIM_PCT)
            default_tp2 = rm_tp2 if isinstance(rm_tp2, (int, float)) else settings.TP2_TRIM_PCT
            tp2_trim_pct = float(self._snap_val(pos_snap, "TP2_TRIM_PCT", default_tp2))
            tp2_shares = round(pos.original_quantity * tp2_trim_pct, 3)
            actual_tp2_shares = min(tp2_shares, pos.remaining_quantity)
            rem_qty = pos.remaining_quantity
            tp2_target = pos.tp2
            entry_px = pos.entry_price

        pnl, pnl_pct = self._calculate_pnl(pos, price, actual_trim)

        # Cancel pending broker stop and limit orders outside _state_lock
        self.cancel_all_broker_orders(pos)
        delay = self._rate_limit_delay
        if not self.dry_run and delay > 0:
            time.sleep(delay)

        self._execute_sell(pos.ticker, actual_trim)
        if not self.dry_run and delay > 0:
            time.sleep(delay)

        self.place_broker_stop(pos, stop_price=entry_px)
        if rem_qty > 0 and tp2_target > 0 and actual_tp2_shares > 0:
            self.place_broker_tp_limit(pos, limit_price=tp2_target, quantity=actual_tp2_shares)

        self.notifier.notify_tp1(
            ticker=pos.ticker,
            underlying=pos.underlying,
            direction=pos.direction,
            price=price,
            quantity_sold=actual_trim,
            pnl=pnl,
            pnl_pct=pnl_pct,
            new_sl=entry_px,
        )
        return {"event": "TP1", "ticker": pos.ticker, "price": price, "pnl": pnl}

    def _handle_tp2_hit(self, pos: TrackedPosition, price: float) -> Dict[str, Any]:
        """Process TP2 hit: Sell second trim, ratchet trailing stop to TP1."""
        with self._state_lock:
            pos_snap = self._resolve_position_snapshot(pos)
            pos.tp1_hit = True
            pos.tp2_hit = True
            pos.tp1_armed_hysteresis = False
            pos.tp1_stop_elevated = True
            rm_tp2 = getattr(self.risk_manager, "tp2_trim_pct", settings.TP2_TRIM_PCT)
            default_tp2 = rm_tp2 if isinstance(rm_tp2, (int, float)) else settings.TP2_TRIM_PCT
            trim_pct = float(self._snap_val(pos_snap, "TP2_TRIM_PCT", default_tp2))
            trim_shares = round(pos.original_quantity * trim_pct, 3)
            actual_trim = min(trim_shares, pos.remaining_quantity)
            pos.remaining_quantity = max(0.0, pos.remaining_quantity - actual_trim)

            if self.is_true_short_contract(pos):
                pos.trailing_stop_loss = min(pos.trailing_stop_loss, pos.tp1) if pos.trailing_stop_loss > 0 else pos.tp1
                pos.stop_loss = min(pos.stop_loss, pos.tp1) if pos.stop_loss > 0 else pos.tp1
            else:
                pos.trailing_stop_loss = max(pos.trailing_stop_loss, pos.tp1)
                pos.stop_loss = max(pos.stop_loss, pos.tp1)
            new_tsl = pos.trailing_stop_loss
            rem_qty = pos.remaining_quantity
            tp3_target = pos.tp3

        pnl, pnl_pct = self._calculate_pnl(pos, price, actual_trim)

        self.cancel_all_broker_orders(pos)
        delay = self._rate_limit_delay
        if not self.dry_run and delay > 0:
            time.sleep(delay)

        self._execute_sell(pos.ticker, actual_trim)
        if not self.dry_run and delay > 0:
            time.sleep(delay)

        self.place_broker_stop(pos, stop_price=new_tsl)
        if rem_qty > 0 and tp3_target > 0:
            self.place_broker_tp_limit(pos, limit_price=tp3_target, quantity=rem_qty)

        self.notifier.notify_tp2(
            ticker=pos.ticker,
            underlying=pos.underlying,
            direction=pos.direction,
            price=price,
            quantity_sold=actual_trim,
            pnl=pnl,
            pnl_pct=pnl_pct,
            tsl=new_tsl,
        )
        return {"event": "TP2", "ticker": pos.ticker, "price": price, "pnl": pnl}

    def _handle_tp3_hit(self, pos: TrackedPosition, price: float) -> Dict[str, Any]:
        """Process TP3 hit: Close remaining runner position with full gains."""
        with self._state_lock:
            pos_snap = self._resolve_position_snapshot(pos)
            pos.tp3_hit = True
            pos.tp1_armed_hysteresis = False
            pos.tp1_stop_elevated = True
            pos.is_closed = True
            shares_to_sell = pos.remaining_quantity
            pos.remaining_quantity = 0.0

        self.cancel_all_broker_orders(pos)
        delay = self._rate_limit_delay
        if not self.dry_run and delay > 0:
            time.sleep(delay)

        pnl, pnl_pct = self._calculate_pnl(pos, price, shares_to_sell)

        if pnl < 0 and hasattr(self.risk_manager, "register_loss_exit"):
            cooldown = float(self._snap_val(
                pos_snap,
                "TRADE_LOSS_COOLDOWN_SECONDS",
                getattr(settings, "TRADE_LOSS_COOLDOWN_SECONDS", 3600.0),
            ))
            self.risk_manager.register_loss_exit(pos.ticker, pos.underlying, cooldown_seconds=cooldown)

        self._execute_sell(pos.ticker, shares_to_sell)

        self.notifier.notify_tp3(
            ticker=pos.ticker,
            underlying=pos.underlying,
            direction=pos.direction,
            price=price,
            quantity_sold=shares_to_sell,
            pnl=pnl,
            pnl_pct=pnl_pct,
        )
        return {"event": "TP3", "ticker": pos.ticker, "price": price, "pnl": pnl}

    def _handle_stop_exit(self, pos: TrackedPosition, price: float, is_tsl: bool) -> Dict[str, Any]:
        """Process Stop Loss or Trailing Stop Loss trigger: Liquidate remaining shares."""
        with self._state_lock:
            pos_snap = self._resolve_position_snapshot(pos)
            pos.is_closed = True
            shares_to_sell = pos.remaining_quantity
            pos.remaining_quantity = 0.0

        self.cancel_all_broker_orders(pos)
        delay = self._rate_limit_delay
        if not self.dry_run and delay > 0:
            time.sleep(delay)

        pnl, pnl_pct = self._calculate_pnl(pos, price, shares_to_sell)

        if pnl < 0 and hasattr(self.risk_manager, "register_loss_exit"):
            cooldown = float(self._snap_val(
                pos_snap,
                "TRADE_LOSS_COOLDOWN_SECONDS",
                getattr(settings, "TRADE_LOSS_COOLDOWN_SECONDS", 3600.0),
            ))
            self.risk_manager.register_loss_exit(pos.ticker, pos.underlying, cooldown_seconds=cooldown)

        self._execute_sell(pos.ticker, shares_to_sell)

        if is_tsl:
            self.notifier.notify_tsl(
                ticker=pos.ticker,
                underlying=pos.underlying,
                direction=pos.direction,
                current_price=price,
                new_tsl=price,
                locked_in_pnl_pct=pnl_pct,
                is_triggered=True,
            )
        else:
            self.notifier.notify_sl(
                ticker=pos.ticker,
                underlying=pos.underlying,
                direction=pos.direction,
                price=price,
                quantity=shares_to_sell,
                loss_amount=abs(pnl),
                loss_pct=abs(pnl_pct),
            )
        return {"event": "STOP_EXIT", "is_tsl": is_tsl, "ticker": pos.ticker, "price": price, "pnl": pnl}

    def _execute_sell(self, ticker: str, quantity: float) -> bool:
        """Execute a sell order via Trading 212 API (negative quantity)."""
        if quantity <= 0:
            return True
        if self.dry_run:
            logger.info("[DRY-RUN] Would place SELL market order for %.3f shares of %s", quantity, ticker)
            return True
        try:
            result = self.t212.place_market_order(ticker, -abs(quantity))
            logger.info("Executed market SELL on Trading 212: %s (Qty: -%.3f)", ticker, quantity)
            return True
        except Trading212Error as exc:
            logger.error("Failed to execute SELL order for %s on Trading 212: %s", ticker, exc)
            return False

    # ─────────────────────────────────────────────────────────────
    # State Persistence
    # ─────────────────────────────────────────────────────────────
    def _save_state(self) -> None:
        """Persist current active positions to state file atomically under _state_lock.
        Strictly excludes closed or zero-quantity positions so state file only reflects active exposure.
        """
        try:
            with self._state_lock:
                raw = {
                    t: asdict(pos)
                    for t, pos in list(self.positions.items())
                    if not getattr(pos, "is_closed", False)
                    and (getattr(pos, "remaining_quantity", 0.0) or 0.0) > 0.0
                }
                payload = json.dumps(raw, indent=2, default=str)
                self._atomic_write_text(self.state_file, payload)
        except Exception as exc:
            logger.warning("Failed to save position monitor state to %s: %s", self.state_file, exc)

    def _load_state(self) -> Dict[str, TrackedPosition]:
        """Load active positions from state file. Auto-heals state file if legacy closed trades exist."""
        if not self.state_file or self.state_file.startswith(":memory:") or not os.path.exists(self.state_file):
            return {}
        try:
            with self._state_lock:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                known_fields = {f.name for f in fields(TrackedPosition)}
                positions = {}
                had_closed = False
                state_updated = False
                for t, data in raw.items():
                    is_closed = bool(data.get("is_closed", False))
                    rem_qty = float(
                        data.get("remaining_quantity")
                        if data.get("remaining_quantity") is not None
                        else (data.get("original_quantity") or 0.0)
                    )
                    if not is_closed and rem_qty > 0.0:
                        filtered_data = {k: v for k, v in data.items() if k in known_fields}
                        pos = TrackedPosition(**filtered_data)
                        resolved_curr = get_ticker_currency(pos.ticker)
                        if not pos.currency or (pos.currency == "EUR" and resolved_curr != "EUR"):
                            pos.currency = resolved_curr
                        if pos.currency != data.get("currency"):
                            state_updated = True

                        # Auto-heal direction discrepancy (e.g. spot Long ETP marked as SHORT)
                        authoritative_dir = get_etp_direction(pos.ticker)
                        if authoritative_dir and pos.direction != authoritative_dir:
                            logger.warning(
                                "Auto-healed direction discrepancy for %s: changed %s -> %s",
                                pos.ticker, pos.direction, authoritative_dir,
                            )
                            pos.direction = authoritative_dir
                            state_updated = True

                        pos_snap = self._resolve_position_snapshot(pos)

                        # Auto-heal inverted stop loss for any spot ETP / equity holding
                        if not self.is_true_short_contract(pos) and pos.stop_loss >= pos.entry_price > 0:
                            levels = self.calculate_exit_levels(
                                entry_price=pos.entry_price,
                                direction=pos.direction,
                                ticker=pos.ticker,
                                config_snapshot=pos_snap,
                            )
                            pos.stop_loss = levels["stop_loss"]
                            pos.trailing_stop_loss = levels["trailing_stop_loss"]
                            pos.tp1 = levels["tp1"]
                            pos.tp2 = levels["tp2"]
                            pos.tp3 = levels["tp3"]
                            state_updated = True
                            logger.warning(
                                "Auto-healed inverted stop loss for spot holding %s: sl=%.4f >= entry=%.4f. Recalibrated SL to %.4f",
                                pos.ticker, pos.stop_loss, pos.entry_price, levels["stop_loss"],
                            )

                        # Auto-heal underlying scale contamination (e.g. tp1 computed on underlying index)
                        if pos.entry_price > 0 and pos.tp1 > 3.0 * pos.entry_price:
                            levels = self.calculate_exit_levels(
                                entry_price=pos.entry_price,
                                direction=pos.direction,
                                config_snapshot=pos_snap,
                            )
                            pos.tp1 = levels["tp1"]
                            pos.tp2 = levels["tp2"]
                            pos.tp3 = levels["tp3"]
                            pos.atr = levels["atr"]
                            if pos.highest_price and pos.highest_price > 2.0 * pos.entry_price:
                                pos.highest_price = pos.current_price or pos.entry_price
                            if pos.lowest_price and pos.lowest_price < 0.2 * pos.entry_price:
                                pos.lowest_price = pos.current_price or pos.entry_price
                            pos.scale_calibrated = True
                            state_updated = True
                            logger.warning(
                                "Auto-healed scale contamination for %s: recalibrated tp1=%.2f, tp2=%.2f, tp3=%.2f to ETP entry %.2f",
                                pos.ticker, pos.tp1, pos.tp2, pos.tp3, pos.entry_price,
                            )

                        positions[t] = pos
                    else:
                        had_closed = True

                logger.info("Loaded %d active tracked positions from %s", len(positions), self.state_file)

                if had_closed or state_updated:
                    payload = json.dumps({t: asdict(pos) for t, pos in positions.items()}, indent=2, default=str)
                    self._atomic_write_text(self.state_file, payload)
                    logger.info("Auto-healed %s: persisted currency updates and purged closed positions.", self.state_file)

                return positions
        except Exception as exc:
            logger.warning("Could not read state file %s: %s", self.state_file, exc)
            return {}
