"""Live Signals Dashboard Server & Generator for 3x Leveraged Trading Engine.

Generates an interactive, standalone HTML dashboard and serves it locally
so the user can monitor triggered signals, exact trade numbers (Entry, SL, TP1-3, TSL),
and manually execute orders in any broker.
"""

from __future__ import annotations

import argparse
from collections import deque
from dataclasses import asdict
from datetime import datetime, timezone
import errno
import http.server
import json
import logging
import math
import os
from pathlib import Path
import queue
import re
import socket
import socketserver
import sys
import tempfile
import threading
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple
import urllib.parse
import webbrowser

from trading_system.agents.risk_manager import RiskManager
from trading_system.config import settings
from trading_system.config_bus import (
    ConfigBus,
    ConfigConflictError,
    ConfigSnapshot,
    ConfigValidationError,
)
from trading_system.config_schema import export_config_schema, get_builtin_profiles
from trading_system.dashboard.auth import (
    DashboardAuthManager,
    is_sensitive_key,
    is_valid_host_header,
    is_valid_origin_header,
    json_safe_for_inline_script,
    scrub_error_message,
)
from trading_system.data.leveraged_universe import (
    CURATED_3X_PAIRS,
    GBX_TICKERS,
    LeveragedUniverse,
    get_currency_display,
    get_etp_direction,
    get_ticker_currency,
)
from trading_system.data.trading212_client import Trading212Client
from trading_system.execution.position_monitor import (
    PositionEpochConflictError,
    ResumeBlockedError,
)

logger = logging.getLogger(__name__)

ASSETS_DIR = Path(__file__).resolve().parent / "assets"
STYLES_ASSET_PATH = ASSETS_DIR / "styles.css"
WORKBENCH_ASSET_PATH = ASSETS_DIR / "workbench.js"
MAX_REQUEST_BODY_BYTES = 131_072
MAX_SSE_SUBSCRIBERS = 32
DEFAULT_STREAM_HEARTBEAT_SECONDS = 10.0
REQUEST_SOCKET_TIMEOUT_SECONDS = 5.0
DEFAULT_CSP_HEADER = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'"
)
READ_ONLY_REASON_DEFAULT = (
    "Standalone dashboard process has no attached Orchestrator controller (HTTP 503 on mutations)."
)
SENSITIVE_FILENAMES = {
    ".env",
    ".dashboard_token",
    "active_trades.json",
    "trades.jsonl",
    "notifications.jsonl",
    "config_overrides.json",
    "dynamic_parameters.json",
    "config_epochs.jsonl",
    "audit_history.jsonl",
    "funnel_state.json",
}
_SAFE_SSE_EVENT_RE = re.compile(r"[^A-Za-z0-9_.:-]")


class ThreadingDashboardServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """Threaded TCP server allowing multiple concurrent long-lived SSE connections."""

    daemon_threads = True
    allow_reuse_address = True

    def shutdown(self) -> None:
        """Wake active SSE streams before shutting down the TCP accept loop."""
        SSEBroadcaster.get_instance().close_all()
        super().shutdown()

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Suppress expected client disconnect noise while surfacing real errors."""
        exc = sys.exc_info()[1]
        if isinstance(
            exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError)
        ):
            return
        super().handle_error(request, client_address)


class SSEBroadcaster:
    """Thread-safe event broadcaster for Server-Sent Events (SSE)."""

    _instance: Optional[SSEBroadcaster] = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._subscribers: List[queue.Queue] = []
        self._drop_oldest_ids: Set[int] = set()
        self._stream_ids: Set[int] = set()
        self._lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> SSEBroadcaster:
        """Return the process-wide SSEBroadcaster singleton."""
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = SSEBroadcaster()
            return cls._instance

    def subscribe(self, drop_oldest: bool = False, maxsize: int = 100) -> queue.Queue:
        """Register a subscriber queue; dead-queue pruning stays active unless ``drop_oldest=True``."""
        q: queue.Queue = queue.Queue(maxsize=maxsize)
        with self._lock:
            self._subscribers.append(q)
            if drop_oldest:
                self._drop_oldest_ids.add(id(q))
        return q

    def subscribe_stream(
        self,
        max_subscribers: int = MAX_SSE_SUBSCRIBERS,
        drop_oldest: bool = True,
        maxsize: int = 64,
    ) -> Optional[queue.Queue]:
        """Acquire a capped HTTP SSE stream slot or return ``None`` when at capacity."""
        q: queue.Queue = queue.Queue(maxsize=maxsize)
        with self._lock:
            if len(self._stream_ids) >= max_subscribers:
                return None
            self._subscribers.append(q)
            self._stream_ids.add(id(q))
            if drop_oldest:
                self._drop_oldest_ids.add(id(q))
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        """Remove a subscriber queue cleanly."""
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)
            q_id = id(q)
            self._drop_oldest_ids.discard(q_id)
            self._stream_ids.discard(q_id)

    unsubscribe_stream = unsubscribe

    def active_stream_count(self) -> int:
        """Return current number of active HTTP SSE stream subscribers."""
        with self._lock:
            return len(self._stream_ids)

    def broadcast(self, event_type: str, data: Any) -> None:
        """Broadcast an event payload to all subscribers, pruning or dropping oldest on backpressure."""
        safe_event = _SAFE_SSE_EVENT_RE.sub("_", str(event_type or "message"))
        payload = {
            "event": safe_event,
            "data": data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        with self._lock:
            dead: List[queue.Queue] = []
            for q in list(self._subscribers):
                q_id = id(q)
                if q_id in self._drop_oldest_ids:
                    try:
                        q.put_nowait(payload)
                    except queue.Full:
                        try:
                            q.get_nowait()
                        except queue.Empty:
                            pass
                        try:
                            q.put_nowait(payload)
                        except queue.Full:
                            dead.append(q)
                else:
                    try:
                        q.put_nowait(payload)
                    except queue.Full:
                        dead.append(q)
            for q in dead:
                if q in self._subscribers:
                    self._subscribers.remove(q)
                q_id = id(q)
                self._drop_oldest_ids.discard(q_id)
                self._stream_ids.discard(q_id)

    publish = broadcast

    def close_all(self) -> None:
        """Signal all active HTTP stream subscribers to exit cleanly during shutdown."""
        with self._lock:
            stream_queues = [q for q in self._subscribers if id(q) in self._stream_ids]
            for q in stream_queues:
                try:
                    q.put_nowait(None)
                except queue.Full:
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        q.put_nowait(None)
                    except queue.Full:
                        pass
            self._subscribers = [q for q in self._subscribers if id(q) not in self._stream_ids]
            self._drop_oldest_ids.difference_update(self._stream_ids)
            self._stream_ids.clear()


def load_active_signals(
    state_file: str = "active_trades.json",
    live_prices: Optional[Dict[str, float]] = None,
    universe: Optional[LeveragedUniverse] = None,
) -> List[Dict[str, Any]]:
    """Load active tracked positions and enrich them with trade metrics."""
    if not os.path.exists(state_file):
        return []
    try:
        if universe is None:
            universe = LeveragedUniverse()
        with open(state_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        signals = []
        for ticker, pos in data.items():
            if bool(pos.get("is_closed", False)):
                continue
            original_quantity = float(pos.get("original_quantity") or 0.0)
            remaining_quantity = float(
                pos.get("remaining_quantity")
                if pos.get("remaining_quantity") is not None
                else original_quantity
            )
            if remaining_quantity <= 0.0:
                continue

            entry = float(pos.get("entry_price") or 0.0)
            sl = float(pos.get("stop_loss") or 0.0)
            tsl = float(pos.get("trailing_stop_loss") or sl)
            tp1 = float(pos.get("tp1") or 0.0)
            tp2 = float(pos.get("tp2") or 0.0)
            tp3 = float(pos.get("tp3") or 0.0)
            # Authoritative direction: if instrument is an ETP, its direction is derived authoritatively
            etp_dir = universe.get_etp_direction(ticker) if universe else get_etp_direction(ticker)
            direction = etp_dir if etp_dir else pos.get("direction", "LONG").upper()
            underlying = pos.get("underlying", "")
            broker_stop_order_id = pos.get("broker_stop_order_id")

            # Determine human-readable asset name and quote currency
            pair = universe.get_pair_for_etp(ticker) if universe else None
            asset_name = pos.get("name") or (pair.name if pair else underlying)
            if not underlying and pair:
                underlying = pair.underlying_symbol
            
            # Authoritative ticker-level currency resolution (prefer ticker currency over pair base currency)
            curr = pos.get("currency")
            if not curr or curr == "EUR":
                ticker_curr = universe.get_ticker_currency(ticker) if universe else get_ticker_currency(ticker)
                if ticker_curr:
                    curr = ticker_curr
            if not curr:
                curr = "EUR"

            currency_code, currency_prefix, currency_suffix = get_currency_display(curr)

            # Estimate or extract current price and live P&L
            highest_price = float(pos.get("highest_price") or entry)
            lowest_price = float(pos.get("lowest_price") or entry)
            if live_prices and ticker in live_prices and live_prices[ticker] > 0:
                current_price = float(live_prices[ticker])
            elif pos.get("current_price") and float(pos.get("current_price")) > 0:
                current_price = float(pos.get("current_price"))
            else:
                current_price = entry if entry > 0 else (highest_price if direction == "LONG" else lowest_price)

            # On Trading 212 Invest, all positions (Long ETPs, Inverse Short ETPs, stocks) are spot assets
            # held long (Q > 0). When current_price > entry, it is ALWAYS a profit; when current_price < entry,
            # it is ALWAYS a loss. Inverse ETPs already reflect underlying moves inversely in their market price.
            if entry > 0 and current_price > 0:
                pnl_pct = ((current_price - entry) / entry) * 100.0
                pnl_amount = (current_price - entry) * remaining_quantity
            else:
                pnl_pct = 0.0
                pnl_amount = 0.0

            # Convert native pnl_amount to EUR
            pnl_amount_eur = pnl_amount
            if curr == "GBX" or ticker in GBX_TICKERS or (curr == "GBP" and (entry > 500 or current_price > 500)):
                pnl_amount_eur = (pnl_amount / 100.0) * 1.204
            elif curr == "GBP":
                pnl_amount_eur = pnl_amount * 1.204
            elif curr == "USD":
                pnl_amount_eur = pnl_amount / 1.08

            # Calculate risk & reward percentages
            risk_pct = abs(entry - sl) / entry * 100 if entry > 0 else 0.0
            tp1_gain_pct = abs(tp1 - entry) / entry * 100 if entry > 0 else 0.0
            tp2_gain_pct = abs(tp2 - entry) / entry * 100 if entry > 0 else 0.0
            tp3_gain_pct = abs(tp3 - entry) / entry * 100 if entry > 0 else 0.0

            # Pure TSL Status Resolution (100% Ride with progressive ratchets)
            session_count = int(pos.get("session_count") or 1)
            is_runner = bool(pos.get("is_runner", False))
            decay_multiplier = RiskManager.get_decay_atr_multiplier(session_count=session_count, is_runner=is_runner)
            decay_label = "Runner (2.00x ATR)" if is_runner else f"Day {session_count} ({decay_multiplier:.2f}x ATR)"

            if pos.get("is_closed"):
                status = "CLOSED"
                tsl_status = "Closed"
            elif tsl > 0 and entry > 0:
                locked_gain = ((tsl - entry) / entry * 100.0)
                if locked_gain >= 1.4:
                    status = f"CUSHION LOCKED (+{locked_gain:.1f}% Locked)"
                    tsl_status = f"+{locked_gain:.1f}% Cushion Locked"
                elif locked_gain >= -0.05:
                    status = "BREAKEVEN LOCKED (Risk-Free Trail Active)"
                    tsl_status = "Break-Even Locked"
                else:
                    status = "ACTIVE (Initial Stop Protection)"
                    tsl_status = "Initial Stop Active"
            else:
                status = "ACTIVE"
                tsl_status = "Initial Stop Active"

            rsi3_raw = pos.get("rsi3")
            rsi3_val = float(rsi3_raw) if rsi3_raw is not None else 100.0
            strategy_type = pos.get("strategy_type") or ("3rsi_mean_reversion" if rsi3_val <= 25 else "momentum")
            rsi3 = pos.get("rsi3", None)
            rsi7 = pos.get("rsi7", None)
            rsi14 = pos.get("rsi14", None)
            sma5 = pos.get("sma5", None)
            win_rate_exp = pos.get("win_rate_exp", "85%" if "3rsi" in strategy_type else "72%")

            earnings_in_days_raw = pos.get("earnings_in_days")
            earnings_days: Optional[float] = None
            if earnings_in_days_raw is not None:
                try:
                    earnings_days = float(earnings_in_days_raw)
                except (ValueError, TypeError):
                    earnings_days = None

            if direction == "SHORT":
                overnight_status = "INTRADAY SHORT (Flattens 16:20 UTC / Winter Time)"
                overnight_badge_class = "bg-rose-500/10 text-rose-300 border-rose-500/30"
            elif earnings_days is not None and 0 <= earnings_days <= 1.0:
                overnight_status = f"EARNINGS BLACKOUT ({earnings_days:.1f}d — Flattens 16:20 UTC)"
                overnight_badge_class = "bg-amber-500/10 text-amber-300 border-amber-500/30"
            elif (tsl >= entry and entry > 0) or pos.get("tp1_hit"):
                radar_tag = f" • Pre-Earnings Radar ({earnings_days:.0f}d)" if (earnings_days and earnings_days <= 21.0) else ""
                overnight_status = f"OVERNIGHT ELIGIBLE (TSL Breakeven Active{radar_tag})"
                overnight_badge_class = "bg-emerald-500/10 text-emerald-300 border-emerald-500/30"
            elif earnings_days is not None and 1.0 < earnings_days <= 21.0:
                overnight_status = f"PRE-EARNINGS RUN-UP RADAR ({earnings_days:.0f}d away • Flattens 16:20 UTC if unhedged)"
                overnight_badge_class = "bg-purple-500/10 text-purple-300 border-purple-500/30"
            else:
                overnight_status = "REQUIRES BREAKEVEN OR FLATTENS 16:20 UTC"
                overnight_badge_class = "bg-sky-500/10 text-sky-300 border-sky-500/30"

            stop_broker_line = f"NATIVE BROKER STOP: Trading 212 Order #{broker_stop_order_id} (Active)\n" if broker_stop_order_id else ""

            signals.append({
                "ticker": ticker,
                "underlying": underlying,
                "name": asset_name,
                "direction": direction,
                "leverage": int(pos.get("leverage") or (5 if "5" in ticker else 3)),
                "strategy_type": strategy_type,
                "rsi3": rsi3,
                "rsi7": rsi7,
                "rsi14": rsi14,
                "sma5": sma5,
                "win_rate_exp": win_rate_exp,
                "overnight_status": overnight_status,
                "overnight_badge_class": overnight_badge_class,
                "earnings_in_days": earnings_days,
                "entry_price": round(entry, 2),
                "current_price": round(current_price, 2),
                "pnl_pct": round(pnl_pct, 2),
                "pnl_amount": round(pnl_amount, 2),
                "pnl_amount_eur": round(pnl_amount_eur, 2),
                "currency_code": currency_code,
                "currency_prefix": currency_prefix,
                "currency_suffix": currency_suffix,
                "stop_loss": round(sl, 2),
                "trailing_stop_loss": round(tsl, 2),
                "tsl_status": tsl_status,
                "session_count": session_count,
                "is_runner": is_runner,
                "decay_multiplier": decay_multiplier,
                "decay_label": decay_label,
                "stop_distance_pct": round(risk_pct, 2),
                "broker_stop_order_id": broker_stop_order_id,
                "quantity": original_quantity,
                "remaining_quantity": remaining_quantity,
                "status": status,
                "entry_time": pos.get("entry_time", ""),
                "atr": round(pos.get("atr", 0.0), 2),
                "copy_text": (
                    f"STRATEGY: {strategy_type.upper()} ({win_rate_exp} Win-Rate)\n"
                    f"ACTION: {direction} {ticker} (Underlying: {underlying})\n"
                    f"ENTRY: {currency_prefix}{entry:.2f}{currency_suffix}\n"
                    f"CURRENT: {currency_prefix}{current_price:.2f}{currency_suffix} ({pnl_pct:+.2f}%)\n"
                    f"STOP LOSS: {currency_prefix}{sl:.2f}{currency_suffix} (-{risk_pct:.2f}%)\n"
                    f"ACTIVE TSL: {currency_prefix}{tsl:.2f}{currency_suffix} ({tsl_status})\n"
                    f"DECAY ENVELOPE: {decay_label}\n"
                    f"{stop_broker_line}"
                    f"QUANTITY: {remaining_quantity:.2f} / {original_quantity:.2f} shares (100% Ride)\n"
                    f"SESSION PROTOCOL: {overnight_status}"
                ),
            })
        return signals
    except Exception as exc:
        logger.warning("Failed to load active trades: %s", exc)
        return []


def load_recent_history(trades_log_path: str = "trades.jsonl", limit: int = 15) -> List[Dict[str, Any]]:
    """Load latest records from trade audit log."""
    if not os.path.exists(trades_log_path):
        return []
    try:
        lines = []
        with open(trades_log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    lines.append(json.loads(line))
        return lines[-limit:][::-1]
    except Exception as exc:
        logger.warning("Failed to load trade log: %s", exc)
        return []


def load_recent_notifications(
    log_path: str = "notifications.jsonl", limit: int = 25, universe: Any = None
) -> List[Dict[str, Any]]:
    """Load latest records from notifications alert log with authoritative direction and P&L normalization."""
    if not os.path.exists(log_path):
        return []
    try:
        records = []
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        record = json.loads(line)
                        tk = record.get("ticker", "")
                        if tk:
                            auth_dir = universe.get_etp_direction(tk) if universe else get_etp_direction(tk)
                            if auth_dir:
                                record["direction"] = auth_dir
                        # Ensure BUY open entries have no realized P&L so UI displays "Open Entry" instead of "0%"
                        if record.get("event_type") == "BUY":
                            record["pnl"] = None
                            record["pnl_pct"] = None
                        records.append(record)
                    except Exception:
                        continue
        return records[-limit:][::-1]
    except Exception as exc:
        logger.warning("Failed to load notifications log: %s", exc)
        return []


def fetch_live_broker_notifications(
    client: Any, universe: Any, limit: int = 25
) -> List[Dict[str, Any]]:
    """Fetch live executed and filled orders from Trading 212 and format as notifications."""
    try:
        hist = client.get_historical_orders(limit=limit)
        items = (
            hist.get("items", [])
            if isinstance(hist, dict)
            else (hist if isinstance(hist, list) else [])
        )
        broker_events = []
        for it in items:
            order = it.get("order", {})
            fill = it.get("fill", {})
            ticker = order.get("ticker", "")
            if not ticker:
                continue

            order_id = str(order.get("id", ""))
            fill_id = str(fill.get("id", ""))
            o_side = (order.get("side") or "").upper()
            o_type = (order.get("type") or "").upper()
            o_status = (order.get("status") or "").upper()
            filled_at = fill.get("filledAt") or order.get("createdAt") or ""
            fill_px = float(fill.get("price") or order.get("stopPrice") or 0.0)
            fill_qty = abs(float(fill.get("quantity") or order.get("filledQuantity") or 0.0))

            # Filter out cancelled, rejected, or 0-quantity orders that never filled
            if fill_qty <= 0.0 or (o_status and o_status not in ("FILLED", "EXECUTED")):
                continue

            wallet_impact = fill.get("walletImpact", {}) or {}
            realized_pnl = wallet_impact.get("realisedProfitLoss")
            pnl_val = float(realized_pnl) if realized_pnl is not None else None
            net_val = float(wallet_impact.get("netValue") or 0.0)

            pnl_currency = wallet_impact.get("currency") or "EUR"
            instrument_currency = (
                order.get("instrument", {}).get("currency")
                or (universe.get_ticker_currency(ticker) if universe else None)
                or order.get("currency")
                or "EUR"
            )

            # Authoritative direction: Derived from the instrument (e.g. IONQl1_EQ is LONG, 3LFEl_EQ is LONG, 3USSm_EQ is SHORT)
            direction = universe.get_etp_direction(ticker) if universe else get_etp_direction(ticker)

            pair = universe.get_pair_for_etp(ticker) if universe else None
            underlying = pair.underlying_symbol if pair else ticker.replace("3", "").replace("5", "").split("_")[0]

            pnl_pct_val: Optional[float] = None
            if o_side == "BUY":
                ev = "BUY"
                pnl_val = None
                pnl_pct_val = None
                rationale = f"Live T212 Market Fill #{fill_id or order_id}"
            else:
                # SELL orders are exits (SL, TP, TSL, or manual sell)
                if pnl_val is not None:
                    # Cost basis = proceeds - realized_pnl
                    if net_val > 0:
                        cost_basis = net_val - pnl_val
                        if cost_basis > 0:
                            pnl_pct_val = round((pnl_val / cost_basis) * 100.0, 2)
                        else:
                            pnl_pct_val = round((pnl_val / net_val) * 100.0, 2)
                    elif fill_px > 0 and fill_qty > 0:
                        exit_notional = fill_px * fill_qty
                        cost_basis = exit_notional - pnl_val
                        if cost_basis > 0:
                            pnl_pct_val = round((pnl_val / cost_basis) * 100.0, 2)
                        else:
                            pnl_pct_val = round((pnl_val / exit_notional) * 100.0, 2)

                if "STOP" in o_type or "TRAILING" in o_type:
                    ev = "SL" if (pnl_val is not None and pnl_val < 0) else "TSL"
                    rationale = f"Live T212 Stop Order Executed #{order_id}"
                elif "LIMIT" in o_type:
                    ev = "TSL" if (pnl_val is not None and pnl_val > 0) else "SELL"
                    rationale = f"Live T212 Limit Target Filled #{order_id}"
                else:
                    if pnl_val is not None and pnl_val < 0:
                        ev = "SL"
                    elif pnl_val is not None and pnl_val > 0:
                        ev = "TSL"
                    else:
                        ev = "SELL"
                    rationale = f"Live T212 Exit Order Filled #{order_id}"

            ts_str = filled_at.replace("T", " ").replace(".000Z", " UTC").replace("Z", " UTC")
            if not ts_str.endswith("UTC"):
                ts_str += " UTC"

            broker_events.append({
                "event_type": ev,
                "ticker": ticker,
                "underlying": underlying,
                "direction": direction,
                "price": fill_px,
                "quantity": fill_qty,
                "stop_loss": float(order.get("stopPrice") or 0.0),
                "trailing_stop": float(order.get("stopPrice") or 0.0),
                "tp1": 0.0, "tp2": 0.0, "tp3": 0.0,
                "pnl": pnl_val,
                "pnl_pct": pnl_pct_val,
                "rationale": rationale,
                "timestamp": ts_str,
                "currency": instrument_currency,
                "pnl_currency": pnl_currency,
                "order_id": order_id,
            })
        return broker_events
    except Exception as exc:
        logger.debug("Failed to fetch live broker notifications: %s", exc)
        return []


_DASHBOARD_DATA_CACHE: Optional[Dict[str, Any]] = None
_DASHBOARD_DATA_CACHE_TIME: float = 0.0
_DASHBOARD_DATA_CACHE_STAMP: Optional[Tuple[Any, ...]] = None
_DASHBOARD_DATA_LOCK = threading.Lock()
# Explicit test injection hook ONLY — never auto-instantiated in production/standalone reads.
_DASHBOARD_SHARED_CLIENT: Optional[Trading212Client] = None


def _invalidate_dashboard_cache() -> None:
    """Clear the cached standalone dashboard payload."""
    global _DASHBOARD_DATA_CACHE, _DASHBOARD_DATA_CACHE_TIME, _DASHBOARD_DATA_CACHE_STAMP
    with _DASHBOARD_DATA_LOCK:
        _DASHBOARD_DATA_CACHE = None
        _DASHBOARD_DATA_CACHE_TIME = 0.0
        _DASHBOARD_DATA_CACHE_STAMP = None


def _sanitize_mapping(value: Any) -> Any:
    """Recursively strip sensitive config keys from JSON payloads."""
    if isinstance(value, ConfigSnapshot):
        value = value.to_dict(include_specs=True)
    if isinstance(value, Mapping):
        cleaned: Dict[str, Any] = {}
        for k, v in value.items():
            key_str = str(k)
            if is_sensitive_key(key_str):
                continue
            cleaned[key_str] = _sanitize_mapping(v)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [_sanitize_mapping(item) for item in value]
    return value


def _read_json_file(path: Path, default: Any = None) -> Any:
    """Safely load a JSON file from disk, returning ``default`` if missing or invalid."""
    try:
        if not path.is_file():
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return default


def _read_jsonl_tail(path: Path, limit: int = 50) -> List[Dict[str, Any]]:
    """Read up to ``limit`` most recent valid JSON objects from a JSONL file."""
    if limit <= 0 or not path.is_file():
        return []
    rows: deque[Dict[str, Any]] = deque(maxlen=limit)
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    parsed = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(parsed, dict):
                    rows.append(parsed)
    except OSError:
        return []
    return list(rows)


def _disk_cache_stamp(data_dir: Path) -> Tuple[Any, ...]:
    """Compute a lightweight mtime/cwd stamp for read-only disk inputs."""
    watched = [
        Path(settings.ACTIVE_TRADES_PATH),
        Path(settings.TRADES_LOG_PATH),
        Path(settings.NOTIFICATIONS_LOG_PATH),
        data_dir / "funnel_state.json",
        data_dir / "audit_history.jsonl",
        data_dir / "config_overrides.json",
        data_dir / "dynamic_parameters.json",
        data_dir / "config_epochs.jsonl",
    ]
    mtimes: List[Optional[int]] = []
    for p in watched:
        try:
            st = p.stat()
            mtimes.append(st.st_mtime_ns)
        except OSError:
            mtimes.append(None)
    return (os.getcwd(), *mtimes)


def _build_pairs_watchlist(universe: LeveragedUniverse) -> List[Dict[str, Any]]:
    """Construct curated leveraged pairs watchlist metadata."""
    return [
        {
            "underlying": p.underlying_symbol,
            "name": p.name,
            "category": p.category,
            "long_ticker": p.long_3x_ticker,
            "short_ticker": p.short_3x_ticker,
            "long_currency": universe.get_ticker_currency(p.long_3x_ticker),
            "short_currency": universe.get_ticker_currency(p.short_3x_ticker),
            "has_5x": p.has_5x,
            "long_5x_ticker": p.long_5x_ticker or "—",
            "short_5x_ticker": p.short_5x_ticker or "—",
            "long_5x_name": p.long_5x_name or "",
            "short_5x_name": p.short_5x_name or "",
            "currency": p.currency,
        }
        for p in CURATED_3X_PAIRS
    ]


def _compute_eur_ratio(
    active_signals: Sequence[Mapping[str, Any]],
    pairs_watchlist: Sequence[Mapping[str, Any]],
) -> str:
    """Compute percentage of EUR-denominated instruments dynamically without placeholders."""
    if active_signals:
        eur_count = sum(
            1
            for s in active_signals
            if str(s.get("currency_code") or s.get("currency") or "").upper() == "EUR"
        )
        pct = round((eur_count / len(active_signals)) * 100)
        return f"{pct}% EUR"
    if pairs_watchlist:
        eur_count = sum(
            1
            for p in pairs_watchlist
            if str(p.get("long_currency") or p.get("currency") or "").upper() == "EUR"
        )
        pct = round((eur_count / len(pairs_watchlist)) * 100)
        return f"{pct}% EUR"
    return "0% EUR"


def _enrich_active_signals_from_raw_state(
    signals: List[Dict[str, Any]],
    raw_state: Any,
) -> List[Dict[str, Any]]:
    """Attach U1-U4 epoch and panic/protection metadata preserved on disk to active signal cards."""
    if not isinstance(raw_state, Mapping):
        return signals
    passthrough_keys = (
        "config_epoch_id",
        "panic_failed",
        "unprotected_reason",
        "broker_stop_order_id",
        "is_tsl_active",
        "initial_stop_loss",
        "entry_config_snapshot",
    )
    for sig in signals:
        ticker = str(sig.get("ticker") or "")
        raw_entry = raw_state.get(ticker)
        if not isinstance(raw_entry, Mapping):
            continue
        for key in passthrough_keys:
            if key in raw_entry and key not in sig:
                val = raw_entry[key]
                sig[key] = _sanitize_mapping(val) if key == "entry_config_snapshot" else val
        if sig.get("panic_failed"):
            sig["unprotected"] = True
    return signals


def _load_read_only_config_bundle(data_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Load effective configuration, schema, presets, and epoch history strictly read-only."""
    resolved_dir = data_dir or Path("data")
    try:
        bus = ConfigBus(data_dir=resolved_dir)
        snapshot_dict = _sanitize_mapping(bus.active_snapshot().to_dict(include_specs=True))
        schema = _sanitize_mapping(bus.get_schema())
        epoch_history = list(reversed(bus.epoch_history(limit=50)))
        epoch_id = bus.active_epoch_id
        pending_restart_keys = list(snapshot_dict.get("pending_restart_keys") or [])
        quarantine_warnings = list(bus.get_quarantine_warnings())
    except Exception as exc:
        logger.debug("Falling back to default read-only schema bundle: %s", exc)
        fallback_snap = ConfigSnapshot.from_dict({"config_epoch_id": "epoch-0"})
        snapshot_dict = _sanitize_mapping(fallback_snap.to_dict(include_specs=True))
        schema = _sanitize_mapping(export_config_schema())
        epoch_history = []
        epoch_id = "epoch-0"
        pending_restart_keys = []
        quarantine_warnings = []

    return {
        "config_epoch_id": epoch_id,
        "config": snapshot_dict,
        "schema": schema,
        "presets": _sanitize_mapping(get_builtin_profiles()),
        "epoch_history": epoch_history,
        "pending_restart_keys": pending_restart_keys,
        "config_quarantine_warnings": quarantine_warnings,
    }


