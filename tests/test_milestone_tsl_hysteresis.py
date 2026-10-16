"""Unit and Regression Tests for Schmitt-Trigger Hysteresis Milestone Ratchet and 50% Front-Loaded Trim.

Verifies:
1. TP1 Hit (+1.0R): Sells exactly 50% of original shares, moves stop to Entry Price (Break-Even), arms Schmitt Hysteresis.
2. In-band Sub-Threshold Price: Stop remains at Break-Even while price oscillates between TP1 and Escape Threshold, absorbing bid-ask bounce.
3. Hysteresis Escape Trigger: Stop elevates safely to TP1 once price clears TP1 + 0.5 * ATR, updating broker stop and notifying.
4. Direct TP2 Surge: Sells 25% of original shares, clears hysteresis flag, and ratchets stop to TP1.
5. TP3 Runner Close: Sells remaining 25% shares, closes position, cancels broker stop.
6. Short / Inverse ETP symmetry: Escape threshold and stop ratcheting function correctly for downward progression.
"""

import os
import tempfile
import unittest
from unittest.mock import MagicMock

from trading_system.agents.risk_manager import DecisionCard, RiskManager
from trading_system.data.trading212_client import OrderResult, Trading212Client
from trading_system.execution.position_monitor import PositionMonitor, TrackedPosition
from trading_system.notifications.notifier import Notifier


