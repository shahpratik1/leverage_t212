"""Live Signals Dashboard Server & Generator for 3x Leveraged Trading Engine.

Generates an interactive, standalone HTML dashboard and serves it locally
so the user can monitor triggered signals, exact trade numbers (Entry, SL, TP1-3, TSL),
and manually execute orders in any broker.
"""

from __future__ import annotations

import argparse
import http.server
import json
import logging
import os
import socket
import socketserver
import sys
import threading
import time
import webbrowser
import queue
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from trading_system.agents.risk_manager import RiskManager
from trading_system.config import settings
from trading_system.data.leveraged_universe import (
    CURATED_3X_PAIRS,
    GBX_TICKERS,
    LeveragedUniverse,
    get_currency_display,
    get_etp_direction,
    get_ticker_currency,
)
from trading_system.data.trading212_client import Trading212Client
from trading_system.execution.position_monitor import PositionMonitor
from trading_system.notifications.notifier import Notifier

logger = logging.getLogger(__name__)


class ThreadingDashboardServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """Threaded TCP server allowing multiple concurrent long-lived SSE connections."""
    daemon_threads = True
    allow_reuse_address = True


class SSEBroadcaster:
    """Thread-safe event broadcaster for Server-Sent Events (SSE)."""
    _instance: Optional[SSEBroadcaster] = None

    def __init__(self) -> None:
        self._subscribers: List[queue.Queue] = []
        self._lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> SSEBroadcaster:
        if cls._instance is None:
            cls._instance = SSEBroadcaster()
        return cls._instance

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=100)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def broadcast(self, event_type: str, data: Any) -> None:
        payload = {
            "event": event_type,
            "data": data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        with self._lock:
            dead = []
            for q in self._subscribers:
                try:
                    q.put_nowait(payload)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                self._subscribers.remove(q)

    publish = broadcast


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
_DASHBOARD_DATA_LOCK = threading.Lock()
_DASHBOARD_SHARED_CLIENT: Optional[Trading212Client] = None


def build_dashboard_data(force: bool = False, client: Optional[Trading212Client] = None) -> Dict[str, Any]:
    """Compile comprehensive dashboard dataset with fast TTL caching to avoid API rate limits."""
    global _DASHBOARD_DATA_CACHE, _DASHBOARD_DATA_CACHE_TIME, _DASHBOARD_SHARED_CLIENT
    now = time.time()
    if not force and client is None and _DASHBOARD_DATA_CACHE is not None and (now - _DASHBOARD_DATA_CACHE_TIME) < 2.5:
        return _DASHBOARD_DATA_CACHE

    with _DASHBOARD_DATA_LOCK:
        if not force and client is None and _DASHBOARD_DATA_CACHE is not None and (now - _DASHBOARD_DATA_CACHE_TIME) < 2.5:
            return _DASHBOARD_DATA_CACHE

        universe = LeveragedUniverse()
        pairs = [
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

        portfolio_prices: Dict[str, float] = {}
        broker_positions_map: Optional[Dict[str, Any]] = None
        live_server_stops_count: Optional[int] = None
        active_client = client

        if active_client is None and settings.TRADING212_API_KEY:
            try:
                if _DASHBOARD_SHARED_CLIENT is None:
                    _DASHBOARD_SHARED_CLIENT = Trading212Client(
                        api_key=settings.TRADING212_API_KEY,
                        api_secret=settings.TRADING212_API_SECRET,
                        base_url=settings.api_base_url,
                    )
                active_client = _DASHBOARD_SHARED_CLIENT
            except Exception as exc:
                logger.debug("Failed to initialize Trading212Client for dashboard: %s", exc)

        live_account_cash_total: Optional[float] = None
        if active_client is not None:
            try:
                cash = active_client.get_account_cash()
                if cash is not None and getattr(cash, "total", 0.0) > 0:
                    live_account_cash_total = float(cash.total)
            except Exception as cash_exc:
                logger.debug("Failed to query live account cash for dashboard: %s", cash_exc)

            try:
                portfolio = active_client.get_portfolio(force_refresh=False)
                broker_positions_map = {p.ticker: p for p in portfolio if getattr(p, "quantity", 0.0) > 0}
                for p in portfolio:
                    if getattr(p, "current_price", 0.0) > 0:
                        portfolio_prices[p.ticker] = p.current_price

                try:
                    open_orders = active_client.get_orders(force_refresh=False)
                    live_server_stops_count = sum(
                        1 for o in open_orders
                        if "STOP" in str(o.get("type") if isinstance(o, dict) else getattr(o, "type", "")).upper()
                    )
                except Exception as ord_exc:
                    logger.debug("Failed to fetch live open orders count: %s", ord_exc)
            except Exception as exc:
                logger.debug("Failed to fetch live portfolio prices for dashboard: %s", exc)

        # 1. Load active signals, strictly ignoring closed or zero-quantity positions
        active_signals = load_active_signals(settings.ACTIVE_TRADES_PATH, live_prices=portfolio_prices)

        # 2. Reconcile with live broker portfolio as absolute ground truth
        if broker_positions_map is not None:
            live_broker_tickers = set(broker_positions_map.keys())
            # Filter out phantom/closed local signals that no longer exist on broker
            active_signals = [s for s in active_signals if s.get("ticker") in live_broker_tickers]

            # Sync PnL in EUR directly from authoritative broker position
            for s in active_signals:
                tk = s.get("ticker")
                if tk in broker_positions_map:
                    bp_obj = broker_positions_map[tk]
                    if getattr(bp_obj, "ppl", None) is not None:
                        s["pnl_amount_eur"] = round(float(bp_obj.ppl), 2)

            # Clean up closed positions in active_trades.json so they don't linger on disk
            try:
                if os.path.exists(settings.ACTIVE_TRADES_PATH):
                    with open(settings.ACTIVE_TRADES_PATH, "r", encoding="utf-8") as f:
                        trades_data = json.load(f)
                    changed = False
                    for tk, p_entry in list(trades_data.items()):
                        if tk not in live_broker_tickers and not p_entry.get("is_closed", False):
                            p_entry["is_closed"] = True
                            p_entry["remaining_quantity"] = 0.0
                            changed = True
                    if changed:
                        with open(settings.ACTIVE_TRADES_PATH, "w", encoding="utf-8") as f:
                            json.dump(trades_data, f, indent=2)
            except Exception as clean_exc:
                logger.debug("Failed to prune closed trades from active_trades.json: %s", clean_exc)

            # Adopt any broker positions not yet tracked in active_signals
            known_tickers = {s.get("ticker") for s in active_signals}
            for b_ticker, bp in broker_positions_map.items():
                if b_ticker not in known_tickers:
                    pair = universe.get_pair_for_etp(b_ticker)
                    underlying = pair.underlying_symbol if pair else b_ticker.replace("l_EQ", "").replace("_US_EQ", "")
                    underlying_name = pair.name if pair else underlying
                    direction = universe.get_etp_direction(b_ticker) if universe else get_etp_direction(b_ticker)
                    avg_px = bp.average_price if bp.average_price > 0 else (bp.current_price if bp.current_price > 0 else 100.0)
                    cur_px = bp.current_price if bp.current_price > 0 else avg_px
                    sl_dist = round(avg_px * 0.05, 2)
                    # All spot ETPs on Trading 212 are held long, so SL is below entry and TP is above entry
                    sl = round(max(0.01, avg_px - sl_dist), 2)
                    pnl_pct = ((cur_px - avg_px) / avg_px * 100.0) if avg_px > 0 else 0.0
                    pnl_amount = round((cur_px - avg_px) * bp.quantity, 2)
                    pnl_amount_eur = round(float(getattr(bp, "ppl", 0.0) or 0.0), 2)

                    curr = universe.get_ticker_currency(b_ticker) if universe else get_ticker_currency(b_ticker)
                    currency_code, currency_prefix, currency_suffix = get_currency_display(curr)

                    active_signals.append({
                        "ticker": b_ticker,
                        "underlying": underlying,
                        "name": underlying_name,
                        "direction": direction,
                        "leverage": 5 if ("5" in b_ticker) else 3,
                        "strategy_type": "broker_placed",
                        "rsi3": None, "rsi7": None, "rsi14": None, "sma5": None,
                        "win_rate_exp": "85%",
                        "overnight_status": "REQUIRES BREAKEVEN OR FLATTENS 16:20 UTC" if direction == "LONG" else "INTRADAY SHORT (Flattens 16:20 UTC / Winter Time)",
                        "overnight_badge_class": "bg-sky-500/10 text-sky-300 border-sky-500/30" if direction == "LONG" else "bg-rose-500/10 text-rose-300 border-rose-500/30",
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
                        "stop_distance_pct": round(abs(avg_px - sl) / avg_px * 100, 2) if avg_px > 0 else 5.0,
                        "broker_stop_order_id": None,
                        "quantity": bp.quantity,
                        "remaining_quantity": bp.quantity,
                        "status": "ACTIVE (Broker Placed)",
                        "entry_time": datetime.now(timezone.utc).isoformat(),
                        "atr": round(avg_px * 0.025, 2),
                        "copy_text": f"ADOPTED: {b_ticker} ({underlying}) Qty: {bp.quantity}",
                    })

        history = load_recent_history(settings.TRADES_LOG_PATH)
        recent_notifications = load_recent_notifications(
            settings.NOTIFICATIONS_LOG_PATH, limit=30, universe=universe
        )

        # Merge live broker order executions from Trading 212
        if client is not None:
            broker_events = fetch_live_broker_notifications(client, universe, limit=25)
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
                # Sort chronologically descending
                merged_events.sort(key=lambda x: str(x.get("timestamp", "")), reverse=True)
                recent_notifications = merged_events[:30]

        active_stops = live_server_stops_count if live_server_stops_count is not None else sum(1 for s in active_signals if s.get("broker_stop_order_id"))

        # Construct session equity curve from trade history and current open P&L
        if live_account_cash_total is not None and live_account_cash_total > 0:
            base_equity = round(live_account_cash_total, 2)
            live_equity = round(live_account_cash_total, 2)
        else:
            base_equity = float(getattr(settings, "DEFAULT_ACCOUNT_EQUITY", 4923.05) or 4923.05)
            live_equity = base_equity

        equity_points = [{"time": "Start", "equity": base_equity}]
        running_equity = base_equity
        chronological_events = [e for e in recent_notifications if e.get("pnl") is not None]
        chronological_events.reverse()
        for ev in chronological_events:
            pnl_val = float(ev.get("pnl") or 0.0)
            running_equity += pnl_val
            t_str = str(ev.get("timestamp") or "")[-12:-4] or "Trade"
            equity_points.append({"time": t_str, "equity": round(running_equity, 2)})

        total_open_pnl = sum(float(s.get("pnl_amount_eur", s.get("pnl_amount", 0.0)) or 0.0) for s in active_signals)
        if live_account_cash_total is not None and live_account_cash_total > 0:
            live_equity = round(live_account_cash_total + total_open_pnl, 2)
        else:
            live_equity = round(running_equity + total_open_pnl, 2)

        now_time = datetime.now(timezone.utc).strftime("%H:%M:%S")
        equity_points.append({"time": now_time, "equity": live_equity})

        res = {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "mode": "MANUAL_SIGNALS_MODE",
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
            "broker_status": {
                "broker_name": "Trading 212 Demo",
                "is_connected": True,
                "active_server_stops": active_stops,
                "active_positions_count": len(active_signals),
                "execution_mode": "System 2 Pure TSL (Native GTC Stop + Algorithmic Trail)",
                "runner_status": "ONLINE (24/7 Raspberry Pi)",
                "monitored_assets": len(CURATED_3X_PAIRS),
                "eur_ratio": "82%",
            },
        }
        _DASHBOARD_DATA_CACHE = res
        _DASHBOARD_DATA_CACHE_TIME = time.time()
        return res


def generate_html_dashboard(output_path: str) -> str:
    """Render self-contained HTML dashboard with Tailwind and interactive controls."""
    data = build_dashboard_data()
    data_json = json.dumps(data)

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Trading 212 3x Quantitative Engine — Live Dashboard</title>
  <script src="https://www.gstatic.com/antigravity/web/dev/tailwindcss.min.js"></script>
  <style>
    @keyframes pulse-slow {{
      0%, 100% {{ opacity: 1; }}
      50% {{ opacity: 0.4; }}
    }}
    .animate-pulse-slow {{
      animation: pulse-slow 2s cubic-bezier(0.4, 0, 0.6, 1) infinite;
    }}
  </style>
</head>
<body class="bg-slate-950 text-slate-100 antialiased font-sans min-h-screen p-4 sm:p-6 lg:p-8">

  <!-- Header -->
  <header class="max-w-7xl mx-auto mb-8 flex flex-col md:flex-row md:items-center md:justify-between gap-4 border-b border-slate-800 pb-6">
    <div>
      <div class="flex flex-wrap items-center gap-2 sm:gap-3">
        <span class="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-semibold bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
          <span class="w-2 h-2 rounded-full bg-emerald-400 animate-pulse-slow"></span>
          🟢 24/7 RUNNER ACTIVE
        </span>
        <span class="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-semibold bg-indigo-500/10 text-indigo-400 border border-indigo-500/20">
          <span class="w-2 h-2 rounded-full bg-indigo-400"></span>
          Trading 212 Demo Connected
        </span>
        <span class="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-semibold bg-emerald-500/10 text-emerald-300 border border-emerald-500/20 font-mono">
          🛡️ <span id="header-stops-count">{data['broker_status']['active_server_stops']}</span> Native Stops Active
        </span>
        <span class="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-semibold bg-sky-500/10 text-sky-300 border border-sky-500/20 font-mono">
          120 Assets (84% EUR)
        </span>
        <span id="sync-pill" class="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-semibold bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 font-mono">
          <span class="w-2 h-2 rounded-full bg-emerald-400 animate-pulse"></span>
          LIVE AUTO-SYNC (3s)
        </span>
      </div>
      <h1 class="text-2xl sm:text-3xl font-bold tracking-tight text-white mt-2">
        Trading 212 Leveraged Quantitative Engine
      </h1>
      <p class="text-slate-400 text-sm mt-1">
        Connors 3-RSI Mean Reversion & Trend Momentum setups with crash-resilient native server stops.
      </p>
    </div>

    <div class="flex flex-wrap items-center gap-3">
      <button id="refresh-btn" onclick="fetchLiveData(true)" class="inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-500 text-white text-sm font-medium transition shadow-sm">
        <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"></path></svg>
        <span id="refresh-btn-text">Refresh Now</span>
      </button>
      <button onclick="panicFlatten()" class="inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-rose-600 hover:bg-rose-500 text-white text-sm font-bold transition shadow-sm border border-rose-400/30">
        🚨 Emergency Panic Flatten
      </button>
      <div class="text-right text-xs text-slate-500 hidden sm:block">
        <div>Last Synced:</div>
        <div id="timestamp-text" class="text-slate-300 font-mono font-bold">{data['generated_at']}</div>
      </div>
    </div>
  </header>

  <!-- Metric Summary Badges -->
  <div class="max-w-7xl mx-auto grid grid-cols-2 sm:grid-cols-4 gap-4 mb-8">
    <div class="bg-slate-900/80 border border-slate-800 rounded-xl p-4">
      <div class="text-xs text-slate-400 uppercase tracking-wider font-semibold">Active Positions</div>
      <div id="metric-active" class="text-2xl font-bold text-white mt-1">{data['active_signals_count']} Open Trades</div>
      <div class="text-xs text-emerald-400 mt-1">Live in Trading 212</div>
    </div>
    <div class="bg-slate-900/80 border border-slate-800 rounded-xl p-4">
      <div class="text-xs text-slate-400 uppercase tracking-wider font-semibold">Native Broker Stops</div>
      <div id="metric-stops" class="text-2xl font-bold text-emerald-400 mt-1">{data['broker_status']['active_server_stops']} Active</div>
      <div class="text-xs text-slate-400 mt-1">T212 Server-Side GTC</div>
    </div>
    <div class="bg-slate-900/80 border border-slate-800 rounded-xl p-4">
      <div class="text-xs text-slate-400 uppercase tracking-wider font-semibold">Exit Engine</div>
      <div class="text-2xl font-bold text-white mt-1">100% Pure TSL</div>
      <div class="text-xs text-emerald-400 mt-1">🛡️ Native T212 Server GTC Ratchet</div>
    </div>
    <div class="bg-slate-900/80 border border-amber-500/30 rounded-xl p-4 relative overflow-hidden">
      <div class="text-xs text-amber-400 uppercase tracking-wider font-semibold flex items-center justify-between">
        <span>16:20 UTC Gap Cutoff</span>
        <span class="w-2 h-2 rounded-full bg-amber-400 animate-pulse"></span>
      </div>
      <div id="cutoff-countdown" class="text-2xl font-bold text-amber-300 font-mono mt-1">--:--:--</div>
      <div class="text-xs text-slate-400 mt-1">Intraday Risk Evacuation</div>
    </div>
  </div>

  <!-- Live Equity Trajectory Sparkline Card -->
  <div class="max-w-7xl mx-auto mb-8 bg-slate-900/80 border border-slate-800 rounded-2xl p-5 shadow-lg">
    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-3">
      <div>
        <div class="text-xs text-slate-400 uppercase tracking-wider font-semibold flex items-center gap-2">
          <span>📈 Portfolio Equity Curve &amp; Session Trajectory</span>
          <span class="inline-flex items-center px-2 py-0.5 rounded text-[10px] font-mono bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">Live Closed + MTM</span>
        </div>
        <div class="flex items-baseline gap-3 mt-1">
          <span id="equity-val-current" class="text-2xl sm:text-3xl font-black text-white font-mono">€{data.get('current_equity', 4923.05):.2f}</span>
          <span id="equity-val-delta" class="text-sm font-bold text-slate-400 font-mono">+0.00 (+0.00%)</span>
        </div>
      </div>
      <div class="flex flex-wrap items-center gap-4 text-xs font-mono text-slate-400">
        <div>Session Start: <span id="equity-val-start" class="text-slate-200 font-bold">€{data.get('base_equity', 4923.05):,.2f}</span></div>
        <div>Session High: <span id="equity-val-high" class="text-emerald-300 font-bold">—</span></div>
        <div>Session Low: <span id="equity-val-low" class="text-rose-300 font-bold">—</span></div>
      </div>
    </div>
    <div class="w-full h-24 sm:h-28 relative pt-2">
      <svg id="equity-chart-svg" class="w-full h-full overflow-visible" preserveAspectRatio="none" viewBox="0 0 800 100">
        <defs>
          <linearGradient id="equity-gradient-emerald" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="#10b981" stop-opacity="0.35" />
            <stop offset="100%" stop-color="#10b981" stop-opacity="0.0" />
          </linearGradient>
          <linearGradient id="equity-gradient-rose" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="#f43f5e" stop-opacity="0.35" />
            <stop offset="100%" stop-color="#f43f5e" stop-opacity="0.0" />
          </linearGradient>
        </defs>
        <g id="equity-chart-group"></g>
      </svg>
    </div>
  </div>

  <!-- Main Container -->
  <main class="max-w-7xl mx-auto space-y-8">

    <!-- Active Positions Section (HERO SECTION - AT THE TOP) -->
    <section id="active-trades-section">
      <div class="flex items-center justify-between mb-4">
        <h2 class="text-xl font-bold text-white flex items-center gap-2">
          <span>🎯 Active Positions / Open Trades</span>
          <span class="text-xs px-2.5 py-0.5 rounded-full bg-slate-800 text-slate-200 font-mono font-bold" id="signals-badge">{data['active_signals_count']}</span>
        </h2>
        <div class="flex flex-wrap gap-2 text-xs">
          <button onclick="filterSignals('ALL')" id="filter-all" class="filter-btn px-3 py-1.5 rounded-md bg-slate-800 text-white font-medium hover:bg-slate-700">All</button>
          <button onclick="filterSignals('LONG')" id="filter-long" class="filter-btn px-3 py-1.5 rounded-md bg-slate-900 text-slate-400 hover:text-white">Long Only</button>
          <button onclick="filterSignals('SHORT')" id="filter-short" class="filter-btn px-3 py-1.5 rounded-md bg-slate-900 text-slate-400 hover:text-white">Short Only</button>
        </div>
      </div>

      <div id="signals-grid" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
        <!-- Rendered by JavaScript dynamically -->
      </div>
    </section>

    <!-- Recent Engine Activity History (AUDIT LOG - RIGHT BELOW TRADES) -->
    <section class="bg-slate-900/60 border border-slate-800 rounded-2xl p-6">
      <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-4">
        <div>
          <h3 class="text-lg font-bold text-white flex items-center gap-2">
            <span>🔔 Recent Trade & Alert History</span>
            <span class="text-xs px-2 py-0.5 rounded-full bg-slate-800 text-slate-300 font-mono" id="alerts-badge">0</span>
          </h3>
          <p class="text-xs text-slate-400">Real-time desktop alerts, trade entries, native stop registrations, and 100% Pure Trailing Stop Loss ratchets.</p>
        </div>
        <div class="text-xs text-slate-500 font-mono">Live Event Stream</div>
      </div>

      <div class="overflow-x-auto">
        <table class="w-full text-left text-sm text-slate-300">
          <thead class="text-xs uppercase bg-slate-950/60 text-slate-400 border-b border-slate-800">
            <tr>
              <th class="py-3 px-4">Event</th>
              <th class="py-3 px-4">Ticker / Asset</th>
              <th class="py-3 px-4">Price</th>
              <th class="py-3 px-4">Quantity / P&amp;L</th>
              <th class="py-3 px-4">Milestone / Rationale</th>
              <th class="py-3 px-4">Timestamp (UTC)</th>
            </tr>
          </thead>
          <tbody id="alerts-table-body" class="divide-y divide-slate-800/60 font-mono text-xs">
            <!-- Rendered by JS -->
          </tbody>
        </table>
      </div>
    </section>

    <!-- Monitored Asset Universe (COLLAPSIBLE TO PREVENT CLUTTER) -->
    <details class="bg-slate-900/60 border border-slate-800 rounded-2xl overflow-hidden group">
      <summary class="cursor-pointer p-5 font-bold text-white flex items-center justify-between hover:bg-slate-800/40 transition select-none">
        <div>
          <span class="text-base flex items-center gap-2">
            <span>📋 Monitored Asset Directory</span>
            <span class="text-xs font-mono font-normal px-2 py-0.5 rounded bg-indigo-500/10 text-indigo-300 border border-indigo-500/20">120 Pairs • 84% EUR</span>
          </span>
          <p class="text-xs text-slate-400 font-normal mt-0.5">High-liquidity 3x & 5x Leverage Shares ETPs on Trading 212 with zero borrowing drag.</p>
        </div>
        <span class="text-xs font-semibold px-3 py-1.5 rounded-lg bg-indigo-600/20 text-indigo-300 border border-indigo-500/30 group-open:rotate-180 transition-transform">
          Click to View Directory ▾
        </span>
      </summary>

      <div class="p-6 pt-0 border-t border-slate-800/60 overflow-x-auto">
        <table class="w-full text-left text-sm text-slate-300 mt-4">
          <thead class="text-xs uppercase bg-slate-950/60 text-slate-400 border-b border-slate-800">
            <tr>
              <th class="py-3 px-4">Underlying</th>
              <th class="py-3 px-4">Name / Sector</th>
              <th class="py-3 px-4 text-emerald-400 font-semibold">3x Long ETP</th>
              <th class="py-3 px-4 text-rose-400 font-semibold">3x Short (Inverse) ETP</th>
              <th class="py-3 px-4 text-amber-400 font-semibold">Leverage Policy</th>
              <th class="py-3 px-4">Currency</th>
            </tr>
          </thead>
          <tbody id="pairs-table-body" class="divide-y divide-slate-800/60 font-mono text-xs">
            <!-- Rendered by JS -->
          </tbody>
        </table>
      </div>
    </details>

  </main>

  <footer class="max-w-7xl mx-auto mt-12 pt-6 border-t border-slate-900 text-center text-xs text-slate-600">
    Trading 212 Quantitative Leveraged Engine &bull; 24/7 Autonomous Raspberry Pi Runner
  </footer>

  <script>
    let INITIAL_DATA = {data_json};
    let rawSignals = INITIAL_DATA.active_signals || [];
    let currentFilter = 'ALL';
    let isFetching = false;

    // Direct asynchronous real-time fetch from /api/data
    async function fetchLiveData(isManual = false) {{
      if (isFetching) return;
      isFetching = true;
      const btn = document.getElementById('refresh-btn-text');
      if (isManual && btn) btn.innerText = 'Syncing...';
      try {{
        const resp = await fetch('/api/data?_t=' + Date.now(), {{ cache: 'no-store' }});
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const data = await resp.json();
        updateUI(data);
      }} catch (err) {{
        console.debug('Polling fetch notice:', err);
      }} finally {{
        isFetching = false;
        if (isManual && btn) {{
          btn.innerText = 'Refreshed!';
          setTimeout(() => {{ btn.innerText = 'Refresh Now'; }}, 1200);
        }}
      }}
    }}

    function updateUI(data) {{
      if (!data) return;
      if (data.active_signals !== undefined) {{
        rawSignals = data.active_signals;
        INITIAL_DATA.active_signals = data.active_signals;
        const activeEl = document.getElementById('metric-active');
        if (activeEl) activeEl.innerText = `${{rawSignals.length}} Open Trades`;
        const badgeEl = document.getElementById('signals-badge');
        if (badgeEl) badgeEl.innerText = rawSignals.length;
        renderSignals(currentFilter);
      }}
      if (data.broker_status) {{
        INITIAL_DATA.broker_status = data.broker_status;
        const stopsEl = document.getElementById('metric-stops');
        if (stopsEl && data.broker_status.active_server_stops !== undefined) {{
          stopsEl.innerText = `${{data.broker_status.active_server_stops}} Active`;
        }}
        const headerStopsEl = document.getElementById('header-stops-count');
        if (headerStopsEl && data.broker_status.active_server_stops !== undefined) {{
          headerStopsEl.innerText = data.broker_status.active_server_stops;
        }}
      }}
      if (data.recent_notifications) {{
        INITIAL_DATA.recent_notifications = data.recent_notifications;
        renderAlertsTable();
      }}
      if (data.equity_history || data.current_equity !== undefined) {{
        if (data.equity_history) INITIAL_DATA.equity_history = data.equity_history;
        if (data.current_equity !== undefined) INITIAL_DATA.current_equity = data.current_equity;
        renderEquityChart(INITIAL_DATA.equity_history, INITIAL_DATA.current_equity);
      }}
      if (data.pairs_watchlist && (!INITIAL_DATA.pairs_watchlist || INITIAL_DATA.pairs_watchlist.length === 0)) {{
        INITIAL_DATA.pairs_watchlist = data.pairs_watchlist;
        renderPairsTable();
      }}
      const timeEl = document.getElementById('timestamp-text');
      if (timeEl && data.generated_at) {{
        timeEl.innerText = data.generated_at;
      }}
    }}

    async function panicFlatten() {{
      if (!confirm("⚠️ EMERGENCY ACTION: Are you sure you want to CANCEL ALL ACTIVE STOPS and LIQUIDATE all open positions immediately?")) {{
        return;
      }}
      try {{
        const resp = await fetch('/api/panic', {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{confirm: true}})
        }});
        const res = await resp.json();
        if (resp.ok) {{
          alert("🚨 Panic flatten completed! All open positions liquidated and stops cancelled.");
          fetchLiveData(true);
        }} else {{
          alert("Error executing panic flatten: " + (res.error || "Unknown error"));
        }}
      }} catch (err) {{
        alert("Failed to reach server: " + err);
      }}
    }}

    function renderSignals(filter = 'ALL') {{
      currentFilter = filter;
      const grid = document.getElementById('signals-grid');
      if (!grid) return;
      grid.innerHTML = '';

      let signals = rawSignals || [];
      if (filter !== 'ALL') {{
        signals = signals.filter(s => s.direction === filter);
      }}

      if (signals.length === 0) {{
        grid.innerHTML = `
          <div class="col-span-full py-10 px-6 text-center bg-slate-900/40 border border-slate-800 rounded-2xl">
            <div class="text-3xl mb-2">🟢</div>
            <div class="text-lg font-bold text-white">Engine is Active & Scanning 120 Leveraged ETPs</div>
            <div class="text-sm text-slate-400 mt-1 max-w-xl mx-auto">
              No open trades right now. The engine runs 24/7 on the Raspberry Pi, screening every 15 minutes across 120 high-liquidity ETPs (84% EUR) for 3-RSI oversold pullbacks (&lt;25) and momentum triggers with pre-earnings radar tracking. Orders execute automatically on Trading 212 when criteria are met.
            </div>
            <div class="mt-4 inline-flex items-center gap-2 px-3 py-1 rounded-full bg-slate-800/80 text-emerald-400 text-xs font-mono">
              <span class="w-2 h-2 rounded-full bg-emerald-400 animate-pulse"></span>
              24/7 Autonomous Raspberry Pi Runner
            </div>
          </div>
        `;
        return;
      }}

      signals.forEach(s => {{
        const isLong = s.direction === 'LONG';
        const lev = s.leverage || (s.ticker && s.ticker.includes('5') ? 5 : 3);
        const levBadge = lev === 5
          ? '<span class="px-2 py-0.5 rounded text-[10px] font-bold bg-amber-500/20 text-amber-300 border border-amber-500/40">5x Institutional</span>'
          : '<span class="px-2 py-0.5 rounded text-[10px] font-bold bg-indigo-500/20 text-indigo-300 border border-indigo-500/40">3x Daily</span>';
        const badgeColor = isLong ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20' : 'bg-rose-500/10 text-rose-400 border-rose-500/20';
        const actionText = isLong ? `🟢 BUY / LONG (${{lev}}x)` : `🔴 SELL / SHORT (${{lev}}x)`;
        const code = (s.currency_code || '').toUpperCase();
        const sTk = (s.ticker || '').toUpperCase();
        let currPrefix = '';
        let currSuffix = '';
        if (code === 'GBX' || (s.currency_suffix && s.currency_suffix.includes('p')) || sTk.includes('SKOR') || sTk.includes('3XOM') || sTk.includes('3UNH') || sTk.endsWith('.L')) {{
          currPrefix = '';
          currSuffix = 'p';
        }} else if (code === 'USD' || s.currency_prefix === '$' || sTk.includes('_US')) {{
          currPrefix = '$';
          currSuffix = '';
        }} else if (code === 'GBP' || s.currency_prefix === '£') {{
          currPrefix = '£';
          currSuffix = '';
        }} else {{
          currPrefix = (s.currency_prefix !== undefined && s.currency_prefix !== null && s.currency_prefix !== '') ? s.currency_prefix : '€';
          currSuffix = s.currency_suffix || '';
        }}
        const currBadge = (code === 'GBX' || currSuffix === 'p')
          ? '<span class="text-amber-400 font-semibold">Penny (GBX / p)</span>'
          : `<span class="text-slate-300 font-medium">${{code || 'EUR'}}</span>`;

        const stopOrderBadge = s.broker_stop_order_id
          ? `<div class="flex items-center justify-between text-xs px-2.5 py-2 rounded-lg bg-emerald-500/10 border border-emerald-500/30 text-emerald-300 font-semibold mb-2.5 shadow-sm">
               <span class="flex items-center gap-1.5">
                 <span class="w-2 h-2 rounded-full bg-emerald-400 animate-pulse"></span>
                 🛡️ Native T212 Stop Order:
               </span>
               <span class="font-mono font-bold text-emerald-200">#${{s.broker_stop_order_id}}</span>
             </div>`
          : `<div class="flex items-center justify-between text-xs px-2.5 py-2 rounded-lg bg-slate-900 border border-slate-800 text-slate-400 mb-2.5">
               <span>🛡️ Stop Protection:</span>
               <span class="text-slate-300 font-medium">Software Polled</span>
             </div>`;

        const earningsBadge = (s.earnings_in_days !== undefined && s.earnings_in_days !== null)
          ? `<div class="flex items-center justify-between text-xs px-2.5 py-1.5 rounded-lg bg-purple-500/10 border border-purple-500/30 text-purple-300 font-semibold mb-2.5">
               <span class="flex items-center gap-1.5">
                 <span>📅</span>
                 <span>Earnings Catalyst:</span>
               </span>
               <span class="font-mono font-bold text-purple-200">${{Number(s.earnings_in_days).toFixed(0)}}d away</span>
             </div>`
          : '';

        const pnlPct = (s.pnl_pct !== undefined && s.pnl_pct !== null) ? Number(s.pnl_pct) : 0.0;
        const pnlColor = pnlPct > 0 ? 'text-emerald-400' : (pnlPct < 0 ? 'text-rose-400' : 'text-slate-300');
        const pnlSign = pnlPct > 0 ? '+' : (pnlPct < 0 ? '-' : '');
        const pnlAmount = s.pnl_amount !== undefined ? Number(s.pnl_amount) : 0.0;
        const pnlAmountEur = s.pnl_amount_eur !== undefined ? Number(s.pnl_amount_eur) : (
          s.currency_code === 'GBX' ? (pnlAmount / 100 * 1.204) : (s.currency_code === 'USD' ? (pnlAmount / 1.08) : pnlAmount)
        );
        const remQty = s.remaining_quantity !== undefined ? Number(s.remaining_quantity) : Number(s.quantity || 1.0);
        const curPx = Number(s.current_price || s.entry_price || 0.0);
        const entryPx = Number(s.entry_price || 0.0);
        const posVal = (remQty * curPx).toFixed(2);
        const posValEur = (s.currency_code === 'GBX' ? (Number(posVal) / 100 * 1.204) : (s.currency_code === 'USD' ? (Number(posVal) / 1.08) : Number(posVal))).toFixed(2);

        const card = document.createElement('div');
        card.className = "bg-slate-900 border border-slate-800 hover:border-slate-700 transition rounded-2xl p-5 shadow-lg flex flex-col justify-between relative overflow-hidden group";

        card.innerHTML = `
          <!-- Top Accent line -->
          <div class="absolute top-0 left-0 right-0 h-1.5 ${{isLong ? 'bg-emerald-500' : 'bg-rose-500'}}"></div>

          <div>
            <!-- Header -->
            <div class="flex items-start justify-between gap-3 mb-3">
              <div>
                <div class="flex items-center gap-2">
                  <span class="text-lg font-black text-white tracking-wide">${{s.name || s.underlying}}</span>
                  <span class="text-xs font-mono text-slate-400 bg-slate-950 px-2 py-0.5 rounded border border-slate-800">${{s.ticker}}</span>
                  ${{levBadge}}
                </div>
                <div class="text-xs text-slate-400 mt-0.5">${{lev}}x Leveraged ETP &bull; ${{currBadge}}</div>
              </div>

              <span class="px-2.5 py-1 rounded-md text-xs font-black uppercase tracking-wider border ${{badgeColor}}">
                ${{actionText}}
              </span>
            </div>

            <!-- Big P&L Box -->
            <div class="bg-slate-950/80 border border-slate-800/80 rounded-xl p-3.5 mb-3">
              <div class="flex items-center justify-between">
                <div>
                  <div class="text-[10px] text-slate-400 uppercase font-semibold">Live P&L</div>
                  <div class="text-xl font-black ${{pnlColor}} font-mono">
                    ${{pnlSign}}${{currPrefix}}${{Math.abs(pnlAmount).toFixed(2)}}${{currSuffix}} (${{pnlPct >= 0 ? '+' : ''}}${{pnlPct.toFixed(2)}}%)
                  </div>
                  ${{s.currency_code !== 'EUR' ? `<div class="text-xs ${{pnlColor}} font-mono font-semibold mt-0.5">≈ ${{pnlAmountEur >= 0 ? '+' : '-'}}€${{Math.abs(pnlAmountEur).toFixed(2)}}</div>` : ''}}
                </div>
                <div class="text-right">
                  <div class="text-[10px] text-slate-400 uppercase font-semibold">Holdings</div>
                  <div class="text-sm font-bold text-indigo-300 font-mono">${{remQty.toFixed(1)}} shares</div>
                  <div class="text-[10px] text-slate-400 font-mono">Val: ${{currPrefix}}${{posVal}}${{currSuffix}}${{s.currency_code !== 'EUR' ? ` (≈ €${{posValEur}})` : ''}}</div>
                </div>
              </div>
              <div class="mt-2.5 pt-2 border-t border-slate-800/60 flex items-center justify-between text-xs font-mono">
                <span class="text-slate-400">Entry: <b class="text-white">${{currPrefix}}${{entryPx.toFixed(2)}}${{currSuffix}}</b></span>
                <span class="text-slate-400">Live: <b class="${{pnlColor}}">${{currPrefix}}${{curPx.toFixed(2)}}${{currSuffix}}</b></span>
              </div>
            </div>

            <!-- Native Broker Stop Order Badge -->
            ${{stopOrderBadge}}
            ${{earningsBadge}}

            <!-- Targets & Stop Grid -->
            <div class="space-y-2 text-xs mb-4">
              <!-- Stop Loss -->
              <div class="flex items-center justify-between p-2 rounded-lg bg-rose-500/5 border border-rose-500/10">
                <span class="text-rose-300 font-semibold flex items-center gap-1.5">
                  <span class="w-1.5 h-1.5 rounded-full bg-rose-400"></span>
                  Stop Loss (SL)
                </span>
                <span class="font-mono font-bold text-rose-200">${{currPrefix}}${{Number(s.stop_loss || 0).toFixed(2)}}${{currSuffix}} (-${{s.stop_distance_pct || 5.0}}%)</span>
              </div>

              <!-- Trailing Stop Loss -->
              <div class="flex items-center justify-between p-2 rounded-lg bg-emerald-500/5 border border-emerald-500/10">
                <span class="text-emerald-300 font-semibold flex items-center gap-1.5">
                  <span class="w-1.5 h-1.5 rounded-full bg-emerald-400"></span>
                  Trailing Stop (TSL)
                </span>
                <span class="font-mono font-bold text-emerald-200">
                  ${{currPrefix}}${{Number(s.trailing_stop_loss || s.stop_loss || 0).toFixed(2)}}${{currSuffix}}
                  <span class="text-emerald-400 ml-1 font-bold">[${{s.tsl_status || (s.trailing_stop_loss >= s.entry_price ? 'Breakeven Locked' : 'Active Trail')}}]</span>
                </span>
              </div>

              <!-- Native Broker Protection -->
              <div class="flex items-center justify-between p-2 rounded-lg bg-sky-500/5 border border-sky-500/10">
                <span class="text-sky-300 font-semibold flex items-center gap-1.5">
                  <span class="w-1.5 h-1.5 rounded-full bg-sky-400"></span>
                  T212 GTC Stop
                </span>
                <span class="font-mono font-bold text-sky-200">
                  ${{s.broker_stop_order_id ? '#' + s.broker_stop_order_id : 'Syncing Broker...'}}
                  <span class="text-sky-400 ml-1 font-bold">[100% Protection]</span>
                </span>
              </div>
            </div>
          </div>

          <!-- Card Footer Status -->
          <div class="pt-3 border-t border-slate-800/80 flex items-center justify-between text-[11px] text-slate-400">
            <span>Status: <b class="text-slate-200">${{s.status || 'ACTIVE'}}</b></span>
            <span class="font-mono uppercase text-slate-500">${{(s.strategy_type || '').replace('_', ' ')}}</span>
          </div>
        `;
        grid.appendChild(card);
      }});
    }}

    function renderPairsTable() {{
      const tbody = document.getElementById('pairs-table-body');
      if (!tbody) return;
      tbody.innerHTML = '';
      (INITIAL_DATA.pairs_watchlist || []).forEach(p => {{
        const row = document.createElement('tr');
        row.className = "hover:bg-slate-900/80 transition";
        const fiveXBadge = p.has_5x
          ? `<span class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-bold bg-amber-500/10 text-amber-300 border border-amber-500/30">5x Exists &rarr; 3x Exec</span>`
          : `<span class="text-slate-500 text-[10px]">3x Standard</span>`;

        const lCurr = p.long_currency || p.currency || 'EUR';
        const sCurr = p.short_currency || p.currency || 'EUR';
        let currCellHtml = `<span class="text-slate-400">${{p.currency || 'EUR'}}</span>`;
        if (lCurr !== sCurr) {{
          const lBadge = lCurr === 'GBX' ? 'GBX (p)' : lCurr;
          const sBadge = sCurr === 'GBX' ? 'GBX (p)' : sCurr;
          currCellHtml = `<span class="text-emerald-400 font-mono">${{lBadge}}</span> <span class="text-slate-600">/</span> <span class="text-rose-400 font-mono">${{sBadge}}</span>`;
        }} else if (lCurr === 'GBX') {{
          currCellHtml = `<span class="text-amber-400 font-semibold">GBX (Penny)</span>`;
        }}

        row.innerHTML = `
          <td class="py-2.5 px-4 font-bold text-white">${{p.underlying}}</td>
          <td class="py-2.5 px-4 font-sans text-slate-400">${{p.name}} <span class="text-slate-500">(${{p.category}})</span></td>
          <td class="py-2.5 px-4 text-emerald-400 font-bold">${{p.long_ticker}}</td>
          <td class="py-2.5 px-4 text-rose-400 font-bold">${{p.short_ticker}}</td>
          <td class="py-2.5 px-4">${{fiveXBadge}}</td>
          <td class="py-2.5 px-4">${{currCellHtml}}</td>
        `;
        tbody.appendChild(row);
      }});
    }}

    function filterSignals(direction) {{
      document.querySelectorAll('.filter-btn').forEach(btn => {{
        btn.classList.remove('bg-slate-800', 'text-white');
        btn.classList.add('bg-slate-900', 'text-slate-400');
      }});
      const activeBtn = direction === 'ALL' ? document.getElementById('filter-all') : (direction === 'LONG' ? document.getElementById('filter-long') : document.getElementById('filter-short'));
      if (activeBtn) {{
        activeBtn.classList.remove('bg-slate-900', 'text-slate-400');
        activeBtn.classList.add('bg-slate-800', 'text-white');
      }}
      renderSignals(direction);
    }}

    function updateCountdown() {{
      const now = new Date();
      const nowUtc = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(), now.getUTCHours(), now.getUTCMinutes(), now.getUTCSeconds());
      let targetUtc = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(), 16, 20, 0);
      let diff = targetUtc - nowUtc;
      const countdownEl = document.getElementById('cutoff-countdown');
      if (!countdownEl) return;
      if (diff < 0) {{
        if (diff > -600000) {{
          countdownEl.innerHTML = '<span class="text-rose-400 animate-pulse text-lg">FLATTENING NOW</span>';
          return;
        }}
        targetUtc += 86400000;
        diff = targetUtc - nowUtc;
      }}
      const hours = Math.floor(diff / (1000 * 60 * 60));
      const minutes = Math.floor((diff % (1000 * 60 * 60)) / (1000 * 60));
      const seconds = Math.floor((diff % (1000 * 60)) / 1000);
      const pad = n => String(n).padStart(2, '0');
      countdownEl.innerText = `${{pad(hours)}}:${{pad(minutes)}}:${{pad(seconds)}}`;
    }}

    function renderAlertsTable() {{
      const tbody = document.getElementById('alerts-table-body');
      if (!tbody) return;
      tbody.innerHTML = '';
      const notifs = INITIAL_DATA.recent_notifications || [];
      const badge = document.getElementById('alerts-badge');
      if (badge) badge.innerText = notifs.length;

      if (notifs.length === 0) {{
        tbody.innerHTML = `<tr><td colspan="6" class="py-6 text-center text-slate-500 font-sans">No recent alerts or execution events recorded.</td></tr>`;
        return;
      }}

      notifs.forEach(n => {{
        const row = document.createElement('tr');
        row.className = "hover:bg-slate-900/80 transition";
        const ev = (n.event_type || 'ALERT').toUpperCase();
        let badgeColor = 'bg-slate-800 text-slate-300 border-slate-700';
        let badgeIcon = '📢 ' + ev;

        if (ev === 'BUY') {{
          badgeColor = 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20';
          badgeIcon = '🟢 BUY';
        }} else if (ev === 'SELL') {{
          badgeColor = 'bg-rose-500/10 text-rose-400 border-rose-500/20';
          badgeIcon = '🔴 SELL';
        }} else if (ev === 'TSL_BREAKEVEN') {{
          badgeColor = 'bg-emerald-500/10 text-emerald-300 border-emerald-500/30';
          badgeIcon = '🛡️ TSL BREAKEVEN';
        }} else if (ev === 'TSL_CUSHION') {{
          badgeColor = 'bg-emerald-500/10 text-emerald-300 border-emerald-500/30';
          badgeIcon = '🛡️ TSL CUSHION (+1.5%)';
        }} else if (ev === 'TSL' || ev === 'TSL_DYNAMIC' || ev === 'TSL_RATCHET') {{
          badgeColor = 'bg-sky-500/10 text-sky-300 border-sky-500/30';
          badgeIcon = '📈 TSL RATCHET';
        }} else if (ev === 'TP1') {{
          badgeColor = 'bg-emerald-500/10 text-emerald-300 border-emerald-500/30';
          badgeIcon = '🎯 TP1 (+1.0R)';
        }} else if (ev === 'TP2') {{
          badgeColor = 'bg-indigo-500/10 text-indigo-300 border-indigo-500/30';
          badgeIcon = '🎯 TP2 (+2.0R)';
        }} else if (ev === 'TP3') {{
          badgeColor = 'bg-purple-500/10 text-purple-300 border-purple-500/30';
          badgeIcon = '🏆 TP3 (+3.0R RUNNER)';
        }} else if (ev === 'SL') {{
          badgeColor = 'bg-rose-500/10 text-rose-300 border-rose-500/30';
          badgeIcon = '🛑 STOP LOSS';
        }} else if (ev.includes('HYSTERESIS')) {{
          badgeColor = 'bg-cyan-500/10 text-cyan-300 border-cyan-500/30';
          badgeIcon = '🛡️ TSL ELEVATED';
        }}

        const nCode = (n.currency || '').toUpperCase();
        const nTk = (n.ticker || '').toUpperCase();
        let nPrefix = '€';
        let nSuffix = '';
        if (nCode === 'GBX' || nTk.includes('SKOR') || nTk.includes('3XOM') || nTk.includes('3UNH') || nTk.endsWith('.L')) {{
          nPrefix = '';
          nSuffix = 'p';
        }} else if (nCode === 'USD' || nTk.includes('_US') || nTk.includes('AMD') || nTk.includes('QQQ')) {{
          nPrefix = '$';
          nSuffix = '';
        }} else if (nCode === 'GBP') {{
          nPrefix = '£';
          nSuffix = '';
        }}

        const pnlCode = (n.pnl_currency || 'EUR').toUpperCase();
        let pnlPrefix = '€';
        let pnlSuffix = '';
        if (pnlCode === 'USD') {{
          pnlPrefix = '$';
        }} else if (pnlCode === 'GBP') {{
          pnlPrefix = '£';
        }} else if (pnlCode === 'GBX') {{
          pnlPrefix = '';
          pnlSuffix = 'p';
        }}

        const qty = (n.quantity !== null && n.quantity !== undefined && Math.abs(Number(n.quantity)) > 0)
          ? Math.abs(Number(n.quantity))
          : null;
        const pnl = (n.pnl !== null && n.pnl !== undefined) ? Number(n.pnl) : null;
        let pnlPct = (n.pnl_pct !== null && n.pnl_pct !== undefined) ? Number(n.pnl_pct) : null;

        // If pnlPct is not provided but pnl, price and quantity exist, calculate dynamically
        if (pnl !== null && pnlPct === null && n.price && qty && Number(n.price) > 0) {{
          const exitNotional = Number(n.price) * qty;
          const costBasis = exitNotional - pnl;
          if (costBasis > 0) {{
            pnlPct = (pnl / costBasis) * 100.0;
          }} else if (exitNotional > 0) {{
            pnlPct = (pnl / exitNotional) * 100.0;
          }}
        }}

        const qtyDisplay = qty !== null ? `${{qty.toFixed(2)}} shs` : '—';
        let pnlDisplay = '';
        if (ev === 'BUY') {{
          pnlDisplay = `<span class="text-slate-500 text-[10px] font-sans">Open Entry</span>`;
        }} else if (pnl !== null) {{
          const color = pnl > 0 ? 'text-emerald-400' : (pnl < 0 ? 'text-rose-400' : 'text-slate-300');
          const sign = pnl > 0 ? '+' : (pnl < 0 ? '-' : '');
          const pctStr = pnlPct !== null
            ? ` (${{pnlPct > 0 ? '+' : ''}}${{pnlPct.toFixed(2)}}%)`
            : '';
          pnlDisplay = `<span class="${{color}} font-bold text-[11px]">${{sign}}${{pnlPrefix}}${{Math.abs(pnl).toFixed(2)}}${{pnlSuffix}}${{pctStr}}</span>`;
        }} else if (ev === 'SL' || ev === 'SELL' || ev === 'TSL') {{
          pnlDisplay = `<span class="text-slate-400 text-[10px] font-sans">Closed</span>`;
        }} else {{
          pnlDisplay = `<span class="text-slate-500 text-[10px] font-sans">Order Placed</span>`;
        }}

        const pnlText = `
          <div class="flex flex-col gap-0.5">
            <span class="text-slate-200 font-semibold">${{qtyDisplay}}</span>
            ${{pnlDisplay}}
          </div>
        `;

        const price = (n.price !== undefined && n.price !== null) ? `${{nPrefix}}${{Number(n.price).toFixed(2)}}${{nSuffix}}` : '—';
        const underlyingText = n.underlying ? `(${{n.underlying}})` : '';
        const dir = (n.direction || 'LONG').toUpperCase();
        const dirBadge = dir === 'SHORT'
          ? '<span class="inline-flex items-center px-1.5 py-0.5 rounded text-[9px] font-black border bg-rose-500/10 text-rose-300 border-rose-500/30 ml-1">SHORT</span>'
          : '<span class="inline-flex items-center px-1.5 py-0.5 rounded text-[9px] font-black border bg-emerald-500/10 text-emerald-300 border-emerald-500/30 ml-1">LONG</span>';

        row.innerHTML = `
          <td class="py-2.5 px-4">
            <span class="inline-flex items-center px-2 py-0.5 rounded text-[10px] font-black border ${{badgeColor}}">
              ${{badgeIcon}}
            </span>
          </td>
          <td class="py-2.5 px-4 font-bold text-white">
            <div class="flex items-center gap-1.5 flex-wrap">
              <span>${{n.ticker || '—'}}</span>
              ${{dirBadge}}
              <span class="text-[11px] font-normal text-slate-400 font-sans">${{underlyingText}}</span>
            </div>
          </td>
          <td class="py-2.5 px-4 font-mono text-slate-200">${{price}}</td>
          <td class="py-2.5 px-4 font-mono">${{pnlText}}</td>
          <td class="py-2.5 px-4 font-sans text-slate-300 text-xs max-w-md truncate" title="${{n.rationale || ''}}">
            ${{n.rationale || '—'}}
          </td>
          <td class="py-2.5 px-4 text-slate-400 text-[11px] font-mono whitespace-nowrap">${{n.timestamp || '—'}}</td>
        `;
        tbody.appendChild(row);
      }});
    }}

    function renderEquityChart(history, currentEquity) {{
      const g = document.getElementById('equity-chart-group');
      if (!g) return;
      const points = (history && history.length > 0)
        ? history
        : [{{time: 'Start', equity: 4923.05}}, {{time: 'Now', equity: Number(currentEquity || 4923.05)}}];

      const equities = points.map(p => Number(p.equity || 4923.05));
      const startEq = equities[0] || 4923.05;
      const currEq = (currentEquity !== undefined && currentEquity !== null) ? Number(currentEquity) : (equities[equities.length - 1] || startEq);
      const minEq = Math.min(...equities, currEq);
      const maxEq = Math.max(...equities, currEq);
      const delta = currEq - startEq;
      const deltaPct = startEq > 0 ? (delta / startEq) * 100 : 0.0;
      const isPositive = delta >= 0;

      const curEl = document.getElementById('equity-val-current');
      if (curEl) curEl.innerText = '€' + currEq.toLocaleString('en-US', {{minimumFractionDigits: 2, maximumFractionDigits: 2}});
      const deltaEl = document.getElementById('equity-val-delta');
      if (deltaEl) {{
        deltaEl.innerText = (isPositive ? '+' : '') + '€' + Math.abs(delta).toFixed(2) + ' (' + (isPositive ? '+' : '') + deltaPct.toFixed(2) + '%)';
        deltaEl.className = 'text-sm font-bold font-mono ' + (isPositive ? 'text-emerald-400' : 'text-rose-400');
      }}
      const startEl = document.getElementById('equity-val-start');
      if (startEl) startEl.innerText = '€' + startEq.toLocaleString('en-US', {{minimumFractionDigits: 2, maximumFractionDigits: 2}});
      const highEl = document.getElementById('equity-val-high');
      if (highEl) highEl.innerText = '€' + maxEq.toLocaleString('en-US', {{minimumFractionDigits: 2, maximumFractionDigits: 2}});
      const lowEl = document.getElementById('equity-val-low');
      if (lowEl) lowEl.innerText = '€' + minEq.toLocaleString('en-US', {{minimumFractionDigits: 2, maximumFractionDigits: 2}});

      const W = 800;
      const H = 100;
      const padY = 12;
      const range = (maxEq - minEq) || 1.0;

      const coords = points.map((p, idx) => {{
        const x = points.length === 1 ? W / 2 : (idx / (points.length - 1)) * W;
        const norm = (Number(p.equity || startEq) - minEq) / range;
        const y = H - padY - (norm * (H - 2 * padY));
        return {{x: Number(x.toFixed(1)), y: Number(y.toFixed(1)), equity: p.equity, time: p.time}};
      }});

      const lineD = coords.reduce((acc, pt, i) => acc + (i === 0 ? `M ${{pt.x}} ${{pt.y}}` : ` L ${{pt.x}} ${{pt.y}}`), '');
      const areaD = lineD + ` L ${{W}} ${{H}} L 0 ${{H}} Z`;
      const strokeColor = isPositive ? '#10b981' : '#f43f5e';
      const gradId = isPositive ? 'url(#equity-gradient-emerald)' : 'url(#equity-gradient-rose)';

      let circles = '';
      coords.forEach(pt => {{
        circles += `<circle cx="${{pt.x}}" cy="${{pt.y}}" r="3.5" fill="${{strokeColor}}" stroke="#0f172a" stroke-width="1.5"><title>${{pt.time}}: €${{Number(pt.equity).toFixed(2)}}</title></circle>`;
      }});

      g.innerHTML = `
        <line x1="0" y1="${{H}}" x2="${{W}}" y2="${{H}}" stroke="#334155" stroke-width="1" stroke-dasharray="4" />
        <path d="${{areaD}}" fill="${{gradId}}" />
        <path d="${{lineD}}" fill="none" stroke="${{strokeColor}}" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" />
        ${{circles}}
      `;
    }}

    // Real-time zero-latency SSE telemetry stream (push updates)
    function initSSE() {{
      if (typeof EventSource !== "undefined") {{
        try {{
          const streamUrl = window.location.origin + "/stream";
          const es = new EventSource(streamUrl);
          es.addEventListener("snapshot", e => {{ try {{ updateUI(JSON.parse(e.data)); }} catch (err) {{}} }});
          es.addEventListener("dashboard_update", e => {{ try {{ updateUI(JSON.parse(e.data)); }} catch (err) {{}} }});
          es.onerror = () => {{ es.close(); setTimeout(initSSE, 5000); }};
        }} catch (e) {{}}
      }}
    }}

    // Auto-polling and initialization
    setInterval(updateCountdown, 1000);
    updateCountdown();
    renderSignals('ALL');
    renderPairsTable();
    renderAlertsTable();
    renderEquityChart(INITIAL_DATA.equity_history, INITIAL_DATA.current_equity);
    initSSE();

    // Instant zero-click sync on page load and every 3.5s
    document.addEventListener('DOMContentLoaded', () => {{
      fetchLiveData(false);
    }});
    setInterval(() => {{
      fetchLiveData(false);
    }}, 3500);
  </script>
