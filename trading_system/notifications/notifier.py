"""Unified Notification Engine for Trading System.

Dispatches real-time alerts for:
- BUY (Long entry)
- SELL (Short entry or position exit)
- TP1 (Take Profit 1 hit)
- TP2 (Take Profit 2 hit)
- TP3 (Take Profit 3 hit)
- SL (Stop Loss hit)
- TSL (Trailing Stop Loss updated or triggered)

Supports multi-channel output:
- Formatted Console / Terminal cards
- macOS Native Desktop Notifications (osascript)
- Telegram Bot Webhooks
- Discord Webhooks
- JSONL structured audit log
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

logger = logging.getLogger(__name__)

NotificationType = Literal[
    "BUY", "SELL", "TP1", "TP2", "TP3", "SL", "TSL",
    "ASYMMETRIC_SESSION_EXIT", "INVERSE_INTRADAY_FLATTEN", "EARNINGS_BLACKOUT_EXIT"
]


@dataclass
class TradeNotification:
    """Represents a trade lifecycle notification."""
    event_type: NotificationType
    ticker: str                         # Trading 212 execution ticker (e.g. QQQ3l_EQ)
    underlying: str                     # Underlying asset (e.g. QQQ)
    direction: str                      # 'LONG' or 'SHORT'
    price: float                        # Event price
    quantity: float                     # Number of shares
    stop_loss: float                    # Current stop loss level
    trailing_stop: float                # Current trailing stop level
    tp1: float                          # Take profit 1
    tp2: float                          # Take profit 2
    tp3: float                          # Take profit 3
    pnl: Optional[float] = None         # Dollar PnL (for exits/TPs)
    pnl_pct: Optional[float] = None     # Percentage gain/loss
    rationale: str = ""                 # Context / commentary
    timestamp: str = ""

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


class Notifier:
    """Multi-channel notification dispatcher."""

    def __init__(
        self,
        telegram_bot_token: Optional[str] = None,
        telegram_chat_id: Optional[str] = None,
        discord_webhook_url: Optional[str] = None,
        log_file: str = "notifications.jsonl",
        enable_desktop: bool = True,
    ):
        self.telegram_bot_token = telegram_bot_token or os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.telegram_chat_id = telegram_chat_id or os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        self.discord_webhook_url = discord_webhook_url or os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
        self.log_file = log_file
        self.enable_desktop = enable_desktop

    # ─────────────────────────────────────────────────────────────
    # High-level dispatch helpers
    # ─────────────────────────────────────────────────────────────
    def notify_buy(
        self,
        ticker: str,
        underlying: str,
        price: float,
        quantity: float,
        sl: float,
        tp1: float,
        tp2: float,
        tp3: float,
        rationale: str = "",
    ) -> None:
        """Send a BUY / LONG Entry notification."""
        note = TradeNotification(
            event_type="BUY",
            ticker=ticker,
            underlying=underlying,
            direction="LONG",
            price=price,
            quantity=quantity,
            stop_loss=sl,
            trailing_stop=sl,
            tp1=tp1,
            tp2=tp2,
            tp3=tp3,
            rationale=rationale or "System detected 3x Bullish breakout momentum (ADX > 25, 20 EMA > 50 EMA)",
        )
        self.dispatch(note)

    def notify_sell(
        self,
        ticker: str,
        underlying: str,
        price: float,
        quantity: float,
        sl: float,
        tp1: float,
        tp2: float,
        tp3: float,
        is_short_entry: bool = True,
        pnl: Optional[float] = None,
        pnl_pct: Optional[float] = None,
        rationale: str = "",
    ) -> None:
        """Send a SELL notification (Short entry or position closure)."""
        direction = "SHORT" if is_short_entry else "EXIT"
        event: NotificationType = "SELL"
        default_rationale = (
            "System detected 3x Bearish breakdown momentum (ADX > 25, 20 EMA < 50 EMA)"
            if is_short_entry
            else "Closed position based on exit signal"
        )
        note = TradeNotification(
            event_type=event,
            ticker=ticker,
            underlying=underlying,
            direction=direction,
            price=price,
            quantity=quantity,
            stop_loss=sl,
            trailing_stop=sl,
            tp1=tp1,
            tp2=tp2,
            tp3=tp3,
            pnl=pnl,
            pnl_pct=pnl_pct,
            rationale=rationale or default_rationale,
        )
        self.dispatch(note)

    def notify_tp1(
        self,
        ticker: str,
        underlying: str,
        direction: str,
        price: float,
        quantity_sold: float,
        pnl: float,
        pnl_pct: float,
        new_sl: float,
    ) -> None:
        """Send a TP1 Take Profit notification (+1.0R)."""
        note = TradeNotification(
            event_type="TP1",
            ticker=ticker,
            underlying=underlying,
            direction=direction,
            price=price,
            quantity=quantity_sold,
            stop_loss=new_sl,
            trailing_stop=new_sl,
            tp1=price,
            tp2=0.0,
            tp3=0.0,
            pnl=pnl,
            pnl_pct=pnl_pct,
            rationale=f"Target 1 (+1.0R) reached! Banked 50% profit. Stop moved to Break-Even (${new_sl:.2f}) with Schmitt Hysteresis armed.",
        )
        self.dispatch(note)

    def notify_tp2(
        self,
        ticker: str,
        underlying: str,
        direction: str,
        price: float,
        quantity_sold: float,
        pnl: float,
        pnl_pct: float,
        tsl: float,
    ) -> None:
        """Send a TP2 Take Profit notification (+2.0R)."""
        note = TradeNotification(
            event_type="TP2",
            ticker=ticker,
            underlying=underlying,
            direction=direction,
            price=price,
            quantity=quantity_sold,
            stop_loss=tsl,
            trailing_stop=tsl,
            tp1=0.0,
            tp2=price,
            tp3=0.0,
            pnl=pnl,
            pnl_pct=pnl_pct,
            rationale=f"Target 2 (+2.0R) reached! Banked additional 25% profit. Trailing Stop ratcheted to ${tsl:.2f}.",
        )
        self.dispatch(note)

    def notify_tp3(
        self,
        ticker: str,
        underlying: str,
        direction: str,
        price: float,
        quantity_sold: float,
        pnl: float,
        pnl_pct: float,
    ) -> None:
        """Send a TP3 Take Profit notification (+3.0R - Full Runner Exit)."""
        note = TradeNotification(
            event_type="TP3",
            ticker=ticker,
            underlying=underlying,
            direction=direction,
            price=price,
            quantity=quantity_sold,
            stop_loss=price,
            trailing_stop=price,
            tp1=0.0,
            tp2=0.0,
            tp3=price,
            pnl=pnl,
            pnl_pct=pnl_pct,
            rationale="Final Target 3 (+3.0R) reached! Closed remaining runner with maximum gains.",
        )
        self.dispatch(note)

    def notify_sl(
        self,
        ticker: str,
        underlying: str,
        direction: str,
        price: float,
        quantity: float,
        loss_amount: float,
        loss_pct: float,
    ) -> None:
        """Send a Stop Loss (SL) hit notification."""
        note = TradeNotification(
            event_type="SL",
            ticker=ticker,
            underlying=underlying,
            direction=direction,
            price=price,
            quantity=quantity,
            stop_loss=price,
            trailing_stop=price,
            tp1=0.0,
            tp2=0.0,
            tp3=0.0,
            pnl=-abs(loss_amount),
            pnl_pct=-abs(loss_pct),
            rationale=f"Stop Loss triggered at ${price:.2f}. Risk preserved, position closed.",
        )
        self.dispatch(note)

    def notify_tsl(
        self,
        ticker: str,
        underlying: str,
        direction: str,
        current_price: float,
        new_tsl: float,
        locked_in_pnl_pct: float,
        is_triggered: bool = False,
    ) -> None:
        """Send a Trailing Stop Loss (TSL) update or execution notification."""
        action_text = (
            f"Trailing Stop TRIGGERED at ${current_price:.2f}! Position closed to bank profits."
            if is_triggered
            else f"Trailing Stop ratcheted UP to ${new_tsl:.2f} (Locking in +{locked_in_pnl_pct:.1f}% min gain)."
        )
        note = TradeNotification(
            event_type="TSL",
            ticker=ticker,
            underlying=underlying,
            direction=direction,
            price=current_price,
            quantity=0.0,
            stop_loss=new_tsl,
            trailing_stop=new_tsl,
            tp1=0.0,
            tp2=0.0,
            tp3=0.0,
            pnl_pct=locked_in_pnl_pct,
            rationale=action_text,
        )
    def notify_session_exit(
        self,
        ticker: str,
        underlying: str,
        direction: str,
        price: float,
        quantity: float,
        exit_reason: str,
        pnl: Optional[float] = None,
        pnl_pct: Optional[float] = None,
    ) -> None:
        """Send a 16:20 UTC Asymmetric Session Exit notification."""
        event_type = exit_reason if exit_reason in ("ASYMMETRIC_SESSION_EXIT", "INVERSE_INTRADAY_FLATTEN", "EARNINGS_BLACKOUT_EXIT") else "SELL"
        rationale_map = {
            "ASYMMETRIC_SESSION_EXIT": "16:20 UTC Asymmetric Session Exit: Long position did not reach TP1 break-even before LSE close. Flattened to protect against overnight gap.",
            "INVERSE_INTRADAY_FLATTEN": "16:20 UTC Inverse Flatten: 3x Short ETP liquidated 100% at session close to eliminate overnight compounding decay.",
            "EARNINGS_BLACKOUT_EXIT": "16:20 UTC Earnings Blackout: Liquidated due to underlying earnings release scheduled within 24 hours.",
        }
        rationale = rationale_map.get(exit_reason, f"Session close exit: {exit_reason}")
        note = TradeNotification(
            event_type=event_type,  # type: ignore
            ticker=ticker,
            underlying=underlying,
            direction=direction,
            price=price,
            quantity=quantity,
            stop_loss=price,
            trailing_stop=price,
            tp1=0.0,
            tp2=0.0,
            tp3=0.0,
            pnl=pnl,
            pnl_pct=pnl_pct,
            rationale=rationale,
        )
        self.dispatch(note)

    # ─────────────────────────────────────────────────────────────
    # Core Dispatcher
    # ─────────────────────────────────────────────────────────────
    def dispatch(self, note: TradeNotification) -> None:
        """Dispatch notification across all configured channels."""
        # 1. Console formatted card
        self._print_console_card(note)

        # 2. Native macOS desktop banner
        if self.enable_desktop:
            self._send_macos_notification(note)

        # 3. Telegram Webhook (if configured)
        if self.telegram_bot_token and self.telegram_chat_id:
            self._send_telegram(note)

        # 4. Discord Webhook (if configured)
        if self.discord_webhook_url:
            self._send_discord(note)

        # 5. Append to JSONL audit log
        self._append_jsonl(note)

    # ─────────────────────────────────────────────────────────────
    # Formatting & Output Channels
    # ─────────────────────────────────────────────────────────────
    def _print_console_card(self, note: TradeNotification) -> None:
        """Render a high-visibility terminal card."""
        icons = {
            "BUY": "🟢 [BUY / LONG ENTRY]",
            "SELL": "🔴 [SELL / SHORT ENTRY]" if note.direction == "SHORT" else "🏁 [POSITION CLOSED]",
            "TP1": "🎯 [TAKE PROFIT 1 HIT — +1.0R]",
            "TP2": "🎯 [TAKE PROFIT 2 HIT — +2.0R]",
            "TP3": "🚀 [TAKE PROFIT 3 HIT — +3.0R]",
            "SL": "🛑 [STOP LOSS TRIGGERED]",
            "TSL": "🛡️ [TRAILING STOP UPDATE]",
            "ASYMMETRIC_SESSION_EXIT": "⏳ [16:20 UTC ASYMMETRIC GAP EVACUATION]",
            "INVERSE_INTRADAY_FLATTEN": "⚡ [16:20 UTC INVERSE INTRADAY FLATTEN]",
            "EARNINGS_BLACKOUT_EXIT": "⚠️ [16:20 UTC EARNINGS BLACKOUT EXIT]",
        }
        title = icons.get(note.event_type, f"📢 [{note.event_type}]")
        border = "═" * 60

        print(f"\n{border}")
        print(f"  {title}")
        print(f"{border}")
        print(f"  Asset:      {note.ticker} (Underlying: {note.underlying}) | Direction: {note.direction}")
        print(f"  Price:      ${note.price:.2f} | Quantity: {note.quantity:.2f} shares")
        if note.stop_loss > 0:
            print(f"  Stop Loss:  ${note.stop_loss:.2f} | Trailing Stop: ${note.trailing_stop:.2f}")
        if note.tp1 > 0:
            print(f"  Targets:    TP1: ${note.tp1:.2f} | TP2: ${note.tp2:.2f} | TP3: ${note.tp3:.2f}")
        if note.pnl is not None and note.pnl_pct is not None:
            sign = "+" if note.pnl >= 0 else ""
            print(f"  Realized:   {sign}${note.pnl:.2f} ({sign}{note.pnl_pct:.2f}%)")
        print(f"  Details:    {note.rationale}")
        print(f"  Time:       {note.timestamp}")
        print(f"{border}\n")

    def _send_macos_notification(self, note: TradeNotification) -> None:
        """Display a native macOS banner alert."""
        try:
            title = f"Trading 212 3x Alert: {note.event_type} {note.direction}"
            message = f"{note.ticker} @ ${note.price:.2f} — {note.rationale[:80]}"
            # Escape quotes
            title_clean = title.replace('"', '\\"')
            msg_clean = message.replace('"', '\\"')
            cmd = f'display notification "{msg_clean}" with title "{title_clean}" sound name "Glass"'
            subprocess.run(["osascript", "-e", cmd], capture_output=True, timeout=3)
        except Exception as exc:
            logger.debug("Desktop notification skipped: %s", exc)

    def _send_telegram(self, note: TradeNotification) -> None:
        """Send message via Telegram Bot API."""
        try:
            url = f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage"
            text = (
                f"*{note.event_type} ALERT: {note.direction}*\n"
                f"*Ticker:* `{note.ticker}` ({note.underlying})\n"
                f"*Price:* `${note.price:.2f}`\n"
                f"*SL:* `${note.stop_loss:.2f}` | *TSL:* `${note.trailing_stop:.2f}`\n"
                f"*TP1:* `${note.tp1:.2f}` | *TP2:* `${note.tp2:.2f}` | *TP3:* `${note.tp3:.2f}`\n"
                f"_{note.rationale}_\n"
                f"`{note.timestamp}`"
            )
            payload = json.dumps({
                "chat_id": self.telegram_chat_id,
                "text": text,
                "parse_mode": "Markdown",
            }).encode("utf-8")
            req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                pass
        except Exception as exc:
            logger.warning("Telegram notification failed: %s", exc)

    def _send_discord(self, note: TradeNotification) -> None:
        """Send message via Discord Webhook."""
        try:
            color = 0x00FF00 if note.event_type in ("BUY", "TP1", "TP2", "TP3") else 0xFF0000
            embed = {
                "title": f"{note.event_type} - {note.direction} {note.ticker}",
                "description": note.rationale,
                "color": color,
                "fields": [
                    {"name": "Price", "value": f"${note.price:.2f}", "inline": True},
                    {"name": "Shares", "value": f"{note.quantity:.2f}", "inline": True},
                    {"name": "Stop Loss", "value": f"${note.stop_loss:.2f}", "inline": True},
                    {"name": "Trailing Stop", "value": f"${note.trailing_stop:.2f}", "inline": True},
                    {"name": "TP1 / TP2 / TP3", "value": f"${note.tp1:.2f} / ${note.tp2:.2f} / ${note.tp3:.2f}", "inline": False},
                ],
                "footer": {"text": note.timestamp},
            }
            payload = json.dumps({"embeds": [embed]}).encode("utf-8")
            req = urllib.request.Request(self.discord_webhook_url, data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                pass
        except Exception as exc:
            logger.warning("Discord notification failed: %s", exc)

    def _append_jsonl(self, note: TradeNotification) -> None:
        """Persist structured notification to file."""
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(note)) + "\n")
        except Exception as exc:
            logger.warning("Failed to write to notification log %s: %s", self.log_file, exc)