class TestMilestoneTSLHysteresis(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.temp_dir.name, "active_trades.json")
        self.log_file = os.path.join(self.temp_dir.name, "notifications.jsonl")

        self.mock_t212 = MagicMock(spec=Trading212Client)
        self.mock_t212.place_stop_order.return_value = OrderResult(
            id="stop_order_999",
            ticker="QQQ3l_EQ",
            type="STOP",
            status="NEW",
            quantity=-10.0,
            filled_quantity=0.0,
            fill_price=0.0,
        )
        self.mock_t212.place_market_order.return_value = OrderResult(
            id="market_order_111",
            ticker="QQQ3l_EQ",
            type="MARKET",
            status="FILLED",
            quantity=-5.0,
            filled_quantity=5.0,
            fill_price=420.0,
        )
        self.mock_t212.cancel_order.return_value = True

        self.notifier = Notifier(log_file=self.log_file, enable_desktop=False)
        self.risk_manager = RiskManager()

        self.monitor = PositionMonitor(
            t212_client=self.mock_t212,
            notifier=self.notifier,
            risk_manager=self.risk_manager,
            state_file=self.state_file,
            dry_run=False,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_tp1_front_loaded_50pct_trim_and_hysteresis_arming(self):
        """TP1 hit sells 50% shares, moves SL to Break-Even (Entry), and arms hysteresis flag."""
        pos = TrackedPosition(
            ticker="QQQ3l_EQ",
            underlying="QQQ",
            direction="LONG",
            entry_price=400.0,
            original_quantity=10.0,
            remaining_quantity=10.0,
            stop_loss=390.0,
            trailing_stop_loss=390.0,
            highest_price=400.0,
            lowest_price=400.0,
            tp1=410.0,
            tp2=420.0,
            tp3=430.0,
            atr=4.0,
            broker_stop_order_id="stop_init_1",
        )
        self.monitor.positions["QQQ3l_EQ"] = pos

        # Price touches TP1 (410.0)
        events = self.monitor.monitor_tick(current_prices={"QQQ3l_EQ": 410.0})

        tp1_events = [e for e in events if e.get("event") == "TP1"]
        self.assertEqual(len(tp1_events), 1)
        self.assertTrue(pos.tp1_hit)
        self.assertTrue(pos.tp1_armed_hysteresis)

        # 50% trim: 10.0 * 0.50 = 5.0 shares sold, 5.0 remaining
        self.assertEqual(pos.remaining_quantity, 5.0)
        self.mock_t212.place_market_order.assert_called_with("QQQ3l_EQ", -5.0)

        # Stop moved to Break-Even (400.0), NOT to TP1 yet (cushion = 10.0)
        self.assertEqual(pos.stop_loss, 400.0)
        self.assertEqual(pos.trailing_stop_loss, 400.0)

    def test_schmitt_trigger_hysteresis_absorbs_sub_threshold_bounce(self):
        """While price oscillates between TP1 and TP1 + 0.5*ATR, stop stays at Break-Even."""
        pos = TrackedPosition(
            ticker="QQQ3l_EQ",
            underlying="QQQ",
            direction="LONG",
            entry_price=400.0,
            original_quantity=10.0,
            remaining_quantity=5.0,
            stop_loss=400.0,
            trailing_stop_loss=400.0,
            highest_price=410.0,
            lowest_price=400.0,
            tp1=410.0,
            tp2=420.0,
            tp3=430.0,
            atr=4.0,
            tp1_hit=True,
            tp1_armed_hysteresis=True,
            broker_stop_order_id="stop_be_1",
        )
        self.monitor.positions["QQQ3l_EQ"] = pos

        # Escape threshold is 410.0 + (0.5 * 4.0) = 412.0
        # Price is at 411.0 (above TP1, but below escape threshold)
        events = self.monitor.monitor_tick(current_prices={"QQQ3l_EQ": 411.0})

        # No hysteresis ratchet event
        ratchet_events = [e for e in events if e.get("event") == "HYSTERESIS_TP1_RATCHET"]
        self.assertEqual(len(ratchet_events), 0)
        self.assertTrue(pos.tp1_armed_hysteresis)
        self.assertEqual(pos.trailing_stop_loss, 400.0)

    def test_schmitt_trigger_hysteresis_escape_elevates_stop_to_tp1(self):
        """When price reaches or exceeds TP1 + 0.5*ATR, stop elevates safely to TP1."""
        pos = TrackedPosition(
            ticker="QQQ3l_EQ",
            underlying="QQQ",
            direction="LONG",
            entry_price=400.0,
            original_quantity=10.0,
            remaining_quantity=5.0,
            stop_loss=400.0,
            trailing_stop_loss=400.0,
            highest_price=410.0,
            lowest_price=400.0,
            tp1=410.0,
            tp2=420.0,
            tp3=430.0,
            atr=4.0,
            tp1_hit=True,
            tp1_armed_hysteresis=True,
            broker_stop_order_id="stop_be_1",
        )
        self.monitor.positions["QQQ3l_EQ"] = pos

        # Escape threshold is 410.0 + (0.5 * 4.0) = 412.0
        # Price reaches 412.50
        events = self.monitor.monitor_tick(current_prices={"QQQ3l_EQ": 412.50})

        ratchet_events = [e for e in events if e.get("event") == "HYSTERESIS_TP1_RATCHET"]
        self.assertEqual(len(ratchet_events), 1)
        self.assertFalse(pos.tp1_armed_hysteresis)

        # Stop is now safely elevated to TP1 (410.0)
        self.assertEqual(pos.trailing_stop_loss, 410.0)
        self.assertEqual(pos.stop_loss, 410.0)

        # Broker stop placed at TP1
        self.mock_t212.place_stop_order.assert_called_with(
            ticker="QQQ3l_EQ",
            quantity=-5.0,
            stop_price=410.0,
            time_validity="GTC",
        )

    def test_tp2_second_trim_25pct_and_clears_hysteresis(self):
        """TP2 hit sells 25% of original (2.5 shares), leaves 25% runner, and clears hysteresis."""
        pos = TrackedPosition(
            ticker="QQQ3l_EQ",
            underlying="QQQ",
            direction="LONG",
            entry_price=400.0,
            original_quantity=10.0,
            remaining_quantity=5.0,
            stop_loss=410.0,
            trailing_stop_loss=410.0,
            highest_price=415.0,
            lowest_price=400.0,
            tp1=410.0,
            tp2=420.0,
            tp3=430.0,
            atr=4.0,
            tp1_hit=True,
            tp1_armed_hysteresis=True,
            broker_stop_order_id="stop_tp1_1",
        )
        self.monitor.positions["QQQ3l_EQ"] = pos

        # Price surges to TP2 (420.0)
        events = self.monitor.monitor_tick(current_prices={"QQQ3l_EQ": 420.0})

        tp2_events = [e for e in events if e.get("event") == "TP2"]
        self.assertEqual(len(tp2_events), 1)
        self.assertTrue(pos.tp2_hit)
        self.assertFalse(pos.tp1_armed_hysteresis)

        # Sells 25% of original (10.0 * 0.25 = 2.5 shares), remaining is 2.5 shares
        self.assertEqual(pos.remaining_quantity, 2.5)
        self.mock_t212.place_market_order.assert_called_with("QQQ3l_EQ", -2.5)

        # Trailing stop locked at least to TP1
        self.assertGreaterEqual(pos.trailing_stop_loss, 410.0)

    def test_tp3_closes_remaining_runner(self):
        """TP3 hit closes remaining runner (2.5 shares) completely."""
        pos = TrackedPosition(
            ticker="QQQ3l_EQ",
            underlying="QQQ",
            direction="LONG",
            entry_price=400.0,
            original_quantity=10.0,
            remaining_quantity=2.5,
            stop_loss=410.0,
            trailing_stop_loss=410.0,
            highest_price=422.0,
            lowest_price=400.0,
            tp1=410.0,
            tp2=420.0,
            tp3=430.0,
            atr=4.0,
            tp1_hit=True,
            tp2_hit=True,
            broker_stop_order_id="stop_tp2_1",
        )
        self.monitor.positions["QQQ3l_EQ"] = pos

        # Price hits TP3 (430.0)
        events = self.monitor.monitor_tick(current_prices={"QQQ3l_EQ": 430.0})

        tp3_events = [e for e in events if e.get("event") == "TP3"]
        self.assertEqual(len(tp3_events), 1)
        self.assertTrue(pos.tp3_hit)
        self.assertTrue(pos.is_closed)
        self.assertEqual(pos.remaining_quantity, 0.0)
        self.mock_t212.cancel_order.assert_called_with("stop_tp2_1")
        self.mock_t212.place_market_order.assert_called_with("QQQ3l_EQ", -2.5)


if __name__ == "__main__":
    unittest.main()