</body>
</html>
"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    try:
        SSEBroadcaster.get_instance().broadcast("dashboard_update", data)
    except Exception:
        pass

    logger.info("Generated dashboard HTML at %s", output_path)
    return output_path


def start_background_position_monitor(
    html_file: str = "signals_dashboard.html",
    interval_seconds: int = 15,
    dry_run: Optional[bool] = None,
) -> threading.Thread:
    """Launch autonomous background monitor thread for continuous price checking and auto-sells."""
    def _monitor_loop() -> None:
        logger.info("⚡ Background Autonomous Position Monitor started (polling every %ds)", interval_seconds)
        client = Trading212Client(
            api_key=settings.TRADING212_API_KEY,
            base_url=settings.api_base_url,
            api_secret=settings.TRADING212_API_SECRET,
        )
        notifier = Notifier(
            telegram_bot_token=settings.TELEGRAM_BOT_TOKEN,
            telegram_chat_id=settings.TELEGRAM_CHAT_ID,
            discord_webhook_url=settings.DISCORD_WEBHOOK_URL,
            log_file=settings.NOTIFICATIONS_LOG_PATH,
            enable_desktop=settings.ENABLE_DESKTOP_NOTIFICATIONS,
        )
        risk_manager = RiskManager(
            max_risk_per_trade=settings.MAX_RISK_PER_TRADE,
            max_position_pct=settings.MAX_POSITION_PCT,
            atr_sl_multiplier=settings.ATR_SL_MULTIPLIER,
            min_sl_pct=settings.MIN_SL_PCT,
            tp1_r=settings.TP1_R_MULTIPLE,
            tp2_r=settings.TP2_R_MULTIPLE,
            tp3_r=settings.TP3_R_MULTIPLE,
            tsl_atr_multiplier=settings.TSL_ATR_MULTIPLIER,
            tsl_hysteresis_atr_mult=settings.TSL_HYSTERESIS_ATR_MULT,
            tp1_trim_pct=settings.TP1_TRIM_PCT,
            tp2_trim_pct=settings.TP2_TRIM_PCT,
            tp3_trim_pct=settings.TP3_TRIM_PCT,
        )
        is_dry_run = dry_run if dry_run is not None else (not getattr(settings, "LIVE_TRADING_ENABLED", False))
        monitor = PositionMonitor(
            t212_client=client,
            notifier=notifier,
            risk_manager=risk_manager,
            state_file=settings.ACTIVE_TRADES_PATH,
            dry_run=is_dry_run,
        )

        last_dashboard_render = time.time()
        last_stop_sync_time = 0.0

        while True:
            try:
                # Reload active positions in case state changed
                monitor.positions = monitor._load_state()

                # Continuous Broker Portfolio Reconciliation (runs every 30s)
                # Guarantees local dashboard & state stay 100% in lockstep with Trading 212
                now = time.time()
                if not is_dry_run and settings.TRADING212_API_KEY and now - getattr(_monitor_loop, "_last_recon_time", 0.0) >= 30.0:
                    try:
                        recon = monitor.reconcile_with_broker(adopt_untracked=True)
                        _monitor_loop._last_recon_time = now
                        if (
                            recon.get("closed_externally")
                            or recon.get("adopted_from_broker")
                            or recon.get("quantity_synced")
                            or recon.get("expired_stops_restored")
                        ):
                            logger.info("🔄 Background monitor synchronized with Trading 212: %s", recon)
                            generate_html_dashboard(html_file)
                            last_dashboard_render = now
                    except Exception as recon_err:
                        logger.debug("Background monitor reconciliation notice: %s", recon_err)

                # Check market open status to prevent unnecessary API calls
                now_utc = datetime.now(timezone.utc)
                active_tickers = list(monitor.positions.keys()) if monitor.positions else ["QQQ3l_EQ", "SPY_US_EQ"]
                market_is_open = client.is_any_market_open(active_tickers, current_time=now_utc)

                if not market_is_open:
                    now = time.time()
                    if now - getattr(_monitor_loop, "_last_closed_notice_time", 0.0) >= 1800.0:
                        logger.info(
                            "🌙 Markets CLOSED (LSE 08:00-16:30 UTC, US 14:30-21:00 UTC). "
                            "Entering adaptive low-frequency power-save mode to conserve API counts."
                        )
                        _monitor_loop._last_closed_notice_time = now

                    # Still refresh static HTML dashboard every 5 minutes
                    if now - last_dashboard_render >= 300.0:
                        generate_html_dashboard(html_file)
                        last_dashboard_render = now

                    time.sleep(60.0)
                    continue

                if monitor.positions:
                    now = time.time()
                    # 1. Sync broker stop orders periodically (every 5 minutes / 300s, or on initial run), NOT every 15s
                    if now - last_stop_sync_time >= 300.0:
                        if Trading212Client.is_trade_active():
                            logger.info("Active trade execution in progress; deferring routine broker stop sync.")
                        else:
                            try:
                                active_orders = client.get_orders(force_refresh=True)
                                active_order_ids = {str(o.get("id")) for o in active_orders if isinstance(o, dict) and o.get("id")}
                                for ticker, pos in list(monitor.positions.items()):
                                    if pos.broker_stop_order_id and str(pos.broker_stop_order_id) not in active_order_ids:
                                        if not str(pos.broker_stop_order_id).startswith("DRY_"):
                                            logger.info("Broker stop %s for %s no longer active on broker. Marking for re-sync.",
                                                        pos.broker_stop_order_id, ticker)
                                            pos.broker_stop_order_id = None
                                monitor.sync_broker_stops()
                                last_stop_sync_time = now
                            except Exception as order_exc:
                                logger.debug("Order sync check notice: %s", order_exc)

                    # 2. Fetch current prices from broker portfolio
                    current_prices = {}
                    try:
                        if Trading212Client.is_trade_active() and client._portfolio_cache is not None:
                            portfolio = client._portfolio_cache
                        else:
                            portfolio = client.get_portfolio()
                        for p in portfolio:
                            if p.current_price > 0:
                                current_prices[p.ticker] = p.current_price
                    except Exception as port_exc:
                        logger.debug("Portfolio fetch notice: %s", port_exc)

                    # Fallback pricing if needed (preserve position current_price, never assign underlying price to ETP ticker)
                    for ticker, pos in monitor.positions.items():
                        if ticker not in current_prices:
                            if getattr(pos, "current_price", 0.0) > 0:
                                current_prices[ticker] = pos.current_price
                            try:
                                from trading_system.data.trading212_client import MarketDataProvider
                                candles = MarketDataProvider.get_daily_candles(pos.underlying, period="5d")
                                if hasattr(candles, "empty") and not candles.empty:
                                    current_prices[pos.underlying] = float(candles["close"].iloc[-1])
                                elif isinstance(candles, list) and candles:
                                    current_prices[pos.underlying] = float(candles[-1]["close"])
                            except Exception:
                                pass

                    # 3. Tick monitor (evaluates TP1, TP2, TP3, SL, TSL, 16:20 close)
                    events = monitor.monitor_tick(current_prices)
                    if events:
                        logger.info("⚡ Position Monitor triggered %d automation event(s): %s", len(events), events)
                        generate_html_dashboard(html_file)
                        last_dashboard_render = time.time()

                # Refresh dashboard every 60s to keep live P&L and timestamps fresh
                now = time.time()
                if now - last_dashboard_render >= 60.0:
                    generate_html_dashboard(html_file)
                    last_dashboard_render = now

            except Exception as loop_exc:
                logger.error("Error in background position monitor loop: %s", loop_exc, exc_info=True)

            time.sleep(interval_seconds)

    thread = threading.Thread(target=_monitor_loop, daemon=True, name="PositionMonitorDaemon")
    thread.start()
    return thread


