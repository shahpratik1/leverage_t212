"""Unit and Integration tests for Multi-Signal Rate Limiter & Concurrency Synchronization.

Verifies:
1. Non-blocking slot reservation: lock is released before sleeping so parallel endpoints do not freeze.
2. Optimistic cache update: market order immediately updates in-memory cash and portfolio without forcing 429-prone network roundtrips.
3. Stop/Cancel orders preserve cash cache: placing or canceling stop loss orders does not clear cash cache.
4. Cross-process trade active flag: warns daemons to yield bandwidth during active signal execution.
5. Orchestrator batch execution: paces orders, prioritizes highest conviction, and incrementally tracks capital.
6. Deliberation concurrency: uses single baseline equity to avoid concurrent /account/cash API hits.
"""

import os
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from trading_system.agents.risk_manager import DecisionCard
from trading_system.data.trading212_client import (
    AccountCash,
    OrderResult,
    Position,
    Trading212Client,
)
from trading_system.execution.position_monitor import PositionMonitor
from trading_system.notifications.notifier import Notifier
from trading_system.orchestrator import Orchestrator
from trading_system.screener.laya_screener import ScreenerResult


class TestMultiSignalRateLimiter(unittest.TestCase):
    def setUp(self):
        Trading212Client._global_next_allowed_time = 0.0
        Trading212Client._global_last_request_time = 0.0
        Trading212Client._endpoint_next_allowed_time.clear()
        self.client = Trading212Client(
            api_key="mock_key",
            base_url="https://demo.trading212.com/api/v0",
            rate_limit_delay=0.01,
        )

    def tearDown(self):
        Trading212Client._global_next_allowed_time = 0.0
        Trading212Client._global_last_request_time = 0.0
        Trading212Client._endpoint_next_allowed_time.clear()
        Trading212Client.set_trade_active(False)

    def test_optimistic_cache_update_post_market_order(self):
        """Placing a market order updates in-memory AccountCash and Position without wiping cache."""
        self.client._account_cash_cache = AccountCash(
            free=10000.0,
            invested=2000.0,
            total=12000.0,
            blocked=0.0,
            result=0.0,
            ppl_result=0.0,
        )
        self.client._account_cash_cache_time = time.time()
        self.client._portfolio_cache = [
            Position(
                ticker="3APLl_EQ",
                quantity=10.0,
                average_price=100.0,
                current_price=100.0,
                ppl=0.0,
                max_buy=1000.0,
                max_sell=1000.0,
            )
        ]
        self.client._portfolio_cache_time = time.time()

        # Mock _request to return a filled order result
        mock_order_response = {
            "id": "ORD_123",
            "ticker": "3APLl_EQ",
            "type": "MARKET",
            "status": "FILLED",
            "quantity": 5.0,
            "filledQuantity": 5.0,
            "fillPrice": 105.0,
        }
        with patch.object(self.client, "_request", return_value=mock_order_response):
            res = self.client.place_market_order("3APLl_EQ", 5.0)

        self.assertEqual(res.id, "ORD_123")
        # Cash should be optimistically deducted: 10000 - (5.0 * 105.0) = 9475.0
        self.assertIsNotNone(self.client._account_cash_cache)
        self.assertAlmostEqual(self.client._account_cash_cache.free, 9475.0)
        self.assertAlmostEqual(self.client._account_cash_cache.invested, 2525.0)

        # Portfolio should be updated: 10 + 5 = 15 shares
        self.assertIsNotNone(self.client._portfolio_cache)
        pos = next(p for p in self.client._portfolio_cache if p.ticker == "3APLl_EQ")
        self.assertEqual(pos.quantity, 15.0)

    def test_stop_order_and_cancel_preserve_cash_cache(self):
        """Placing a stop order or cancelling an order must NOT invalidate account cash or portfolio."""
        self.client._account_cash_cache = AccountCash(
            free=5000.0, invested=1000.0, total=6000.0, blocked=0.0, result=0.0, ppl_result=0.0
        )
        self.client._portfolio_cache = []
        self.client._orders_cache = [{"id": "ORD_OLD"}]

        with patch.object(self.client, "_request", return_value={"id": "STOP_1"}):
            self.client.place_stop_order("3APLl_EQ", -10.0, 95.0)

        # Cash and portfolio cache are preserved
        self.assertIsNotNone(self.client._account_cash_cache)
        self.assertEqual(self.client._account_cash_cache.free, 5000.0)
        self.assertIsNotNone(self.client._portfolio_cache)
        # Orders cache is invalidated
        self.assertIsNone(self.client._orders_cache)

        with patch.object(self.client, "_request", return_value={}):
            self.client.cancel_order("STOP_1")

        self.assertIsNotNone(self.client._account_cash_cache)
        self.assertIsNone(self.client._orders_cache)

    def test_cross_process_trade_active_coordination(self):
        """Test setting and reading cross-process trade active state."""
        Trading212Client.set_trade_active(True)
        self.assertTrue(Trading212Client.is_trade_active(window_seconds=10.0))

        Trading212Client.set_trade_active(False)
        self.assertFalse(Trading212Client.is_trade_active(window_seconds=10.0))

    def test_deep_analyse_uses_passed_baseline_equity(self):
        """Verify deep_analyse skips get_account_cash when account_equity is provided."""
        notifier = Notifier(enable_desktop=False)
        orchestrator = Orchestrator(dry_run=True, notifier=notifier)
        orchestrator.t212.get_account_cash = MagicMock()

        cand = ScreenerResult(
            ticker="AAPL",
            bias="bullish",
            bias_confidence=0.85,
            setup_score="favorable",
            setup_confidence=0.80,
            catalyst_risk=0.10,
            overall_confidence=0.82,
            metrics={"close": 200.0, "rsi": 55.0, "adx": 26.0, "atr": 4.0},
        )

        card = orchestrator.deep_analyse(cand, account_equity=25000.0)
        self.assertIsNotNone(card)
        # get_account_cash should NOT have been called because baseline equity was provided
        orchestrator.t212.get_account_cash.assert_not_called()

    def test_execute_trades_batch_conviction_priority_and_cash_tracking(self):
        """Verify execute_trades_batch prioritizes higher conviction and checks remaining cash."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_state = os.path.join(temp_dir, "active_trades.json")
            temp_log = os.path.join(temp_dir, "notifications.jsonl")
            temp_trades = os.path.join(temp_dir, "trades.jsonl")

            notifier = Notifier(log_file=temp_log, enable_desktop=False)
            orchestrator = Orchestrator(
                dry_run=True,
                notifier=notifier,
                trades_log=temp_trades,
            )
            # Ensure position monitor writes to temp state file
            orchestrator.position_monitor = PositionMonitor(
                t212_client=orchestrator.t212,
                notifier=orchestrator.notifier,
                risk_manager=orchestrator.risk_manager,
                state_file=temp_state,
                dry_run=True,
            )
            orchestrator.t212.rate_limit_delay = 0.01

            # Mock initial account cash: $3000 free
            orchestrator.t212.get_account_cash = MagicMock(return_value=AccountCash(
                free=3000.0, invested=1000.0, total=4000.0, blocked=0.0, result=0.0, ppl_result=0.0
            ))
            orchestrator.t212.get_portfolio = MagicMock(return_value=[])

            # Signal 1: Conviction 0.70, requires $2000
            card_low = DecisionCard(
                ticker="AAPL", trading212_ticker="3APLl_EQ", direction="LONG", decision="BUY_ENTRY",
                entry_price=100.0, stop_loss=95.0, trailing_stop_loss=95.0, stop_distance_pct=0.05,
                take_profit_1=105.0, take_profit_2=110.0, take_profit_3=115.0, position_size_shares=20.0,
                position_notional=2000.0, risk_amount=100.0, risk_pct_of_equity=0.01, conviction_score=0.70,
                rationale="Low conviction",
            )
            cand_low = ScreenerResult("AAPL", "bullish", 0.7, "fav", 0.7, 0.1, 0.7, {"atr": 2.0})

            # Signal 2: Conviction 0.95, requires $2000
            card_high = DecisionCard(
                ticker="NVDA", trading212_ticker="3NVDl_EQ", direction="LONG", decision="BUY_ENTRY",
                entry_price=100.0, stop_loss=95.0, trailing_stop_loss=95.0, stop_distance_pct=0.05,
                take_profit_1=105.0, take_profit_2=110.0, take_profit_3=115.0, position_size_shares=20.0,
                position_notional=2000.0, risk_amount=100.0, risk_pct_of_equity=0.01, conviction_score=0.95,
                rationale="High conviction",
            )
            cand_high = ScreenerResult("NVDA", "bullish", 0.95, "fav", 0.95, 0.1, 0.95, {"atr": 2.0})

            candidate_cards = [
                (cand_low, card_low),
                (cand_high, card_high),
            ]

            executed = orchestrator.execute_trades_batch(candidate_cards)

            # High conviction (NVDA) should execute first using $2000 of the $3000 cash.
            # Low conviction (AAPL) requires $2000, but only $1000 remains, so it should be rejected.
            self.assertEqual(len(executed), 1)
            self.assertEqual(executed[0].ticker, "NVDA")
            self.assertEqual(executed[0].conviction_score, 0.95)


if __name__ == "__main__":
    unittest.main()
