"""Unit tests for Dashboard Broker Order History alignment, direction resolution, and P&L calculations."""

import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock
from trading_system.dashboard.server import (
    fetch_live_broker_notifications,
    load_recent_notifications,
)
from trading_system.data.leveraged_universe import LeveragedUniverse


class TestDashboardBrokerHistory(unittest.TestCase):
    """Test suite for broker order historical sync and dashboard notifications."""

    def setUp(self):
        self.universe = LeveragedUniverse()

    def test_fetch_live_broker_notifications_spot_long_exit(self):
        """Verify that closing a spot long position like IONQl1_EQ is labeled direction=LONG (not SHORT)."""
        mock_client = MagicMock()
        mock_client.get_historical_orders.return_value = {
            "items": [
                {
                    "order": {
                        "id": 999111,
                        "strategy": "QUANTITY",
                        "type": "STOP",
                        "ticker": "IONQl1_EQ",
                        "quantity": -2.5,
                        "filledQuantity": -2.5,
                        "stopPrice": 30.50,
                        "status": "FILLED",
                        "side": "SELL",
                        "createdAt": "2026-10-27T10:00:00.000Z",
                    },
                    "fill": {
                        "id": 999222,
                        "quantity": -2.5,
                        "price": 30.45,
                        "filledAt": "2026-10-27T10:05:00.000Z",
                        "walletImpact": {
                            "currency": "EUR",
                            "netValue": 76.125,
                            "realisedProfitLoss": -3.50,
                            "fxRate": 1,
                        },
                    },
                }
            ]
        }

        events = fetch_live_broker_notifications(mock_client, self.universe, limit=10)
        self.assertEqual(len(events), 1)
        ev = events[0]

        # Authoritative direction must be LONG because IONQ is a long spot holding
        self.assertEqual(ev["ticker"], "IONQl1_EQ")
        self.assertEqual(ev["direction"], "LONG")
        self.assertEqual(ev["event_type"], "SL")
        self.assertEqual(ev["pnl"], -3.50)
        # Cost basis = 76.125 - (-3.50) = 79.625 => pnl_pct = -3.50 / 79.625 * 100 = -4.40%
        self.assertAlmostEqual(ev["pnl_pct"], -4.40, places=2)
        self.assertEqual(ev["quantity"], 2.5)
        self.assertEqual(ev["order_id"], "999111")

    def test_fetch_live_broker_notifications_buy_entry(self):
        """Verify that open BUY entry has direction=LONG and pnl=None, pnl_pct=None."""
        mock_client = MagicMock()
        mock_client.get_historical_orders.return_value = {
            "items": [
                {
                    "order": {
                        "id": 888111,
                        "strategy": "QUANTITY",
                        "type": "MARKET",
                        "ticker": "3LFEl_EQ",
                        "quantity": 1.5,
                        "filledQuantity": 1.5,
                        "status": "FILLED",
                        "side": "BUY",
                        "createdAt": "2026-10-27T11:00:00.000Z",
                    },
                    "fill": {
                        "id": 888222,
                        "quantity": 1.5,
                        "price": 28.50,
                        "filledAt": "2026-10-27T11:00:01.000Z",
                        "walletImpact": {
                            "currency": "EUR",
                            "netValue": 42.75,
                            "realisedProfitLoss": None,
                        },
                    },
                }
            ]
        }

        events = fetch_live_broker_notifications(mock_client, self.universe, limit=10)
        self.assertEqual(len(events), 1)
        ev = events[0]

        self.assertEqual(ev["ticker"], "3LFEl_EQ")
        self.assertEqual(ev["direction"], "LONG")
        self.assertEqual(ev["event_type"], "BUY")
        self.assertIsNone(ev["pnl"])
        self.assertIsNone(ev["pnl_pct"])
        self.assertEqual(ev["quantity"], 1.5)

    def test_fetch_live_broker_notifications_skips_unfilled_orders(self):
        """Verify that cancelled, rejected, or 0-quantity orders are skipped."""
        mock_client = MagicMock()
        mock_client.get_historical_orders.return_value = {
            "items": [
                {
                    "order": {
                        "id": 777111,
                        "type": "LIMIT",
                        "ticker": "GPTSl_EQ",
                        "quantity": 0,
                        "filledQuantity": 0,
                        "status": "CANCELLED",
                        "side": "SELL",
                    },
                    "fill": {},
                }
            ]
        }

        events = fetch_live_broker_notifications(mock_client, self.universe, limit=10)
        self.assertEqual(len(events), 0)

    def test_fetch_live_broker_notifications_inverse_short_etp(self):
        """Verify that an inverse short ETP like 5STLd_EQ is labeled direction=SHORT."""
        mock_client = MagicMock()
        mock_client.get_historical_orders.return_value = {
            "items": [
                {
                    "order": {
                        "id": 666111,
                        "strategy": "QUANTITY",
                        "type": "STOP",
                        "ticker": "5STLd_EQ",
                        "quantity": -1.12,
                        "filledQuantity": -1.12,
                        "stopPrice": 14.94,
                        "status": "FILLED",
                        "side": "SELL",
                    },
                    "fill": {
                        "id": 666222,
                        "quantity": -1.12,
                        "price": 14.876,
                        "walletImpact": {
                            "currency": "EUR",
                            "netValue": 16.66,
                            "realisedProfitLoss": -0.45,
                        },
                    },
                }
            ]
        }

        events = fetch_live_broker_notifications(mock_client, self.universe, limit=10)
        self.assertEqual(len(events), 1)
        ev = events[0]

        self.assertEqual(ev["ticker"], "5STLd_EQ")
        self.assertEqual(ev["direction"], "SHORT")
        self.assertEqual(ev["event_type"], "SL")
        self.assertEqual(ev["pnl"], -0.45)
        # Cost basis = 16.66 - (-0.45) = 17.11 => pnl_pct = -0.45 / 17.11 * 100 = -2.63%
        self.assertAlmostEqual(ev["pnl_pct"], -2.63, places=2)

    def test_load_recent_notifications_normalizes_direction_and_pnl(self):
        """Verify that local log entries with legacy direction bugs are normalized."""
        with tempfile.NamedTemporaryFile("w+", suffix=".jsonl", delete=False) as tf:
            # Write a legacy bugged entry where IONQl1_EQ was recorded with direction=SHORT and BUY had pnl=0.0
            tf.write(json.dumps({
                "event_type": "SELL",
                "ticker": "IONQl1_EQ",
                "direction": "SHORT",  # legacy bug
                "price": 32.28,
                "quantity": 1.37,
                "pnl": -1.5,
                "pnl_pct": -3.5,
            }) + "\n")
            tf.write(json.dumps({
                "event_type": "BUY",
                "ticker": "IONQl1_EQ",
                "direction": "LONG",
                "price": 30.0,
                "quantity": 1.37,
                "pnl": 0.0,  # legacy 0% bug
                "pnl_pct": 0.0,
            }) + "\n")
            temp_path = tf.name

        try:
            records = load_recent_notifications(temp_path, limit=10, universe=self.universe)
            self.assertEqual(len(records), 2)

            # Record 0 (latest = BUY due to reverse order)
            buy_rec = records[0]
            self.assertEqual(buy_rec["event_type"], "BUY")
            self.assertEqual(buy_rec["direction"], "LONG")
            self.assertIsNone(buy_rec["pnl"])
            self.assertIsNone(buy_rec["pnl_pct"])

            # Record 1 (SELL)
            sell_rec = records[1]
            self.assertEqual(sell_rec["event_type"], "SELL")
            self.assertEqual(sell_rec["direction"], "LONG")  # Auto-normalized to LONG!
            self.assertEqual(sell_rec["pnl"], -1.5)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)


if __name__ == "__main__":
    unittest.main()
