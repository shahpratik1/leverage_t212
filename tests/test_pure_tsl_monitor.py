"""Unit tests for 100% Pure Trailing Stop Loss (TSL) Mode.

Verifies:
1. Pure TSL mode keeps 100% of shares intact (no partial trims at TP1/TP2/TP3).
2. Progressive Breakeven lock: at +1.5% profit, stop ratchets to entry price.
3. Progressive Cushion lock: at +3.0% profit, stop ratchets to entry + 1.5%.
4. Continuous ATR trailing stop ratchet above +3.0% behind new peak prices.
5. Every ratchet updates the native GTC Stop order directly on Trading 212.
6. Zero broker TP Limit order contention: sync_broker_orders only places stops.
7. 16:20 UTC Asymmetric Session: Longs with Breakeven/Cushion locked are eligible overnight.
8. Exit on TSL hit liquidates 100% of shares and cancels the broker stop order.
"""

import os
import tempfile
import unittest
from unittest.mock import MagicMock

from trading_system.agents.risk_manager import DecisionCard, RiskManager
from trading_system.data.trading212_client import OrderResult, Trading212Client
from trading_system.execution.position_monitor import PositionMonitor, TrackedPosition
from trading_system.notifications.notifier import Notifier


class TestPureTSLMonitor(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.temp_dir.name, "active_trades.json")
        self.log_file = os.path.join(self.temp_dir.name, "notifications.jsonl")

        self.mock_t212 = MagicMock(spec=Trading212Client)
        self.mock_t212.place_stop_order.return_value = OrderResult(
            id="stop_gtc_001",
            ticker="QQQ3l_EQ",
            type="STOP",
            status="NEW",
            quantity=-10.0,
            filled_quantity=0.0,
            fill_price=0.0,
        )
        self.mock_t212.place_market_order.return_value = OrderResult(
            id="market_sell_001",
            ticker="QQQ3l_EQ",
            type="MARKET",
            status="FILLED",
            quantity=-10.0,
            filled_quantity=10.0,
            fill_price=405.0,
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
            pure_tsl_mode=True,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_pure_tsl_maintains_100pct_position_on_profit_surge(self):
        """Pure TSL does NOT trim 50% or 25% at traditional TP targets."""
        pos = TrackedPosition(
            ticker="QQQ3l_EQ",
            underlying="QQQ",
            direction="LONG",
            entry_price=400.0,
            original_quantity=10.0,
            remaining_quantity=10.0,
            stop_loss=380.0,
            trailing_stop_loss=380.0,
            highest_price=400.0,
            lowest_price=400.0,
            tp1=410.0,
            tp2=420.0,
            tp3=430.0,
            atr=5.0,
            broker_stop_order_id="stop_initial",
        )
        self.monitor.positions["QQQ3l_EQ"] = pos

        # Price advances through TP1, TP2, and TP3
        events1 = self.monitor.monitor_tick(current_prices={"QQQ3l_EQ": 410.0})
        self.assertEqual(pos.remaining_quantity, 10.0)  # 100% position retained!
        self.assertFalse(pos.is_closed)

        events2 = self.monitor.monitor_tick(current_prices={"QQQ3l_EQ": 420.0})
        self.assertEqual(pos.remaining_quantity, 10.0)  # Still 100% intact!
        self.assertFalse(pos.is_closed)

        events3 = self.monitor.monitor_tick(current_prices={"QQQ3l_EQ": 435.0})
        self.assertEqual(pos.remaining_quantity, 10.0)  # Still 100% running!
        self.assertFalse(pos.is_closed)

        # No market sell orders were placed to trim shares
        self.mock_t212.place_market_order.assert_not_called()

    def test_progressive_stops_lock_breakeven_at_1_5_pct(self):
        """At +1.5% profit, stop ratchets to entry price and updates broker."""
        pos = TrackedPosition(
            ticker="QQQ3l_EQ",
            underlying="QQQ",
            direction="LONG",
            entry_price=400.0,
            original_quantity=10.0,
            remaining_quantity=10.0,
            stop_loss=380.0,
            trailing_stop_loss=380.0,
            highest_price=400.0,
            lowest_price=400.0,
            tp1=410.0,
            tp2=420.0,
            tp3=430.0,
            atr=5.0,
            broker_stop_order_id="stop_init",
        )
        self.monitor.positions["QQQ3l_EQ"] = pos

        # Price moves up +1.5% (400 * 1.015 = 406.0)
        events = self.monitor.monitor_tick(current_prices={"QQQ3l_EQ": 406.0})

        self.assertEqual(pos.trailing_stop_loss, 400.0)
        self.assertEqual(pos.stop_loss, 400.0)
        # Broker stop placed at entry price
        self.mock_t212.place_stop_order.assert_called_with(
            ticker="QQQ3l_EQ",
            quantity=-10.0,
            stop_price=400.0,
            time_validity="GOOD_TILL_CANCEL",
        )

    def test_progressive_stops_lock_cushion_at_3_0_pct(self):
        """At +3.0% profit, stop ratchets to entry + 1.5% cushion."""
        pos = TrackedPosition(
            ticker="QQQ3l_EQ",
            underlying="QQQ",
            direction="LONG",
            entry_price=400.0,
            original_quantity=10.0,
            remaining_quantity=10.0,
            stop_loss=400.0,
            trailing_stop_loss=400.0,
            highest_price=406.0,
            lowest_price=400.0,
            tp1=410.0,
            tp2=420.0,
            tp3=430.0,
            atr=5.0,
            broker_stop_order_id="stop_be",
        )
        self.monitor.positions["QQQ3l_EQ"] = pos

        # Price moves up +3.0% (400 * 1.03 = 412.0)
        events = self.monitor.monitor_tick(current_prices={"QQQ3l_EQ": 412.0})

        # Cushion = 400 * 1.015 = 406.0
        self.assertGreaterEqual(pos.trailing_stop_loss, 406.0)
        self.mock_t212.place_stop_order.assert_called_with(
            ticker="QQQ3l_EQ",
            quantity=-10.0,
            stop_price=pos.trailing_stop_loss,
            time_validity="GOOD_TILL_CANCEL",
        )

    def test_dynamic_atr_trail_as_price_extends(self):
        """As price rises significantly, ATR trailing stop trails behind peak."""
        pos = TrackedPosition(
            ticker="QQQ3l_EQ",
            underlying="QQQ",
            direction="LONG",
            entry_price=400.0,
            original_quantity=10.0,
            remaining_quantity=10.0,
            stop_loss=406.0,
            trailing_stop_loss=406.0,
            highest_price=412.0,
            lowest_price=400.0,
            tp1=410.0,
            tp2=420.0,
            tp3=430.0,
            atr=5.0,
            broker_stop_order_id="stop_cushion",
        )
        self.monitor.positions["QQQ3l_EQ"] = pos

        # Price surges to 440.0 (Peak = 440.0). Distance = max(1.2 * 5.0, 440.0 * 0.02) = max(6.0, 8.8) = 8.8 -> Stop = 431.20
        events = self.monitor.monitor_tick(current_prices={"QQQ3l_EQ": 440.0})

        self.assertEqual(pos.highest_price, 440.0)
        self.assertEqual(pos.trailing_stop_loss, 431.20)
        self.mock_t212.place_stop_order.assert_called_with(
            ticker="QQQ3l_EQ",
            quantity=-10.0,
            stop_price=431.20,
            time_validity="GOOD_TILL_CANCEL",
        )

    def test_pure_tsl_sync_orders_places_zero_tp_limits(self):
        """In pure TSL mode, sync_broker_orders only places stops, avoiding T212 400 order contention."""
        pos = TrackedPosition(
            ticker="QQQ3l_EQ",
            underlying="QQQ",
            direction="LONG",
            entry_price=400.0,
            original_quantity=10.0,
            remaining_quantity=10.0,
            stop_loss=380.0,
            trailing_stop_loss=380.0,
            highest_price=400.0,
            lowest_price=400.0,
            tp1=410.0,
            tp2=420.0,
            tp3=430.0,
            atr=5.0,
            broker_stop_order_id=None,
            broker_tp_order_id=None,
        )
        self.monitor.positions["QQQ3l_EQ"] = pos

        res = self.monitor.sync_broker_orders()
        self.assertIn("QQQ3l_EQ", res["stops"])
        self.assertEqual(res["tps"], [])  # Zero limit orders synced!
        self.mock_t212.place_limit_order.assert_not_called()

    def test_overnight_eligibility_with_breakeven_tsl(self):
        """Long position with TSL >= entry_price is retained overnight."""
        from datetime import datetime, timezone
        pos = TrackedPosition(
            ticker="QQQ3l_EQ",
            underlying="QQQ",
            direction="LONG",
            entry_price=400.0,
            original_quantity=10.0,
            remaining_quantity=10.0,
            stop_loss=400.0,
            trailing_stop_loss=400.0,  # Breakeven locked
            highest_price=408.0,
            lowest_price=400.0,
            tp1=410.0,
            tp2=420.0,
            tp3=430.0,
            atr=5.0,
        )
        self.monitor.positions["QQQ3l_EQ"] = pos

        cutoff = datetime(2026, 10, 20, 16, 22, tzinfo=timezone.utc)
        events = self.monitor.evaluate_asymmetric_session(current_time=cutoff, current_prices={"QQQ3l_EQ": 405.0})

        self.assertEqual(len(events), 0)
        self.assertTrue(pos.is_overnight_eligible)
        self.assertFalse(pos.is_closed)

    def test_overnight_liquidation_if_tsl_below_entry(self):
        """Long position with stop still below entry is flattened at 16:20 UTC."""
        from datetime import datetime, timezone
        pos = TrackedPosition(
            ticker="QQQ3l_EQ",
            underlying="QQQ",
            direction="LONG",
            entry_price=400.0,
            original_quantity=10.0,
            remaining_quantity=10.0,
            stop_loss=385.0,
            trailing_stop_loss=385.0,  # Below entry (at risk)
            highest_price=402.0,
            lowest_price=398.0,
            tp1=410.0,
            tp2=420.0,
            tp3=430.0,
            atr=5.0,
        )
        self.monitor.positions["QQQ3l_EQ"] = pos

        cutoff = datetime(2026, 10, 20, 16, 22, tzinfo=timezone.utc)
        events = self.monitor.evaluate_asymmetric_session(current_time=cutoff, current_prices={"QQQ3l_EQ": 401.0})

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "ASYMMETRIC_SESSION_EXIT")
        self.assertTrue(pos.is_closed)

    def test_full_liquidation_on_tsl_trigger(self):
        """When price retraces to trailing stop, liquidates 100% shares and cancels broker order."""
        import time
        pos = TrackedPosition(
            ticker="QQQ3l_EQ",
            underlying="QQQ",
            direction="LONG",
            entry_price=400.0,
            original_quantity=10.0,
            remaining_quantity=10.0,
            stop_loss=415.0,
            trailing_stop_loss=415.0,  # Ratcheted stop
            highest_price=425.0,
            lowest_price=400.0,
            tp1=410.0,
            tp2=420.0,
            tp3=430.0,
            atr=5.0,
            broker_stop_order_id="stop_gtc_415",
        )
        pos.entry_timestamp = time.time() - 300.0  # past grace period
        self.monitor.positions["QQQ3l_EQ"] = pos

        # Price drops to 415.0 (hits TSL)
        events = self.monitor.monitor_tick(current_prices={"QQQ3l_EQ": 415.0})

        stop_events = [e for e in events if e.get("event") == "STOP_EXIT"]
        self.assertEqual(len(stop_events), 1)
        self.assertTrue(stop_events[0]["is_tsl"])
        self.assertTrue(pos.is_closed)
        self.assertEqual(pos.remaining_quantity, 0.0)
        self.mock_t212.cancel_order.assert_called_with("stop_gtc_415")
        self.mock_t212.place_market_order.assert_called_with("QQQ3l_EQ", -10.0)


if __name__ == "__main__":
    unittest.main()
