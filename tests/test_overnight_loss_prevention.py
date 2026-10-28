"""Unit tests for Overnight Gap and Loss Prevention:
1. MAX_SL_PCT (3.0% hard stop distance cap) enforcement.
2. Scaled Take-Profit targets aligned with tight stop boundaries.
3. Persistent 16:20 UTC Asymmetric Session evaluation (closing narrow 10m window blindspot).
4. FLATTEN_ALL_LEVERAGED_AT_SESSION_CLOSE policy.
"""

import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock

from trading_system.agents.risk_manager import DecisionCard, RiskManager
from trading_system.config import settings
from trading_system.execution.position_monitor import PositionMonitor, TrackedPosition


class TestOvernightLossPrevention(unittest.TestCase):
    def setUp(self):
        self.rm = RiskManager(
            max_risk_per_trade=0.015,
            max_position_pct=0.20,
            atr_sl_multiplier=2.2,
            min_sl_pct=0.005,
            max_sl_pct=0.03,
            tp1_r=1.0,
            tp2_r=2.0,
            tp3_r=3.0,
        )

    def test_stop_loss_clamped_to_max_sl_pct(self):
        """Even with high volatility / high ATR, stop distance must not exceed 3.0%."""
        entry_price = 100.0
        high_atr = 8.0  # 2.2 * 8 = 17.6 distance (17.6%), which is dangerous
        sl = self.rm.calculate_stop_loss(entry_price, high_atr, direction="LONG")

        # Must be strictly clamped to entry_price * (1 - 0.03) = 97.00
        self.assertEqual(sl, 97.00)
        dist_pct = (entry_price - sl) / entry_price
        self.assertAlmostEqual(dist_pct, 0.03, places=3)

    def test_short_etp_stop_loss_clamped_to_max_sl_pct(self):
        """Inverse ETP (spot buy) stop loss must also be clamped to at most 3.0% below entry."""
        entry_price = 50.0
        high_atr = 5.0  # 2.2 * 5 = 11.0 distance (22%)
        sl = self.rm.calculate_stop_loss(entry_price, high_atr, direction="SHORT_ETP")

        # Must be clamped to 50.0 * (1 - 0.03) = 48.50
        self.assertEqual(sl, 48.50)
        dist_pct = (entry_price - sl) / entry_price
        self.assertAlmostEqual(dist_pct, 0.03, places=3)

    def test_take_profits_aligned_with_3pct_risk(self):
        """With 3% risk, TP1 is +3%, TP2 is +6%, TP3 is +9%."""
        entry_price = 100.0
        sl = 97.0
        tp1, tp2, tp3 = self.rm.calculate_take_profits(entry_price, sl, direction="LONG")

        self.assertEqual(tp1, 103.0)
        self.assertEqual(tp2, 106.0)
        self.assertEqual(tp3, 109.0)

    def test_persistent_evacuation_catches_lingering_unhedged_outside_10m_window(self):
        """Monitor running after 16:30 UTC or next morning must catch unhedged positions."""
        mock_t212 = MagicMock()
        mock_notifier = MagicMock()
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            f.write("{}")
            temp_path = f.name
        try:
            monitor = PositionMonitor(
                t212_client=mock_t212,
                notifier=mock_notifier,
                risk_manager=self.rm,
                state_file=temp_path,
                dry_run=True,
            )

            pos = TrackedPosition(
                ticker="3LFEl_EQ",
                underlying="META",
                direction="LONG",
                entry_price=28.38,
                original_quantity=1.0,
                remaining_quantity=1.0,
                stop_loss=27.53,
                trailing_stop_loss=27.53,
                highest_price=28.38,
                lowest_price=28.38,
                tp1=29.23,
                tp2=30.08,
                tp3=30.93,
                atr=0.5,
                tp1_hit=False,
                is_overnight_eligible=False,
            )
            monitor.positions["3LFEl_EQ"] = pos

            # Test at 17:15 UTC (outside the old 16:20-16:30 window)
            after_hours_time = datetime(2026, 10, 23, 17, 15, 0, tzinfo=timezone.utc)
            events = monitor.evaluate_asymmetric_session(
                current_time=after_hours_time,
                current_prices={"3LFEl_EQ": 28.20},
            )

            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["event"], "ASYMMETRIC_SESSION_EXIT")
            self.assertEqual(events[0]["ticker"], "3LFEl_EQ")
            self.assertTrue(pos.is_closed)
            self.assertNotIn("3LFEl_EQ", monitor.positions)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def test_flatten_all_leveraged_policy(self):
        """When FLATTEN_ALL_LEVERAGED_AT_SESSION_CLOSE is True, even positions with TP1 hit flatten."""
        mock_t212 = MagicMock()
        mock_notifier = MagicMock()
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            f.write("{}")
            temp_path = f.name
        try:
            monitor = PositionMonitor(
                t212_client=mock_t212,
                notifier=mock_notifier,
                risk_manager=self.rm,
                state_file=temp_path,
                dry_run=True,
            )

            pos = TrackedPosition(
                ticker="3SEMd_EQ",
                underlying="SOXX",
                direction="LONG",
                entry_price=70.0,
                original_quantity=1.0,
                remaining_quantity=0.5,
                stop_loss=70.0,
                trailing_stop_loss=70.0,
                highest_price=72.1,
                lowest_price=70.0,
                tp1=72.1,
                tp2=74.2,
                tp3=76.3,
                atr=1.0,
                tp1_hit=True,  # TP1 was hit
                is_overnight_eligible=True,
            )
            monitor.positions["3SEMd_EQ"] = pos

            cutoff_time = datetime(2026, 10, 23, 16, 22, 0, tzinfo=timezone.utc)
            from unittest.mock import patch
            with patch.object(settings, "FLATTEN_ALL_LEVERAGED_AT_SESSION_CLOSE", True):
                events = monitor.evaluate_asymmetric_session(
                    current_time=cutoff_time,
                    current_prices={"3SEMd_EQ": 71.5},
                )

            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["event"], "ASYMMETRIC_SESSION_EXIT")
            self.assertEqual(events[0]["ticker"], "3SEMd_EQ")
            self.assertTrue(pos.is_closed)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)


if __name__ == "__main__":
    unittest.main()
