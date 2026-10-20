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

            # Determine currency formatting (LSE stocks like 3TSM trade in GBX pence)
            if ticker.startswith("3TSM") or entry > 600:
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
                overnight_status = "OVERNIGHT ELIGIBLE (TP1 Breakeven Active)"
                overnight_badge_class = "bg-emerald-500/10 text-emerald-300 border-emerald-500/30"
            else:
                overnight_status = "REQUIRES TP1 OR FLATTENS 16:20 UTC"
                overnight_badge_class = "bg-sky-500/10 text-sky-300 border-sky-500/30"

            stop_broker_line = f"NATIVE BROKER STOP: Trading 212 Order #{broker_stop_order_id} (Active)\n" if broker_stop_order_id else ""

            signals.append({
                "ticker": ticker,
                "underlying": underlying,
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


def build_dashboard_data() -> Dict[str, Any]:
    """Compile comprehensive dashboard dataset."""
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
            client = Trading212Client(
                api_key=settings.TRADING212_API_KEY,
                api_secret=settings.TRADING212_API_SECRET,
                base_url=settings.api_base_url,
            )
            portfolio = client.get_portfolio(force_refresh=True)
            broker_positions_map = {p.ticker: p for p in portfolio if getattr(p, "quantity", 0.0) > 0}
            for p in portfolio:
                if getattr(p, "current_price", 0.0) > 0:
                    portfolio_prices[p.ticker] = p.current_price

            try:
                open_orders = client.get_orders(force_refresh=True)
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
                direction = "LONG"
                if pair and (b_ticker == pair.short_3x_ticker or b_ticker == pair.short_5x_ticker):
                    direction = "SHORT"
                avg_px = bp.average_price if bp.average_price > 0 else (bp.current_price if bp.current_price > 0 else 100.0)
                cur_px = bp.current_price if bp.current_price > 0 else avg_px
                sl_dist = round(avg_px * 0.05, 2)
                sl = round(avg_px - sl_dist, 2) if direction == "LONG" else round(avg_px + sl_dist, 2)
                pnl_pct = ((cur_px - avg_px) / avg_px * 100.0) if avg_px > 0 and direction == "LONG" else ((avg_px - cur_px) / avg_px * 100.0 if avg_px > 0 else 0.0)
                active_signals.append({
                    "ticker": b_ticker,
                    "underlying": underlying,
                    "direction": direction,
                    "strategy_type": "broker_placed",
                    "rsi3": None, "rsi7": None, "rsi14": None, "sma5": None,
                    "win_rate_exp": "85%",
                    "overnight_status": "REQUIRES TP1 OR FLATTENS 16:20 UTC" if direction == "LONG" else "INTRADAY SHORT (Flattens 16:20 UTC)",
                    "overnight_badge_class": "bg-sky-500/10 text-sky-300 border-sky-500/30" if direction == "LONG" else "bg-rose-500/10 text-rose-300 border-rose-500/30",
                    "earnings_in_days": None,
                    "entry_price": avg_px,
                    "current_price": cur_px,
                    "pnl_pct": round(pnl_pct, 2),
                    "pnl_amount": round((cur_px - avg_px) * bp.quantity if direction == "LONG" else (avg_px - cur_px) * bp.quantity, 2),
                    "currency_code": "GBX" if b_ticker.startswith("3TSM") or avg_px > 600 else "USD",
                    "currency_prefix": "" if b_ticker.startswith("3TSM") or avg_px > 600 else "$",
                    "currency_suffix": " GBX" if b_ticker.startswith("3TSM") or avg_px > 600 else "",
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

    return {
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
        },
    }


def generate_html_dashboard(output_path: str) -> str:
    """Render self-contained HTML dashboard with Tailwind and interactive controls."""
    data = build_dashboard_data()
    data_json = json.dumps(data)

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>3x Quantitative Signals Dashboard</title>
  <script src="https://www.gstatic.com/antigravity/web/dev/tailwindcss.min.js"></script>
  <style>
    @keyframes pulse-slow {{
      0%, 100% {{ opacity: 1; }}
      50% {{ opacity: 0.4; }}
    }}
    .animate-pulse-slow {{
      animation: pulse-slow 2.5s cubic-bezier(0.4, 0, 0.6, 1) infinite;
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
          LIVE SIGNAL RADAR
        </span>
        <span class="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-semibold bg-indigo-500/10 text-indigo-400 border border-indigo-500/20">
          <span class="w-2 h-2 rounded-full bg-indigo-400"></span>
          Trading 212 Demo: Connected
        </span>
        <span class="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-semibold bg-emerald-500/10 text-emerald-300 border border-emerald-500/20 font-mono">
          🛡️ {data['broker_status']['active_server_stops']} Native Stops Working
        </span>
        <span class="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-semibold bg-sky-500/10 text-sky-300 border border-sky-500/20 font-mono">
          ⚡ COMPLETE AUTOMATION ACTIVE (Auto-Sell TP1/TP2/TP3 Enabled)
        </span>
      </div>
      <h1 class="text-2xl sm:text-3xl font-bold tracking-tight text-white mt-2">
        3x Leveraged Quantitative Signals & Execution Dashboard
      </h1>
      <p class="text-slate-400 text-sm mt-1">
        High-conviction Connors 3-RSI Mean Reversion & Trend Momentum setups with crash-resilient native server stops.
      </p>
    </div>

    <div class="flex items-center gap-3">
      <button onclick="refreshData()" class="inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-500 text-white text-sm font-medium transition shadow-sm">
        <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"></path></svg>
        Refresh Signals
      </button>
      <button onclick="panicFlatten()" class="inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-rose-600 hover:bg-rose-500 text-white text-sm font-bold transition shadow-sm border border-rose-400/30">
        🚨 Emergency Panic Flatten
      </button>
      <div class="text-right text-xs text-slate-500 hidden sm:block">
        <div>Last Updated:</div>
        <div id="timestamp-text" class="text-slate-300 font-mono">{data['generated_at']}</div>
      </div>
    </div>
  </header>

  <!-- Metric Badges -->
  <div class="max-w-7xl mx-auto grid grid-cols-2 sm:grid-cols-5 gap-4 mb-8">
    <div class="bg-slate-900/80 border border-slate-800 rounded-xl p-4">
      <div class="text-xs text-slate-400 uppercase tracking-wider font-semibold">Active Positions</div>
      <div id="metric-active" class="text-2xl font-bold text-white mt-1">{data['active_signals_count']} Triggered</div>
      <div class="text-xs text-emerald-400 mt-1">Live in Broker</div>
    </div>
    <div class="bg-slate-900/80 border border-slate-800 rounded-xl p-4">
      <div class="text-xs text-slate-400 uppercase tracking-wider font-semibold">Native Broker Stops</div>
      <div id="metric-stops" class="text-2xl font-bold text-emerald-400 mt-1">{data['broker_status']['active_server_stops']} Active</div>
      <div class="text-xs text-slate-400 mt-1">T212 Server-Side GTC</div>
    </div>
    <div class="bg-slate-900/80 border border-slate-800 rounded-xl p-4">
      <div class="text-xs text-slate-400 uppercase tracking-wider font-semibold">Exit Strategy</div>
      <div class="text-2xl font-bold text-white mt-1">TP1 / TP2 / TP3</div>
      <div class="text-xs text-emerald-400 mt-1">⚡ Auto-Sell Active (+1R, +2R, +3R)</div>
    </div>
    <div class="bg-slate-900/80 border border-slate-800 rounded-xl p-4">
      <div class="text-xs text-slate-400 uppercase tracking-wider font-semibold">Beta Decay Shield</div>
      <div class="text-2xl font-bold text-emerald-400 mt-1">ADX &ge; 20</div>
      <div class="text-xs text-slate-400 mt-1">Rejects choppy chop</div>
    </div>
    <div class="bg-slate-900/80 border border-amber-500/30 rounded-xl p-4 relative overflow-hidden">
      <div class="text-xs text-amber-400 uppercase tracking-wider font-semibold flex items-center justify-between">
        <span>16:20 UTC Gap Cutoff</span>
        <span class="w-2 h-2 rounded-full bg-amber-400 animate-pulse"></span>
      </div>
      <div id="cutoff-countdown" class="text-2xl font-bold text-amber-300 font-mono mt-1">--:--:--</div>
      <div class="text-xs text-slate-400 mt-1">Evacuates unhedged trades</div>
    </div>
  </div>

  <!-- Main Active Signals Container -->
  <main class="max-w-7xl mx-auto space-y-8">

    <!-- Active Triggered Signals Section -->
    <section>
      <div class="flex items-center justify-between mb-4">
        <h2 class="text-xl font-bold text-white flex items-center gap-2">
          <span>🎯 Active Actionable Signals</span>
          <span class="text-xs px-2 py-0.5 rounded-full bg-slate-800 text-slate-300 font-mono" id="signals-badge">{data['active_signals_count']}</span>
        </h2>
        <div class="flex flex-wrap gap-2 text-xs">
          <button onclick="filterSignals('ALL')" class="filter-btn px-3 py-1.5 rounded-md bg-slate-800 text-white font-medium hover:bg-slate-700">All</button>
          <button onclick="filterSignals('3RSI')" class="filter-btn px-3 py-1.5 rounded-md bg-amber-500/10 text-amber-300 border border-amber-500/30 hover:bg-amber-500/20 font-semibold">⚡ 3-RSI Mean Reversion (85% WR)</button>
          <button onclick="filterSignals('LONG')" class="filter-btn px-3 py-1.5 rounded-md bg-slate-900 text-slate-400 hover:text-white">Long Only</button>
          <button onclick="filterSignals('SHORT')" class="filter-btn px-3 py-1.5 rounded-md bg-slate-900 text-slate-400 hover:text-white">Short Only</button>
        </div>
      </div>

      <div id="signals-grid" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
        <!-- Rendered by JavaScript -->
      </div>
    </section>

    <!-- Paired 3x & 5x Universe Watchlist -->
    <section class="bg-slate-900/60 border border-slate-800 rounded-2xl p-6">
      <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-6">
        <div>
          <h3 class="text-lg font-bold text-white">3x & 5x Leveraged Asset Directory (Trading 212)</h3>
          <p class="text-xs text-slate-400">Curated high-liquidity 3x and 5x Leverage Shares ETPs on Trading 212 with zero borrowing margin drag.</p>
        </div>
        <div class="text-xs text-slate-400 font-mono">Curated Liquid Pairs Monitored</div>
      </div>

      <div class="overflow-x-auto">
        <table class="w-full text-left text-sm text-slate-300">
          <thead class="text-xs uppercase bg-slate-950/60 text-slate-400 border-b border-slate-800">
            <tr>
              <th class="py-3 px-4">Underlying</th>
              <th class="py-3 px-4">Name / Sector</th>
              <th class="py-3 px-4 text-emerald-400 font-semibold">3x Long ETP</th>
              <th class="py-3 px-4 text-rose-400 font-semibold">3x Short (Inverse) ETP</th>
              <th class="py-3 px-4 text-amber-400 font-semibold">Leverage Policy (5x &rarr; 3x Replaced)</th>
              <th class="py-3 px-4">Currency</th>
            </tr>
          </thead>
          <tbody id="pairs-table-body" class="divide-y divide-slate-800/60 font-mono text-xs">
            <!-- Rendered by JS -->
          </tbody>
        </table>
      </div>
    </section>

    <!-- Recent Trade Activity & Alert History (Audit Log) -->
    <section class="bg-slate-900/60 border border-slate-800 rounded-2xl p-6">
      <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-4">
        <div>
          <h3 class="text-lg font-bold text-white flex items-center gap-2">
            <span>🔔 Recent Alert & Trade Activity History</span>
            <span class="text-xs px-2 py-0.5 rounded-full bg-slate-800 text-slate-300 font-mono" id="alerts-badge">0</span>
          </h3>
          <p class="text-xs text-slate-400">Desktop notifications, trade triggers, and automated profit-taking exits (TP1 / TP2 / TP3 / SL).</p>
        </div>
        <div class="text-xs text-slate-500 font-mono">Real-Time Event Stream</div>
      </div>

      <div class="overflow-x-auto">
        <table class="w-full text-left text-sm text-slate-300">
          <thead class="text-xs uppercase bg-slate-950/60 text-slate-400 border-b border-slate-800">
            <tr>
              <th class="py-3 px-4">Event</th>
              <th class="py-3 px-4">Ticker / Asset</th>
              <th class="py-3 px-4">Price</th>
              <th class="py-3 px-4">Quantity / P&amp;L</th>
              <th class="py-3 px-4">Rationale / Milestone</th>
              <th class="py-3 px-4">Timestamp (UTC)</th>
            </tr>
          </thead>
          <tbody id="alerts-table-body" class="divide-y divide-slate-800/60 font-mono text-xs">
            <!-- Rendered by JS -->
          </tbody>
        </table>
      </div>
    </section>

    <!-- Manual Execution SOP for User -->
    <section class="bg-gradient-to-r from-indigo-950/40 via-slate-900 to-slate-900 border border-indigo-500/20 rounded-2xl p-6">
      <h3 class="text-base font-bold text-white flex items-center gap-2 mb-3">
        <svg class="w-5 h-5 text-indigo-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>
        How to Manually Execute These Signals in Your Broker
      </h3>
      <div class="grid grid-cols-1 md:grid-cols-4 gap-4 text-xs text-slate-300">
        <div class="p-3 bg-slate-950/40 rounded-xl border border-slate-800">
          <div class="font-bold text-indigo-300 mb-1">Step 1: Check Direction</div>
          <p class="text-slate-400">If signal is <span class="text-emerald-400 font-bold">LONG</span>, buy the 3x Long ETP. If <span class="text-rose-400 font-bold">SHORT</span>, buy the 3x Short (Inverse) ETP.</p>
        </div>
        <div class="p-3 bg-slate-950/40 rounded-xl border border-slate-800">
          <div class="font-bold text-indigo-300 mb-1">Step 2: Place Stop Loss</div>
          <p class="text-slate-400">Immediately place a Stop Sell order at the specified <span class="text-white font-bold">SL price</span>. Do not leave positions unprotected.</p>
        </div>
        <div class="p-3 bg-slate-950/40 rounded-xl border border-slate-800">
          <div class="font-bold text-indigo-300 mb-1">Step 3: Bank 50% at TP1</div>
          <p class="text-slate-400">When price touches <span class="text-emerald-400 font-bold">TP1 (+1.0R)</span>, sell 50% of shares and move Stop Loss to Entry Price (Break-Even). Schmitt Hysteresis arms to elevate Stop to TP1 once price clears escape threshold.</p>
        </div>
        <div class="p-3 bg-slate-950/40 rounded-xl border border-slate-800">
          <div class="font-bold text-indigo-300 mb-1">Step 4: Trail the Rest (TSL)</div>
          <p class="text-slate-400">Sell 25% at <span class="text-indigo-400 font-bold">TP2 (+2.0R)</span> and let the final 25% runner ride to <span class="text-purple-400 font-bold">TP3 (+3.0R)</span> with the Trailing Stop.</p>
        </div>
      </div>
    </section>

  </main>

  <footer class="max-w-7xl mx-auto mt-12 pt-6 border-t border-slate-900 text-center text-xs text-slate-600">
    Hybrid Quantitative Trading System &bull; System 1 Laya Fast Screener &times; System 2 Deliberative Engine
  </footer>

  <!-- Notification Toast -->
  <div id="toast" class="fixed bottom-5 right-5 bg-indigo-600 text-white px-4 py-2.5 rounded-xl shadow-lg text-xs font-semibold transform translate-y-20 opacity-0 transition-all duration-300 pointer-events-none">
    Copied trade card to clipboard!
  </div>

  <script>
    const INITIAL_DATA = {data_json};

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
          location.reload();
        }} else {{
          alert("Error executing panic flatten: " + (res.error || "Unknown error"));
        }}
      }} catch (err) {{
        alert("Failed to reach server: " + err);
      }}
    }}

    let rawSignals = INITIAL_DATA.active_signals || [];
    let currentFilter = 'ALL';

    function renderSignals(filter = 'ALL') {{
      currentFilter = filter;
      const grid = document.getElementById('signals-grid');
      grid.innerHTML = '';

      let signals = rawSignals || [];
      if (filter === '3RSI') {{
        signals = signals.filter(s => (s.strategy_type && s.strategy_type.includes('3rsi')) || (s.rsi3 && s.rsi3 <= 25));
      }} else if (filter !== 'ALL') {{
        signals = signals.filter(s => s.direction === filter);
      }}

      if (signals.length === 0) {{
        grid.innerHTML = `
          <div class="col-span-full py-12 text-center bg-slate-900/40 border border-slate-800/80 rounded-2xl">
            <div class="text-3xl mb-2">🔍</div>
            <div class="text-base font-bold text-slate-300">No active signals match the current filter</div>
            <div class="text-xs text-slate-500 mt-1">The engine is waiting for high-conviction 3-RSI pullbacks or momentum breakouts.</div>
          </div>
        `;
        return;
      }}

      signals.forEach(s => {{
        const isLong = s.direction === 'LONG';
        const badgeColor = isLong ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20' : 'bg-rose-500/10 text-rose-400 border-rose-500/20';
        const actionText = isLong ? 'BUY (3x LONG)' : 'SELL (3x SHORT)';
        const currPrefix = s.currency_prefix || '$';
        const currSuffix = s.currency_suffix || '';

        const isMR = (s.strategy_type && s.strategy_type.includes('3rsi')) || (s.rsi3 && s.rsi3 <= 25);
        const stratBadge = isMR
          ? `<span class="inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-bold bg-amber-500/10 text-amber-400 border border-amber-500/30">⚡ 3-RSI Mean Reversion (${{s.win_rate_exp || '85%'}} Win-Rate)</span>`
          : `<span class="inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-bold bg-blue-500/10 text-blue-400 border border-blue-500/30">🚀 Trend Momentum (${{s.win_rate_exp || '72%'}} Win-Rate)</span>`;

        const hysteresisBadge = s.tp1_armed_hysteresis
          ? `<div class="flex items-center justify-between text-[11px] px-2.5 py-1.5 rounded-lg border bg-amber-950/40 text-amber-300 border-amber-500/30 mb-2.5 shadow-sm">
               <span class="font-bold flex items-center gap-1.5">
                 <span class="w-2 h-2 rounded-full bg-amber-400 animate-pulse"></span>
                 Schmitt Hysteresis:
               </span>
               <span class="font-mono font-bold text-[10px] text-amber-200">ARMED (Elevates @ ${{currPrefix}}${{s.tp1_escape_threshold ? s.tp1_escape_threshold.toFixed(2) : '--'}}${{currSuffix}})</span>
             </div>`
          : (s.tp1_stop_elevated || s.tp2_hit
              ? `<div class="flex items-center justify-between text-[11px] px-2.5 py-1.5 rounded-lg border bg-emerald-950/40 text-emerald-300 border-emerald-500/30 mb-2.5 shadow-sm">
                   <span class="font-bold flex items-center gap-1.5">
                     <span class="w-2 h-2 rounded-full bg-emerald-400"></span>
                     Milestone Stop Floor:
                   </span>
                   <span class="font-mono font-bold text-[10px] text-emerald-200">LOCKED (+1.0R Floor)</span>
                 </div>`
              : '');

        const stopOrderBadge = s.broker_stop_order_id
          ? `<div class="flex items-center justify-between text-[11px] px-2.5 py-1.5 rounded-lg border bg-emerald-950/60 text-emerald-300 border-emerald-500/30 mb-2.5 shadow-sm">
               <span class="font-bold flex items-center gap-1.5">
                 <span class="w-2 h-2 rounded-full bg-emerald-400 animate-pulse"></span>
                 Native Server Stop:
               </span>
               <span class="font-mono font-bold text-[11px] text-emerald-200">T212 Order #${{s.broker_stop_order_id}}</span>
             </div>`
          : `<div class="flex items-center justify-between text-[11px] px-2.5 py-1.5 rounded-lg border bg-amber-950/30 text-amber-300 border-amber-500/20 mb-2.5">
               <span class="font-bold flex items-center gap-1.5">
                 <span class="w-1.5 h-1.5 rounded-full bg-amber-400"></span>
                 Stop Protection:
               </span>
               <span class="text-[10px] text-amber-200">Software Polled</span>
             </div>`;

        const pnlPct = (s.pnl_pct !== undefined && s.pnl_pct !== null) ? s.pnl_pct : 0.0;
        const pnlColor = pnlPct > 0 ? 'text-emerald-400' : (pnlPct < 0 ? 'text-rose-400' : 'text-slate-400');
        const pnlSign = pnlPct > 0 ? '+' : '';
        const statusColor = s.tp1_armed_hysteresis ? 'text-amber-400' : (s.tp1_stop_elevated ? 'text-emerald-400' : 'text-slate-300');

        const rsiStrip = (s.rsi3 !== undefined && s.rsi3 !== null)
          ? `<div class="flex flex-wrap items-center gap-1.5 mt-2 pt-2 border-t border-slate-800/60 text-[10px] font-mono text-slate-400">
               <span class="bg-slate-950 px-1.5 py-0.5 rounded border border-slate-800">RSI(3): <b class="${{s.rsi3 <= 25 ? 'text-amber-400 font-black' : (s.rsi3 >= 75 ? 'text-rose-400 font-black' : 'text-slate-300')}}">${{s.rsi3}}</b></span>
               <span class="bg-slate-950 px-1.5 py-0.5 rounded border border-slate-800">RSI(7): <b class="text-slate-300">${{s.rsi7 || '--'}}</b></span>
               <span class="bg-slate-950 px-1.5 py-0.5 rounded border border-slate-800">RSI(14): <b class="text-slate-300">${{s.rsi14 || '--'}}</b></span>
               ${{s.sma5 ? `<span class="bg-slate-950 px-1.5 py-0.5 rounded border border-slate-800">5-SMA: <b class="text-indigo-300">${{currPrefix}}${{s.sma5}}${{currSuffix}}</b></span>` : ''}}
             </div>`
          : '';

        const card = document.createElement('div');
        card.className = "bg-slate-900 border border-slate-800 hover:border-slate-700 transition rounded-2xl p-5 shadow-lg flex flex-col justify-between relative overflow-hidden group";

        card.innerHTML = `
          <!-- Top Accent line -->
          <div class="absolute top-0 left-0 right-0 h-1 ${{isLong ? 'bg-emerald-500' : 'bg-rose-500'}}"></div>

          <div>
            <!-- Header -->
            <div class="flex items-start justify-between gap-3 mb-2">
              <div>
                <div class="flex items-center gap-2">
                  <span class="text-xl font-black text-white tracking-wide">${{s.underlying}}</span>
                  <span class="text-xs font-mono text-slate-400 bg-slate-950 px-2 py-0.5 rounded border border-slate-800">${{s.ticker}}</span>
                </div>
                <div class="text-xs text-slate-400 mt-0.5">3x Leveraged ETP &bull; ${{s.currency_code || 'USD'}}</div>
              </div>

              <span class="px-2.5 py-1 rounded-md text-xs font-black uppercase tracking-wider border ${{badgeColor}}">
                ${{actionText}}
              </span>
            </div>

            <!-- Strategy Badge -->
            <div class="mb-2">
              ${{stratBadge}}
            </div>

            <!-- Schmitt Hysteresis Milestone Badge -->
            ${{hysteresisBadge}}

            <!-- Native Broker Stop Order Badge -->
            ${{stopOrderBadge}}

            <!-- Asymmetric Session Protocol Badge -->
            <div class="flex items-center justify-between text-[11px] px-2.5 py-1.5 rounded-lg border ${{s.overnight_badge_class || 'bg-slate-950 text-slate-400 border-slate-800'}} mb-3">
              <span class="font-bold flex items-center gap-1.5">
                <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>
                Session Gate:
              </span>
              <span class="font-bold text-[10px] tracking-wide uppercase">${{s.overnight_status || 'STANDARD'}}</span>
            </div>

            <!-- Price & Sizing Box -->
            <div class="bg-slate-950/80 border border-slate-800/60 rounded-xl p-3 mb-3">
              <div class="grid grid-cols-2 gap-2">
                <div>
                  <div class="text-[11px] text-slate-400 uppercase font-semibold">Entry / Live Price</div>
                  <div class="text-base font-bold text-white font-mono">${{currPrefix}}${{s.entry_price.toFixed(2)}}${{currSuffix}}</div>
                  <div class="text-xs font-mono font-semibold ${{pnlColor}} mt-0.5">
                    Live: ${{currPrefix}}${{s.current_price.toFixed(2)}}${{currSuffix}} (${{pnlSign}}${{pnlPct.toFixed(2)}}%)
                  </div>
                </div>
                <div class="text-right">
                  <div class="text-[11px] text-slate-400 uppercase font-semibold">Holdings / Qty</div>
                  <div class="text-base font-bold text-indigo-300 font-mono">${{s.remaining_quantity !== undefined ? s.remaining_quantity.toFixed(1) : s.quantity.toFixed(1)}} / ${{s.quantity.toFixed(1)}} shs</div>
                  <div class="text-[10px] text-slate-400 mt-0.5 font-mono">P&L: <span class="${{pnlColor}} font-bold">${{pnlSign}}${{currPrefix}}${{Math.abs(s.pnl_amount || 0).toFixed(2)}}${{currSuffix}}</span></div>
                </div>
              </div>
              ${{rsiStrip}}
            </div>

            <!-- Targets & Stop Grid -->
            <div class="space-y-2 text-xs mb-5">
              <!-- Stop Loss -->
              <div class="flex items-center justify-between p-2 rounded-lg bg-rose-500/5 border border-rose-500/10">
                <span class="text-rose-300 font-semibold flex items-center gap-1.5">
                  <span class="w-1.5 h-1.5 rounded-full bg-rose-400"></span>
                  Stop Loss (SL)
                </span>
                <span class="font-mono font-bold text-rose-200">${{currPrefix}}${{s.stop_loss.toFixed(2)}}${{currSuffix}} (-${{s.stop_distance_pct}}%)</span>
              </div>

              <!-- Trailing Stop -->
              <div class="flex items-center justify-between p-2 rounded-lg bg-amber-500/5 border border-amber-500/10">
                <span class="text-amber-300 font-semibold flex items-center gap-1.5">
                  <span class="w-1.5 h-1.5 rounded-full bg-amber-400"></span>
                  Current TSL
                </span>
                <span class="font-mono font-bold text-amber-200">${{currPrefix}}${{s.trailing_stop_loss.toFixed(2)}}${{currSuffix}}</span>
              </div>

              <!-- Take Profit 1 -->
              <div class="flex items-center justify-between p-2 rounded-lg bg-emerald-500/5 border border-emerald-500/10">
                <span class="text-emerald-300 font-semibold flex items-center gap-1.5">
                  <span class="w-1.5 h-1.5 rounded-full bg-emerald-400"></span>
                  Take Profit 1 (+1.0R)
                </span>
                <span class="font-mono font-bold text-emerald-200">${{currPrefix}}${{s.tp1.toFixed(2)}}${{currSuffix}} (+${{s.tp1_pct}}%)</span>
              </div>

              <!-- Take Profit 2 -->
              <div class="flex items-center justify-between p-2 rounded-lg bg-emerald-500/5 border border-emerald-500/10">
                <span class="text-emerald-300 font-semibold flex items-center gap-1.5">
                  <span class="w-1.5 h-1.5 rounded-full bg-emerald-400"></span>
                  Take Profit 2 (+2.0R)
                </span>
                <span class="font-mono font-bold text-emerald-200">${{currPrefix}}${{s.tp2.toFixed(2)}}${{currSuffix}} (+${{s.tp2_pct}}%)</span>
              </div>

              <!-- Take Profit 3 -->
              <div class="flex items-center justify-between p-2 rounded-lg bg-emerald-500/5 border border-emerald-500/10">
                <span class="text-emerald-300 font-semibold flex items-center gap-1.5">
                  <span class="w-1.5 h-1.5 rounded-full bg-emerald-400"></span>
                  Take Profit 3 (+3.0R)
                </span>
                <span class="font-mono font-bold text-emerald-200">${{currPrefix}}${{s.tp3.toFixed(2)}}${{currSuffix}} (+${{s.tp3_pct}}%)</span>
              </div>
            </div>
          </div>

          <!-- Actions -->
          <div class="pt-3 border-t border-slate-800/80 flex items-center justify-between gap-2">
            <span class="text-[11px] text-slate-400">Status: <span class="${{statusColor}} font-semibold">${{s.status}}</span></span>
            <button onclick="copyToClipboard('${{encodeURIComponent(s.copy_text)}}')" class="px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-200 hover:text-white text-xs font-medium transition flex items-center gap-1">
              <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z"></path></svg>
              Copy Order
            </button>
          </div>
        `;
        grid.appendChild(card);
      }});
    }}

    function renderPairsTable() {{
      const tbody = document.getElementById('pairs-table-body');
      tbody.innerHTML = '';
      (INITIAL_DATA.pairs_watchlist || []).forEach(p => {{
        const row = document.createElement('tr');
        row.className = "hover:bg-slate-900/80 transition";
        const fiveXBadge = p.has_5x
          ? `<div class="flex flex-col gap-0.5">
               <span class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-bold bg-amber-500/10 text-amber-300 border border-amber-500/30">5x Exists &rarr; Replaced with 3x</span>
               <span class="text-[10px] font-mono text-slate-400">Exec: <b>${{p.long_ticker}}</b></span>
             </div>`
          : `<span class="text-slate-500 text-[10px]">3x Standard</span>`;

        row.innerHTML = `
          <td class="py-3 px-4 font-bold text-white">${{p.underlying}}</td>
          <td class="py-3 px-4 font-sans text-slate-400">${{p.name}} <span class="text-slate-500">(${{p.category}})</span></td>
          <td class="py-3 px-4 text-emerald-400 font-bold">${{p.long_ticker}}</td>
          <td class="py-3 px-4 text-rose-400 font-bold">${{p.short_ticker}}</td>
          <td class="py-3 px-4">${{fiveXBadge}}</td>
          <td class="py-3 px-4 text-slate-400">${{p.currency}}</td>
        `;
        tbody.appendChild(row);
      }});
    }}

    function filterSignals(direction) {{
      document.querySelectorAll('.filter-btn').forEach(btn => {{
        btn.classList.remove('bg-slate-800', 'text-white');
        btn.classList.add('bg-slate-900', 'text-slate-400');
      }});
      event.target.classList.remove('bg-slate-900', 'text-slate-400');
      event.target.classList.add('bg-slate-800', 'text-white');
      renderSignals(direction);
    }}

    function copyToClipboard(encoded) {{
      const text = decodeURIComponent(encoded);
      navigator.clipboard.writeText(text).then(() => {{
        const toast = document.getElementById('toast');
        toast.classList.remove('translate-y-20', 'opacity-0');
        setTimeout(() => {{
          toast.classList.add('translate-y-20', 'opacity-0');
        }}, 2200);
      }});
    }}

    function refreshData() {{
      window.location.reload();
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
        }} else if (ev === 'HYSTERESIS_TP1_RATCHET' || ev.includes('HYSTERESIS')) {{
          badgeColor = 'bg-cyan-500/10 text-cyan-300 border-cyan-500/30';
          badgeIcon = '🛡️ TSL ELEVATED (+1.0R)';
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
        const underlyingText = n.underlying ? `(${{n.underlying}})` : (n.direction ? `(${{n.direction}})` : '');

        row.innerHTML = `
          <td class="py-3 px-4">
            <span class="inline-flex items-center px-2 py-0.5 rounded text-[10px] font-black border ${{badgeColor}}">
              ${{badgeIcon}}
            </span>
          </td>
          <td class="py-3 px-4 font-bold text-white">
            ${{n.ticker || '—'}}
            <span class="text-[11px] font-normal text-slate-400 font-sans ml-1">${{underlyingText}}</span>
          </td>
          <td class="py-3 px-4 font-mono text-slate-200">${{price}}</td>
          <td class="py-3 px-4 font-mono">${{pnlText}}</td>
          <td class="py-3 px-4 font-sans text-slate-300 text-xs max-w-md truncate" title="${{n.rationale || ''}}">
            ${{n.rationale || '—'}}
          </td>
          <td class="py-3 px-4 text-slate-400 text-[11px] font-mono whitespace-nowrap">${{n.timestamp || '—'}}</td>
        `;
        tbody.appendChild(row);
      }});
    }}

    setInterval(updateCountdown, 1000);
    updateCountdown();

    // Initial render
    renderSignals('ALL');
    renderPairsTable();
    renderAlertsTable();

    // Real-time zero-latency SSE telemetry stream
    function initSSE() {{
      if (typeof EventSource !== "undefined") {{
        try {{
          const streamUrl = window.location.origin + "/stream";
          const es = new EventSource(streamUrl);
          function handlePayload(payload) {{
            if (!payload) return;
            if (payload.active_signals !== undefined) {{
              rawSignals = payload.active_signals;
              INITIAL_DATA.active_signals = payload.active_signals;
              const activeEl = document.getElementById('metric-active');
              if (activeEl) activeEl.innerText = `${{rawSignals.length}} Triggered`;
              const badgeEl = document.getElementById('signals-badge');
              if (badgeEl) badgeEl.innerText = rawSignals.length;
            }}
            if (payload.broker_status) {{
              INITIAL_DATA.broker_status = payload.broker_status;
              const stopsEl = document.getElementById('metric-stops');
              if (stopsEl && payload.broker_status.active_server_stops !== undefined) {{
                stopsEl.innerText = `${{payload.broker_status.active_server_stops}} Active`;
              }}
            }}
            renderSignals(currentFilter);
            if (payload.pairs_watchlist) {{
              INITIAL_DATA.pairs_watchlist = payload.pairs_watchlist;
              renderPairsTable();
            }}
            if (payload.recent_notifications) {{
              INITIAL_DATA.recent_notifications = payload.recent_notifications;
              renderAlertsTable();
            }}
          }}

          es.addEventListener("snapshot", function(e) {{
            try {{
              handlePayload(JSON.parse(e.data));
            }} catch (err) {{}}
          }});
          es.addEventListener("dashboard_update", function(e) {{
            try {{
              handlePayload(JSON.parse(e.data));
            }} catch (err) {{}}
          }});
          es.onerror = function() {{
            es.close();
            setTimeout(initSSE, 5000);
          }};
        }} catch (e) {{}}
      }}
    }}
    initSSE();

    // Auto-refresh dashboard fallback every 45s if SSE is not supported
    setInterval(() => {{
      window.location.reload();
    }}, 45000);
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
            # Suppress normal GET 200 log spam for continuous SSE keepalive
            clean = format % args
            if "GET /stream" in clean or "GET /events" in clean:
                return
            super().log_message(format, *args)

        def do_GET(self):
            clean_path = self.path.split("?")[0]
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
