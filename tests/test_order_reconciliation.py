"""Unit tests for Broker Pre-Flight Order Reconciliation and In-Flight Deduplication."""

import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

from trading_system.orchestrator import Orchestrator
from trading_system.agents.risk_manager import DecisionCard


class TestBrokerOrderReconciliation(unittest.TestCase):
    """Test suite for pre-flight order queue reconciliation and deduplication."""

    def setUp(self) -> None:
        self.mock_t212 = MagicMock()
        self.mock_notifier = MagicMock()
        self.mock_pos_monitor = MagicMock()
        self.mock_pos_monitor.positions = {}
        self.mock_pos_monitor.is_system_halted.return_value = False
        self.mock_pos_monitor.is_halted = False

        self.orchestrator = Orchestrator(
            t212_client=self.mock_t212,
            notifier=self.mock_notifier,
            position_monitor=self.mock_pos_monitor,
            dry_run=False,
        )

    def test_stale_order_closed_market_cancelled(self) -> None:
        """Market order resting while market is closed is purged."""
        self.mock_t212.get_orders.return_value = [
            {
                "id": "ord_closed_1",
                "ticker": "3ABEl_EQ",
                "type": "MARKET",
                "status": "NEW",
                "creationTime": datetime.now(timezone.utc).isoformat(),
            }
        ]
        self.mock_t212.is_market_open.return_value = False
        self.mock_t212.cancel_order.return_value = True

        self.orchestrator.reconcile_pending_orders()

        self.mock_t212.cancel_order.assert_called_once_with("ord_closed_1")
        self.assertNotIn("3ABEl_EQ", self.orchestrator.in_flight_tickers)

    def test_stale_order_age_exceeded_cancelled(self) -> None:
        """Market order older than 300 seconds is purged as stale."""
        old_time = datetime.now(timezone.utc) - timedelta(seconds=450)
        self.mock_t212.get_orders.return_value = [
            {
                "id": "ord_old_2",
                "ticker": "BTC3EURs_EQ",
                "type": "MARKET",
                "status": "NEW",
                "creationTime": old_time.isoformat(),
            }
        ]
        self.mock_t212.is_market_open.return_value = True
        self.mock_t212.cancel_order.return_value = True

        self.orchestrator.reconcile_pending_orders()

        self.mock_t212.cancel_order.assert_called_once_with("ord_old_2")
        self.assertNotIn("BTC3EURs_EQ", self.orchestrator.in_flight_tickers)

    def test_fresh_active_order_tracked_in_flight(self) -> None:
        """Active order within 300s during open market is kept and tracked in-flight."""
        fresh_time = datetime.now(timezone.utc) - timedelta(seconds=45)
        self.mock_t212.get_orders.return_value = [
            {
                "id": "ord_fresh_3",
                "ticker": "QQQ3_EQ",
                "type": "MARKET",
                "status": "NEW",
                "creationTime": fresh_time.isoformat(),
            }
        ]
        self.mock_t212.is_market_open.return_value = True

        self.orchestrator.reconcile_pending_orders()

        self.mock_t212.cancel_order.assert_not_called()
        self.assertIn("QQQ3_EQ", self.orchestrator.in_flight_tickers)

    def test_in_flight_deduplication_blocks_duplicate_order(self) -> None:
        """Duplicate entry order is rejected if ticker is already in-flight."""
        self.orchestrator.in_flight_tickers.add("BTC3EURs_EQ")

        card = DecisionCard(
            ticker="BTC",
            trading212_ticker="BTC3EURs_EQ",
            direction="LONG",
            decision="BUY_ENTRY",
            entry_price=10.0,
            stop_loss=9.5,
            trailing_stop_loss=9.5,
            stop_distance_pct=0.05,
            take_profit_1=10.5,
            take_profit_2=11.0,
            take_profit_3=11.5,
            position_size_shares=5.0,
            position_notional=50.0,
            risk_amount=2.5,
            risk_pct_of_equity=0.01,
            conviction_score=0.85,
            rationale="Test in-flight deduplication",
        )

        executed = self.orchestrator.execute_trade(card)
        self.assertFalse(executed)
        self.mock_t212.place_market_order.assert_not_called()

    def test_active_position_deduplication_blocks_duplicate_order(self) -> None:
        """Duplicate entry order is rejected if ticker is already an active position."""
        self.mock_pos_monitor.positions["QQQ3_EQ"] = MagicMock()

        card = DecisionCard(
            ticker="QQQ",
            trading212_ticker="QQQ3_EQ",
            direction="LONG",
            decision="BUY_ENTRY",
            entry_price=100.0,
            stop_loss=95.0,
            trailing_stop_loss=95.0,
            stop_distance_pct=0.05,
            take_profit_1=105.0,
            take_profit_2=110.0,
            take_profit_3=115.0,
            position_size_shares=2.0,
            position_notional=200.0,
            risk_amount=10.0,
            risk_pct_of_equity=0.01,
            conviction_score=0.88,
            rationale="Test position deduplication",
        )

        executed = self.orchestrator.execute_trade(card)
        self.assertFalse(executed)
        self.mock_t212.place_market_order.assert_not_called()


if __name__ == "__main__":
    unittest.main()