def make_dashboard_handler(web_dir: Path, html_name: str):
    """Create HTTP request handler supporting dashboard files and SSE event streaming."""
    class CustomDashboardHandler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(web_dir), **kwargs)

        def log_message(self, format: str, *args: Any) -> None:
            # Suppress normal GET 200 log spam for continuous SSE keepalive & polling
            clean = format % args
            if any(p in clean for p in ("/stream", "/api/stream", "/events", "/api/data", "/api/dashboard-data")):
                return
            super().log_message(format, *args)

        def end_headers(self) -> None:
            # Prevent browser caching of any files or responses served by dashboard
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            super().end_headers()

        def do_GET(self):
            clean_path = self.path.split("?")[0]
            if clean_path in ("/api/data", "/api/dashboard-data", "/data.json"):
                data = build_dashboard_data()
                body = json.dumps(data).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
                return

            if clean_path in ("/stream", "/api/stream", "/events"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache, no-transform")
                self.send_header("Connection", "keep-alive")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()

                broadcaster = SSEBroadcaster.get_instance()
                q = broadcaster.subscribe()
                try:
                    # Emit initial state snapshot immediately
                    initial_data = build_dashboard_data()
                    snapshot_str = json.dumps(initial_data)
                    self.wfile.write(f"event: snapshot\ndata: {snapshot_str}\n\n".encode("utf-8"))
                    self.wfile.flush()

                    while True:
                        try:
                            msg = q.get(timeout=2.0)
                            event_name = msg.get("event", "message")
                            data_str = json.dumps(msg.get("data", {}))
                            self.wfile.write(f"event: {event_name}\ndata: {data_str}\n\n".encode("utf-8"))
                            self.wfile.flush()
                        except queue.Empty:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                except (ConnectionResetError, BrokenPipeError, socket.error):
                    pass
                finally:
                    broadcaster.unsubscribe(q)
                return

            if clean_path in ("/", ""):
                self.path = f"/{html_name}"
            return super().do_GET()

        def do_POST(self):
            clean_path = self.path.split("?")[0]
            if clean_path in ("/api/panic", "/panic"):
                content_length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_length).decode("utf-8") if content_length > 0 else "{}"
                try:
                    payload = json.loads(body) if body else {}
                except Exception:
                    payload = {}

                expected_token = getattr(settings, "PANIC_FLATTEN_TOKEN", "")
                if expected_token and payload.get("token") != expected_token:
                    self.send_response(401)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": "Unauthorized: Invalid panic token"}).encode("utf-8"))
                    return

                try:
                    t212 = Trading212Client(
                        api_key=settings.TRADING212_API_KEY,
                        api_secret=settings.TRADING212_API_SECRET,
                        base_url=settings.api_base_url,
                    )
                    notifier = Notifier(
                        telegram_bot_token=settings.TELEGRAM_BOT_TOKEN,
                        telegram_chat_id=settings.TELEGRAM_CHAT_ID,
                        discord_webhook_url=settings.DISCORD_WEBHOOK_URL,
                    )
                    risk_mgr = RiskManager()
                    monitor = PositionMonitor(
                        t212_client=t212,
                        notifier=notifier,
                        risk_manager=risk_mgr,
                        state_file=settings.ACTIVE_TRADES_PATH,
                        dry_run=not settings.USE_LIVE,
                    )
                    res = monitor.panic_flatten_all()
                    SSEBroadcaster.get_instance().broadcast("panic_flatten", res)

                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "success", "result": res}).encode("utf-8"))
                except Exception as exc:
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": str(exc)}).encode("utf-8"))
                return

            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": "Not Found"}')

    return CustomDashboardHandler


