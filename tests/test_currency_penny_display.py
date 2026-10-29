"""Unit tests for British Penny 'p' (GBX) currency display across Dashboard, State, and Alerts."""

import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock

from trading_system.agents.risk_manager import DecisionCard
from trading_system.data.leveraged_universe import (
    LeveragedUniverse,
    format_price,
    get_currency_display,
    get_ticker_currency,
)
from trading_system.data.trading212_client import Position
from trading_system.dashboard.server import load_active_signals
from trading_system.execution.position_monitor import PositionMonitor, TrackedPosition
from trading_system.notifications.notifier import Notifier, TradeNotification


class TestCurrencyPennyDisplay(unittest.TestCase):
    """Verify penny 'p' (GBX) currency resolution, formatting, and auto-healing."""

    def test_currency_display_helpers(self):
        """Test get_currency_display and format_price for GBX, EUR, USD, GBP."""
        # GBX
        code, prefix, suffix = get_currency_display("GBX")
        self.assertEqual(code, "GBX")
        self.assertEqual(prefix, "")
        self.assertEqual(suffix, "p")
        self.assertEqual(format_price(3190.0, "GBX"), "3190.00p")
        self.assertEqual(format_price(430.90, "GBX"), "430.90p")

        # EUR
        code, prefix, suffix = get_currency_display("EUR")
        self.assertEqual(code, "EUR")
        self.assertEqual(prefix, "€")
        self.assertEqual(suffix, "")
        self.assertEqual(format_price(45.2, "EUR"), "€45.20")

        # USD
        code, prefix, suffix = get_currency_display("USD")
        self.assertEqual(code, "USD")
        self.assertEqual(prefix, "$")
        self.assertEqual(suffix, "")
        self.assertEqual(format_price(150.5, "USD"), "$150.50")

    def test_ticker_currency_resolution(self):
        """Verify that known tickers resolve to their exact broker currency."""
        # GBX Penny tickers
        for gbx_t in ["3XOMl_EQ", "SKORl_EQ", "3UNHl_EQ", "3ASMl1_EQ", "USGBl_EQ"]:
            self.assertEqual(get_ticker_currency(gbx_t), "GBX", f"Failed for {gbx_t}")

        # EUR tickers
        for eur_t in ["URASa_EQ", "WTI3m_EQ", "QQQ3m_EQ", "3NVDp_EQ", "3ASMe_EQ"]:
            self.assertEqual(get_ticker_currency(eur_t), "EUR", f"Failed for {eur_t}")

        # USD tickers
        for usd_t in ["AMD3l_EQ", "QQQ3l_EQ", "3SEMl_EQ"]:
            self.assertEqual(get_ticker_currency(usd_t), "USD", f"Failed for {usd_t}")

    def test_position_monitor_auto_heal_currency(self):
        """Verify PositionMonitor auto-heals missing or EUR-defaulted currency for GBX positions."""
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json") as f:
            state_path = f.name
            raw_state = {
                "3XOMl_EQ": {
                    "ticker": "3XOMl_EQ",
                    "underlying": "XOM",
                    "direction": "LONG",
                    "entry_price": 3190.0,
                    "original_quantity": 1.0,
                    "remaining_quantity": 1.0,
                    "stop_loss": 3030.5,
                    "trailing_stop_loss": 3030.5,
                    "highest_price": 3190.0,
                    "lowest_price": 3190.0,
                    "tp1": 3349.5,
                    "tp2": 3509.0,
                    "tp3": 3668.5,
                    "atr": 79.75,
                    "currency": "EUR",  # Stored erroneously as EUR previously
                },
                "URASa_EQ": {
                    "ticker": "URASa_EQ",
                    "underlying": "URA",
                    "direction": "LONG",
                    "entry_price": 45.0,
                    "original_quantity": 10.0,
                    "remaining_quantity": 10.0,
                    "stop_loss": 42.75,
                    "trailing_stop_loss": 42.75,
                    "highest_price": 45.0,
                    "lowest_price": 45.0,
                    "tp1": 47.25,
                    "tp2": 49.50,
                    "tp3": 51.75,
                    "atr": 1.12,
                },
            }
            json.dump(raw_state, f)

        try:
            mock_t212 = MagicMock()
            mock_notifier = MagicMock()
            mock_rm = MagicMock()
            pm = PositionMonitor(mock_t212, mock_notifier, mock_rm, state_file=state_path)

            # Check that 3XOMl_EQ was auto-healed to GBX
            self.assertEqual(pm.positions["3XOMl_EQ"].currency, "GBX")
            # Check that URASa_EQ resolved to EUR
            self.assertEqual(pm.positions["URASa_EQ"].currency, "EUR")

            # Check persisted state on disk
            with open(state_path, "r") as f:
                saved = json.load(f)
            self.assertEqual(saved["3XOMl_EQ"]["currency"], "GBX")
            self.assertEqual(saved["URASa_EQ"]["currency"], "EUR")
        finally:
            if os.path.exists(state_path):
                os.remove(state_path)

    def test_dashboard_active_signals_currency_formatting(self):
        """Verify dashboard load_active_signals sets correct prefixes, suffixes, and copy text."""
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json") as f:
            state_path = f.name
            raw_state = {
                "3XOMl_EQ": {
                    "ticker": "3XOMl_EQ",
                    "underlying": "XOM",
                    "direction": "LONG",
                    "entry_price": 3190.0,
                    "original_quantity": 1.0,
                    "remaining_quantity": 1.0,
                    "stop_loss": 3030.5,
                    "trailing_stop_loss": 3030.5,
                    "highest_price": 3190.0,
                    "lowest_price": 3190.0,
                    "tp1": 3349.5,
                    "tp2": 3509.0,
                    "tp3": 3668.5,
                    "atr": 79.75,
                    "currency": "GBX",
                },
                "WTI3m_EQ": {
                    "ticker": "WTI3m_EQ",
                    "underlying": "USO",
                    "direction": "LONG",
                    "entry_price": 12.50,
                    "original_quantity": 10.0,
                    "remaining_quantity": 10.0,
                    "stop_loss": 11.88,
                    "trailing_stop_loss": 11.88,
                    "highest_price": 12.50,
                    "lowest_price": 12.50,
                    "tp1": 13.12,
                    "tp2": 13.75,
                    "tp3": 14.38,
                    "atr": 0.31,
                    "currency": "EUR",
                },
            }
            json.dump(raw_state, f)

        try:
            universe = LeveragedUniverse()
            signals = load_active_signals(state_path, universe=universe, live_prices={"3XOMl_EQ": 3210.0, "WTI3m_EQ": 12.80})
            sig_map = {s["ticker"]: s for s in signals}

            # Verify 3XOMl_EQ
            xom = sig_map["3XOMl_EQ"]
            self.assertEqual(xom["currency_code"], "GBX")
            self.assertEqual(xom["currency_prefix"], "")
            self.assertEqual(xom["currency_suffix"], "p")
            self.assertIn("ENTRY: 3190.00p", xom["copy_text"])
            self.assertIn("STOP LOSS: 3030.50p", xom["copy_text"])
            self.assertIn("ACTIVE TSL: 3030.50p", xom["copy_text"])
            self.assertNotIn("€3190", xom["copy_text"])

            # Verify WTI3m_EQ
            wti = sig_map["WTI3m_EQ"]
            self.assertEqual(wti["currency_code"], "EUR")
            self.assertEqual(wti["currency_prefix"], "€")
            self.assertEqual(wti["currency_suffix"], "")
            self.assertIn("ENTRY: €12.50", wti["copy_text"])
            self.assertIn("STOP LOSS: €11.88", wti["copy_text"])
        finally:
            if os.path.exists(state_path):
                os.remove(state_path)

    def test_trade_notification_currency_formatting(self):
        """Verify TradeNotification formats price according to ticker's currency."""
        # 3XOMl_EQ (GBX)
        note_gbx = TradeNotification(
            event_type="BUY",
            ticker="3XOMl_EQ",
            underlying="XOM",
            direction="LONG",
            price=3190.0,
            quantity=1.0,
            stop_loss=3030.5,
            trailing_stop=3030.5,
            tp1=3349.5,
            tp2=3509.0,
            tp3=3668.5,
        )
        self.assertEqual(note_gbx.format_price(note_gbx.price), "3190.00p")
        self.assertEqual(note_gbx.format_price(note_gbx.stop_loss), "3030.50p")

        # URASa_EQ (EUR)
        note_eur = TradeNotification(
            event_type="BUY",
            ticker="URASa_EQ",
            underlying="URA",
            direction="LONG",
            price=45.0,
            quantity=10.0,
            stop_loss=42.75,
            trailing_stop=42.75,
            tp1=47.25,
            tp2=49.50,
            tp3=51.75,
        )
        self.assertEqual(note_eur.format_price(note_eur.price), "€45.00")
        self.assertEqual(note_eur.format_price(note_eur.stop_loss), "€42.75")


if __name__ == "__main__":
    unittest.main()
