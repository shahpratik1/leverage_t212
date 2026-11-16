"""Unit tests verifying broker P&L synchronization and scale accuracy."""

import unittest
from unittest.mock import MagicMock
from trading_system.data.leveraged_universe import LeveragedUniverse, normalize_quote_to_eur
from trading_system.data.trading212_client import Position
from trading_system.dashboard.server import build_dashboard_data


class TestBrokerPnLSync(unittest.TestCase):
    def test_normalize_quote_to_eur_preserves_sign(self):
        """Verify negative P&L amounts are properly normalized with sign preserved."""
        # EUR
        self.assertAlmostEqual(normalize_quote_to_eur("3LMOp_EQ", -1.88, "EUR"), -1.88, places=2)
        self.assertAlmostEqual(normalize_quote_to_eur("3LMOp_EQ", 1.88, "EUR"), 1.88, places=2)
        # USD
        self.assertAlmostEqual(normalize_quote_to_eur("GPTSl_EQ", -1.085, "USD"), -1.0, places=2)
        self.assertAlmostEqual(normalize_quote_to_eur("GPTSl_EQ", 1.085, "USD"), 1.0, places=2)
        # GBX (pence)
        self.assertAlmostEqual(normalize_quote_to_eur("3LAZl_EQ", -100.0, "GBX"), -1.204, places=3)
        self.assertAlmostEqual(normalize_quote_to_eur("3LAZl_EQ", 100.0, "GBX"), 1.204, places=3)

    def test_moderna_pnl_calculation_matches_broker(self):
        """Verify Moderna (3LMOp_EQ) computes +€1.88 (+0.87%) instead of +€66.31 (+43.57%)."""
        mock_client = MagicMock()
        mock_client.get_account_cash.return_value = MagicMock(total=10000.0)
        mock_client.get_orders.return_value = []
        mock_client.get_portfolio.return_value = [
            Position(
                ticker="3LMOp_EQ",
                quantity=1.0,
                average_price=216.10,
                current_price=217.98,
                ppl=1.88,
                max_buy=100.0,
                max_sell=1.0,
                currency="EUR",
            )
        ]

        data = build_dashboard_data(force=True, client=mock_client)
        signals = {s["ticker"]: s for s in data["active_signals"]}

        self.assertIn("3LMOp_EQ", signals)
        mod = signals["3LMOp_EQ"]
        self.assertEqual(mod["entry_price"], 216.10)
        self.assertEqual(mod["current_price"], 217.98)
        self.assertEqual(mod["pnl_amount_eur"], 1.88)
        self.assertAlmostEqual(mod["pnl_pct"], 0.87, places=2)
        self.assertEqual(mod["quantity"], 1.0)

    def test_gbx_position_pnl_synchronization(self):
        """Verify GBX positions calculate pnl_pct against EUR cost basis cleanly."""
        mock_client = MagicMock()
        mock_client.get_account_cash.return_value = MagicMock(total=10000.0)
        mock_client.get_orders.return_value = []
        mock_client.get_portfolio.return_value = [
            Position(
                ticker="3LAZl_EQ",
                quantity=4.0,
                average_price=3280.0,
                current_price=3279.997,
                ppl=-0.27,
                max_buy=100.0,
                max_sell=4.0,
                currency="GBX",
            )
        ]

        data = build_dashboard_data(force=True, client=mock_client)
        signals = {s["ticker"]: s for s in data["active_signals"]}

        self.assertIn("3LAZl_EQ", signals)
        laz = signals["3LAZl_EQ"]
        self.assertEqual(laz["pnl_amount_eur"], -0.27)
        # Cost basis in EUR = (3280 / 100 * 1.204) * 4 = 157.96 EUR
        # -0.27 / 157.96 * 100 = -0.17%
        self.assertAlmostEqual(laz["pnl_pct"], -0.17, places=2)

    def test_orchestrator_snapshot_broker_sync(self):
        """Verify orchestrator._build_active_signals_snapshot syncs broker positions and PnL."""
        from trading_system.orchestrator import Orchestrator
        from trading_system.config_bus import ConfigSnapshot

        orch = MagicMock(spec=Orchestrator)
        orch.leveraged_universe = LeveragedUniverse()

        cfg_snap = ConfigSnapshot()

        pos_rows = [
            (
                "3LMOp_EQ",
                {
                    "underlying": "MRNA",
                    "direction": "LONG",
                    "entry_price": 151.67,  # Stale/uncalibrated entry
                    "current_price": 217.98,
                    "original_quantity": 1.0,
                    "remaining_quantity": 1.0,
                    "stop_loss": 140.0,
                    "trailing_stop_loss": 140.0,
                },
            )
        ]
        broker_map = {
            "3LMOp_EQ": Position(
                ticker="3LMOp_EQ",
                quantity=1.0,
                average_price=216.10,
                current_price=217.98,
                ppl=1.88,
                max_buy=100.0,
                max_sell=1.0,
                currency="EUR",
            )
        }

        signals, behind, unprot = Orchestrator._build_active_signals_snapshot(
            orch, pos_rows, {"3LMOp_EQ": 217.98}, cfg_snap, broker_positions_map=broker_map
        )

        self.assertEqual(len(signals), 1)
        mod = signals[0]
        self.assertEqual(mod["entry_price"], 216.10)
        self.assertEqual(mod["current_price"], 217.98)
        self.assertEqual(mod["pnl_amount_eur"], 1.88)
        self.assertAlmostEqual(mod["pnl_pct"], 0.87, places=2)


if __name__ == "__main__":
    unittest.main()
