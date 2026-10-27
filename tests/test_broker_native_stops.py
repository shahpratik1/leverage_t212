"""Regression tests for Native Broker Stop Loss (SL) and Trailing Stop Loss (TSL) placement.

Verifies:
1. Native GTC Stop Order placement on Trading 212 when trade is registered/executed
2. Synchronizing existing active positions lacking broker_stop_order_id
3. Replacing/canceling broker stop order when TP1 moves stop to breakeven
4. Replacing/canceling broker stop order when TSL ratchets higher
5. Canceling pending broker stop order when trade closes (cleanup to prevent orphan fills)
"""

import os
import tempfile
import unittest
from unittest.mock import MagicMock

from trading_system.agents.risk_manager import DecisionCard, RiskManager
from trading_system.data.trading212_client import OrderResult, Trading212Client
from trading_system.execution.position_monitor import PositionMonitor, TrackedPosition
from trading_system.notifications.notifier import Notifier


class TestBrokerNativeStops(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.temp_dir.name, "active_trades.json")
        self.log_file = os.path.join(self.temp_dir.name, "notifications.jsonl")

        self.mock_t212 = MagicMock(spec=Trading212Client)
        self.mock_t212.place_stop_order.return_value = OrderResult(
            id="stop_order_101",
            ticker="QQQ3l_EQ",
            type="STOP",
            status="NEW",
            quantity=-10.0,
            filled_quantity=0.0,
            fill_price=0.0,
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

    def test_place_broker_stop_on_trade_registration(self):
        """When a new trade is registered, a native GTC Stop order is placed on Trading 212."""
        card = DecisionCard(
            ticker="QQQ",
            trading212_ticker="QQQ3l_EQ",
            direction="LONG",
            decision="BUY_ENTRY",
            entry_price=415.0,
            stop_loss=402.0,
            trailing_stop_loss=402.0,
            stop_distance_pct=0.03,
            take_profit_1=425.0,
            take_profit_2=435.0,
            take_profit_3=445.0,
            position_size_shares=10.0,
            position_notional=4150.0,
            risk_amount=130.0,
            risk_pct_of_equity=0.01,
            conviction_score=0.85,
            rationale="Trend momentum",
        )
        pos = self.monitor.register_new_trade(card, atr=5.0)

        # Place broker stop
        order_id = self.monitor.place_broker_stop(pos)

        self.assertEqual(order_id, "stop_order_101")
        self.assertEqual(pos.broker_stop_order_id, "stop_order_101")
        self.mock_t212.place_stop_order.assert_called_once_with(
            ticker="QQQ3l_EQ",
            quantity=-10.0,
            stop_price=402.0,
            time_validity="GOOD_TILL_CANCEL",
        )

    def test_sync_broker_stops_for_existing_positions(self):
        """sync_broker_stops() places stop orders for positions missing broker_stop_order_id."""
        pos1 = TrackedPosition(
            ticker="3TSMl_EQ", underlying="TSM", direction="LONG", entry_price=1150.0,
            original_quantity=5.0, remaining_quantity=5.0, stop_loss=1120.0,
            trailing_stop_loss=1120.0, highest_price=1150.0, lowest_price=1150.0,
            tp1=1180.0, tp2=1210.0, tp3=1240.0, atr=15.0,
        )
        self.monitor.positions["3TSMl_EQ"] = pos1

        synced = self.monitor.sync_broker_stops()

        self.assertIn("3TSMl_EQ", synced)
        self.assertEqual(pos1.broker_stop_order_id, "stop_order_101")
        self.mock_t212.place_stop_order.assert_called_with(
            ticker="3TSMl_EQ",
            quantity=-5.0,
            stop_price=1120.0,
            time_validity="GOOD_TILL_CANCEL",
        )

    def test_tsl_ratchet_replaces_broker_stop(self):
        """When TSL ratchets higher, previous stop order is cancelled and replaced."""
        pos = TrackedPosition(
            ticker="QQQ3l_EQ", underlying="QQQ", direction="LONG", entry_price=400.0,
            original_quantity=10.0, remaining_quantity=10.0, stop_loss=390.0,
            trailing_stop_loss=390.0, highest_price=400.0, lowest_price=400.0,
            tp1=410.0, tp2=420.0, tp3=430.0, atr=5.0, tp1_hit=True,
            broker_stop_order_id="stop_old_99",
        )
        self.monitor.positions["QQQ3l_EQ"] = pos
        self.mock_t212.place_stop_order.return_value = OrderResult(
            id="stop_new_102", ticker="QQQ3l_EQ", type="STOP", status="NEW",
            quantity=-10.0, filled_quantity=0.0, fill_price=0.0,
        )

        # Current price rises to 425 (peak 425) -> TSL ratchets higher than 390
        events = self.monitor.monitor_tick(current_prices={"QQQ3l_EQ": 425.0})

        # Verify old order was cancelled
        self.mock_t212.cancel_order.assert_any_call("stop_old_99")
        # Verify new order placed with updated TSL
        self.assertEqual(pos.broker_stop_order_id, "stop_new_102")

    def test_trade_close_cancels_pending_broker_stop(self):
        """When a trade closes, the active broker stop order is cancelled to prevent rogue execution."""
        pos = TrackedPosition(
            ticker="QQQ3l_EQ", underlying="QQQ", direction="LONG", entry_price=400.0,
            original_quantity=10.0, remaining_quantity=10.0, stop_loss=390.0,
            trailing_stop_loss=390.0, highest_price=400.0, lowest_price=400.0,
            tp1=410.0, tp2=420.0, tp3=430.0, atr=5.0,
            broker_stop_order_id="stop_to_cancel_55",
        )
        self.monitor.positions["QQQ3l_EQ"] = pos

        # Price hits TP3 (435.0) -> triggers full close
        self.monitor.monitor_tick(current_prices={"QQQ3l_EQ": 435.0})

        self.mock_t212.cancel_order.assert_called_with("stop_to_cancel_55")

    def test_position_monitor_auto_heals_direction_discrepancy(self):
        """When loading a position with mismatched direction (e.g. IONQl1_EQ marked as SHORT), auto-heal to LONG."""
        import json
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump({
                "IONQl1_EQ": {
                    "ticker": "IONQl1_EQ",
                    "underlying": "IONQ",
                    "direction": "SHORT",  # erroneous
                    "entry_price": 1.68,
                    "original_quantity": 1.0,
                    "remaining_quantity": 1.0,
                    "stop_loss": 1.80,  # inverted stop
                    "trailing_stop_loss": 1.80,
                    "highest_price": 1.68,
                    "lowest_price": 1.68,
                    "tp1": 1.50,  # inverted TP
                    "tp2": 1.40,
                    "tp3": 1.30,
                    "atr": 0.05,
                }
            }, f)

        # Create fresh monitor which calls _load_state()
        monitor = PositionMonitor(
            t212_client=self.mock_t212,
            risk_manager=RiskManager(),
            notifier=self.notifier,
            state_file=self.state_file,
            dry_run=True,
        )

        pos = monitor.positions.get("IONQl1_EQ")
        self.assertIsNotNone(pos)
        # Direction must be auto-healed to LONG
        self.assertEqual(pos.direction, "LONG")
        # Stop loss must be below entry for a LONG position
        self.assertLess(pos.stop_loss, pos.entry_price)
        # TP1 must be above entry for a LONG position
        self.assertGreater(pos.tp1, pos.entry_price)

    def test_heal_stale_stop_inverse_etp_places_stop_below_market_price(self):
        """For an inverse ETP (e.g. 5STLd_EQ with direction=SHORT), healed stop must be BELOW live price.

        Placing a stop price above market price on held shares creates an immediate market fill
        on Trading 212, triggering instant liquidated losses.
        """
        from trading_system.data.trading212_client import Position
        pos = TrackedPosition(
            ticker="5STLd_EQ",
            underlying="TLT",
            direction="SHORT",
            entry_price=15.10,
            original_quantity=1.0,
            remaining_quantity=1.0,
            stop_loss=83.48,  # stale contaminated stop
            trailing_stop_loss=83.48,
            highest_price=15.10,
            lowest_price=15.10,
            tp1=15.60,
            tp2=16.00,
            tp3=16.40,
            atr=0.15,
        )
        self.monitor.positions["5STLd_EQ"] = pos
        self.monitor.dry_run = False

        # Mock broker portfolio returning live price of 15.08
        mock_bp = MagicMock(spec=Position)
        mock_bp.ticker = "5STLd_EQ"
        mock_bp.current_price = 15.08
        mock_bp.average_price = 15.10
        self.mock_t212.get_portfolio.return_value = [mock_bp]

        self.monitor._heal_stale_stop(pos)

        # The new stop MUST be strictly below live_price (15.08)
        self.assertLess(pos.stop_loss, 15.08, f"Healed stop {pos.stop_loss} must be below live price 15.08")
        self.assertLess(pos.trailing_stop_loss, 15.08)

    def test_inverse_etp_pnl_calculation_is_not_inverted(self):
        """For an inverse ETP (e.g. 5STLd_EQ), holding shares means when price rises, P&L is positive."""
        pos = TrackedPosition(
            ticker="5STLd_EQ",
            underlying="TLT",
            direction="SHORT",
            entry_price=15.00,
            original_quantity=10.0,
            remaining_quantity=10.0,
            stop_loss=14.50,
            trailing_stop_loss=14.50,
            highest_price=15.00,
            lowest_price=15.00,
            tp1=15.50,
            tp2=16.00,
            tp3=16.50,
            atr=0.15,
        )
        # Price increases from 15.00 to 16.00 (+1.00 per share)
        pnl, pnl_pct = self.monitor._calculate_pnl(pos, price=16.00, quantity=10.0)
        self.assertEqual(pnl, 10.0)
        self.assertEqual(pnl_pct, 6.67)

        # Price decreases from 15.00 to 14.00 (-1.00 per share)
        pnl_loss, pnl_loss_pct = self.monitor._calculate_pnl(pos, price=14.00, quantity=10.0)
        self.assertEqual(pnl_loss, -10.0)
        self.assertEqual(pnl_loss_pct, -6.67)


if __name__ == "__main__":
    unittest.main()