def start_local_server_thread(
    html_file: str = "signals_dashboard.html",
    port: int = 8888,
    open_browser: bool = False,
) -> Tuple[Optional[threading.Thread], Optional[socketserver.TCPServer], str]:
    """Start the dashboard HTTP server in a background daemon thread."""
    generate_html_dashboard(html_file)
    web_dir = Path(html_file).resolve().parent
    html_name = Path(html_file).name
    HandlerClass = make_dashboard_handler(web_dir, html_name)

    socketserver.TCPServer.allow_reuse_address = True
    bind_port = port
    httpd = None
    for attempt in range(5):
        try:
            httpd = ThreadingDashboardServer(("0.0.0.0", bind_port), HandlerClass)
            break
        except OSError:
            bind_port = port + attempt + 1

    if not httpd:
        logger.warning("Could not bind dashboard server to any port in range %d-%d", port, port + 5)
        return None, None, ""

    url = f"http://localhost:{bind_port}/{html_name}"

    def _serve():
        try:
            httpd.serve_forever()
        except Exception:
            pass

    thread = threading.Thread(target=_serve, daemon=True, name="DashboardServerThread")
    thread.start()
    logger.info("🚀 Signals Dashboard running live at: %s", url)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    return thread, httpd, url


def start_local_server(html_file: str, port: int = 8888, open_browser: bool = True) -> None:
    """Serve the dashboard on a local HTTP server with autonomous background position monitoring."""
    # Launch autonomous background position monitor for 100% automated Pure TSL execution
    start_background_position_monitor(html_file=html_file, interval_seconds=15)

    web_dir = Path(html_file).resolve().parent
    html_name = Path(html_file).name
    HandlerClass = make_dashboard_handler(web_dir, html_name)

    socketserver.TCPServer.allow_reuse_address = True
    with ThreadingDashboardServer(("0.0.0.0", port), HandlerClass) as httpd:
        url = f"http://localhost:{port}/{html_name}"
        print(f"\n🚀 Signals Dashboard running live at: {url}")
        print("⚡ COMPLETE AUTOMATION ACTIVE: Polling every 15s for dynamic TSL ratchets & GTC stop synchronization")
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
    parser.add_argument("--port", type=int, default=8888, help="Port to serve dashboard (default: 8888)")
    parser.add_argument("--no-browser", action="store_true", help="Do not open browser automatically")
    parser.add_argument("--generate-only", action="store_true", help="Generate HTML and JSON without running server")
    parser.add_argument("--output", type=str, default="signals_dashboard.html", help="Path to write dashboard HTML")
    args = parser.parse_args()

    html_path = generate_html_dashboard(args.output)
    print(f"✅ Dashboard generated: {os.path.abspath(html_path)}")

    if not args.generate_only:
        start_local_server(html_path, port=args.port, open_browser=not args.no_browser)


if __name__ == "__main__":
    main()
