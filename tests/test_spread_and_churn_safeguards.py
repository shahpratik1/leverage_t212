"""Tests for anti-churn loss cooldown, spread-to-stop ratio gating, and entry grace period."""

import os
import tempfile
import time
import unittest
from unittest.mock import MagicMock

from trading_system.agents.risk_manager import DecisionCard, RiskManager
from trading_system.data.leveraged_universe import LeveragedUniverse
from trading_system.execution.position_monitor import PositionMonitor, TrackedPosition


class TestSpreadAndChurnSafeguards(unittest.TestCase):
    def setUp(self):
        self.risk_manager = RiskManager()
        self.mock_t212 = MagicMock()
        self.mock_notifier = MagicMock()
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.tmp_dir.name, "active_trades.json")
        self.monitor = PositionMonitor(
            t212_client=self.mock_t212,
            notifier=self.mock_notifier,
            risk_manager=self.risk_manager,
            state_file=self.state_file,
            dry_run=True,
        )

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_post_loss_anti_churn_cooldown(self):
        """Verify that registering a loss puts the ticker and underlying on cooldown, blocking immediate re-entry."""
        ticker = "3OILm_EQ"
        underlying = "USO"

        # Initially, not in cooldown
        in_cd, _ = self.risk_manager.is_in_loss_cooldown(ticker, underlying)
        self.assertFalse(in_cd)

        # Register loss exit
        self.risk_manager.register_loss_exit(ticker, underlying, cooldown_seconds=1800.0)

        # Now in cooldown for both
        in_cd, rem = self.risk_manager.is_in_loss_cooldown(ticker)
        self.assertTrue(in_cd)
        self.assertGreater(rem, 1700.0)

        in_cd_und, _ = self.risk_manager.is_in_loss_cooldown(underlying)
        self.assertTrue(in_cd_und)

        # Validate trade should fail
        card = DecisionCard(
            ticker=underlying,
            trading212_ticker=ticker,
            direction="LONG",
            decision="BUY_ENTRY",
            entry_price=12.0,
            stop_loss=11.2,
            trailing_stop_loss=11.2,
            stop_distance_pct=0.066,
            take_profit_1=12.8,
            take_profit_2=13.6,
            take_profit_3=14.4,
            position_size_shares=10.0,
            position_notional=120.0,
            risk_amount=8.0,
            risk_pct_of_equity=0.002,
            conviction_score=0.85,
            rationale="Test setup",
        )
        valid, reason = self.risk_manager.validate_trade(card, account_cash=5000.0, existing_positions=[])
        self.assertFalse(valid)
        self.assertIn("Anti-churn cooldown active", reason)

    def test_spread_to_stop_ratio_rejection(self):
        """Verify that a trade is rejected if spread exceeds 50% of the stop loss distance."""
        card = DecisionCard(
            ticker="USO",
            trading212_ticker="WTI3m_EQ",
            direction="LONG",
            decision="BUY_ENTRY",
            entry_price=12.71,
            stop_loss=11.87,
            trailing_stop_loss=11.87,
            stop_distance_pct=0.066,  # 6.6% stop
            take_profit_1=13.55,
            take_profit_2=14.39,
            take_profit_3=15.22,
            position_size_shares=1.0,
            position_notional=12.71,
            risk_amount=0.84,
            risk_pct_of_equity=0.001,
            conviction_score=0.85,
            rationale="Test",
        )
        # If spread is 0.32% (< 0.35% absolute cap, but > 50% of 0.60% stop = 0.30%), must be rejected
        card.stop_distance_pct = 0.006
        valid, reason = self.risk_manager.validate_trade(
            card, account_cash=5000.0, existing_positions=[], spread_pct=0.0032
        )
        self.assertFalse(valid)
        self.assertIn("vulnerable to immediate spread-stop whipsaw", reason)

        # If spread is tight (0.15% < 0.30%), passes
        valid_tight, _ = self.risk_manager.validate_trade(
            card, account_cash=5000.0, existing_positions=[], spread_pct=0.0015
        )
        self.assertTrue(valid_tight)

    def test_anti_bid_ask_bounce_grace_period(self):
        """Verify that monitor_tick absorbs initial resting bid spread during the first 120s without exiting."""
        pos = TrackedPosition(
            ticker="TEST3l_EQ",
            underlying="TEST",
            direction="LONG",
            entry_price=100.0,
            original_quantity=10.0,
            remaining_quantity=10.0,
            stop_loss=95.0,  # 5% SL
            trailing_stop_loss=95.0,
            tp1=105.0,
            tp2=110.0,
            tp3=115.0,
            highest_price=100.0,
            lowest_price=100.0,
            atr=2.5,
            scale_calibrated=True,
        )
        pos.entry_timestamp = time.time() - 30.0  # Entered 30 seconds ago (< 120s MIN_HOLD_SECONDS)
        self.monitor.positions["TEST3l_EQ"] = pos

        # Price is at 94.0 (resting bid right below 95.0 stop, drop = 6.0% < 15% catastrophic)
        events = self.monitor.monitor_tick(current_prices={"TEST3l_EQ": 94.0})

        # Grace period should absorb it, position should NOT be closed
        self.assertEqual(events, [])
        self.assertFalse(pos.is_closed)
        self.assertIn("TEST3l_EQ", self.monitor.positions)

        # Now simulate entry 150 seconds ago (> 120s MIN_HOLD_SECONDS)
        pos.entry_timestamp = time.time() - 150.0
        events_after = self.monitor.monitor_tick(current_prices={"TEST3l_EQ": 94.0})
        self.assertEqual(len(events_after), 1)
        self.assertEqual(events_after[0]["event"], "STOP_EXIT")
        self.assertTrue(pos.is_closed)

    def test_reconciliation_recalibrates_targets_on_scale_mismatch(self):
        """Verify that broker reconciliation recalculates tp1, tp2, tp3, and SL when entry scale differs by >5x."""
        pos = TrackedPosition(
            ticker="3LFEl_EQ",
            underlying="META",
            direction="LONG",
            entry_price=648.0,  # Stored underlying price
            original_quantity=1.0,
            remaining_quantity=1.0,
            stop_loss=605.0,
            trailing_stop_loss=605.0,
            tp1=708.0,
            tp2=767.0,
            tp3=826.0,
            highest_price=648.0,
            lowest_price=648.0,
            atr=26.0,
        )
        self.monitor.positions["3LFEl_EQ"] = pos

        # Broker reports real position at 28.38 EUR
        mock_pos = MagicMock()
        mock_pos.ticker = "3LFEl_EQ"
        mock_pos.quantity = 1.0
        mock_pos.average_price = 28.38
        mock_pos.current_price = 28.00

        self.mock_t212.get_portfolio.return_value = [mock_pos]
        self.mock_t212.get_orders.return_value = []

        summary = self.monitor.reconcile_with_broker()

        # Pos entry price should be corrected to 28.38
        self.assertAlmostEqual(pos.entry_price, 28.38, places=2)
        # Target TP1 must be scaled to ETP price (~30-32), NOT left at $708!
        self.assertLess(pos.tp1, 40.0)
        self.assertGreater(pos.tp1, 28.0)
        # Stop loss must be scaled to ETP price (~26-27), NOT left at $605!
        self.assertLess(pos.stop_loss, 28.0)
        self.assertGreater(pos.stop_loss, 20.0)

    def test_universe_wti_pair_resolution(self):
        """Verify that LeveragedUniverse resolves USO to 3OILm_EQ (liquid EUR) or WTI3l_EQ (USD)."""
        univ = LeveragedUniverse()
        pair = univ.get_pair_for_underlying("USO")
        self.assertIsNotNone(pair)
        self.assertEqual(pair.long_3x_ticker, "3OILm_EQ")


if __name__ == "__main__":
    unittest.main()
