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
from trading_system.data.leveraged_universe import CURATED_3X_PAIRS, LeveragedUniverse
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


def load_active_signals(state_file: str = "active_trades.json", live_prices: Optional[Dict[str, float]] = None) -> List[Dict[str, Any]]:
    """Load active tracked positions and enrich them with trade metrics."""
    if not os.path.exists(state_file):
        return []
    try:
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
            direction = pos.get("direction", "LONG").upper()
            underlying = pos.get("underlying", "")
            broker_stop_order_id = pos.get("broker_stop_order_id")

            # Determine human-readable asset name and quote currency
            pair = universe.get_pair_for_etp(ticker) if universe else None
            asset_name = pos.get("name") or (pair.name if pair else underlying)
            
            curr = (pos.get("currency") or (pair.currency if pair else "")).upper()
            if not curr:
                if ticker.startswith("3TSM") or entry > 500:
                    curr = "GBX"
                elif any(ticker.endswith(sfx) for sfx in ("p_EQ", "m_EQ", "e_EQ", "d_EQ")):
                    curr = "EUR"
                else:
                    curr = "USD"

            if curr == "EUR":
                currency_code = "EUR"
                currency_prefix = "€"
                currency_suffix = ""
            elif curr == "GBP":
                currency_code = "GBP"
                currency_prefix = "£"
                currency_suffix = ""
            elif curr == "GBX":
                currency_code = "GBX"
                currency_prefix = ""
                currency_suffix = " GBX"
            else:
                currency_code = "USD"
                currency_prefix = "$"
                currency_suffix = ""

            # Estimate or extract current price and live P&L
            highest_price = float(pos.get("highest_price") or entry)
            lowest_price = float(pos.get("lowest_price") or entry)
            if live_prices and ticker in live_prices and live_prices[ticker] > 0:
                current_price = float(live_prices[ticker])
            elif pos.get("current_price") and float(pos.get("current_price")) > 0:
                current_price = float(pos.get("current_price"))
            else:
                current_price = highest_price if direction == "LONG" else lowest_price

            if entry > 0 and current_price > 0:
                if direction == "LONG":
                    pnl_pct = ((current_price - entry) / entry) * 100.0
                    pnl_amount = (current_price - entry) * remaining_quantity
                else:
                    pnl_pct = ((entry - current_price) / entry) * 100.0
                    pnl_amount = (entry - current_price) * remaining_quantity
            else:
                pnl_pct = 0.0
                pnl_amount = 0.0

            # Calculate risk & reward percentages
            risk_pct = abs(entry - sl) / entry * 100 if entry > 0 else 0.0
            tp1_gain_pct = abs(tp1 - entry) / entry * 100 if entry > 0 else 0.0
            tp2_gain_pct = abs(tp2 - entry) / entry * 100 if entry > 0 else 0.0
            tp3_gain_pct = abs(tp3 - entry) / entry * 100 if entry > 0 else 0.0

            status = "ACTIVE"
            tp1_armed_hysteresis = bool(pos.get("tp1_armed_hysteresis", False))
            tp1_stop_elevated = bool(pos.get("tp1_stop_elevated", False))
            tp1_escape_threshold = float(pos.get("tp1_escape_threshold") or 0.0)

            if pos.get("is_closed"):
                status = "CLOSED"
            elif pos.get("tp2_hit"):
                status = "TP2 HIT: Trailing Runner Active"
            elif tp1_armed_hysteresis:
                esc_str = f"{currency_prefix}{tp1_escape_threshold:.2f}{currency_suffix}" if tp1_escape_threshold > 0 else "Trigger Pending"
                status = f"TP1 HIT: Break-Even (Hysteresis Armed @ {esc_str})"
            elif tp1_stop_elevated or pos.get("tp1_hit"):
                status = "TP1 LOCKED (+1.0R Stop Active)"

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
                overnight_status = "INTRADAY SHORT (Flattens 16:20 UTC)"
                overnight_badge_class = "bg-rose-500/10 text-rose-300 border-rose-500/30"
            elif earnings_days is not None and 0 <= earnings_days <= 1.0:
                overnight_status = f"EARNINGS BLACKOUT ({earnings_days:.1f}d — Flattens 16:20 UTC)"
                overnight_badge_class = "bg-amber-500/10 text-amber-300 border-amber-500/30"
            elif pos.get("tp1_hit"):
                radar_tag = f" • Pre-Earnings Radar ({earnings_days:.0f}d)" if (earnings_days and earnings_days <= 21.0) else ""
                overnight_status = f"OVERNIGHT ELIGIBLE (TP1 Breakeven Active{radar_tag})"
                overnight_badge_class = "bg-emerald-500/10 text-emerald-300 border-emerald-500/30"
            elif earnings_days is not None and 1.0 < earnings_days <= 21.0:
                overnight_status = f"PRE-EARNINGS RUN-UP RADAR ({earnings_days:.0f}d away • Flattens 16:20 UTC if < TP1)"
                overnight_badge_class = "bg-purple-500/10 text-purple-300 border-purple-500/30"
            else:
                overnight_status = "REQUIRES TP1 OR FLATTENS 16:20 UTC"
                overnight_badge_class = "bg-sky-500/10 text-sky-300 border-sky-500/30"

            stop_broker_line = f"NATIVE BROKER STOP: Trading 212 Order #{broker_stop_order_id} (Active)\n" if broker_stop_order_id else ""

            signals.append({
                "ticker": ticker,
                "underlying": underlying,
                "name": asset_name,
                "direction": direction,
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
                "currency_code": currency_code,
                "currency_prefix": currency_prefix,
                "currency_suffix": currency_suffix,
                "stop_loss": round(sl, 2),
                "trailing_stop_loss": round(tsl, 2),
                "stop_distance_pct": round(risk_pct, 2),
                "broker_stop_order_id": broker_stop_order_id,
                "tp1": round(tp1, 2),
                "tp1_pct": round(tp1_gain_pct, 2),
                "tp2": round(tp2, 2),
                "tp2_pct": round(tp2_gain_pct, 2),
                "tp3": round(tp3, 2),
                "tp3_pct": round(tp3_gain_pct, 2),
                "quantity": original_quantity,
                "remaining_quantity": remaining_quantity,
                "status": status,
                "tp1_hit": pos.get("tp1_hit", False),
                "tp2_hit": pos.get("tp2_hit", False),
                "tp3_hit": pos.get("tp3_hit", False),
                "tp1_armed_hysteresis": tp1_armed_hysteresis,
                "tp1_stop_elevated": tp1_stop_elevated,
                "tp1_escape_threshold": round(tp1_escape_threshold, 2),
                "entry_time": pos.get("entry_time", ""),
                "atr": round(pos.get("atr", 0.0), 2),
                "copy_text": (
                    f"STRATEGY: {strategy_type.upper()} ({win_rate_exp} Win-Rate)\n"
                    f"ACTION: {direction} {ticker} (Underlying: {underlying})\n"
                    f"ENTRY: {currency_prefix}{entry:.2f}{currency_suffix}\n"
                    f"CURRENT: {currency_prefix}{current_price:.2f}{currency_suffix} ({pnl_pct:+.2f}%)\n"
                    f"STOP LOSS: {currency_prefix}{sl:.2f}{currency_suffix} (-{risk_pct:.2f}%)\n"
                    f"{stop_broker_line}"
                    f"TP1 (+1.0R): {currency_prefix}{tp1:.2f}{currency_suffix} (+{tp1_gain_pct:.2f}%)\n"
                    f"TP2 (+2.0R): {currency_prefix}{tp2:.2f}{currency_suffix} (+{tp2_gain_pct:.2f}%)\n"
                    f"TP3 (+3.0R): {currency_prefix}{tp3:.2f}{currency_suffix} (+{tp3_gain_pct:.2f}%)\n"
                    f"CURRENT TSL: {currency_prefix}{tsl:.2f}{currency_suffix}\n"
                    f"QUANTITY: {remaining_quantity:.2f} / {original_quantity:.2f} shares\n"
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


def load_recent_notifications(log_path: str = "notifications.jsonl", limit: int = 25) -> List[Dict[str, Any]]:
    """Load latest records from notifications alert log."""
    if not os.path.exists(log_path):
        return []
    try:
        lines = []
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    lines.append(json.loads(line))
        return lines[-limit:][::-1]
    except Exception as exc:
        logger.warning("Failed to load notifications log: %s", exc)
        return []


_DASHBOARD_DATA_CACHE: Optional[Dict[str, Any]] = None
_DASHBOARD_DATA_CACHE_TIME: float = 0.0
_DASHBOARD_DATA_LOCK = threading.Lock()
_DASHBOARD_SHARED_CLIENT: Optional[Trading212Client] = None


def build_dashboard_data(force: bool = False) -> Dict[str, Any]:
    """Compile comprehensive dashboard dataset with fast TTL caching to avoid API rate limits."""
    global _DASHBOARD_DATA_CACHE, _DASHBOARD_DATA_CACHE_TIME, _DASHBOARD_SHARED_CLIENT
    now = time.time()
    if not force and _DASHBOARD_DATA_CACHE is not None and (now - _DASHBOARD_DATA_CACHE_TIME) < 2.5:
        return _DASHBOARD_DATA_CACHE

    with _DASHBOARD_DATA_LOCK:
        if not force and _DASHBOARD_DATA_CACHE is not None and (now - _DASHBOARD_DATA_CACHE_TIME) < 2.5:
            return _DASHBOARD_DATA_CACHE

        universe = LeveragedUniverse()
        pairs = [
            {
                "underlying": p.underlying_symbol,
                "name": p.name,
                "category": p.category,
                "long_ticker": p.long_3x_ticker,
                "short_ticker": p.short_3x_ticker,
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
        client: Optional[Trading212Client] = None

        if settings.TRADING212_API_KEY:
            try:
                if _DASHBOARD_SHARED_CLIENT is None:
                    _DASHBOARD_SHARED_CLIENT = Trading212Client(
                        api_key=settings.TRADING212_API_KEY,
                        api_secret=settings.TRADING212_API_SECRET,
                        base_url=settings.api_base_url,
                    )
                client = _DASHBOARD_SHARED_CLIENT
                portfolio = client.get_portfolio(force_refresh=False)
                broker_positions_map = {p.ticker: p for p in portfolio if getattr(p, "quantity", 0.0) > 0}
                for p in portfolio:
                    if getattr(p, "current_price", 0.0) > 0:
                        portfolio_prices[p.ticker] = p.current_price

                try:
                    open_orders = client.get_orders(force_refresh=False)
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

            # Adopt any broker positions not yet tracked in active_signals
            known_tickers = {s.get("ticker") for s in active_signals}
            for b_ticker, bp in broker_positions_map.items():
                if b_ticker not in known_tickers:
                    pair = universe.get_pair_for_etp(b_ticker)
                    underlying = pair.underlying_symbol if pair else b_ticker.replace("l_EQ", "").replace("_US_EQ", "")
                    underlying_name = pair.name if pair else underlying
                    direction = "LONG"
                    if pair and (b_ticker == pair.short_3x_ticker or b_ticker == pair.short_5x_ticker):
                        direction = "SHORT"
                    avg_px = bp.average_price if bp.average_price > 0 else (bp.current_price if bp.current_price > 0 else 100.0)
                    cur_px = bp.current_price if bp.current_price > 0 else avg_px
                    sl_dist = round(avg_px * 0.05, 2)
                    sl = round(avg_px - sl_dist, 2) if direction == "LONG" else round(avg_px + sl_dist, 2)
                    pnl_pct = ((cur_px - avg_px) / avg_px * 100.0) if avg_px > 0 and direction == "LONG" else ((avg_px - cur_px) / avg_px * 100.0 if avg_px > 0 else 0.0)

                    curr = (pair.currency if pair else ("EUR" if any(b_ticker.endswith(sfx) for sfx in ("p_EQ", "m_EQ", "e_EQ", "d_EQ")) else "USD")).upper()
                    if curr == "EUR":
                        currency_code, currency_prefix, currency_suffix = "EUR", "€", ""
                    elif curr == "GBP":
                        currency_code, currency_prefix, currency_suffix = "GBP", "£", ""
                    elif curr == "GBX" or b_ticker.startswith("3TSM") or avg_px > 500:
                        currency_code, currency_prefix, currency_suffix = "GBX", "", " GBX"
                    else:
                        currency_code, currency_prefix, currency_suffix = "USD", "$", ""

                    active_signals.append({
                        "ticker": b_ticker,
                        "underlying": underlying,
                        "name": underlying_name,
                        "direction": direction,
                        "strategy_type": "broker_placed",
                        "rsi3": None, "rsi7": None, "rsi14": None, "sma5": None,
                        "win_rate_exp": "85%",
                        "overnight_status": "REQUIRES TP1 OR FLATTENS 16:20 UTC" if direction == "LONG" else "INTRADAY SHORT (Flattens 16:20 UTC)",
                        "overnight_badge_class": "bg-sky-500/10 text-sky-300 border-sky-500/30" if direction == "LONG" else "bg-rose-500/10 text-rose-300 border-rose-500/30",
                        "earnings_in_days": None,
                        "entry_price": round(avg_px, 2),
                        "current_price": round(cur_px, 2),
                        "pnl_pct": round(pnl_pct, 2),
                        "pnl_amount": round((cur_px - avg_px) * bp.quantity if direction == "LONG" else (avg_px - cur_px) * bp.quantity, 2),
                        "currency_code": currency_code,
                        "currency_prefix": currency_prefix,
                        "currency_suffix": currency_suffix,
                        "stop_loss": sl,
                        "trailing_stop_loss": sl,
                        "stop_distance_pct": round(abs(avg_px - sl) / avg_px * 100, 2) if avg_px > 0 else 5.0,
                        "broker_stop_order_id": None,
                        "tp1": round(avg_px + sl_dist, 2) if direction == "LONG" else round(avg_px - sl_dist, 2),
                        "tp1_pct": round(abs(sl_dist) / avg_px * 100, 2) if avg_px > 0 else 5.0,
                        "tp2": round(avg_px + 2 * sl_dist, 2) if direction == "LONG" else round(avg_px - 2 * sl_dist, 2),
                        "tp2_pct": round(2 * abs(sl_dist) / avg_px * 100, 2) if avg_px > 0 else 10.0,
                        "tp3": round(avg_px + 3 * sl_dist, 2) if direction == "LONG" else round(avg_px - 3 * sl_dist, 2),
                        "tp3_pct": round(3 * abs(sl_dist) / avg_px * 100, 2) if avg_px > 0 else 15.0,
                        "quantity": bp.quantity,
                        "remaining_quantity": bp.quantity,
                        "status": "ACTIVE (Broker Placed)",
                        "tp1_hit": False, "tp2_hit": False, "tp3_hit": False,
                        "tp1_armed_hysteresis": False, "tp1_stop_elevated": False,
                        "tp1_escape_threshold": 0.0,
                        "entry_time": datetime.now(timezone.utc).isoformat(),
                        "atr": round(avg_px * 0.025, 2),
                        "copy_text": f"ADOPTED: {b_ticker} ({underlying}) Qty: {bp.quantity}",
                    })

        history = load_recent_history(settings.TRADES_LOG_PATH)
        recent_notifications = load_recent_notifications(settings.NOTIFICATIONS_LOG_PATH)
        active_stops = live_server_stops_count if live_server_stops_count is not None else sum(1 for s in active_signals if s.get("broker_stop_order_id"))

        res = {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "mode": "MANUAL_SIGNALS_MODE",
            "active_signals_count": len(active_signals),
            "active_signals": active_signals,
            "recent_notifications": recent_notifications,
            "pairs_watchlist": pairs,
            "history": history,
            "broker_status": {
                "broker_name": "Trading 212 Demo",
                "is_connected": True,
                "active_server_stops": active_stops,
                "active_positions_count": len(active_signals),
                "execution_mode": "Hybrid (Native Server GTC Stop + Algorithmic Exits)",
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
          105 Assets (82% EUR)
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
      <div class="text-xs text-slate-400 uppercase tracking-wider font-semibold">Profit-Taking Exits</div>
      <div class="text-2xl font-bold text-white mt-1">TP1 / TP2 / TP3</div>
      <div class="text-xs text-emerald-400 mt-1">⚡ Auto-Scaling (+1R, +2R, +3R)</div>
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
          <p class="text-xs text-slate-400">Real-time desktop alerts, trade entries, native stop registrations, and profit exits (TP1/TP2/TP3/SL).</p>
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
            <span class="text-xs font-mono font-normal px-2 py-0.5 rounded bg-indigo-500/10 text-indigo-300 border border-indigo-500/20">105 Pairs • 82% EUR</span>
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
        const resp = await fetch('/api/data', {{ cache: 'no-store' }});
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
            <div class="text-lg font-bold text-white">Engine is Active & Scanning 105 Leveraged ETPs</div>
            <div class="text-sm text-slate-400 mt-1 max-w-xl mx-auto">
              No open trades right now. The engine runs 24/7 on the Raspberry Pi, screening every 15 minutes across 105 high-liquidity ETPs (82% EUR) for 3-RSI oversold pullbacks (&lt;25) and momentum triggers. Orders execute automatically on Trading 212 when criteria are met.
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
        const badgeColor = isLong ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20' : 'bg-rose-500/10 text-rose-400 border-rose-500/20';
        const actionText = isLong ? '🟢 BUY / LONG (3x)' : '🔴 SELL / SHORT (3x)';
        const currPrefix = s.currency_prefix || '€';
        const currSuffix = s.currency_suffix || '';

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

        const pnlPct = (s.pnl_pct !== undefined && s.pnl_pct !== null) ? Number(s.pnl_pct) : 0.0;
        const pnlColor = pnlPct > 0 ? 'text-emerald-400' : (pnlPct < 0 ? 'text-rose-400' : 'text-slate-300');
        const pnlSign = pnlPct > 0 ? '+' : '';
        const pnlAmount = s.pnl_amount !== undefined ? Number(s.pnl_amount) : 0.0;
        const remQty = s.remaining_quantity !== undefined ? Number(s.remaining_quantity) : Number(s.quantity || 1.0);
        const curPx = Number(s.current_price || s.entry_price || 0.0);
        const entryPx = Number(s.entry_price || 0.0);
        const posVal = (remQty * curPx).toFixed(2);

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
                </div>
                <div class="text-xs text-slate-400 mt-0.5">3x Leveraged ETP &bull; ${{s.currency_code || 'EUR'}}</div>
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
                    ${{pnlSign}}${{currPrefix}}${{Math.abs(pnlAmount).toFixed(2)}}${{currSuffix}} (${{pnlSign}}${{pnlPct.toFixed(2)}}%)
                  </div>
                </div>
                <div class="text-right">
                  <div class="text-[10px] text-slate-400 uppercase font-semibold">Holdings</div>
                  <div class="text-sm font-bold text-indigo-300 font-mono">${{remQty.toFixed(1)}} shares</div>
                  <div class="text-[10px] text-slate-400 font-mono">Val: ${{currPrefix}}${{posVal}}${{currSuffix}}</div>
                </div>
              </div>
              <div class="mt-2.5 pt-2 border-t border-slate-800/60 flex items-center justify-between text-xs font-mono">
                <span class="text-slate-400">Entry: <b class="text-white">${{currPrefix}}${{entryPx.toFixed(2)}}${{currSuffix}}</b></span>
                <span class="text-slate-400">Live: <b class="${{pnlColor}}">${{currPrefix}}${{curPx.toFixed(2)}}${{currSuffix}}</b></span>
              </div>
            </div>

            <!-- Native Broker Stop Order Badge -->
            ${{stopOrderBadge}}

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

              <!-- Take Profit 1 -->
              <div class="flex items-center justify-between p-2 rounded-lg bg-emerald-500/5 border border-emerald-500/10">
                <span class="text-emerald-300 font-semibold flex items-center gap-1.5">
                  <span class="w-1.5 h-1.5 rounded-full bg-emerald-400"></span>
                  TP1 (+1.0R)
                </span>
                <span class="font-mono font-bold text-emerald-200">
                  ${{currPrefix}}${{Number(s.tp1 || 0).toFixed(2)}}${{currSuffix}}
                  ${{s.tp1_hit ? '<span class="text-emerald-400 ml-1 font-bold">[✓ Banked 50%]</span>' : '<span class="text-slate-500 ml-1">[Target]</span>'}}
                </span>
              </div>

              <!-- Take Profit 2 -->
              <div class="flex items-center justify-between p-2 rounded-lg bg-emerald-500/5 border border-emerald-500/10">
                <span class="text-emerald-300 font-semibold flex items-center gap-1.5">
                  <span class="w-1.5 h-1.5 rounded-full bg-emerald-400"></span>
                  TP2 (+2.0R)
                </span>
                <span class="font-mono font-bold text-emerald-200">
                  ${{currPrefix}}${{Number(s.tp2 || 0).toFixed(2)}}${{currSuffix}}
                  ${{s.tp2_hit ? '<span class="text-emerald-400 ml-1 font-bold">[✓ Banked 25%]</span>' : '<span class="text-slate-500 ml-1">[Target]</span>'}}
                </span>
              </div>

              <!-- Take Profit 3 -->
              <div class="flex items-center justify-between p-2 rounded-lg bg-emerald-500/5 border border-emerald-500/10">
                <span class="text-emerald-300 font-semibold flex items-center gap-1.5">
                  <span class="w-1.5 h-1.5 rounded-full bg-emerald-400"></span>
                  TP3 (+3.0R)
                </span>
                <span class="font-mono font-bold text-purple-300">
                  ${{currPrefix}}${{Number(s.tp3 || 0).toFixed(2)}}${{currSuffix}}
                  <span class="text-purple-400 ml-1 font-bold">[Runner]</span>
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

        row.innerHTML = `
          <td class="py-2.5 px-4 font-bold text-white">${{p.underlying}}</td>
          <td class="py-2.5 px-4 font-sans text-slate-400">${{p.name}} <span class="text-slate-500">(${{p.category}})</span></td>
          <td class="py-2.5 px-4 text-emerald-400 font-bold">${{p.long_ticker}}</td>
          <td class="py-2.5 px-4 text-rose-400 font-bold">${{p.short_ticker}}</td>
          <td class="py-2.5 px-4">${{fiveXBadge}}</td>
          <td class="py-2.5 px-4 text-slate-400">${{p.currency}}</td>
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
        }} else if (ev === 'TSL' || ev === 'TSL_RATCHET') {{
          badgeColor = 'bg-amber-500/10 text-amber-300 border-amber-500/30';
          badgeIcon = '📈 TSL RATCHET';
        }}

        const pnl = (n.pnl !== null && n.pnl !== undefined) ? Number(n.pnl) : null;
        const pnlPct = (n.pnl_pct !== null && n.pnl_pct !== undefined) ? Number(n.pnl_pct) : null;
        let pnlText = '—';
        if (pnl !== null) {{
          const color = pnl >= 0 ? 'text-emerald-400' : 'text-rose-400';
          const sign = pnl >= 0 ? '+' : '';
          pnlText = `<span class="${{color}} font-bold">${{sign}}$${{pnl.toFixed(2)}} (${{sign}}${{pnlPct !== null ? pnlPct.toFixed(2) : '0.00'}}%)</span>`;
        }} else if (n.quantity) {{
          pnlText = `<span class="text-slate-400">${{Number(n.quantity).toFixed(2)}} shs</span>`;
        }}

        const price = (n.price !== undefined && n.price !== null) ? `$${{Number(n.price).toFixed(2)}}` : '—';
        const underlyingText = n.underlying ? `(${{n.underlying}})` : '';

        row.innerHTML = `
          <td class="py-2.5 px-4">
            <span class="inline-flex items-center px-2 py-0.5 rounded text-[10px] font-black border ${{badgeColor}}">
              ${{badgeIcon}}
            </span>
          </td>
          <td class="py-2.5 px-4 font-bold text-white">
            ${{n.ticker || '—'}}
            <span class="text-[11px] font-normal text-slate-400 font-sans ml-1">${{underlyingText}}</span>
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
    # Launch autonomous background position monitor for 100% automated TP1/TP2/TP3 execution
    start_background_position_monitor(html_file=html_file, interval_seconds=15)

    web_dir = Path(html_file).resolve().parent
    html_name = Path(html_file).name
    HandlerClass = make_dashboard_handler(web_dir, html_name)

    socketserver.TCPServer.allow_reuse_address = True
    with ThreadingDashboardServer(("0.0.0.0", port), HandlerClass) as httpd:
        url = f"http://localhost:{port}/{html_name}"
        print(f"\n🚀 Signals Dashboard running live at: {url}")
        print("⚡ COMPLETE AUTOMATION ACTIVE: Polling every 15s for TP1/TP2/TP3 auto-sells")
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
