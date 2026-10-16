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

    def test_calculate_escape_threshold_long_and_short(self):
        """RiskManager.calculate_escape_threshold computes correct buffers for LONG and SHORT."""
        # Long: 105.0 + 0.5 * 4.0 = 107.0
        esc_long = self.risk_manager.calculate_escape_threshold(tp1=105.0, atr=4.0, direction="LONG", multiplier=0.5)
        self.assertEqual(esc_long, 107.0)

        # Short: 95.0 - 0.5 * 4.0 = 93.0
        esc_short = self.risk_manager.calculate_escape_threshold(tp1=95.0, atr=4.0, direction="SHORT", multiplier=0.5)
        self.assertEqual(esc_short, 93.0)

    def test_short_symmetry_hysteresis_escape(self):
        """Short positions elevate stop downward to TP1 once price drops past escape threshold."""
        pos = TrackedPosition(
            ticker="QQQ3S_EQ",
            underlying="QQQ",
            direction="SHORT",
            entry_price=100.0,
            original_quantity=10.0,
            remaining_quantity=5.0,
            stop_loss=100.0,
            trailing_stop_loss=100.0,
            highest_price=100.0,
            lowest_price=95.0,
            tp1=95.0,
            tp2=90.0,
            tp3=85.0,
            atr=4.0,
            tp1_hit=True,
            tp1_armed_hysteresis=True,
            tp1_escape_threshold=93.0,
            broker_stop_order_id="stop_short_1",
        )
        self.monitor.positions["QQQ3S_EQ"] = pos

        # Price at 94.0 (below TP1 95.0, but hasn't cleared escape 93.0)
        events = self.monitor.monitor_tick(current_prices={"QQQ3S_EQ": 94.0})
        self.assertEqual(len([e for e in events if e.get("event") == "HYSTERESIS_TP1_RATCHET"]), 0)
        self.assertTrue(pos.tp1_armed_hysteresis)

        # Price drops to 92.50 (clears escape 93.0)
        events = self.monitor.monitor_tick(current_prices={"QQQ3S_EQ": 92.50})
        self.assertEqual(len([e for e in events if e.get("event") == "HYSTERESIS_TP1_RATCHET"]), 1)
        self.assertFalse(pos.tp1_armed_hysteresis)
        self.assertTrue(pos.tp1_stop_elevated)
        self.assertEqual(pos.stop_loss, 95.0)

    def test_pullback_to_breakeven_locks_in_net_profit(self):
        """Pullback to Break-Even post-TP1 exits remaining shares at $0 loss, leaving net +0.50R profit."""
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
        )
        self.monitor.positions["QQQ3l_EQ"] = pos

        # 1. Price hits TP1 (410.0): Sells 50% (5.0 shares), banks $50 profit
        events1 = self.monitor.monitor_tick(current_prices={"QQQ3l_EQ": 410.0})
        tp1_event = [e for e in events1 if e.get("event") == "TP1"][0]
        self.assertEqual(tp1_event["pnl"], 50.0)
        self.assertEqual(pos.remaining_quantity, 5.0)
        self.assertEqual(pos.stop_loss, 400.0)

        # 2. Price falls back to entry price Break-Even (400.0) before escape threshold is reached
        events2 = self.monitor.monitor_tick(current_prices={"QQQ3l_EQ": 400.0})
        stop_event = [e for e in events2 if e.get("event") == "STOP_EXIT"][0]
        self.assertEqual(stop_event["pnl"], 0.0)  # Exited remaining 5 shares at $400 ($0 loss)
        self.assertTrue(pos.is_closed)
        self.assertEqual(pos.remaining_quantity, 0.0)

    def test_gap_surge_directly_to_tp2_handles_sequence_safely(self):
        """Single-tick gap surge to TP2 with tp1_hit=False triggers TP1 & TP2 sequence cleanly."""
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
            tp1_hit=False,
            tp2_hit=False,
        )
        self.monitor.positions["QQQ3l_EQ"] = pos

        # Price surges in a single tick past TP1 straight to TP2 (420.0)
        events = self.monitor.monitor_tick(current_prices={"QQQ3l_EQ": 420.0})

        event_names = [e.get("event") for e in events]
        self.assertIn("TP1", event_names)
        self.assertIn("TP2", event_names)

        # Trims 50% on TP1 and 25% on TP2 -> remaining is 2.5 shares
        self.assertEqual(pos.remaining_quantity, 2.5)
        self.assertTrue(pos.tp1_hit)
        self.assertTrue(pos.tp2_hit)
        # Hysteresis must be cleared and stop ratcheted to at least TP1
        self.assertFalse(pos.tp1_armed_hysteresis)
        self.assertGreaterEqual(pos.trailing_stop_loss, 410.0)
        self.assertGreaterEqual(pos.stop_loss, 410.0)

    def test_place_broker_stop_uses_negative_quantity_for_all_positions(self):
        """Protective broker stops sell held shares using negative quantity for both long and short."""
        # Long position
        pos_long = TrackedPosition(
            ticker="QQQ3l_EQ",
            underlying="QQQ",
            direction="LONG",
            entry_price=400.0,
            original_quantity=10.0,
            remaining_quantity=7.5,
            stop_loss=390.0,
            trailing_stop_loss=390.0,
            highest_price=400.0,
            lowest_price=400.0,
            tp1=410.0,
            tp2=420.0,
            tp3=430.0,
            atr=4.0,
        )
        self.monitor.place_broker_stop(pos_long, stop_price=390.0)
        self.mock_t212.place_stop_order.assert_called_with(
            ticker="QQQ3l_EQ",
            quantity=-7.5,
            stop_price=390.0,
            time_validity="GTC",
        )

        # Short position (cash inverse ETP)
        pos_short = TrackedPosition(
            ticker="QQQ3S_EQ",
            underlying="QQQ",
            direction="SHORT",
            entry_price=100.0,
            original_quantity=20.0,
            remaining_quantity=10.0,
            stop_loss=110.0,
            trailing_stop_loss=110.0,
            highest_price=100.0,
            lowest_price=100.0,
            tp1=95.0,
            tp2=90.0,
            tp3=85.0,
            atr=4.0,
        )
        self.monitor.place_broker_stop(pos_short, stop_price=110.0)
        self.mock_t212.place_stop_order.assert_called_with(
            ticker="QQQ3S_EQ",
            quantity=-10.0,
            stop_price=110.0,
            time_validity="GTC",
        )

    def test_tracked_position_hysteresis_state_serialization_round_trip(self):
        """TrackedPosition preserves hysteresis state across JSON serialization and load_state."""
        pos = TrackedPosition(
            ticker="3TSM_EQ",
            underlying="TSM",
            direction="LONG",
            entry_price=50.0,
            original_quantity=100.0,
            remaining_quantity=50.0,
            stop_loss=50.0,
            trailing_stop_loss=50.0,
            highest_price=53.0,
            lowest_price=50.0,
            tp1=52.5,
            tp2=55.0,
            tp3=57.5,
            atr=1.5,
            tp1_hit=True,
            tp1_armed_hysteresis=True,
            tp1_escape_threshold=53.25,
            tp1_stop_elevated=False,
        )
        self.monitor.positions["3TSM_EQ"] = pos
        self.monitor._save_state()

        # Load fresh state monitor from the same file
        fresh_monitor = PositionMonitor(
            t212_client=self.mock_t212,
            notifier=self.notifier,
            risk_manager=self.risk_manager,
            state_file=self.state_file,
            dry_run=False,
        )
        loaded = fresh_monitor.positions.get("3TSM_EQ")
        self.assertIsNotNone(loaded)
        self.assertTrue(loaded.tp1_armed_hysteresis)
        self.assertEqual(loaded.tp1_escape_threshold, 53.25)
        self.assertFalse(loaded.tp1_stop_elevated)
        self.assertTrue(loaded.tp1_hit)


if __name__ == "__main__":
    unittest.main()
