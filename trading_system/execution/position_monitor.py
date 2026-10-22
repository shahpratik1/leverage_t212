"""Active Position & Lifecycle Monitor for 3x Leveraged Trades.

Monitors open positions in real time, manages multi-stage exits (TP1, TP2, TP3),
ratchets dynamic Trailing Stop Losses (TSL), triggers auto-executions on Trading 212,
and dispatches real-time alerts.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from trading_system.agents.risk_manager import DecisionCard, RiskManager
from trading_system.config import settings
from trading_system.data.trading212_client import Trading212Client, Trading212Error
from trading_system.notifications.notifier import Notifier

logger = logging.getLogger(__name__)


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

    def __post_init__(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        if not self.entry_time:
            self.entry_time = now
        if not self.last_update_time:
            self.last_update_time = now
        if self.highest_price <= 0:
            self.highest_price = self.entry_price
        if self.lowest_price <= 0:
            self.lowest_price = self.entry_price


class PositionMonitor:
    """Manages active trades, checks exit targets, updates TSL, and fires notifications."""

    def __init__(
        self,
        t212_client: Trading212Client,
        notifier: Notifier,
        risk_manager: RiskManager,
        state_file: str = "active_trades.json",
        dry_run: bool = False,
    ):
        self.t212 = t212_client
        self.notifier = notifier
        self.risk_manager = risk_manager
        self.state_file = state_file
        self.dry_run = dry_run
        self.positions: Dict[str, TrackedPosition] = self._load_state()
        self._last_session_close_date: str = ""
        self.is_halted: bool = False
        if os.path.exists(self.halt_marker_file):
            self.is_halted = True

    @property
    def halt_marker_file(self) -> str:
        return f"{self.state_file}.halted"

    def is_system_halted(self) -> bool:
        """Check whether system is in emergency halt state (in-memory or persistent marker)."""
        if self.is_halted:
            return True
        if os.path.exists(self.halt_marker_file):
            self.is_halted = True
            return True
        return False

    def resume_trading(self) -> None:
        """Clear emergency halt state and delete persistent halt marker file."""
        self.is_halted = False
        if os.path.exists(self.halt_marker_file):
            try:
                os.remove(self.halt_marker_file)
            except Exception as exc:
                logger.warning("Could not remove halt marker file: %s", exc)

    # ─────────────────────────────────────────────────────────────
    # Trade Registration
    # ─────────────────────────────────────────────────────────────
    def register_new_trade(
        self,
        card: DecisionCard,
        atr: float = 0.0,
        earnings_in_days: Optional[float] = None,
        fill_price: Optional[float] = None,
    ) -> TrackedPosition:
        """Register a newly opened trade for active lifecycle tracking."""
        # On Trading 212 Invest/Stocks, all leveraged ETPs (both 3x Long and 3x Inverse Short)
        # are spot instruments bought long; an increasing ETP price represents profit.
        is_cfd_short = (
            card.direction.upper() == "SHORT"
            and not (
                card.trading212_ticker.endswith("_EQ")
                or card.trading212_ticker.endswith(".L")
                or card.direction.upper().endswith("_ETP")
            )
        )

        # Derive relative price proportions from DecisionCard
        stop_dist_pct = abs(card.entry_price - card.stop_loss) / card.entry_price if card.entry_price > 0 else 0.05
        tp1_dist_pct = abs(card.take_profit_1 - card.entry_price) / card.entry_price if card.entry_price > 0 else 0.05
        tp2_dist_pct = abs(card.take_profit_2 - card.entry_price) / card.entry_price if card.entry_price > 0 else 0.10
        tp3_dist_pct = abs(card.take_profit_3 - card.entry_price) / card.entry_price if card.entry_price > 0 else 0.15
        atr_pct = (atr / card.entry_price) if (atr > 0 and card.entry_price > 0) else 0.02

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
        )
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
    def place_broker_stop(self, pos: TrackedPosition, stop_price: Optional[float] = None) -> Optional[str]:
        """Place or replace a native GTC Stop Order on Trading 212 broker server."""
        price = stop_price if stop_price is not None else pos.trailing_stop_loss
        if pos.remaining_quantity <= 0 or price <= 0:
            return None

        # Cancel previous broker stop order if one was already placed
        if pos.broker_stop_order_id:
            self.cancel_broker_stop(pos)

        if self.dry_run:
            fake_id = f"DRY_STOP_{pos.ticker}_{int(time.time())}"
            pos.broker_stop_order_id = fake_id
            self._save_state()
            logger.info("[DRY-RUN] Would place native broker Stop Order for %s: %.3f shares @ $%.2f",
                        pos.ticker, pos.remaining_quantity, price)
            return fake_id

        try:
            # In Trading 212 Invest/Stocks API:
            # Protective stops for cash equity and 3x/5x leveraged ETPs sell held shares (negative quantity)
            order_qty = -abs(pos.remaining_quantity)
            logger.info(
                "Placing native Stop Order on Trading 212 for %s: %.3f shares @ stopPrice $%.2f",
                pos.ticker, order_qty, price
            )
            # Try GOOD_TILL_CANCEL first (standard for persistent protective stops), fallback to DAY
            try:
                res = self.t212.place_stop_order(
                    ticker=pos.ticker,
                    quantity=order_qty,
                    stop_price=round(price, 2),
                    time_validity="GOOD_TILL_CANCEL",
                )
            except Exception as first_exc:
                logger.debug("Stop order with validity GOOD_TILL_CANCEL failed (%s), attempting DAY...", first_exc)
                # Respect rate limit spacing before fallback order submission
                delay = getattr(self.t212, "rate_limit_delay", 1.25)
                time.sleep(delay)
                res = self.t212.place_stop_order(
                    ticker=pos.ticker,
                    quantity=order_qty,
                    stop_price=round(price, 2),
                    time_validity="DAY",
                )
            pos.broker_stop_order_id = res.id
            setattr(pos, "_last_stop_fail_time", 0.0)
            self._save_state()
            logger.info("Successfully placed broker Stop Order on Trading 212: %s (id=%s)", pos.ticker, res.id)
            return res.id
        except Exception as exc:
            setattr(pos, "_last_stop_fail_time", time.time())
            logger.error("Failed to place native broker Stop Order for %s on Trading 212: %s", pos.ticker, exc)
            # Detect stale stop-price rejection (HTTP 400 / "price too far") and self-heal
            err_str = str(exc).lower()
            if "400" in err_str or "price" in err_str or "far" in err_str or "invalid" in err_str:
                logger.warning(
                    "Detected possible stale stop price for %s — attempting self-heal", pos.ticker
                )
                self._heal_stale_stop(pos)
            return None

    def _heal_stale_stop(self, pos: TrackedPosition) -> None:
        """Re-fetch live market price from broker and recompute a fresh ATR-based stop loss.

        Called automatically when a stop-order placement gets a 400 price-too-far rejection.
        Updates `pos.stop_loss` and `pos.trailing_stop_loss` in-place and persists state.
        A subsequent monitor cycle will re-attempt stop placement with the healed price.
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
            # Use stored ATR percentage if available; default to 2% (conservative for 3x ETPs)
            atr_pct = getattr(pos, "_last_atr_pct", None)
            if not atr_pct or atr_pct <= 0:
                atr_pct = 0.02
            # Compute fresh stop: 2.5× ATR away from current price in the protective direction
            if pos.direction == "LONG":
                new_sl = round(live_price * (1.0 - atr_pct * 2.5), 4)
            else:
                new_sl = round(live_price * (1.0 + atr_pct * 2.5), 4)
            logger.warning(
                "_heal_stale_stop %s: old_sl=%.4f → new_sl=%.4f (live_price=%.4f, atr_pct=%.4f)",
                pos.ticker, pos.stop_loss, new_sl, live_price, atr_pct,
            )
            pos.stop_loss = new_sl
            pos.trailing_stop_loss = new_sl
            # Reset fail timer so next monitor cycle retries immediately
            setattr(pos, "_last_stop_fail_time", 0.0)
            self._save_state()
        except Exception as heal_exc:
            logger.debug("_heal_stale_stop failed for %s: %s", pos.ticker, heal_exc)

    def cancel_broker_stop(self, pos: TrackedPosition) -> bool:
        """Cancel pending broker stop order on Trading 212."""
        order_id = pos.broker_stop_order_id
        if not order_id:
            return True

        if self.dry_run or order_id.startswith("DRY_"):
            pos.broker_stop_order_id = None
            self._save_state()
            logger.info("[DRY-RUN] Cancelled broker stop order %s for %s", order_id, pos.ticker)
            return True

        try:
            logger.info("Canceling broker Stop Order %s for %s on Trading 212", order_id, pos.ticker)
            success = self.t212.cancel_order(order_id)
            if success:
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
        price = limit_price if limit_price is not None else pos.tp1
        qty = quantity if quantity is not None else round(pos.remaining_quantity * 0.5, 4)
        if qty <= 0 or price <= 0:
            return None

        # Cancel previous broker TP limit order if one exists
        if pos.broker_tp_order_id:
            self.cancel_broker_tp_limit(pos)

        if self.dry_run:
            fake_id = f"DRY_LIMIT_{pos.ticker}_{int(time.time())}"
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
                delay = getattr(self.t212, "rate_limit_delay", 1.25)
                time.sleep(delay)
                res = self.t212.place_limit_order(
                    ticker=pos.ticker,
                    quantity=order_qty,
                    limit_price=round(price, 2),
                    time_validity="DAY",
                )
            pos.broker_tp_order_id = res.id
            self._save_state()
            logger.info("Successfully placed broker Limit Order on Trading 212: %s (id=%s)", pos.ticker, res.id)
            return res.id
        except Exception as exc:
            logger.debug("Placing native broker Limit Order for %s deferred: %s", pos.ticker, exc)
            return None

    def cancel_broker_tp_limit(self, pos: TrackedPosition) -> bool:
        """Cancel pending broker TP limit order on Trading 212."""
        order_id = pos.broker_tp_order_id
        if not order_id:
            return True

        if self.dry_run or order_id.startswith("DRY_"):
            pos.broker_tp_order_id = None
            self._save_state()
            logger.info("[DRY-RUN] Cancelled broker limit order %s for %s", order_id, pos.ticker)
            return True

        try:
            logger.info("Canceling broker Limit Order %s for %s on Trading 212", order_id, pos.ticker)
            success = self.t212.cancel_order(order_id)
            if success:
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
        synced = []
        now = time.time()
        for ticker, pos in list(self.positions.items()):
            if not pos.is_closed and pos.remaining_quantity > 0 and not pos.broker_stop_order_id:
                last_fail = getattr(pos, "_last_stop_fail_time", 0.0)
                if now - last_fail < 180.0:
                    logger.debug("Cooldown active for %s stop sync (failed %.1fs ago)", ticker, now - last_fail)
                    continue

                logger.info("Syncing missing broker Stop Order for active position %s...", ticker)
                order_id = self.place_broker_stop(pos)
                if order_id:
                    synced.append(ticker)
                delay = getattr(self.t212, "rate_limit_delay", 1.25)
                time.sleep(delay)
        if synced:
            self._save_state()
            logger.info("Synced native broker stops for %d positions: %s", len(synced), synced)
        return synced

    def apply_daily_rollover_stops(self) -> List[Dict[str, Any]]:
        """Apply new-day session stop loss elevation to permanently lock in prior-day gains.

        Rule: For any active position sitting in profit from yesterday (peak > entry),
        ratchet the stop loss to at least Entry + 50% of the peak unrealized gain,
        or Chandelier ATR trail (Peak - 1.5 * ATR), whichever is higher.
        """
        results = []
        for ticker, pos in list(self.positions.items()):
            if pos.is_closed or pos.remaining_quantity <= 0 or pos.entry_price <= 0:
                continue

            is_short = (pos.direction.upper() == "SHORT" and not pos.direction.upper().endswith("_ETP"))
            peak = pos.lowest_price if is_short else pos.highest_price

            # Check if position experienced positive gain
            has_gain = (peak < pos.entry_price) if is_short else (peak > pos.entry_price)
            if not has_gain:
                continue

            gain_amt = (pos.entry_price - peak) if is_short else (peak - pos.entry_price)
            # 50% profit lock
            target_50pct = (pos.entry_price - 0.50 * gain_amt) if is_short else (pos.entry_price + 0.50 * gain_amt)

            # Chandelier ATR trail
            atr_trail_dist = max(pos.atr * 1.5, peak * 0.015) if pos.atr > 0 else (peak * 0.02)
            target_chandelier = (peak + atr_trail_dist) if is_short else (peak - atr_trail_dist)

            # Choose the tighter of the two (higher for long, lower for short)
            recommended_sl = min(target_50pct, target_chandelier) if is_short else max(target_50pct, target_chandelier)
            recommended_sl = round(recommended_sl, 2)

            # Check if this ratchets tighter than current stop
            should_ratchet = (recommended_sl < pos.trailing_stop_loss) if is_short else (recommended_sl > pos.trailing_stop_loss)
            if should_ratchet:
                old_sl = pos.trailing_stop_loss
                pos.trailing_stop_loss = recommended_sl
                pos.stop_loss = recommended_sl
                # If TP1 level is reached or surpassed, ensure TP1 state aligns
                if (recommended_sl >= pos.tp1 if not is_short else recommended_sl <= pos.tp1):
                    pos.tp1_armed_hysteresis = False
                    pos.tp1_stop_elevated = True

                locked_pct = abs((recommended_sl - pos.entry_price) / pos.entry_price) * 100.0 if pos.entry_price > 0 else 0.0
                logger.info(
                    "🌅 Daily Session Rollover: %s stop ratcheted $%.2f -> $%.2f (Peak: $%.2f, +%.2f%% locked)",
                    ticker, old_sl, recommended_sl, peak, locked_pct,
                )
                self.place_broker_stop(pos, stop_price=recommended_sl)
                if hasattr(self.notifier, "notify_tsl"):
                    try:
                        self.notifier.notify_tsl(
                            ticker=pos.ticker,
                            underlying=pos.underlying,
                            direction=pos.direction,
                            current_price=pos.current_price or recommended_sl,
                            new_tsl=recommended_sl,
                            locked_in_pnl_pct=locked_pct,
                            event_type="DAILY_ROLLOVER_RATCHET",
                        )
                    except Exception as notif_exc:
                        logger.debug("Rollover notification notice: %s", notif_exc)

                results.append({
                    "ticker": ticker,
                    "old_sl": old_sl,
                    "new_sl": recommended_sl,
                    "peak": peak,
                    "locked_pct": round(locked_pct, 2),
                })
        if results:
            self._save_state()
        return results

    def reconcile_with_broker(self, adopt_untracked: bool = True) -> Dict[str, Any]:
        """Reconcile local active positions with actual broker portfolio.

        Detects:
        - Positions closed externally (stopped out via native stop, limit filled, or manual app close)
        - Partial quantity mismatches and external trims
        - Untracked broker positions (with automatic adoption)
        """
        summary: Dict[str, Any] = {
            "closed_externally": [],
            "quantity_synced": [],
            "untracked_broker_positions": [],
            "adopted_from_broker": [],
            "expired_stops_restored": [],
        }

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
                open_orders = self.t212.get_orders(force_refresh=True)
                for o in open_orders:
                    o_ticker = o.get("ticker") if isinstance(o, dict) else getattr(o, "ticker", "")
                    o_type = str(o.get("type") if isinstance(o, dict) else getattr(o, "type", "")).upper()
                    if "STOP" in o_type:
                        stop_orders_by_ticker[o_ticker] = o
                    elif "LIMIT" in o_type:
                        limit_orders_by_ticker[o_ticker] = o
        except Exception as ord_exc:
            logger.debug("Reconciliation open orders fetch notice: %s", ord_exc)

        # 1. Check local positions against broker
        for ticker, pos in list(self.positions.items()):
            if pos.is_closed:
                continue

            if ticker not in broker_map:
                # In live mode, if position is missing from broker, mark closed externally
                if not self.dry_run:
                    logger.warning(
                        "Reconciliation: %s not found in broker portfolio! Marked closed externally.",
                        ticker,
                    )
                    pos.is_closed = True
                    pos.remaining_quantity = 0.0
                    pos.last_update_time = datetime.now(timezone.utc).isoformat()
                    self.cancel_all_broker_orders(pos)
                    summary["closed_externally"].append(ticker)

                    # Dispatch notification
                    if hasattr(self.notifier, "notify_stop_loss"):
                        try:
                            self.notifier.notify_stop_loss(
                                ticker=pos.ticker,
                                underlying=pos.underlying,
                                price=pos.stop_loss,
                                quantity=pos.original_quantity,
                                realized_pnl=0.0,
                                realized_pnl_pct=0.0,
                                is_tsl=False,
                                details="Reconciliation: Position closed on broker (Native stop hit, limit filled, or manual close)",
                            )
                        except Exception as notif_exc:
                            logger.debug("Reconciliation exit notification failed: %s", notif_exc)
            else:
                bp = broker_map[ticker]
                cur_px = bp.current_price if (bp.current_price or 0.0) > 0 else (bp.average_price or 0.0)
                if cur_px and cur_px > 0:
                    pos.current_price = cur_px
                    pos.highest_price = max(pos.highest_price or cur_px, cur_px)
                    pos.lowest_price = min(pos.lowest_price or cur_px, cur_px)

                # Detect and fix entry-price currency scale mismatch (e.g. GBX pence stored vs EUR)
                # If entry_price and broker average_price differ by more than 5× either way, trust broker.
                broker_avg = getattr(bp, "average_price", 0.0) or 0.0
                if broker_avg > 0 and pos.entry_price > 0:
                    ratio = pos.entry_price / broker_avg
                    if ratio > 5.0 or ratio < 0.20:
                        logger.warning(
                            "Entry price scale mismatch for %s: stored=%.4f broker_avg=%.4f (ratio=%.1f×). "
                            "Correcting to broker average price.",
                            ticker, pos.entry_price, broker_avg, ratio,
                        )
                        pos.entry_price = broker_avg
                        self._save_state()

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
                    # Broker has NO active stop order for this open position (it expired overnight or was cancelled)
                    if pos.broker_stop_order_id and not pos.broker_stop_order_id.startswith("DRY_"):
                        logger.warning(
                            "Reconciliation: Native broker stop order %s for %s has EXPIRED or was cancelled on broker! Auto-healing...",
                            pos.broker_stop_order_id, ticker,
                        )
                        pos.broker_stop_order_id = None
                        summary["expired_stops_restored"].append(ticker)
                        # Re-place immediately with GOOD_TILL_CANCEL
                        self.place_broker_stop(pos)

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
                        self.cancel_all_broker_orders(pos)

        # 2. Check for untracked positions on broker and adopt them
        for b_ticker, bp in broker_map.items():
            if b_ticker not in self.positions or self.positions[b_ticker].is_closed:
                summary["untracked_broker_positions"].append((b_ticker, bp.quantity))
                if adopt_untracked:
                    from trading_system.data.leveraged_universe import LeveragedUniverse
                    univ = LeveragedUniverse()
                    pair = univ.get_pair_for_etp(b_ticker)
                    underlying = pair.underlying_symbol if pair else b_ticker.replace("l_EQ", "").replace("_US_EQ", "")
                    direction = "LONG"
                    if pair and (b_ticker == pair.short_3x_ticker or b_ticker == pair.short_5x_ticker):
                        direction = "SHORT"

                    avg_px = bp.average_price if bp.average_price > 0 else (bp.current_price if bp.current_price > 0 else 100.0)
                    cur_px = bp.current_price if bp.current_price > 0 else avg_px

                    # Check if broker already has a live pending stop order for this position
                    so = stop_orders_by_ticker.get(b_ticker)
                    so_id = str(so.get("id") if isinstance(so, dict) else getattr(so, "id", "")) if so else None
                    so_price = float(so.get("stopPrice", 0.0) if isinstance(so, dict) else getattr(so, "stop_price", 0.0)) if so else 0.0

                    if so_price > 0:
                        sl = round(so_price, 2)
                        sl_dist = abs(avg_px - sl)
                    else:
                        sl_dist = round(avg_px * 0.05, 2)
                        sl = round(avg_px - sl_dist, 2) if direction == "LONG" else round(avg_px + sl_dist, 2)

                    tp1 = round(avg_px + sl_dist, 2) if direction == "LONG" else round(avg_px - sl_dist, 2)
                    tp2 = round(avg_px + 2 * sl_dist, 2) if direction == "LONG" else round(avg_px - 2 * sl_dist, 2)
                    tp3 = round(avg_px + 3 * sl_dist, 2) if direction == "LONG" else round(avg_px - 3 * sl_dist, 2)

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
                        stop_loss=sl,
                        trailing_stop_loss=sl,
                        highest_price=max(avg_px, cur_px),
                        lowest_price=min(avg_px, cur_px),
                        tp1=tp1,
                        tp2=tp2,
                        tp3=tp3,
                        atr=round(avg_px * 0.025, 2),
                        strategy_type="broker_placed",
                        broker_stop_order_id=broker_stop_id,
                        broker_tp_order_id=broker_tp_id,
                        scale_calibrated=True,
                        current_price=cur_px,
                    )
                    self.positions[b_ticker] = adopted_pos
                    summary["adopted_from_broker"].append(b_ticker)
                    logger.info(
                        "Reconciliation: Adopted broker-placed position %s (%s, %.3f shares @ $%.2f, SL: $%.2f, StopID: %s)",
                        b_ticker, underlying, bp.quantity, avg_px, sl, broker_stop_id,
                    )

        if (
            summary["closed_externally"]
            or summary["quantity_synced"]
            or summary["adopted_from_broker"]
            or summary.get("expired_stops_restored")
        ):
            self._save_state()

        return summary

    def panic_flatten_all(self) -> Dict[str, Any]:
        """Emergency fail-safe: cancel all broker orders and liquidate all open positions."""
        logger.critical("🚨 EMERGENCY PANIC FLATTEN TRIGGERED! Liquidating all positions and cancelling orders.")
        results: Dict[str, Any] = {
            "cancelled_stops": 0,
            "liquidated_positions": [],
            "failed_liquidations": [],
        }
        self.is_halted = True
        try:
            with open(self.halt_marker_file, "w", encoding="utf-8") as f:
                json.dump({
                    "halted": True,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "reason": "Emergency Panic Flatten"
                }, f, indent=2)
        except Exception as exc:
            logger.warning("Could not write persistent halt marker to %s: %s", self.halt_marker_file, exc)

        for ticker, pos in list(self.positions.items()):
            if pos.is_closed:
                continue

            # 1. Cancel all orders on broker
            try:
                self.cancel_all_broker_orders(pos)
                results["cancelled_stops"] += 1
            except Exception as exc:
                logger.error("Failed to cancel orders for %s during panic flatten: %s", ticker, exc)

            # 2. Sell / liquidate remaining quantity
            if pos.remaining_quantity > 0 and not self.dry_run:
                try:
                    success = self._execute_sell(pos.ticker, pos.remaining_quantity)
                    if success:
                        results["liquidated_positions"].append((ticker, pos.remaining_quantity))
                    else:
                        results["failed_liquidations"].append((ticker, pos.remaining_quantity))
                except Exception as exc:
                    logger.error("Failed to liquidate %s during panic flatten: %s", ticker, exc)
                    results["failed_liquidations"].append((ticker, pos.remaining_quantity))
            else:
                results["liquidated_positions"].append((ticker, pos.remaining_quantity))

            pos.is_closed = True
            pos.remaining_quantity = 0.0
            pos.last_update_time = datetime.now(timezone.utc).isoformat()

        self._save_state()

        # Notify via all channels
        if hasattr(self.notifier, "notify_alert"):
            try:
                self.notifier.notify_alert(
                    "🚨 EMERGENCY PANIC FLATTEN EXECUTED",
                    f"Liquidated: {len(results['liquidated_positions'])} positions, Cancelled stops: {results['cancelled_stops']}. System halted.",
                )
            except Exception as notif_exc:
                logger.debug("Panic flatten notification skipped: %s", notif_exc)

        return results

    # ─────────────────────────────────────────────────────────────
    # Asymmetric Session Protocol (16:20 UTC Gap Evacuation & Quota Management)
    # ─────────────────────────────────────────────────────────────
    def evaluate_asymmetric_session(
        self,
        current_time: Optional[datetime] = None,
        force_evaluation: bool = False,
        current_prices: Optional[Dict[str, float]] = None,
    ) -> List[Dict[str, Any]]:
        """Evaluate 16:20 UTC Asymmetric Session rules:
        - 3x Short/Inverse positions: Unconditionally liquidated (INVERSE_INTRADAY_FLATTEN).
        - 3x Long positions:
            * If not tp1_hit (stop not at breakeven): Liquidated (ASYMMETRIC_SESSION_EXIT).
            * If tp1_hit but earnings <= 24h: Liquidated (EARNINGS_BLACKOUT_EXIT).
            * If tp1_hit and no earnings: Retained overnight (is_overnight_eligible = True).
        """
        now_utc = current_time or datetime.now(timezone.utc)
        today_str = now_utc.strftime("%Y-%m-%d")

        if not force_evaluation:
            try:
                cutoff_parts = settings.SESSION_CLOSE_CUTOFF_UTC.split(":")
                cutoff_h, cutoff_m = int(cutoff_parts[0]), int(cutoff_parts[1])
            except Exception:
                cutoff_h, cutoff_m = 16, 20

            current_minutes = now_utc.hour * 60 + now_utc.minute
            cutoff_minutes = cutoff_h * 60 + cutoff_m

            # Active within 10-minute window: 16:20 UTC to 16:30 UTC
            in_window = cutoff_minutes <= current_minutes < (cutoff_minutes + 10)
            if not in_window:
                return []

            if self._last_session_close_date == today_str:
                return []

        logger.info(
            "Evaluating Asymmetric Session Protocol at %s UTC (Cutoff: %s UTC)...",
            now_utc.strftime("%H:%M:%S"), settings.SESSION_CLOSE_CUTOFF_UTC
        )

        prices = current_prices or {}
        positions_to_close: List[Tuple[TrackedPosition, str, float]] = []
        tickers_to_remove: List[str] = []

        for ticker, pos in list(self.positions.items()):
            if pos.is_closed or pos.remaining_quantity <= 0:
                tickers_to_remove.append(ticker)
                continue

            current_price = prices.get(ticker) or prices.get(pos.underlying) or pos.entry_price

            if pos.direction.upper() == "SHORT":
                if settings.ENFORCE_INVERSE_INTRADAY_ONLY:
                    positions_to_close.append((pos, "INVERSE_INTRADAY_FLATTEN", current_price))
                    tickers_to_remove.append(ticker)
            elif pos.direction.upper() == "LONG":
                if settings.ENFORCE_LONG_BREAKEVEN_OVERNIGHT:
                    if not pos.tp1_hit:
                        positions_to_close.append((pos, "ASYMMETRIC_SESSION_EXIT", current_price))
                        tickers_to_remove.append(ticker)
                    elif (
                        pos.earnings_in_days is not None
                        and isinstance(pos.earnings_in_days, (int, float))
                        and 0 <= pos.earnings_in_days <= (settings.EARNINGS_BLACKOUT_HOURS / 24.0)
                    ):
                        positions_to_close.append((pos, "EARNINGS_BLACKOUT_EXIT", current_price))
                        tickers_to_remove.append(ticker)
                    else:
                        pos.is_overnight_eligible = True
                        logger.info("Retaining Long %s for overnight swing (TP1 hit, Breakeven Stop active)", ticker)

        events: List[Dict[str, Any]] = []
        if positions_to_close:
            logger.info("Queued %d positions for rate-limited 16:20 session liquidation", len(positions_to_close))
            events = self._execute_liquidation_queue(positions_to_close)

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
            shares_to_sell = pos.remaining_quantity
            logger.info(
                "[%d/%d] Executing 16:20 session liquidation for %s (%s): %.3f shares (Reason: %s)",
                i + 1, total, pos.ticker, pos.direction, shares_to_sell, reason
            )

            # Cancel native broker stop order to prevent orphan execution
            self.cancel_broker_stop(pos)

            success = self._execute_sell(pos.ticker, shares_to_sell)

            pos.is_closed = True
            pos.remaining_quantity = 0.0
            pos.last_update_time = datetime.now(timezone.utc).isoformat()

            pnl = shares_to_sell * (price - pos.entry_price)
            pnl_pct = ((price - pos.entry_price) / pos.entry_price) * 100 if pos.entry_price > 0 else 0.0

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

            # Respect rate limit quota if more orders remain in queue
            if i < total - 1:
                time.sleep(settings.RATE_LIMIT_INTER_CALL_DELAY)

        return events

    # ─────────────────────────────────────────────────────────────
    # Inspection & Lifecycle Update Tick
    # ─────────────────────────────────────────────────────────────
    def monitor_tick(
        self,
        current_prices: Dict[str, float],
        current_time: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """Run one check cycle across all active positions using latest prices.

        Args:
            current_prices: Dictionary mapping ticker to latest market price.
            current_time: Optional UTC datetime override (useful for testing session cutoff).

        Returns:
            List of event summaries generated during this tick.
        """
        if getattr(self, "is_halted", False):
            return []

        events = []

        # 1. Asymmetric Session Protocol Check (16:20 UTC cutoff)
        session_events = self.evaluate_asymmetric_session(
            current_time=current_time,
            current_prices=current_prices,
        )
        events.extend(session_events)

        tickers_to_remove = []

        for ticker, pos in list(self.positions.items()):
            if pos.is_closed:
                tickers_to_remove.append(ticker)
                continue

            current_price = current_prices.get(ticker)
            if not current_price or current_price <= 0:
                # If direct price not available, try underlying
                current_price = current_prices.get(pos.underlying)

            if not current_price or current_price <= 0:
                continue

            # ── 1. GBX (Pence) vs Base Currency Normalization ──
            # Many LSE instruments quote in GBX (pence), e.g. 9173 pence vs £91.73 entry
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
                self.place_broker_stop(pos)
                self._save_state()

            # ── 2. Scale Discrepancy Calibration (Underlying vs ETP) ──
            # If price differs by > 30% from registered entry on initial tick, this is an underlying-vs-ETP scale mismatch.
            # Recalibrate levels to actual live price instead of triggering a false stop liquidation.
            # GUARD: Only runs on initial tick check (scale_calibrated guard) to avoid misinterpreting flash crashes.
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
                        self.place_broker_stop(pos)
                        self._save_state()

            pos.last_update_time = datetime.now(timezone.utc).isoformat()

            # Track peak price
            if current_price > pos.highest_price:
                pos.highest_price = current_price
            if current_price < pos.lowest_price:
                pos.lowest_price = current_price

            # Check exits in priority order
            is_short = (pos.tp1 < pos.entry_price) if (pos.tp1 > 0 and pos.entry_price > 0) else (
                pos.direction.upper() == "SHORT" and not (
                    pos.ticker.endswith("_EQ") or pos.ticker.endswith(".L") or pos.direction.upper().endswith("_ETP")
                )
            )

            # 1. Stop Loss & Trailing Stop Loss
            if is_short:
                active_stop = min(pos.stop_loss, pos.trailing_stop_loss) if pos.trailing_stop_loss > 0 else pos.stop_loss
                stop_hit = current_price >= active_stop
            else:
                active_stop = max(pos.stop_loss, pos.trailing_stop_loss)
                stop_hit = current_price <= active_stop

            if stop_hit:
                event = self._handle_stop_exit(pos, current_price, is_tsl=(active_stop == pos.trailing_stop_loss and pos.tp1_hit))
                events.append(event)
                tickers_to_remove.append(ticker)
                continue

            # 2. Take Profit 3 (Final target - Full close)
            tp3_hit = (current_price <= pos.tp3) if is_short else (current_price >= pos.tp3)
            if tp3_hit and not pos.tp3_hit:
                event = self._handle_tp3_hit(pos, current_price)
                events.append(event)
                tickers_to_remove.append(ticker)
                continue

            # 3. Take Profit 1 (First trim - 50% + Breakeven SL + Arm Hysteresis)
            tp1_hit = (current_price <= pos.tp1) if is_short else (current_price >= pos.tp1)
            if tp1_hit and not pos.tp1_hit:
                event = self._handle_tp1_hit(pos, current_price)
                events.append(event)

            # 4. Take Profit 2 (Second trim - 25% + Ratchet to TP1)
            tp2_hit = (current_price <= pos.tp2) if is_short else (current_price >= pos.tp2)
            if tp2_hit and not pos.tp2_hit:
                event = self._handle_tp2_hit(pos, current_price)
                events.append(event)

            # 5. Schmitt-Trigger Hysteresis: Elevate stop to TP1 once price clears escape threshold
            if getattr(pos, "tp1_armed_hysteresis", False) and not pos.tp2_hit:
                escape_threshold = getattr(pos, "tp1_escape_threshold", 0.0)
                if not escape_threshold or escape_threshold <= 0:
                    escape_threshold = self.risk_manager.calculate_escape_threshold(
                        tp1=pos.tp1, atr=pos.atr, direction=pos.direction
                    )
                    pos.tp1_escape_threshold = escape_threshold

                escape_cleared = (current_price <= escape_threshold) if is_short else (current_price >= escape_threshold)
                if escape_cleared:
                    pos.tp1_armed_hysteresis = False
                    pos.tp1_stop_elevated = True
                    if pos.direction.upper() == "SHORT" and not pos.direction.upper().endswith("_ETP"):
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
                    # Update native broker stop order on Trading 212
                    self.place_broker_stop(pos, stop_price=pos.tp1)
                    self.notifier.notify_tsl(
                        ticker=pos.ticker,
                        underlying=pos.underlying,
                        direction=pos.direction,
                        current_price=current_price,
                        new_tsl=pos.tp1,
                        locked_in_pnl_pct=locked_pct,
                        event_type="HYSTERESIS_TP1_RATCHET",
                    )
                    events.append({"event": "HYSTERESIS_TP1_RATCHET", "ticker": ticker, "new_tsl": pos.tp1})

            # 6. Trailing Stop Ratchet (Ratchets higher with new highs once hysteresis clears)
            if pos.tp1_hit and not getattr(pos, "tp1_armed_hysteresis", False):
                favorable_price = pos.lowest_price if is_short else pos.highest_price
                new_tsl = self.risk_manager.calculate_trailing_stop(
                    current_price=current_price,
                    peak_price=favorable_price,
                    atr=pos.atr,
                    current_tsl=pos.trailing_stop_loss,
                    direction=pos.direction,
                )
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

                    # Update native broker stop order on Trading 212
                    self.place_broker_stop(pos, stop_price=new_tsl)

                    self.notifier.notify_tsl(
                        ticker=pos.ticker,
                        underlying=pos.underlying,
                        direction=pos.direction,
                        current_price=current_price,
                        new_tsl=new_tsl,
                        locked_in_pnl_pct=locked_pct,
                        event_type="TSL_RATCHET",
                    )
                    events.append({"event": "TSL_RATCHET", "ticker": ticker, "new_tsl": new_tsl})

        # Clean up closed positions
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
    # Internal Target Handlers
    # ─────────────────────────────────────────────────────────────
    @staticmethod
    def _calculate_pnl(pos: TrackedPosition, price: float, quantity: float) -> Tuple[float, float]:
        """Compute realized P&L amount and percentage accounting for direction."""
        is_short = (pos.tp1 < pos.entry_price) if (pos.tp1 > 0 and pos.entry_price > 0) else (
            pos.direction.upper() == "SHORT" and not (
                pos.ticker.endswith("_EQ") or pos.ticker.endswith(".L") or pos.direction.upper().endswith("_ETP")
            )
        )
        if is_short:
            diff = pos.entry_price - price
        else:
            diff = price - pos.entry_price
        pnl = quantity * diff
        pnl_pct = (diff / pos.entry_price) * 100.0 if pos.entry_price > 0 else 0.0
        return round(pnl, 2), round(pnl_pct, 2)

    def _handle_tp1_hit(self, pos: TrackedPosition, price: float) -> Dict[str, Any]:
        """Process TP1 hit: Sell 50%, bank profit, move stop to break-even, arm hysteresis."""
        pos.tp1_hit = True
        pos.tp1_armed_hysteresis = True
        pos.tp1_stop_elevated = False
        pos.tp1_escape_threshold = self.risk_manager.calculate_escape_threshold(
            tp1=pos.tp1,
            atr=pos.atr,
            direction=pos.direction,
        )
        trim_pct = getattr(self.risk_manager, "tp1_trim_pct", settings.TP1_TRIM_PCT)
        trim_shares = round(pos.original_quantity * trim_pct, 3)
        actual_trim = min(trim_shares, pos.remaining_quantity)
        pos.remaining_quantity = max(0.0, pos.remaining_quantity - actual_trim)

        # Move stop to Break-Even (Entry price) - keeps 1.0R buffer to prevent instant whipsaw
        pos.stop_loss = pos.entry_price
        pos.trailing_stop_loss = pos.entry_price

        pnl, pnl_pct = self._calculate_pnl(pos, price, actual_trim)

        # Cancel pending broker stop and limit orders to unblock shares for sell execution
        self.cancel_all_broker_orders(pos)
        delay = getattr(self.t212, "rate_limit_delay", 1.25)
        if not self.dry_run and delay > 0:
            time.sleep(delay)

        # Execute sell order on Trading 212
        self._execute_sell(pos.ticker, actual_trim)
        if not self.dry_run and delay > 0:
            time.sleep(delay)

        # Update native broker stop order on Trading 212 for remaining shares
        self.place_broker_stop(pos, stop_price=pos.entry_price)

        # Notify
        self.notifier.notify_tp1(
            ticker=pos.ticker,
            underlying=pos.underlying,
            direction=pos.direction,
            price=price,
            quantity_sold=actual_trim,
            pnl=pnl,
            pnl_pct=pnl_pct,
            new_sl=pos.entry_price,
        )
        return {"event": "TP1", "ticker": pos.ticker, "price": price, "pnl": pnl}

    def _handle_tp2_hit(self, pos: TrackedPosition, price: float) -> Dict[str, Any]:
        """Process TP2 hit: Sell 25% of original, ratchet trailing stop to TP1."""
        pos.tp1_hit = True
        pos.tp2_hit = True
        pos.tp1_armed_hysteresis = False
        pos.tp1_stop_elevated = True
        trim_pct = getattr(self.risk_manager, "tp2_trim_pct", settings.TP2_TRIM_PCT)
        trim_shares = round(pos.original_quantity * trim_pct, 3)
        actual_trim = min(trim_shares, pos.remaining_quantity)
        pos.remaining_quantity = max(0.0, pos.remaining_quantity - actual_trim)

        # Ratchet trailing stop to TP1
        if pos.direction.upper() == "SHORT" and not pos.direction.upper().endswith("_ETP"):
            pos.trailing_stop_loss = min(pos.trailing_stop_loss, pos.tp1) if pos.trailing_stop_loss > 0 else pos.tp1
            pos.stop_loss = min(pos.stop_loss, pos.tp1) if pos.stop_loss > 0 else pos.tp1
        else:
            pos.trailing_stop_loss = max(pos.trailing_stop_loss, pos.tp1)
            pos.stop_loss = max(pos.stop_loss, pos.tp1)

        pnl, pnl_pct = self._calculate_pnl(pos, price, actual_trim)

        # Cancel pending broker orders to unblock shares for sell execution
        self.cancel_all_broker_orders(pos)
        delay = getattr(self.t212, "rate_limit_delay", 1.25)
        if not self.dry_run and delay > 0:
            time.sleep(delay)

        # Execute sell order on Trading 212
        self._execute_sell(pos.ticker, actual_trim)
        if not self.dry_run and delay > 0:
            time.sleep(delay)

        # Update native broker stop order on Trading 212 for remaining shares
        self.place_broker_stop(pos, stop_price=pos.trailing_stop_loss)

        # Notify
        self.notifier.notify_tp2(
            ticker=pos.ticker,
            underlying=pos.underlying,
            direction=pos.direction,
            price=price,
            quantity_sold=actual_trim,
            pnl=pnl,
            pnl_pct=pnl_pct,
            tsl=pos.trailing_stop_loss,
        )
        return {"event": "TP2", "ticker": pos.ticker, "price": price, "pnl": pnl}

    def _handle_tp3_hit(self, pos: TrackedPosition, price: float) -> Dict[str, Any]:
        """Process TP3 hit: Close remaining runner position with full gains."""
        pos.tp3_hit = True
        pos.tp1_armed_hysteresis = False
        pos.tp1_stop_elevated = True
        pos.is_closed = True
        shares_to_sell = pos.remaining_quantity
        pos.remaining_quantity = 0.0

        # Cancel all pending broker orders on Trading 212
        self.cancel_all_broker_orders(pos)
        delay = getattr(self.t212, "rate_limit_delay", 1.25)
        if not self.dry_run and delay > 0:
            time.sleep(delay)

        pnl, pnl_pct = self._calculate_pnl(pos, price, shares_to_sell)

        # Execute sell order on Trading 212
        self._execute_sell(pos.ticker, shares_to_sell)

        # Notify
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
        pos.is_closed = True
        shares_to_sell = pos.remaining_quantity
        pos.remaining_quantity = 0.0

        # Cancel all pending broker orders on Trading 212
        self.cancel_all_broker_orders(pos)
        delay = getattr(self.t212, "rate_limit_delay", 1.25)
        if not self.dry_run and delay > 0:
            time.sleep(delay)

        pnl, pnl_pct = self._calculate_pnl(pos, price, shares_to_sell)

        # Execute sell order on Trading 212
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
            # In Trading 212, selling existing shares is negative quantity
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
        """Persist current active positions to state file atomically.
        Strictly excludes closed or zero-quantity positions so state file only reflects active exposure.
        """
        try:
            raw = {
                t: asdict(pos)
                for t, pos in self.positions.items()
                if not getattr(pos, "is_closed", False)
                and (getattr(pos, "remaining_quantity", 0.0) or 0.0) > 0.0
            }
            tmp_file = f"{self.state_file}.tmp.{os.getpid()}"
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(raw, f, indent=2)
            os.replace(tmp_file, self.state_file)
        except Exception as exc:
            logger.warning("Failed to save position monitor state to %s: %s", self.state_file, exc)

    def _load_state(self) -> Dict[str, TrackedPosition]:
        """Load active positions from state file. Auto-heals state file if legacy closed trades exist."""
        if not os.path.exists(self.state_file):
            return {}
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                raw = json.load(f)
            known_fields = {f.name for f in fields(TrackedPosition)}
            positions = {}
            had_closed = False
            for t, data in raw.items():
                is_closed = bool(data.get("is_closed", False))
                rem_qty = float(
                    data.get("remaining_quantity")
                    if data.get("remaining_quantity") is not None
                    else (data.get("original_quantity") or 0.0)
                )
                if not is_closed and rem_qty > 0.0:
                    filtered_data = {k: v for k, v in data.items() if k in known_fields}
                    positions[t] = TrackedPosition(**filtered_data)
                else:
                    had_closed = True

            logger.info("Loaded %d active tracked positions from %s", len(positions), self.state_file)

            # Auto-heal state file on disk if closed positions were present
            if had_closed:
                tmp_file = f"{self.state_file}.tmp.{os.getpid()}"
                with open(tmp_file, "w", encoding="utf-8") as f:
                    json.dump({t: asdict(pos) for t, pos in positions.items()}, f, indent=2)
                os.replace(tmp_file, self.state_file)
                logger.info("Auto-healed %s: purged legacy closed positions from disk.", self.state_file)

            return positions
        except Exception as exc:
            logger.warning("Could not read state file %s: %s", self.state_file, exc)
            return {}