def _load_funnel_and_audit(data_dir: Optional[Path] = None) -> Tuple[Dict[str, Any], Dict[str, Any], List[Dict[str, Any]]]:
    """Read persisted screening funnel and strategy improvement audit history from disk."""
    resolved_dir = data_dir or Path("data")
    funnel_raw = _read_json_file(resolved_dir / "funnel_state.json", default=None)
    if not isinstance(funnel_raw, dict):
        funnel: Dict[str, Any] = {
            "timestamp": "",
            "config_epoch_id": None,
            "stage_counts": {
                "universe": 0,
                "screened": 0,
                "strategy": 0,
                "debate": 0,
                "gates": 0,
                "executed": 0,
            },
            "gate_breakdown": [],
            "_compact_indicators": {},
            "metric_percentiles": {},
            "passed_tickers": [],
            "executed_tickers": [],
        }
    else:
        funnel = dict(funnel_raw)

    audit_rows = _read_jsonl_tail(resolved_dir / "audit_history.jsonl", limit=30)
    audit_desc = list(reversed(audit_rows))
    audit_obj = {
        "latest": audit_desc[0] if audit_desc else None,
        "history": audit_desc,
    }
    return funnel, audit_obj, audit_desc


def _normalize_dashboard_snapshot(
    raw: Mapping[str, Any],
    *,
    orchestrator: Optional[Any] = None,
    active_client: Optional[Trading212Client] = None,
) -> Dict[str, Any]:
    """Normalize orchestrator or disk-built snapshot into the full workbench JSON contract."""
    snap = dict(raw)
    controller_attached = orchestrator is not None
    snap.setdefault("generated_at", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"))
    snap["controller_attached"] = bool(snap.get("controller_attached", controller_attached))
    if snap["controller_attached"]:
        snap["read_only_reason"] = str(snap.get("read_only_reason") or "")
    else:
        snap["read_only_reason"] = str(snap.get("read_only_reason") or READ_ONLY_REASON_DEFAULT)

    use_live = bool(getattr(settings, "USE_LIVE", False))
    snap.setdefault("mode", "LIVE" if use_live else "DEMO")

    active_signals = list(snap.get("active_signals") or [])
    snap["active_signals"] = active_signals
    snap["active_signals_count"] = int(snap.get("active_signals_count", len(active_signals)))
    snap.setdefault("recent_notifications", [])
    snap.setdefault("history", [])

    universe = LeveragedUniverse()
    if not snap.get("pairs_watchlist"):
        snap["pairs_watchlist"] = _build_pairs_watchlist(universe)

    equity_series = snap.get("equity_points") or snap.get("equity_history") or []
    snap["equity_points"] = list(equity_series)
    snap["equity_history"] = list(equity_series)

    default_equity = float(getattr(settings, "DEFAULT_ACCOUNT_EQUITY", 10000.0) or 10000.0)
    if snap.get("base_equity") is None:
        snap["base_equity"] = default_equity
    if snap.get("starting_equity") is None:
        snap["starting_equity"] = snap["base_equity"]
    if snap.get("current_equity") is None:
        snap["current_equity"] = snap["base_equity"]

    if "rate_limit_telemetry" not in snap or not isinstance(snap.get("rate_limit_telemetry"), Mapping):
        if active_client is not None and hasattr(active_client, "get_rate_limit_telemetry"):
            try:
                snap["rate_limit_telemetry"] = active_client.get_rate_limit_telemetry()
            except Exception:
                snap["rate_limit_telemetry"] = Trading212Client.get_rate_limit_telemetry()
        else:
            snap["rate_limit_telemetry"] = Trading212Client.get_rate_limit_telemetry()

    if (
        "config" not in snap
        or "schema" not in snap
        or "presets" not in snap
        or "config_epoch_id" not in snap
    ):
        config_bus = getattr(orchestrator, "config_bus", None) if orchestrator is not None else None
        if config_bus is not None:
            try:
                cfg_dict = _sanitize_mapping(config_bus.active_snapshot().to_dict(include_specs=True))
                snap.setdefault("config_epoch_id", config_bus.active_epoch_id)
                snap.setdefault("config", cfg_dict)
                snap.setdefault("schema", _sanitize_mapping(config_bus.get_schema()))
                snap.setdefault("presets", _sanitize_mapping(get_builtin_profiles()))
                snap.setdefault("epoch_history", list(reversed(config_bus.epoch_history(limit=50))))
                snap.setdefault(
                    "pending_restart_keys",
                    list(cfg_dict.get("pending_restart_keys") or []),
                )
                snap.setdefault(
                    "config_quarantine_warnings",
                    list(config_bus.get_quarantine_warnings()),
                )
            except Exception as exc:
                logger.debug("Failed to augment snapshot from orchestrator.config_bus: %s", exc)
        if "config" not in snap or "schema" not in snap or "presets" not in snap:
            disk_cfg = _load_read_only_config_bundle()
            for k, v in disk_cfg.items():
                snap.setdefault(k, v)

    snap["config"] = _sanitize_mapping(snap.get("config") or {})
    snap["schema"] = _sanitize_mapping(snap.get("schema") or {})
    snap["presets"] = _sanitize_mapping(snap.get("presets") or get_builtin_profiles())
    snap.setdefault("epoch_history", [])
    snap.setdefault("pending_restart_keys", list(snap["config"].get("pending_restart_keys") or []))
    snap.setdefault(
        "config_quarantine_warnings",
        list(snap["config"].get("config_quarantine_warnings") or []),
    )

    if "funnel" not in snap or "audit" not in snap or "audit_history" not in snap:
        funnel_disk, audit_disk, audit_hist_disk = _load_funnel_and_audit()
        snap.setdefault("funnel", funnel_disk)
        snap.setdefault("audit", audit_disk)
        snap.setdefault("audit_history", audit_hist_disk)

    halted = bool(snap.get("is_halted") or snap.get("system_halted"))
    snap["is_halted"] = halted
    snap["system_halted"] = halted
    snap.setdefault("halt_reason", "")

    if not isinstance(snap.get("broker_status"), Mapping):
        snap["broker_status"] = {}
    else:
        snap["broker_status"] = dict(snap["broker_status"])

    bs = snap["broker_status"]
    bs.setdefault("broker_name", "Trading 212 (Live)" if use_live else "Trading 212 (Demo)")
    bs.setdefault("is_connected", bool(controller_attached or active_client is not None))
    bs.setdefault(
        "active_server_stops",
        sum(1 for s in active_signals if s.get("broker_stop_order_id")),
    )
    bs.setdefault("active_positions_count", len(active_signals))
    active_strategy = str((snap["config"] or {}).get("ACTIVE_STRATEGY") or "S3 Dual-Core")
    pure_tsl = bool((snap["config"] or {}).get("PURE_TSL_MODE", True))
    bs.setdefault(
        "execution_mode",
        f"{active_strategy} ({'Pure TSL' if pure_tsl else 'Standard TP/SL'})",
    )
    bs.setdefault("runner_status", "ONLINE (Controller)" if controller_attached else "READ-ONLY")
    bs.setdefault("monitored_assets", len(CURATED_3X_PAIRS))
    bs.setdefault("eur_ratio", _compute_eur_ratio(active_signals, snap["pairs_watchlist"]))
    return snap


def build_dashboard_data(
    force: bool = False,
    client: Optional[Trading212Client] = None,
    orchestrator: Optional[Any] = None,
) -> Dict[str, Any]:
    """Compile dashboard payload from attached Orchestrator, explicit test client, or read-only disk state."""
    global _DASHBOARD_DATA_CACHE, _DASHBOARD_DATA_CACHE_TIME, _DASHBOARD_DATA_CACHE_STAMP

    active_client = client if client is not None else _DASHBOARD_SHARED_CLIENT

    if orchestrator is not None:
        raw_snapshot = orchestrator.get_dashboard_snapshot()
        return _normalize_dashboard_snapshot(
            raw_snapshot, orchestrator=orchestrator, active_client=active_client
        )

    data_dir = Path("data")
    now = time.time()
    stamp = _disk_cache_stamp(data_dir)
    if not force and active_client is None:
        with _DASHBOARD_DATA_LOCK:
            if (
                _DASHBOARD_DATA_CACHE is not None
                and _DASHBOARD_DATA_CACHE_STAMP == stamp
                and (now - _DASHBOARD_DATA_CACHE_TIME) < 2.5
            ):
                return dict(_DASHBOARD_DATA_CACHE)

    with _DASHBOARD_DATA_LOCK:
        now = time.time()
        if (
            not force
            and active_client is None
            and _DASHBOARD_DATA_CACHE is not None
            and _DASHBOARD_DATA_CACHE_STAMP == stamp
            and (now - _DASHBOARD_DATA_CACHE_TIME) < 2.5
        ):
            return dict(_DASHBOARD_DATA_CACHE)

        universe = LeveragedUniverse()
        pairs = _build_pairs_watchlist(universe)

        portfolio_prices: Dict[str, float] = {}
        broker_positions_map: Optional[Dict[str, Any]] = None
        live_server_stops_count: Optional[int] = None
        live_account_cash_total: Optional[float] = None

        # Only query broker client if explicitly passed or explicitly test-patched via _DASHBOARD_SHARED_CLIENT.
        if active_client is not None:
            try:
                cash = active_client.get_account_cash()
                total_val = getattr(cash, "total", None) if cash is not None else None
                if isinstance(total_val, (int, float)) and not isinstance(total_val, bool):
                    live_account_cash_total = float(total_val)
            except Exception as cash_exc:
                logger.debug("Failed to query injected client account cash: %s", cash_exc)

            try:
                portfolio = active_client.get_portfolio(force_refresh=False)
                broker_positions_map = {
                    p.ticker: p for p in (portfolio or []) if getattr(p, "quantity", 0.0) > 0
                }
                for p in portfolio or []:
                    if getattr(p, "current_price", 0.0) > 0:
                        portfolio_prices[p.ticker] = p.current_price

                try:
                    open_orders = active_client.get_orders(force_refresh=False)
                    live_server_stops_count = sum(
                        1
                        for o in (open_orders or [])
                        if "STOP"
                        in str(
                            o.get("type") if isinstance(o, dict) else getattr(o, "type", "")
                        ).upper()
                    )
                except Exception as ord_exc:
                    logger.debug("Failed to fetch injected client open orders: %s", ord_exc)
            except Exception as exc:
                logger.debug("Failed to fetch injected client portfolio prices: %s", exc)

        # 1. Read active signals from local disk strictly read-only (ZERO writes to active_trades.json).
        raw_trades_state = _read_json_file(Path(settings.ACTIVE_TRADES_PATH), default={})
        active_signals = load_active_signals(
            settings.ACTIVE_TRADES_PATH, live_prices=portfolio_prices, universe=universe
        )
        active_signals = _enrich_active_signals_from_raw_state(active_signals, raw_trades_state)

        # 2. In-memory reconciliation when an explicit client was injected
        if broker_positions_map is not None:
            live_broker_tickers = set(broker_positions_map.keys())
            active_signals = [s for s in active_signals if s.get("ticker") in live_broker_tickers]
            for s in active_signals:
                tk = s.get("ticker")
                if tk in broker_positions_map:
                    bp_obj = broker_positions_map[tk]
                    if getattr(bp_obj, "ppl", None) is not None:
                        s["pnl_amount_eur"] = round(float(bp_obj.ppl), 2)

            known_tickers = {s.get("ticker") for s in active_signals}
            for b_ticker, bp in broker_positions_map.items():
                if b_ticker in known_tickers:
                    continue
                pair = universe.get_pair_for_etp(b_ticker)
                underlying = (
                    pair.underlying_symbol
                    if pair
                    else b_ticker.replace("l_EQ", "").replace("_US_EQ", "")
                )
                underlying_name = pair.name if pair else underlying
                direction = (
                    universe.get_etp_direction(b_ticker)
                    if universe
                    else get_etp_direction(b_ticker)
                )
                avg_px = (
                    bp.average_price
                    if getattr(bp, "average_price", 0.0) > 0
                    else (bp.current_price if getattr(bp, "current_price", 0.0) > 0 else 100.0)
                )
                cur_px = bp.current_price if getattr(bp, "current_price", 0.0) > 0 else avg_px
                sl_dist = round(avg_px * 0.05, 2)
                sl = round(max(0.01, avg_px - sl_dist), 2)
                pnl_pct = ((cur_px - avg_px) / avg_px * 100.0) if avg_px > 0 else 0.0
                pnl_amount = round((cur_px - avg_px) * bp.quantity, 2)
                pnl_amount_eur = round(float(getattr(bp, "ppl", 0.0) or 0.0), 2)

                curr = universe.get_ticker_currency(b_ticker) if universe else get_ticker_currency(b_ticker)
                currency_code, currency_prefix, currency_suffix = get_currency_display(curr)

                active_signals.append(
                    {
                        "ticker": b_ticker,
                        "underlying": underlying,
                        "name": underlying_name,
                        "direction": direction,
                        "leverage": 5 if ("5" in b_ticker) else 3,
                        "strategy_type": "broker_placed",
                        "rsi3": None,
                        "rsi7": None,
                        "rsi14": None,
                        "sma5": None,
                        "win_rate_exp": "",
                        "overnight_status": (
                            "REQUIRES BREAKEVEN OR FLATTENS 16:20 UTC"
                            if direction == "LONG"
                            else "INTRADAY SHORT (Flattens 16:20 UTC / Winter Time)"
                        ),
                        "overnight_badge_class": (
                            "bg-sky-500/10 text-sky-300 border-sky-500/30"
                            if direction == "LONG"
                            else "bg-rose-500/10 text-rose-300 border-rose-500/30"
                        ),
                        "earnings_in_days": None,
                        "entry_price": round(avg_px, 2),
                        "current_price": round(cur_px, 2),
                        "pnl_pct": round(pnl_pct, 2),
                        "pnl_amount": pnl_amount,
                        "pnl_amount_eur": pnl_amount_eur,
                        "currency_code": currency_code,
                        "currency_prefix": currency_prefix,
                        "currency_suffix": currency_suffix,
                        "stop_loss": sl,
                        "trailing_stop_loss": sl,
                        "stop_distance_pct": round(abs(avg_px - sl) / avg_px * 100, 2)
                        if avg_px > 0
                        else 5.0,
                        "broker_stop_order_id": None,
                        "quantity": bp.quantity,
                        "remaining_quantity": bp.quantity,
                        "status": "ACTIVE (Broker Placed)",
                        "entry_time": datetime.now(timezone.utc).isoformat(),
                        "atr": round(avg_px * 0.025, 2),
                        "copy_text": f"ADOPTED: {b_ticker} ({underlying}) Qty: {bp.quantity}",
                    }
                )

        history = load_recent_history(settings.TRADES_LOG_PATH)
        recent_notifications = load_recent_notifications(
            settings.NOTIFICATIONS_LOG_PATH, limit=30, universe=universe
        )

        if active_client is not None:
            broker_events = fetch_live_broker_notifications(active_client, universe, limit=25)
            if broker_events:
                seen_orders = set()
                merged_events = []
                for b_ev in broker_events:
                    oid = str(b_ev.get("order_id") or "")
                    if oid:
                        seen_orders.add(oid)
                    merged_events.append(b_ev)
                for n in recent_notifications:
                    n_oid = str(n.get("order_id") or "")
                    if n_oid and n_oid in seen_orders:
                        continue
                    tk = n.get("ticker", "")
                    if tk:
                        auth_dir = universe.get_etp_direction(tk) if universe else get_etp_direction(tk)
                        if auth_dir:
                            n["direction"] = auth_dir
                    merged_events.append(n)
                merged_events.sort(key=lambda x: str(x.get("timestamp", "")), reverse=True)
                recent_notifications = merged_events[:30]

        active_stops = (
            live_server_stops_count
            if live_server_stops_count is not None
            else sum(1 for s in active_signals if s.get("broker_stop_order_id"))
        )

        if live_account_cash_total is not None:
            base_equity = round(live_account_cash_total, 2)
        else:
            default_eq = float(getattr(settings, "DEFAULT_ACCOUNT_EQUITY", 10000.0) or 10000.0)
            base_equity = round(default_eq, 2)

        equity_points = [{"time": "Start", "equity": base_equity}]
        running_equity = base_equity
        chronological_events = [e for e in recent_notifications if e.get("pnl") is not None]
        chronological_events.reverse()
        for ev in chronological_events:
            pnl_val = float(ev.get("pnl") or 0.0)
            running_equity += pnl_val
            t_str = str(ev.get("timestamp") or "")[-12:-4] or "Trade"
            equity_points.append({"time": t_str, "equity": round(running_equity, 2)})

        total_open_pnl = sum(
            float(s.get("pnl_amount_eur", s.get("pnl_amount", 0.0)) or 0.0) for s in active_signals
        )
        if live_account_cash_total is not None and live_account_cash_total > 0:
            live_equity = round(live_account_cash_total + total_open_pnl, 2)
        else:
            live_equity = round(running_equity + total_open_pnl, 2)

        now_time = datetime.now(timezone.utc).strftime("%H:%M:%S")
        equity_points.append({"time": now_time, "equity": live_equity})

        config_bundle = _load_read_only_config_bundle(data_dir)
        funnel_data, audit_obj, audit_desc = _load_funnel_and_audit(data_dir)
        use_live = bool(getattr(settings, "USE_LIVE", False))
        cfg_values = config_bundle["config"]
        active_strategy = str(cfg_values.get("ACTIVE_STRATEGY") or "S3 Dual-Core")
        pure_tsl = bool(cfg_values.get("PURE_TSL_MODE", True))

        is_halted = any(bool(s.get("panic_failed")) for s in active_signals)
        halt_reason = (
            "Panic liquidation failure detected in persisted position state."
            if is_halted
            else ""
        )

        res: Dict[str, Any] = {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "mode": "LIVE" if use_live else "DEMO",
            "controller_attached": False,
            "read_only_reason": READ_ONLY_REASON_DEFAULT,
            "is_halted": is_halted,
            "system_halted": is_halted,
            "halt_reason": halt_reason,
            "active_signals_count": len(active_signals),
            "active_signals": active_signals,
            "recent_notifications": recent_notifications,
            "pairs_watchlist": pairs,
            "history": history,
            "equity_history": equity_points,
            "equity_points": equity_points,
            "base_equity": base_equity,
            "starting_equity": base_equity,
            "current_equity": live_equity,
            "rate_limit_telemetry": (
                active_client.get_rate_limit_telemetry()
                if active_client is not None and hasattr(active_client, "get_rate_limit_telemetry")
                else Trading212Client.get_rate_limit_telemetry()
            ),
            "funnel": funnel_data,
            "audit": audit_obj,
            "audit_history": audit_desc,
            "broker_status": {
                "broker_name": "Trading 212 (Live)" if use_live else "Trading 212 (Demo)",
                "is_connected": active_client is not None,
                "active_server_stops": active_stops,
                "active_positions_count": len(active_signals),
                "execution_mode": (
                    f"{active_strategy} ({'Pure TSL' if pure_tsl else 'Standard TP/SL'})"
                ),
                "runner_status": "READ-ONLY (Disk Snapshot)",
                "monitored_assets": len(CURATED_3X_PAIRS),
                "eur_ratio": _compute_eur_ratio(active_signals, pairs),
            },
            **config_bundle,
        }
        if active_client is None:
            _DASHBOARD_DATA_CACHE = res
            _DASHBOARD_DATA_CACHE_TIME = time.time()
            _DASHBOARD_DATA_CACHE_STAMP = stamp
        return res


def _load_bundled_assets() -> Tuple[str, str]:
    """Read packaged CSS and JS workbench assets from disk."""
    css_text = STYLES_ASSET_PATH.read_text(encoding="utf-8") if STYLES_ASSET_PATH.is_file() else ""
    js_text = WORKBENCH_ASSET_PATH.read_text(encoding="utf-8") if WORKBENCH_ASSET_PATH.is_file() else ""
    safe_css = re.sub(r"</style", r"\\3c/style", css_text, flags=re.IGNORECASE)
    safe_js = re.sub(r"</script", r"<\\/script", js_text, flags=re.IGNORECASE)
    return safe_css, safe_js


def _render_dashboard_html_string(data: Mapping[str, Any]) -> str:
    """Render self-hosted single-file HTML bundle with inline CSS, INITIAL_DATA, and workbench.js."""
    sanitized = _sanitize_mapping(data)
    safe_json = json_safe_for_inline_script(sanitized)
    safe_css, safe_js = _load_bundled_assets()
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Trading 212 Leveraged Quantitative Engine — Control Workbench</title>
  <style>
{safe_css}
  </style>
</head>
<body>
  <div id="workbench-app" class="wb-app-shell">
    <noscript>JavaScript is required to run the Trading 212 Control Workbench.</noscript>
  </div>
  <script>
    let INITIAL_DATA = {safe_json};
    window.__INITIAL_DATA__ = INITIAL_DATA;
  </script>
  <script>
{safe_js}
  </script>
</body>
</html>
"""


def generate_html_dashboard(
    output_path: str = "signals_dashboard.html",
    orchestrator: Optional[Any] = None,
    client: Optional[Any] = None,
) -> str:
    """Render self-hosted dashboard HTML atomically using packaged styles.css and workbench.js."""
    data = build_dashboard_data(force=True, client=client, orchestrator=orchestrator)
    html_content = _render_dashboard_html_string(data)

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(out.resolve().parent),
            prefix=f".{out.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp_file:
            tmp_path = tmp_file.name
            tmp_file.write(html_content)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, out)
    except Exception:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise

    try:
        SSEBroadcaster.get_instance().broadcast("dashboard_update", data)
    except Exception:
        pass

    logger.info("Generated self-hosted dashboard HTML at %s", output_path)
    return str(output_path)


def _has_unsafe_path_syntax(raw_path: str) -> bool:
    """Detect path traversal, dotfiles, backslashes, or null-byte attempts before routing."""
    if "\x00" in raw_path or "\\" in raw_path:
        return True
    decoded = urllib.parse.unquote(raw_path)
    if "\x00" in decoded or "\\" in decoded:
        return True
    for candidate in (raw_path, decoded):
        for seg in candidate.split("/"):
            if not seg:
                continue
            if seg in (".", ".."):
                return True
            if seg.startswith("."):
                return True
    return False


def _is_sensitive_path(clean_path: str) -> bool:
    """Return True if path attempts to access sensitive workspace files or directories."""
    if _has_unsafe_path_syntax(clean_path):
        return True
    decoded = urllib.parse.unquote(clean_path)
    segments = [seg.lower() for seg in decoded.split("/") if seg]
    if not segments:
        return False
    if segments[0] == "data":
        return True
    return any(seg in SENSITIVE_FILENAMES for seg in segments)


def _snapshot_to_dict(value: Any) -> Dict[str, Any]:
    """Serialize ConfigSnapshot or mapping objects into sanitized dictionaries."""
    if isinstance(value, ConfigSnapshot):
        return _sanitize_mapping(value.to_dict(include_specs=True))
    if isinstance(value, Mapping):
        return _sanitize_mapping(dict(value))
    return {}


def _reject_json_constant(token: str) -> Any:
    """Reject non-standard NaN/Infinity JSON tokens."""
    raise ValueError(f"Unsupported JSON constant: {token}")


def make_dashboard_handler(
    web_dir: Path,
    html_name: str,
    orchestrator: Optional[Any] = None,
    auth_manager: Optional[DashboardAuthManager] = None,
    *,
    stream_heartbeat_seconds: float = DEFAULT_STREAM_HEARTBEAT_SECONDS,
    max_sse_subscribers: int = MAX_SSE_SUBSCRIBERS,
):
    """Create fail-closed HTTP request handler for dashboard SPA, zero-quota telemetry, SSE, and controls."""
    resolved_web_dir = Path(web_dir).resolve()
    safe_html_name = Path(html_name).name
    shared_auth_manager = auth_manager or DashboardAuthManager()
    heartbeat_interval = max(0.05, float(stream_heartbeat_seconds))
    sse_cap = max(1, int(max_sse_subscribers))

    _re_param_delete = re.compile(r"^/api/params/([^/]+)/delete$")
    _re_pos_re_epoch_preview = re.compile(r"^/api/positions/([^/]+)/re-epoch/preview$")
    _re_pos_re_epoch = re.compile(r"^/api/positions/([^/]+)/re-epoch$")
    _re_pos_retry_liq = re.compile(r"^/api/positions/([^/]+)/retry-liquidation$")

    class ControlGatewayHandler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def setup(self) -> None:
            conn = getattr(self, "connection", None)
            if conn is not None and hasattr(conn, "settimeout"):
                try:
                    conn.settimeout(REQUEST_SOCKET_TIMEOUT_SECONDS)
                except OSError:
                    pass
            super().setup()

        def log_message(self, format: str, *args: Any) -> None:
            try:
                clean = format % args
            except Exception:
                clean = str(format)
            if any(
                p in clean
                for p in ("/stream", "/api/stream", "/events", "/api/data", "/api/dashboard-data")
            ):
                return
            logger.debug("%s - %s", self._client_ip(), clean)

        def _client_ip(self) -> str:
            addr = getattr(self, "client_address", None)
            if isinstance(addr, (tuple, list)) and addr:
                return str(addr[0])
            return "127.0.0.1"

        def _scrub(self, exc_or_msg: Any) -> str:
            if hasattr(shared_auth_manager, "scrub_error_message"):
                return shared_auth_manager.scrub_error_message(exc_or_msg)
            return scrub_error_message(exc_or_msg)

        def _write_common_security_headers(self, no_store: bool = True) -> None:
            self.send_header("Content-Security-Policy", DEFAULT_CSP_HEADER)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            if no_store:
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")

        def _send_bytes(
            self,
            status_code: int,
            body: bytes,
            content_type: str,
            *,
            no_store: bool = True,
            extra_headers: Optional[Mapping[str, str]] = None,
            send_body: bool = True,
            close_conn: bool = False,
        ) -> None:
            if close_conn:
                self.close_connection = True
            self.send_response(status_code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self._write_common_security_headers(no_store=no_store)
            if close_conn:
                self.send_header("Connection", "close")
            if extra_headers:
                for k, v in extra_headers.items():
                    self.send_header(k, v)
            self.end_headers()
            if send_body and body:
                self.wfile.write(body)

        def _send_json(
            self,
            status_code: int,
            payload: Any,
            *,
            extra_headers: Optional[Mapping[str, str]] = None,
            send_body: bool = True,
            close_conn: bool = False,
        ) -> None:
            body = json.dumps(payload, default=str).encode("utf-8")
            self._send_bytes(
                status_code,
                body,
                "application/json; charset=utf-8",
                no_store=True,
                extra_headers=extra_headers,
                send_body=send_body,
                close_conn=close_conn,
            )

        def _send_error_json(
            self,
            status_code: int,
            message: Any,
            *,
            extra_fields: Optional[Mapping[str, Any]] = None,
            extra_headers: Optional[Mapping[str, str]] = None,
            send_body: bool = True,
            close_conn: bool = True,
        ) -> None:
            payload: Dict[str, Any] = {"error": self._scrub(message)}
            if extra_fields:
                payload.update(extra_fields)
            self._send_json(
                status_code,
                payload,
                extra_headers=extra_headers,
                send_body=send_body,
                close_conn=close_conn,
            )

        def _validate_host(self, *, send_body: bool = True) -> bool:
            host_header = self.headers.get("Host")
            if not is_valid_host_header(host_header):
                self._send_error_json(
                    403,
                    "Forbidden: invalid or missing Host header.",
                    send_body=send_body,
                    close_conn=True,
                )
                return False
            return True

        def _validate_origin(self, *, send_body: bool = True) -> bool:
            origin_header = self.headers.get("Origin")
            host_header = self.headers.get("Host")
            if not is_valid_origin_header(origin_header, host_header):
                self._send_error_json(
                    403,
                    "Forbidden: Origin header mismatch.",
                    send_body=send_body,
                    close_conn=True,
                )
                return False
            return True

        def _authenticate_before_body_read(self) -> bool:
            auth_res = shared_auth_manager.verify_request_auth(self.headers, self._client_ip())
            if auth_res.ok:
                return True
            headers: Dict[str, str] = {}
            extra: Dict[str, Any] = {}
            if auth_res.status_code == 401:
                headers["WWW-Authenticate"] = 'Bearer realm="dashboard"'
            if auth_res.retry_after is not None:
                headers["Retry-After"] = str(max(1, int(math.ceil(auth_res.retry_after))))
                extra["retry_after"] = auth_res.retry_after
            self._send_error_json(
                auth_res.status_code,
                auth_res.error or "Unauthorized.",
                extra_fields=extra,
                extra_headers=headers,
                close_conn=True,
            )
            return False

        def _parse_json_body(self, *, allow_empty_body: bool = False) -> Tuple[bool, Dict[str, Any]]:
            transfer_encoding = str(self.headers.get("Transfer-Encoding") or "").strip().lower()
            if transfer_encoding:
                self._send_error_json(411, "Chunked Transfer-Encoding is not supported.")
                return False, {}

            raw_len = self.headers.get("Content-Length")
            if raw_len is None:
                if allow_empty_body:
                    ctype = str(self.headers.get("Content-Type") or "").strip()
                    if ctype and ctype.split(";", 1)[0].strip().lower() != "application/json":
                        self._send_error_json(415, "Unsupported Media Type: expected application/json.")
                        return False, {}
                    return True, {}
                self._send_error_json(411, "Length Required: Content-Length header is required.")
                return False, {}

            try:
                content_length = int(str(raw_len).strip())
            except ValueError:
                self._send_error_json(400, "Bad Request: invalid Content-Length header.")
                return False, {}

            if content_length < 0:
                self._send_error_json(400, "Bad Request: negative Content-Length.")
                return False, {}

            if content_length > MAX_REQUEST_BODY_BYTES:
                self._send_error_json(
                    413,
                    f"Payload Too Large: maximum request size is {MAX_REQUEST_BODY_BYTES} bytes.",
                )
                return False, {}

            raw_ctype = self.headers.get("Content-Type")
            media_type = (
                str(raw_ctype).split(";", 1)[0].strip().lower() if raw_ctype is not None else ""
            )
            if content_length == 0:
                if raw_ctype is not None and media_type != "application/json":
                    self._send_error_json(415, "Unsupported Media Type: expected application/json.")
                    return False, {}
                if raw_ctype is None and not allow_empty_body:
                    self._send_error_json(415, "Unsupported Media Type: Content-Type application/json required.")
                    return False, {}
                return True, {}

            if media_type != "application/json":
                self._send_error_json(415, "Unsupported Media Type: expected application/json.")
                return False, {}

            try:
                chunks: List[bytes] = []
                remaining = content_length
                while remaining > 0:
                    chunk = self.rfile.read(remaining)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                raw_body = b"".join(chunks)
            except (TimeoutError, socket.timeout):
                self._send_error_json(408, "Request Timeout while reading JSON payload.")
                return False, {}
            except OSError:
                self._send_error_json(400, "Bad Request: failed to read request body.")
                return False, {}

            if len(raw_body) != content_length:
                self._send_error_json(400, "Bad Request: truncated request body.")
                return False, {}

            try:
                decoded = raw_body.decode("utf-8")
                parsed = json.loads(decoded, parse_constant=_reject_json_constant)
            except Exception:
                self._send_error_json(400, "Bad Request: malformed JSON body.")
                return False, {}

            if not isinstance(parsed, dict):
                self._send_error_json(400, "Bad Request: JSON body must be an object.")
                return False, {}
            return True, parsed

        def _get_snapshot(self, *, force: bool = False) -> Dict[str, Any]:
            return build_dashboard_data(force=force, orchestrator=orchestrator)

        def _current_epoch_id(self) -> str:
            if orchestrator is not None and getattr(orchestrator, "config_bus", None) is not None:
                try:
                    return str(orchestrator.config_bus.active_epoch_id)
                except Exception:
                    pass
            try:
                snap = self._get_snapshot(force=False)
                return str(snap.get("config_epoch_id") or "epoch-0")
            except Exception:
                return "epoch-0"

        def _handle_sse_stream(self, send_body: bool = True) -> None:
            broadcaster = SSEBroadcaster.get_instance()
            q = broadcaster.subscribe_stream(
                max_subscribers=sse_cap, drop_oldest=True, maxsize=64
            )
            if q is None:
                self._send_error_json(
                    503,
                    f"Service Unavailable: maximum concurrent SSE subscribers ({sse_cap}) reached.",
                    send_body=send_body,
                    close_conn=True,
                )
                return

            self.close_connection = True
            conn = getattr(self, "connection", None)
            if conn is not None and hasattr(conn, "settimeout"):
                try:
                    conn.settimeout(None)
                except OSError:
                    pass

            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Connection", "close")
                self.send_header("X-Accel-Buffering", "no")
                self._write_common_security_headers(no_store=True)
                self.end_headers()
                if not send_body:
                    return

                initial_data = self._get_snapshot(force=False)
                snapshot_str = json.dumps(initial_data, default=str)
                self.wfile.write(f"event: snapshot\ndata: {snapshot_str}\n\n".encode("utf-8"))
                self.wfile.flush()

                while True:
                    try:
                        msg = q.get(timeout=heartbeat_interval)
                        if msg is None:
                            break
                        if isinstance(msg, Mapping):
                            event_name = _SAFE_SSE_EVENT_RE.sub(
                                "_", str(msg.get("event") or "message")
                            )
                            data_str = json.dumps(msg.get("data", {}), default=str)
                        else:
                            event_name = "message"
                            data_str = json.dumps(msg, default=str)
                        self.wfile.write(
                            f"event: {event_name}\ndata: {data_str}\n\n".encode("utf-8")
                        )
                        self.wfile.flush()
                    except queue.Empty:
                        hb = {
                            "epoch": self._current_epoch_id(),
                            "ts": datetime.now(timezone.utc).isoformat(),
                        }
                        self.wfile.write(
                            f"event: heartbeat\ndata: {json.dumps(hb)}\n\n".encode("utf-8")
                        )
                        self.wfile.flush()
            except (
                BrokenPipeError,
                ConnectionResetError,
                ConnectionAbortedError,
                TimeoutError,
                OSError,
            ):
                pass
            finally:
                broadcaster.unsubscribe_stream(q)

        def _serve_packaged_asset(
            self, asset_path: Path, content_type: str, *, send_body: bool
        ) -> None:
            resolved_asset = asset_path.resolve()
            if not resolved_asset.is_relative_to(ASSETS_DIR.resolve()) or not resolved_asset.is_file():
                self._send_error_json(404, "Asset not found.", send_body=send_body, close_conn=False)
                return
            try:
                content = resolved_asset.read_bytes()
            except OSError:
                self._send_error_json(404, "Asset not found.", send_body=send_body, close_conn=False)
                return
            self._send_bytes(
                200,
                content,
                content_type,
                no_store=True,
                send_body=send_body,
                close_conn=False,
            )

        def _serve_dashboard_html(self, *, send_body: bool) -> None:
            candidate = resolved_web_dir / safe_html_name
            try:
                resolved = candidate.resolve()
            except OSError:
                self._send_error_json(403, "Forbidden.", send_body=send_body, close_conn=True)
                return

            if not resolved.is_relative_to(resolved_web_dir):
                self._send_error_json(403, "Forbidden.", send_body=send_body, close_conn=True)
                return

            if candidate.is_symlink():
                # Reject symlinks pointing outside web_dir (already caught by resolve()) and unsafe links
                self._send_error_json(403, "Forbidden: symlinked HTML rejected.", send_body=send_body, close_conn=True)
                return

            if resolved.is_file():
                try:
                    html_bytes = resolved.read_bytes()
                except OSError:
                    html_bytes = _render_dashboard_html_string(
                        self._get_snapshot(force=False)
                    ).encode("utf-8")
            else:
                html_bytes = _render_dashboard_html_string(
                    self._get_snapshot(force=False)
                ).encode("utf-8")

            self._send_bytes(
                200,
                html_bytes,
                "text/html; charset=utf-8",
                no_store=True,
                send_body=send_body,
                close_conn=False,
            )

        def _handle_get_or_head(self, *, send_body: bool) -> None:
            if not self._validate_host(send_body=send_body):
                return

            raw_path = urllib.parse.urlsplit(self.path).path or "/"
            if _is_sensitive_path(raw_path):
                self._send_error_json(403, "Forbidden.", send_body=send_body, close_conn=True)
                return

            clean_path = urllib.parse.unquote(raw_path)

            if clean_path in ("/", f"/{safe_html_name}"):
                self._serve_dashboard_html(send_body=send_body)
                return

            if clean_path == "/assets/styles.css":
                self._serve_packaged_asset(
                    STYLES_ASSET_PATH, "text/css; charset=utf-8", send_body=send_body
                )
                return

            if clean_path == "/assets/workbench.js":
                self._serve_packaged_asset(
                    WORKBENCH_ASSET_PATH,
                    "application/javascript; charset=utf-8",
                    send_body=send_body,
                )
                return

            if clean_path in ("/api/data", "/api/dashboard-data", "/data.json"):
                data = self._get_snapshot(force=False)
                self._send_json(200, data, send_body=send_body, close_conn=False)
                return

            if clean_path == "/api/schema/config":
                snap = self._get_snapshot(force=False)
                self._send_json(
                    200,
                    {
                        "config_epoch_id": snap.get("config_epoch_id", "epoch-0"),
                        "schema": snap.get("schema", {}),
                        "presets": snap.get("presets", {}),
                        "controller_attached": bool(snap.get("controller_attached", False)),
                    },
                    send_body=send_body,
                    close_conn=False,
                )
                return

            if clean_path == "/api/config":
                snap = self._get_snapshot(force=False)
                cfg = _sanitize_mapping(snap.get("config") or {})
                self._send_json(
                    200,
                    {
                        "config_epoch_id": snap.get("config_epoch_id", "epoch-0"),
                        "config": cfg,
                        "values": cfg,
                        "pending_restart_keys": snap.get("pending_restart_keys", []),
                        "config_quarantine_warnings": snap.get("config_quarantine_warnings", []),
                        "epoch_history": snap.get("epoch_history", []),
                        "controller_attached": bool(snap.get("controller_attached", False)),
                    },
                    send_body=send_body,
                    close_conn=False,
                )
                return

            if clean_path in ("/stream", "/api/stream", "/events"):
                self._handle_sse_stream(send_body=send_body)
                return

            self._send_error_json(404, "Not Found.", send_body=send_body, close_conn=False)

        def do_GET(self) -> None:
            self._handle_get_or_head(send_body=True)

        def do_HEAD(self) -> None:
            self._handle_get_or_head(send_body=False)

        def do_OPTIONS(self) -> None:
            if not self._validate_host(send_body=True):
                return
            if not self._validate_origin(send_body=True):
                return
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self._write_common_security_headers(no_store=True)
            self.end_headers()

        def _handle_control_exception(self, exc: Exception) -> None:
            exc_name = type(exc).__name__
            if isinstance(exc, ConfigConflictError) or exc_name == "ConfigConflictError":
                latest = getattr(exc, "latest_snapshot", None)
                latest_dict = _snapshot_to_dict(latest)
                if not latest_dict and orchestrator is not None:
                    bus = getattr(orchestrator, "config_bus", None)
                    if bus is not None:
                        try:
                            latest_dict = _snapshot_to_dict(bus.active_snapshot())
                        except Exception:
                            latest_dict = {}
                actual_epoch = getattr(exc, "actual_epoch_id", None) or latest_dict.get(
                    "config_epoch_id"
                )
                self._send_error_json(
                    409,
                    exc,
                    extra_fields={
                        "expected_epoch_id": getattr(exc, "expected_epoch_id", None),
                        "actual_epoch_id": actual_epoch,
                        "active_epoch_id": actual_epoch,
                        "config_epoch_id": actual_epoch,
                        "latest_snapshot": latest_dict,
                        "config": latest_dict,
                    },
                    close_conn=False,
                )
                return

            if (
                isinstance(exc, PositionEpochConflictError)
                or exc_name == "PositionEpochConflictError"
            ):
                snap = self._get_snapshot(force=True)
                self._send_error_json(
                    409,
                    exc,
                    extra_fields={
                        "ticker": getattr(exc, "ticker", ""),
                        "expected_epoch_id": getattr(exc, "expected_epoch_id", None),
                        "actual_epoch_id": getattr(exc, "actual_epoch_id", None),
                        "expected_position_epoch_id": getattr(
                            exc, "expected_position_epoch_id", None
                        ),
                        "actual_position_epoch_id": getattr(
                            exc, "actual_position_epoch_id", None
                        ),
                        "expected_target_epoch_id": getattr(
                            exc, "expected_target_epoch_id", None
                        ),
                        "actual_target_epoch_id": getattr(exc, "actual_target_epoch_id", None),
                        "dashboard": snap,
                    },
                    close_conn=False,
                )
                return

            if isinstance(exc, ResumeBlockedError) or exc_name == "ResumeBlockedError":
                snap = self._get_snapshot(force=True)
                self._send_error_json(
                    409,
                    exc,
                    extra_fields={
                        "unprotected_positions": getattr(exc, "unprotected_positions", None)
                        or [],
                        "dashboard": snap,
                    },
                    close_conn=False,
                )
                return

            if isinstance(exc, ConfigValidationError) or exc_name == "ConfigValidationError":
                self._send_error_json(
                    422,
                    exc,
                    extra_fields={"validation_errors": list(getattr(exc, "errors", []) or [])},
                    close_conn=False,
                )
                return

            if isinstance(exc, KeyError):
                key_label = str(exc.args[0]) if exc.args else "resource"
                self._send_error_json(
                    404,
                    f"Not found: {key_label}",
                    close_conn=False,
                )
                return

            if isinstance(exc, ValueError):
                self._send_error_json(400, exc, close_conn=False)
                return

            self._send_error_json(500, exc, close_conn=False)

        def _publish_mutation_snapshot(self, event_name: str, result: Any) -> Dict[str, Any]:
            _invalidate_dashboard_cache()
            snapshot = self._get_snapshot(force=True)
            result_dict = _snapshot_to_dict(result)
            try:
                if event_name == "config_epoch_changed":
                    SSEBroadcaster.get_instance().broadcast(
                        "config_epoch_changed",
                        {
                            "config_epoch_id": snapshot.get("config_epoch_id"),
                            "dashboard": snapshot,
                        },
                    )
                elif event_name == "panic_flatten":
                    broadcaster = SSEBroadcaster.get_instance()
                    broadcaster.broadcast("panic_flatten", snapshot)
                    broadcaster.broadcast("dashboard_update", snapshot)
                else:
                    SSEBroadcaster.get_instance().broadcast(event_name, snapshot)
            except Exception as b_exc:
                logger.debug("SSE broadcast failed after mutation: %s", b_exc)
            response: Dict[str, Any] = dict(result_dict)
            response["ok"] = True
            response["dashboard"] = snapshot
            response.setdefault("config_epoch_id", snapshot.get("config_epoch_id"))
            response.setdefault("config", snapshot.get("config"))
            return response

        def do_POST(self) -> None:
            if not self._validate_host():
                return
            if not self._validate_origin():
                return

            # Pre-auth body refusal: authenticate BEFORE reading a single byte of rfile.
            if not self._authenticate_before_body_read():
                return

            raw_path = urllib.parse.urlsplit(self.path).path or "/"
            if _is_sensitive_path(raw_path):
                self._send_error_json(403, "Forbidden.", close_conn=True)
                return

            clean_path = urllib.parse.unquote(raw_path)
            allow_empty = raw_path in ("/api/auth/verify", "/api/panic", "/panic")
            ok_body, payload = self._parse_json_body(allow_empty_body=allow_empty)
            if not ok_body:
                return

            if clean_path == "/api/auth/verify":
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "controller_attached": orchestrator is not None,
                        "read_only_reason": ""
                        if orchestrator is not None
                        else READ_ONLY_REASON_DEFAULT,
                    },
                    close_conn=False,
                )
                return

            param_delete_match = _re_param_delete.match(raw_path)
            pos_preview_match = _re_pos_re_epoch_preview.match(raw_path)
            pos_re_epoch_match = _re_pos_re_epoch.match(raw_path)
            pos_retry_match = _re_pos_retry_liq.match(raw_path)

            known_control_route = (
                clean_path
                in (
                    "/api/config/preview",
                    "/api/config",
                    "/api/params",
                    "/api/positions/re-epoch-all/preview",
                    "/api/positions/re-epoch-all",
                    "/api/panic",
                    "/panic",
                    "/api/resume",
                )
                or param_delete_match is not None
                or pos_preview_match is not None
                or pos_re_epoch_match is not None
                or pos_retry_match is not None
            )
            if not known_control_route:
                self._send_error_json(404, "Not Found.", close_conn=False)
                return

            if orchestrator is None:
                self._send_error_json(
                    503,
                    READ_ONLY_REASON_DEFAULT,
                    extra_fields={"controller_attached": False},
                    close_conn=False,
                )
                return

            try:
                if clean_path == "/api/config/preview":
                    profile_id = payload.get("profile_id")
                    if profile_id:
                        profiles = get_builtin_profiles()
                        if str(profile_id) not in profiles:
                            raise KeyError(f"Unknown config profile: {profile_id}")
                        patch = dict(profiles[str(profile_id)].get("overrides", profiles[str(profile_id)]))
                    else:
                        patch = payload.get("patch")
                        if not isinstance(patch, Mapping):
                            raise ValueError("patch must be a JSON object.")
                    expected_epoch_id = payload.get("expected_epoch_id")
                    preview_res = orchestrator.preview_config_patch(
                        dict(patch), expected_epoch_id=expected_epoch_id
                    )
                    self._send_json(200, _sanitize_mapping(preview_res), close_conn=False)
                    return

                if clean_path == "/api/config":
                    expected_epoch_id = payload.get("expected_epoch_id")
                    if expected_epoch_id is None:
                        raise ValueError("expected_epoch_id is required.")
                    author = str(payload.get("author") or "operator")
                    reason = str(payload.get("reason") or "")
                    profile_id = payload.get("profile_id")
                    if profile_id:
                        if hasattr(orchestrator, "apply_config_profile") and callable(
                            orchestrator.apply_config_profile
                        ):
                            res = orchestrator.apply_config_profile(
                                str(profile_id),
                                str(expected_epoch_id),
                                author=author,
                                reason=reason,
                            )
                        else:
                            profiles = get_builtin_profiles()
                            if str(profile_id) not in profiles:
                                raise KeyError(f"Unknown config profile: {profile_id}")
                            profile_patch = dict(
                                profiles[str(profile_id)].get("overrides", profiles[str(profile_id)])
                            )
                            res = orchestrator.commit_config_patch(
                                profile_patch,
                                str(expected_epoch_id),
                                author=author,
                                reason=reason,
                            )
                    else:
                        patch = payload.get("patch")
                        if not isinstance(patch, Mapping):
                            raise ValueError("patch must be a JSON object.")
                        res = orchestrator.commit_config_patch(
                            dict(patch),
                            str(expected_epoch_id),
                            author=author,
                            reason=reason,
                        )
                    self._send_json(
                        200,
                        self._publish_mutation_snapshot("config_epoch_changed", res),
                        close_conn=False,
                    )
                    return

                if clean_path == "/api/params":
                    spec = payload.get("spec")
                    if not isinstance(spec, Mapping):
                        spec = {
                            k: v
                            for k, v in payload.items()
                            if k not in ("expected_epoch_id", "author", "reason")
                        }
                    if not spec:
                        raise ValueError("Dynamic parameter spec is required.")
                    expected_epoch_id = payload.get("expected_epoch_id")
                    res = orchestrator.add_custom_parameter(
                        dict(spec), expected_epoch_id=expected_epoch_id
                    )
                    self._send_json(
                        200,
                        self._publish_mutation_snapshot("config_epoch_changed", res),
                        close_conn=False,
                    )
                    return

                if param_delete_match is not None:
                    param_key = urllib.parse.unquote(param_delete_match.group(1)).strip()
                    if not param_key:
                        raise ValueError("Parameter key is required.")
                    expected_epoch_id = payload.get("expected_epoch_id")
                    res = orchestrator.delete_custom_parameter(
                        param_key, expected_epoch_id=expected_epoch_id
                    )
                    self._send_json(
                        200,
                        self._publish_mutation_snapshot("config_epoch_changed", res),
                        close_conn=False,
                    )
                    return

                if pos_preview_match is not None:
                    ticker = urllib.parse.unquote(pos_preview_match.group(1)).strip()
                    if not ticker:
                        raise ValueError("Ticker is required.")
                    preview_res = orchestrator.preview_position_re_epoch(
                        ticker,
                        target_epoch_id=payload.get("target_epoch_id"),
                        confirm_pure_tsl_mode_change=bool(
                            payload.get("confirm_pure_tsl_mode_change", False)
                        ),
                    )
                    self._send_json(200, _sanitize_mapping(preview_res), close_conn=False)
                    return

                if pos_re_epoch_match is not None:
                    ticker = urllib.parse.unquote(pos_re_epoch_match.group(1)).strip()
                    if not ticker:
                        raise ValueError("Ticker is required.")
                    res = orchestrator.re_epoch_position(
                        ticker,
                        expected_position_epoch_id=payload.get("expected_position_epoch_id"),
                        target_epoch_id=payload.get("target_epoch_id"),
                        confirm_pure_tsl_mode_change=bool(
                            payload.get("confirm_pure_tsl_mode_change", False)
                        ),
                    )
                    self._send_json(
                        200,
                        self._publish_mutation_snapshot("dashboard_update", res),
                        close_conn=False,
                    )
                    return

                if clean_path == "/api/positions/re-epoch-all/preview":
                    preview_res = orchestrator.preview_re_epoch_all(
                        target_epoch_id=payload.get("target_epoch_id"),
                        confirm_pure_tsl_mode_change=bool(
                            payload.get("confirm_pure_tsl_mode_change", False)
                        ),
                    )
                    self._send_json(200, _sanitize_mapping(preview_res), close_conn=False)
                    return

                if clean_path == "/api/positions/re-epoch-all":
                    res = orchestrator.re_epoch_all_positions(
                        expected_epoch_id=payload.get("expected_epoch_id"),
                        target_epoch_id=payload.get("target_epoch_id"),
                        confirm_pure_tsl_mode_change=bool(
                            payload.get("confirm_pure_tsl_mode_change", False)
                        ),
                    )
                    self._send_json(
                        200,
                        self._publish_mutation_snapshot("dashboard_update", res),
                        close_conn=False,
                    )
                    return

                if clean_path in ("/api/panic", "/panic"):
                    reason = str(payload.get("reason") or "Dashboard emergency panic flatten")
                    res = orchestrator.trigger_panic_flatten(reason=reason)
                    self._send_json(
                        200,
                        self._publish_mutation_snapshot("panic_flatten", res),
                        close_conn=False,
                    )
                    return

                if clean_path == "/api/resume":
                    confirm_unprotected = bool(payload.get("confirm_unprotected", False))
                    res = orchestrator.resume_trading(confirm_unprotected=confirm_unprotected)
                    self._send_json(
                        200,
                        self._publish_mutation_snapshot("dashboard_update", res),
                        close_conn=False,
                    )
                    return

                if pos_retry_match is not None:
                    ticker = urllib.parse.unquote(pos_retry_match.group(1)).strip()
                    if not ticker:
                        raise ValueError("Ticker is required.")
                    res = orchestrator.retry_panic_liquidation(ticker)
                    self._send_json(
                        200,
                        self._publish_mutation_snapshot("dashboard_update", res),
                        close_conn=False,
                    )
                    return

            except Exception as exc:
                self._handle_control_exception(exc)

        def _reject_unsupported_method(self) -> None:
            if not self._validate_host():
                return
            if not self._validate_origin():
                return
            self._send_error_json(405, f"Method {self.command} is not allowed.", close_conn=True)

        do_PUT = _reject_unsupported_method
        do_DELETE = _reject_unsupported_method
        do_PATCH = _reject_unsupported_method

    return ControlGatewayHandler


def _resolve_dashboard_bind_host(host: Optional[str] = None) -> str:
    """Resolve bind host: explicit host -> ``DASHBOARD_HOST`` env -> test/LAN default."""
    if isinstance(host, str) and host.strip():
        return host.strip()
    env_host = os.environ.get("DASHBOARD_HOST", "").strip()
    if env_host:
        return env_host
    return "127.0.0.1" if "unittest" in sys.modules else "0.0.0.0"


def _ensure_port_free(host: str, port: int) -> None:
    """Detect active listeners occupying ``port`` across wildcard/loopback interfaces."""
    if port <= 0:
        return
    if host in ("0.0.0.0", "127.0.0.1", "localhost"):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("0.0.0.0", port))
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as conn_probe:
                conn_probe.settimeout(0.05)
                if conn_probe.connect_ex(("127.0.0.1", port)) == 0:
                    raise OSError(
                        errno.EADDRINUSE,
                        f"Port {port} is already accepting connections on localhost",
                    )
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                raise


def _bind_dashboard_server(
    host: str, port: int, handler_cls: Any
) -> ThreadingDashboardServer:
    """Bind ThreadingDashboardServer to ``(host, port)`` after checking active listeners."""
    _ensure_port_free(host, port)
    return ThreadingDashboardServer((host, port), handler_cls)


def start_local_server_thread(
    html_file: str = "signals_dashboard.html",
    port: int = 8888,
    open_browser: bool = False,
    orchestrator: Optional[Any] = None,
    auth_manager: Optional[DashboardAuthManager] = None,
    allow_port_fallback: bool = True,
    host: Optional[str] = None,
) -> Tuple[Optional[threading.Thread], Optional[socketserver.TCPServer], str]:
    """Start the dashboard HTTP control gateway server in a background daemon thread."""
    active_auth = auth_manager or DashboardAuthManager()
    bind_host = _resolve_dashboard_bind_host(host)
    generate_html_dashboard(html_file, orchestrator=orchestrator)
    web_dir = Path(html_file).resolve().parent
    html_name = Path(html_file).name
    HandlerClass = make_dashboard_handler(
        web_dir,
        html_name,
        orchestrator=orchestrator,
        auth_manager=active_auth,
    )

    ThreadingDashboardServer.allow_reuse_address = True
    httpd: Optional[ThreadingDashboardServer] = None
    bind_port = port

    if allow_port_fallback:
        for attempt in range(5):
            candidate = port + attempt
            try:
                httpd = _bind_dashboard_server(bind_host, candidate, HandlerClass)
                bind_port = httpd.server_address[1] or candidate
                break
            except OSError:
                continue
        if httpd is None:
            logger.warning(
                "Could not bind dashboard server on %s to any port in range %d-%d",
                bind_host,
                port,
                port + 4,
            )
            return None, None, ""
    else:
        last_exc: Optional[OSError] = None
        for attempt in range(6):
            try:
                httpd = _bind_dashboard_server(bind_host, port, HandlerClass)
                bind_port = httpd.server_address[1] or port
                break
            except OSError as exc:
                last_exc = exc
                if attempt < 5:
                    time.sleep(0.1)
        if httpd is None:
            assert last_exc is not None
            raise last_exc

    display_host = "localhost" if bind_host in ("0.0.0.0", "127.0.0.1", "::", "::1") else bind_host
    url = f"http://{display_host}:{bind_port}/{html_name}"

    def _serve() -> None:
        try:
            httpd.serve_forever()
        except Exception:
            pass

    thread = threading.Thread(target=_serve, daemon=True, name="DashboardServerThread")
    thread.start()
    logger.info("Signals Dashboard gateway listening on %s:%d (%s)", bind_host, bind_port, url)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    return thread, httpd, url


def start_local_server(
    html_file: str = "signals_dashboard.html",
    port: int = 8888,
    open_browser: bool = True,
    auth_manager: Optional[DashboardAuthManager] = None,
    host: Optional[str] = None,
) -> None:
    """Serve the standalone dashboard strictly read-only without sidecar monitors."""
    active_auth = auth_manager or DashboardAuthManager()
    active_auth.get_token()
    bind_host = _resolve_dashboard_bind_host(host)
    generate_html_dashboard(html_file, orchestrator=None)

    web_dir = Path(html_file).resolve().parent
    html_name = Path(html_file).name
    HandlerClass = make_dashboard_handler(
        web_dir,
        html_name,
        orchestrator=None,
        auth_manager=active_auth,
    )

    ThreadingDashboardServer.allow_reuse_address = True
    with _bind_dashboard_server(bind_host, port, HandlerClass) as httpd:
        actual_port = httpd.server_address[1] or port
        display_host = "localhost" if bind_host in ("0.0.0.0", "127.0.0.1", "::", "::1") else bind_host
        url = f"http://{display_host}:{actual_port}/{html_name}"
        print(
            f"\nSignals Dashboard running (standalone read-only mode, bound to {bind_host}:{actual_port}) at: {url}"
        )
        print(active_auth.setup_hint())
        print("Press Ctrl+C to stop the dashboard server.\n")
        if open_browser:
            webbrowser.open(url)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nDashboard server stopped.")


def main() -> None:
    """CLI entry point for Signals Dashboard."""
    parser = argparse.ArgumentParser(description="3x Quantitative Signals Dashboard")
    parser.add_argument(
        "--port", type=int, default=8888, help="Port to serve dashboard (default: 8888)"
    )
    parser.add_argument(
        "--host",
        type=str,
        default=None,
        help="Host interface to bind (default: DASHBOARD_HOST env or 0.0.0.0 in production)",
    )
    parser.add_argument(
        "--no-browser", action="store_true", help="Do not open browser automatically"
    )
    parser.add_argument(
        "--generate-only",
        action="store_true",
        help="Generate HTML and exit without running server",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="signals_dashboard.html",
        help="Path to write dashboard HTML",
    )
    parser.add_argument(
        "--print-token",
        action="store_true",
        help="Print the active dashboard authentication token and exit",
    )
    parser.add_argument(
        "--token-file",
        type=str,
        default=None,
        help="Optional path to the persistent dashboard token file",
    )
    args = parser.parse_args()

    auth_mgr = DashboardAuthManager(token_file=args.token_file)
    if args.print_token:
        auth_mgr.print_token()
        return

    html_path = generate_html_dashboard(args.output)
    print(f"Dashboard generated: {os.path.abspath(html_path)}")

    if not args.generate_only:
        start_local_server(
            html_path,
            port=args.port,
            open_browser=not args.no_browser,
            auth_manager=auth_mgr,
            host=args.host,
        )


if __name__ == "__main__":
    main()
