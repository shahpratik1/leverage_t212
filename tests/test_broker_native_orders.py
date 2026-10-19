"""Tests for Broker-Native GTC Stop and Limit order placement and lifecycle synchronization."""

import unittest
from unittest.mock import MagicMock
from trading_system.execution.position_monitor import PositionMonitor, TrackedPosition
from trading_system.data.trading212_client import OrderResult


class TestBrokerNativeOrders(unittest.TestCase):
    def setUp(self):
        self.mock_t212 = MagicMock()
        self.mock_notifier = MagicMock()
        self.mock_rm = MagicMock()
        self.monitor = PositionMonitor(
            t212_client=self.mock_t212,
            notifier=self.mock_notifier,
            risk_manager=self.mock_rm,
            dry_run=False,
            state_file=":memory:",
        )
        # Avoid disk writes
        self.monitor._save_state = MagicMock()

    def test_place_broker_stop_order(self):
        """Verify placing server-side GTC stop loss order calls t212.place_stop_order."""
        self.mock_t212.place_stop_order.return_value = OrderResult(
            id="ORDER_STOP_123",
            ticker="QQQ3l_EQ",
            type="STOP",
            status="NEW",
            quantity=-10.0,
            filled_quantity=0.0,
            fill_price=0.0,
        )

        pos = TrackedPosition(
            ticker="QQQ3l_EQ",
            underlying="QQQ",
            direction="LONG",
            entry_price=100.0,
            original_quantity=10.0,
            remaining_quantity=10.0,
            stop_loss=95.0,
            trailing_stop_loss=95.0,
            highest_price=100.0,
            lowest_price=100.0,
            tp1=105.0,
            tp2=110.0,
            tp3=115.0,
            atr=2.5,
        )

        order_id = self.monitor.place_broker_stop(pos)
        self.assertEqual(order_id, "ORDER_STOP_123")
        self.assertEqual(pos.broker_stop_order_id, "ORDER_STOP_123")
        self.mock_t212.place_stop_order.assert_called_once_with(
            ticker="QQQ3l_EQ",
            quantity=-10.0,
            stop_price=95.0,
            time_validity="GTC",
        )

    def test_place_broker_tp_limit_order(self):
        """Verify placing server-side GTC limit order calls t212.place_limit_order."""
        self.mock_t212.place_limit_order.return_value = OrderResult(
            id="ORDER_LIMIT_456",
            ticker="QQQ3l_EQ",
            type="LIMIT",
            status="NEW",
            quantity=-5.0,
            filled_quantity=0.0,
            fill_price=0.0,
        )

        pos = TrackedPosition(
            ticker="QQQ3l_EQ",
            underlying="QQQ",
            direction="LONG",
            entry_price=100.0,
            original_quantity=10.0,
            remaining_quantity=10.0,
            stop_loss=95.0,
            trailing_stop_loss=95.0,
            highest_price=100.0,
            lowest_price=100.0,
            tp1=105.0,
            tp2=110.0,
            tp3=115.0,
            atr=2.5,
        )

        order_id = self.monitor.place_broker_tp_limit(pos)
        self.assertEqual(order_id, "ORDER_LIMIT_456")
        self.assertEqual(pos.broker_tp_order_id, "ORDER_LIMIT_456")
        self.mock_t212.place_limit_order.assert_called_once_with(
            ticker="QQQ3l_EQ",
            quantity=-5.0,
            limit_price=105.0,
            time_validity="GTC",
        )

    def test_cancel_all_broker_orders(self):
        """Verify cancel_all_broker_orders cancels both stop and TP limit orders."""
        self.mock_t212.cancel_order.return_value = True

        pos = TrackedPosition(
            ticker="QQQ3l_EQ",
            underlying="QQQ",
            direction="LONG",
            entry_price=100.0,
            original_quantity=10.0,
            remaining_quantity=10.0,
            stop_loss=95.0,
            trailing_stop_loss=95.0,
            highest_price=100.0,
            lowest_price=100.0,
            tp1=105.0,
            tp2=110.0,
            tp3=115.0,
            atr=2.5,
            broker_stop_order_id="STOP_001",
            broker_tp_order_id="LIMIT_002",
        )

        self.monitor.cancel_all_broker_orders(pos)
        self.assertIsNone(pos.broker_stop_order_id)
        self.assertIsNone(pos.broker_tp_order_id)
        self.assertEqual(self.mock_t212.cancel_order.call_count, 2)

    def test_dry_run_broker_orders(self):
        """In dry-run mode, simulated IDs are assigned without calling broker network."""
        monitor_dry = PositionMonitor(
            t212_client=self.mock_t212,
            notifier=self.mock_notifier,
            risk_manager=self.mock_rm,
            dry_run=True,
            state_file=":memory:",
        )
        monitor_dry._save_state = MagicMock()

        pos = TrackedPosition(
            ticker="3NVDl_EQ",
            underlying="NVDA",
            direction="LONG",
            entry_price=120.0,
            original_quantity=5.0,
            remaining_quantity=5.0,
            stop_loss=114.0,
            trailing_stop_loss=114.0,
            highest_price=120.0,
            lowest_price=120.0,
            tp1=126.0,
            tp2=132.0,
            tp3=138.0,
            atr=3.0,
        )

        stop_id = monitor_dry.place_broker_stop(pos)
        limit_id = monitor_dry.place_broker_tp_limit(pos)

        self.assertTrue(stop_id.startswith("DRY_STOP_"))
        self.assertTrue(limit_id.startswith("DRY_LIMIT_"))
        self.mock_t212.place_stop_order.assert_not_called()
        self.mock_t212.place_limit_order.assert_not_called()


if __name__ == "__main__":
    unittest.main()
