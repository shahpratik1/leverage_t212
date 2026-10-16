"""Unit and Integration Tests for Asymmetric Session Protocol and Broker Rate Limiting.

Verifies:
- Thread-safe Trading 212 API request rate limiting (1.15s delay mutex)
- HTTP 429 exponential backoff and retry
- AE1: 3x Long position under TP1 flattens at 16:20 UTC (ASYMMETRIC_SESSION_EXIT)
- AE2: 3x Long position with TP1 hit retained overnight (is_overnight_eligible = True)
- AE2b: 3x Long position with earnings <= 24h flattens at 16:20 UTC (EARNINGS_BLACKOUT_EXIT)
- AE3: 3x Inverse Short position flattens unconditionally at 16:20 UTC (INVERSE_INTRADAY_FLATTEN)
- AE4: Serialized liquidation queue rate pacing
- Orchestrator 48h pre-trade earnings filter (R2.2)
"""

import concurrent.futures
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from trading_system.agents.risk_manager import DecisionCard, RiskManager
from trading_system.config import settings
from trading_system.data.trading212_client import (
    AccountCash,
    OrderResult,
    Position,
    Trading212Client,
    Trading212Error,
)
from trading_system.execution.position_monitor import PositionMonitor, TrackedPosition
from trading_system.notifications.notifier import Notifier
from trading_system.orchestrator import Orchestrator
from trading_system.screener.laya_screener import ScreenerResult


class TestTrading212RateLimiter(unittest.TestCase):
    """Verifies KTD1 / R3.2 / R3.3 thread-safe mutex and 429 backoff."""

    def setUp(self):
        Trading212Client._global_next_allowed_time = 0.0
        Trading212Client._global_last_request_time = 0.0
        Trading212Client._endpoint_next_allowed_time.clear()
        self.client = Trading212Client(
            api_key="mock_key",
            base_url="https://demo.trading212.com/api/v0",
        )
        self.client.rate_limit_delay = 0.05  # use 50ms for fast unit test execution

    def tearDown(self):
        Trading212Client._global_next_allowed_time = 0.0
        Trading212Client._global_last_request_time = 0.0
        Trading212Client._endpoint_next_allowed_time.clear()

    def test_thread_safe_rate_limiter_serialization(self):
        """Verify concurrent requests across worker threads serialize with rate_limit_delay."""
        timestamps = []

        def _mock_urlopen(req, timeout=30):
            timestamps.append(time.time())
            mock_resp = MagicMock()
            mock_resp.read.return_value = b'{"status": "ok"}'
            mock_resp.__enter__.return_value = mock_resp
            return mock_resp

        with patch("urllib.request.urlopen", side_effect=_mock_urlopen):
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                futures = [
                    executor.submit(self.client._request, "GET", f"/test/{i}")
                    for i in range(4)
                ]
                for f in futures:
                    f.result()

        self.assertEqual(len(timestamps), 4)
        # Check that consecutive requests are spaced by at least ~0.04s
        for i in range(1, len(timestamps)):
            diff = timestamps[i] - timestamps[i - 1]
            self.assertGreaterEqual(diff, 0.04)

    def test_429_exponential_backoff_and_retry(self):
        """Verify that HTTP 429 triggers exponential backoff and succeeds on subsequent try."""
        import io
        import urllib.error

        err_429 = urllib.error.HTTPError(
            url="https://demo.trading212.com/api/v0/test/rate-limit",
            code=429,
            msg="Too Many Requests",
            hdrs={},
            fp=io.BytesIO(b'{"message": "rate limit"}'),
        )

        mock_resp_200 = MagicMock()
        mock_resp_200.read.return_value = b'{"status": "recovered"}'
        mock_resp_200.__enter__.return_value = mock_resp_200

        with patch("urllib.request.urlopen", side_effect=[err_429, mock_resp_200]):
            with patch("time.sleep") as mock_sleep:
                result = self.client._request("GET", "/test/rate-limit")
                self.assertEqual(result, {"status": "recovered"})
                mock_sleep.assert_called()

    def test_close_position_method(self):
        """Verify close_position submits market sell order with negative quantity."""
        with patch.object(self.client, "place_market_order") as mock_order:
            mock_order.return_value = OrderResult(
                id="ord_1", ticker="QQQ3l_EQ", type="MARKET", status="FILLED",
                quantity=-10.0, filled_quantity=10.0, fill_price=420.0
            )
            res = self.client.close_position("QQQ3l_EQ", quantity=10.0)
            mock_order.assert_called_once_with("QQQ3l_EQ", -10.0)
            self.assertEqual(res.id, "ord_1")

    def test_429_retry_after_header_parsing(self):
        """Verify that HTTP 429 parses Retry-After header and waits the designated interval."""
        import io
        import urllib.error
        from email.message import Message

        msg = Message()
        msg["Retry-After"] = "4.5"

        err_429 = urllib.error.HTTPError(
            url="https://demo.trading212.com/api/v0/test/rate-limit",
            code=429,
            msg="Too Many Requests",
            hdrs=msg,
            fp=io.BytesIO(b'{"message": "rate limit"}'),
        )

        mock_resp_200 = MagicMock()
        mock_resp_200.read.return_value = b'{"status": "recovered"}'
        mock_resp_200.headers = {}
        mock_resp_200.__enter__.return_value = mock_resp_200

        with patch("urllib.request.urlopen", side_effect=[err_429, mock_resp_200]):
            with patch("time.sleep") as mock_sleep:
                result = self.client._request("GET", "/test/retry-after")
                self.assertEqual(result, {"status": "recovered"})
                sleep_calls = [c[0][0] for c in mock_sleep.call_args_list]
                self.assertTrue(any(s >= 4.5 for s in sleep_calls))

    def test_proactive_rate_limit_throttle(self):
        """Verify that x-ratelimit-remaining <= 1 proactively delays next request until reset."""
        from email.message import Message
        msg = Message()
        future_ts = time.time() + 0.1
        msg["x-ratelimit-remaining"] = "0"
        msg["x-ratelimit-reset"] = str(future_ts)

        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"data": "ok"}'
        mock_resp.headers = msg
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp):
            self.client._request("GET", "/test/proactive")
            self.assertGreaterEqual(Trading212Client._global_next_allowed_time, future_ts)

    def test_portfolio_and_cash_caching(self):
        """Verify that get_portfolio and get_account_cash use TTL cache to avoid redundant calls."""
        with patch.object(self.client, "_request") as mock_req:
            mock_req.return_value = [{"ticker": "QQQ3l_EQ", "quantity": 10.0, "currentPrice": 420.0}]
            res1 = self.client.get_portfolio()
            res2 = self.client.get_portfolio()
            self.assertEqual(mock_req.call_count, 1)
            self.assertEqual(len(res1), 1)
            self.assertEqual(len(res2), 1)

            res3 = self.client.get_portfolio(force_refresh=True)
            self.assertEqual(mock_req.call_count, 2)

    def test_cache_invalidation_on_order(self):
        """Verify placing an order invalidates the portfolio and cash cache."""
        self.client._portfolio_cache = [MagicMock()]
        self.client._account_cash_cache = MagicMock()
        with patch.object(self.client, "_request", return_value={"id": "ord_99", "status": "FILLED"}):
            self.client.place_market_order("QQQ3l_EQ", 5.0)
            self.assertIsNone(self.client._portfolio_cache)
            self.assertIsNone(self.client._account_cash_cache)

    def test_get_orders_returns_data(self):
        """Verify get_orders returns open orders on initial and cached calls."""
        with patch.object(self.client, "_request", return_value=[{"id": "ord_1", "status": "WORKING"}]):
            res1 = self.client.get_orders()
            self.assertEqual(len(res1), 1)
            self.assertEqual(res1[0]["id"], "ord_1")

            res2 = self.client.get_orders()
            self.assertEqual(len(res2), 1)
            self.assertEqual(res2[0]["id"], "ord_1")

    def test_endpoint_classification_and_delays(self):
        """Verify endpoint classification and official minimum pacing intervals."""
        self.assertEqual(self.client._classify_endpoint("GET", "/equity/metadata/instruments"), "metadata_instruments")
        self.assertEqual(self.client._classify_endpoint("GET", "/equity/metadata/exchanges"), "metadata_exchanges")
        self.assertEqual(self.client._classify_endpoint("GET", "/equity/account/cash"), "account_cash")
        self.assertEqual(self.client._classify_endpoint("GET", "/equity/account/summary"), "account_summary")
        self.assertEqual(self.client._classify_endpoint("GET", "/equity/orders"), "orders_query")
        self.assertEqual(self.client._classify_endpoint("POST", "/equity/orders/market"), "orders_mutation")
        self.assertEqual(self.client._classify_endpoint("DELETE", "/equity/orders/123"), "orders_mutation")
        self.assertEqual(self.client._classify_endpoint("GET", "/equity/portfolio"), "portfolio")
        self.assertEqual(self.client._classify_endpoint("GET", "/equity/positions"), "portfolio")

        # In production mode (rate_limit_delay >= 0.5):
        self.client.rate_limit_delay = 1.25
        self.assertAlmostEqual(self.client._get_endpoint_min_delay("GET", "/equity/metadata/instruments"), 50.5)
        self.assertAlmostEqual(self.client._get_endpoint_min_delay("GET", "/equity/metadata/exchanges"), 30.5)
        self.assertAlmostEqual(self.client._get_endpoint_min_delay("GET", "/equity/account/cash"), 5.2)
        self.assertAlmostEqual(self.client._get_endpoint_min_delay("GET", "/equity/orders"), 5.2)
        self.assertAlmostEqual(self.client._get_endpoint_min_delay("GET", "/equity/portfolio"), 1.5)
        self.assertAlmostEqual(self.client._get_endpoint_min_delay("POST", "/equity/orders/market"), 1.5)

    def test_server_clock_skew_mitigation(self):
        """Verify that Date header is used as time reference to avoid clock skew."""
        import io
        import urllib.error
        from email.message import Message

        # Server clock is at epoch 1000.0, reset is at 1004.0 (4 seconds wait)
        msg = Message()
        msg["Date"] = "Thu, 01 Jan 1970 00:16:40 GMT"  # timestamp = 1000.0
        msg["x-ratelimit-reset"] = "1004.0"

        err_429 = urllib.error.HTTPError(
            url="https://demo.trading212.com/api/v0/equity/orders",
            code=429,
            msg="Too Many Requests",
            hdrs=msg,
            fp=io.BytesIO(b'{"message": "rate limit"}'),
        )

        mock_resp_200 = MagicMock()
        mock_resp_200.read.return_value = b'{"status": "ok"}'
        mock_resp_200.headers = {}
        mock_resp_200.__enter__.return_value = mock_resp_200

        with patch("urllib.request.urlopen", side_effect=[err_429, mock_resp_200]):
            with patch("time.sleep") as mock_sleep:
                # Local clock is ahead at 1005.0
                with patch("time.time", return_value=1005.0):
                    self.client._request("GET", "/equity/orders")
                    # Should calculate (1004 - 1000) + 0.5 = 4.5s against server Date, NOT against local clock (which would be < 0)
                    sleep_calls = [c[0][0] for c in mock_sleep.call_args_list]
                    self.assertTrue(any(s >= 4.0 for s in sleep_calls))

    def test_http_date_retry_after_parsing(self):
        """Verify that HTTP-date formatted Retry-After is correctly parsed and waited."""
        import io
        import urllib.error
        from email.message import Message

        msg = Message()
        msg["Date"] = "Wed, 21 Oct 2026 07:28:00 GMT"
        msg["Retry-After"] = "Wed, 21 Oct 2026 07:28:06 GMT"  # 6.0 seconds

        err_429 = urllib.error.HTTPError(
            url="https://demo.trading212.com/api/v0/equity/account/cash",
            code=429,
            msg="Too Many Requests",
            hdrs=msg,
            fp=io.BytesIO(b'{"message": "rate limit"}'),
        )

        mock_resp_200 = MagicMock()
        mock_resp_200.read.return_value = b'{"status": "recovered"}'
        mock_resp_200.headers = {}
        mock_resp_200.__enter__.return_value = mock_resp_200

        with patch("urllib.request.urlopen", side_effect=[err_429, mock_resp_200]):
            with patch("time.sleep") as mock_sleep:
                self.client._request("GET", "/equity/account/cash")
                sleep_calls = [c[0][0] for c in mock_sleep.call_args_list]
                self.assertTrue(any(s >= 6.0 for s in sleep_calls))

    def test_production_429_backoff_floor(self):
        """Verify that in production mode, 429 backoff enforces 5.0s floor for orders."""
        import io
        import urllib.error

        err_429 = urllib.error.HTTPError(
            url="https://demo.trading212.com/api/v0/equity/orders",
            code=429,
            msg="Too Many Requests",
            hdrs={},
            fp=io.BytesIO(b'{"message": "rate limit"}'),
        )

        mock_resp_200 = MagicMock()
        mock_resp_200.read.return_value = b'{"status": "ok"}'
        mock_resp_200.headers = {}
        mock_resp_200.__enter__.return_value = mock_resp_200

        self.client.rate_limit_delay = 1.25  # production delay
        with patch("urllib.request.urlopen", side_effect=[err_429, mock_resp_200]):
            with patch("time.sleep") as mock_sleep:
                self.client._request("GET", "/equity/orders")
                sleep_calls = [c[0][0] for c in mock_sleep.call_args_list]
                # In production, floor is max(retry_delay=2.0, endpoint_min_delay=5.2, 5.0) -> >= 5.0
                self.assertTrue(any(s >= 5.0 for s in sleep_calls))

    def test_persistent_disk_caching_for_instruments(self):
        """Verify get_instruments loads from persistent disk cache without API call."""
        import json
        import tempfile
        from trading_system.data.trading212_client import Instrument

        with tempfile.NamedTemporaryFile("w+", suffix=".json", delete=False) as tmp:
            tmp_path = tmp.name
            json.dump([
                {
                    "ticker": "AAPL_US_EQ",
                    "isin": "US0378331005",
                    "currencyCode": "USD",
                    "name": "Apple Inc",
                    "type": "EQUITY",
                    "shortName": "AAPL",
                    "minTradeQuantity": 0.1,
                    "maxOpenQuantity": 1000.0,
                }
            ], tmp)
            tmp.flush()

        try:
            self.client.rate_limit_delay = 1.25  # production mode enabling disk cache
            with patch("trading_system.data.trading212_client._get_instruments_cache_path", return_value=tmp_path):
                with patch.object(self.client, "_request") as mock_req:
                    instruments = self.client.get_instruments()
                    self.assertEqual(len(instruments), 1)
                    self.assertEqual(instruments[0].ticker, "AAPL_US_EQ")
                    self.assertEqual(instruments[0].shortname, "AAPL")
                    # Must NOT have made any HTTP request
                    mock_req.assert_not_called()

                    # Force refresh bypasses cache
                    mock_req.return_value = [
                        {
                            "ticker": "MSFT_US_EQ",
                            "isin": "US5949181045",
                            "currencyCode": "USD",
                            "name": "Microsoft Corp",
                            "type": "EQUITY",
                            "shortName": "MSFT",
                            "minTradeQuantity": 0.1,
                            "maxOpenQuantity": 1000.0,
                        }
                    ]
                    instruments2 = self.client.get_instruments(force_refresh=True)
                    self.assertEqual(len(instruments2), 1)
                    self.assertEqual(instruments2[0].ticker, "MSFT_US_EQ")
                    mock_req.assert_called_once()
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def test_cross_process_state_helpers(self):
        """Verify cross-process rate limit state read and write operations."""
        import tempfile
        from trading_system.data.trading212_client import _read_cross_process_state, _write_cross_process_state

        with tempfile.NamedTemporaryFile("w+", suffix=".json", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            initial = _read_cross_process_state(tmp_path)
            self.assertEqual(initial, {})

            sample_state = {
                "global_next_allowed_time": 123456.7,
                "endpoints": {"orders_query": 123459.0},
            }
            _write_cross_process_state(tmp_path, sample_state)
            loaded = _read_cross_process_state(tmp_path)
            self.assertEqual(loaded["global_next_allowed_time"], 123456.7)
            self.assertEqual(loaded["endpoints"]["orders_query"], 123459.0)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)


class TestAsymmetricSessionProtocol(unittest.TestCase):
    """Verifies R1, R2, R4 Asymmetric Session rules in PositionMonitor."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.temp_dir.name, "active_trades.json")
        self.log_file = os.path.join(self.temp_dir.name, "notifications.jsonl")

        self.mock_t212 = MagicMock(spec=Trading212Client)
        self.mock_t212.place_market_order.return_value = OrderResult(
            id="order_close", ticker="test", type="MARKET", status="FILLED",
            quantity=-1.0, filled_quantity=1.0, fill_price=100.0
        )
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

    def test_ae1_long_position_without_tp1_flattens_at_1620(self):
        """AE1: Active long trade with tp1_hit == False flattens at 16:20 UTC (ASYMMETRIC_SESSION_EXIT)."""
        card = DecisionCard(
            ticker="QQQ",
            trading212_ticker="QQQ3l_EQ",
            direction="LONG",
            decision="BUY_ENTRY",
            entry_price=415.0,
            stop_loss=405.0,
            trailing_stop_loss=405.0,
            stop_distance_pct=0.024,
            take_profit_1=425.0,
            take_profit_2=435.0,
            take_profit_3=445.0,
            position_size_shares=10.0,
            position_notional=4150.0,
            risk_amount=100.0,
            risk_pct_of_equity=0.01,
            conviction_score=0.85,
            rationale="Trend momentum",
        )
        pos = self.monitor.register_new_trade(card, atr=5.0)
        self.assertFalse(pos.tp1_hit)

        # Trigger at 16:22 UTC
        cutoff_time = datetime(2026, 10, 15, 16, 22, 0, tzinfo=timezone.utc)
        events = self.monitor.evaluate_asymmetric_session(
            current_time=cutoff_time,
            current_prices={"QQQ3l_EQ": 420.0},
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "ASYMMETRIC_SESSION_EXIT")
        self.assertEqual(events[0]["ticker"], "QQQ3l_EQ")
        self.assertEqual(events[0]["quantity"], 10.0)
        # Position should be closed and removed from active tracking
        self.assertTrue(pos.is_closed)
        self.assertNotIn("QQQ3l_EQ", self.monitor.positions)
        self.mock_t212.place_market_order.assert_called_with("QQQ3l_EQ", -10.0)

    def test_ae2_long_position_with_tp1_hit_retained(self):
        """AE2: Active long trade with tp1_hit == True and no earnings is retained overnight."""
        card = DecisionCard(
            ticker="TSM",
            trading212_ticker="3TSMl_EQ",
            direction="LONG",
            decision="BUY_ENTRY",
            entry_price=1150.0,
            stop_loss=1120.0,
            trailing_stop_loss=1120.0,
            stop_distance_pct=0.026,
            take_profit_1=1180.0,
            take_profit_2=1210.0,
            take_profit_3=1240.0,
            position_size_shares=6.0,
            position_notional=6900.0,
            risk_amount=180.0,
            risk_pct_of_equity=0.01,
            conviction_score=0.90,
            rationale="Chip demand breakout",
        )
        pos = self.monitor.register_new_trade(card, atr=15.0)
        pos.tp1_hit = True
        pos.stop_loss = pos.entry_price  # Breakeven stop

        cutoff_time = datetime(2026, 10, 15, 16, 25, 0, tzinfo=timezone.utc)
        events = self.monitor.evaluate_asymmetric_session(
            current_time=cutoff_time,
            current_prices={"3TSMl_EQ": 1190.0},
        )

        # No liquidation event
        self.assertEqual(len(events), 0)
        self.assertFalse(pos.is_closed)
        self.assertTrue(pos.is_overnight_eligible)
        self.assertIn("3TSMl_EQ", self.monitor.positions)
        self.mock_t212.place_market_order.assert_not_called()

    def test_ae2b_earnings_blackout_flattens_even_with_tp1_hit(self):
        """AE2b: Active long trade with TP1 hit but earnings <= 24h flattens (EARNINGS_BLACKOUT_EXIT)."""
        card = DecisionCard(
            ticker="NVDA",
            trading212_ticker="3LNVl_EQ",
            direction="LONG",
            decision="BUY_ENTRY",
            entry_price=130.0,
            stop_loss=125.0,
            trailing_stop_loss=125.0,
            stop_distance_pct=0.038,
            take_profit_1=135.0,
            take_profit_2=140.0,
            take_profit_3=145.0,
            position_size_shares=20.0,
            position_notional=2600.0,
            risk_amount=100.0,
            risk_pct_of_equity=0.01,
            conviction_score=0.88,
            rationale="AI chip leader",
            earnings_in_days=0.5,  # Reporting post-market today (within 24h)
        )
        pos = self.monitor.register_new_trade(card, atr=3.0, earnings_in_days=0.5)
        pos.tp1_hit = True

        cutoff_time = datetime(2026, 10, 15, 16, 21, 0, tzinfo=timezone.utc)
        events = self.monitor.evaluate_asymmetric_session(
            current_time=cutoff_time,
            current_prices={"3LNVl_EQ": 136.0},
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "EARNINGS_BLACKOUT_EXIT")
        self.assertTrue(pos.is_closed)
        self.assertNotIn("3LNVl_EQ", self.monitor.positions)

    def test_ae3_inverse_short_unconditionally_flattens_at_1620(self):
        """AE3: All 3x Inverse Short trades unconditionally liquidate at 16:20 UTC."""
        card = DecisionCard(
            ticker="QQQ",
            trading212_ticker="QQQSl_EQ",
            direction="SHORT",
            decision="SELL_ENTRY",
            entry_price=15.0,
            stop_loss=16.0,
            trailing_stop_loss=16.0,
            stop_distance_pct=0.066,
            take_profit_1=14.0,
            take_profit_2=13.0,
            take_profit_3=12.0,
            position_size_shares=100.0,
            position_notional=1500.0,
            risk_amount=100.0,
            risk_pct_of_equity=0.01,
            conviction_score=0.82,
            rationale="Intraday market hedge",
        )
        pos = self.monitor.register_new_trade(card, atr=0.5)

        cutoff_time = datetime(2026, 10, 15, 16, 24, 0, tzinfo=timezone.utc)
        events = self.monitor.evaluate_asymmetric_session(
            current_time=cutoff_time,
            current_prices={"QQQSl_EQ": 14.5},
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "INVERSE_INTRADAY_FLATTEN")
        self.assertTrue(pos.is_closed)
        self.assertNotIn("QQQSl_EQ", self.monitor.positions)
        self.mock_t212.place_market_order.assert_called_with("QQQSl_EQ", -100.0)

    def test_ae4_serialized_liquidation_queue_pacing(self):
        """AE4: When multiple positions require 16:20 close, executions are paced serially."""
        card1 = DecisionCard(
            ticker="AAPL", trading212_ticker="3APLl_EQ", direction="LONG", decision="BUY_ENTRY",
            entry_price=100.0, stop_loss=95.0, trailing_stop_loss=95.0, stop_distance_pct=0.05,
            take_profit_1=105.0, take_profit_2=110.0, take_profit_3=115.0, position_size_shares=10.0,
            position_notional=1000.0, risk_amount=50.0, risk_pct_of_equity=0.01, conviction_score=0.8,
            rationale="Test 1",
        )
        card2 = DecisionCard(
            ticker="MSFT", trading212_ticker="3MSFl_EQ", direction="LONG", decision="BUY_ENTRY",
            entry_price=200.0, stop_loss=190.0, trailing_stop_loss=190.0, stop_distance_pct=0.05,
            take_profit_1=210.0, take_profit_2=220.0, take_profit_3=230.0, position_size_shares=5.0,
            position_notional=1000.0, risk_amount=50.0, risk_pct_of_equity=0.01, conviction_score=0.8,
            rationale="Test 2",
        )
        self.monitor.register_new_trade(card1)
        self.monitor.register_new_trade(card2)

        cutoff_time = datetime(2026, 10, 15, 16, 20, 0, tzinfo=timezone.utc)
        with patch("time.sleep") as mock_sleep:
            events = self.monitor.evaluate_asymmetric_session(
                current_time=cutoff_time,
                current_prices={"3APLl_EQ": 102.0, "3MSFl_EQ": 204.0},
            )

            self.assertEqual(len(events), 2)
            # Verify rate limit sleep was invoked between orders
            mock_sleep.assert_called_with(settings.RATE_LIMIT_INTER_CALL_DELAY)
            self.assertEqual(self.mock_t212.place_market_order.call_count, 2)


class TestOrchestratorPreTradeEarningsFilter(unittest.TestCase):
    """Verifies R2.2 Screener / Intake Earnings Gate."""

    def setUp(self):
        self.orchestrator = Orchestrator(dry_run=True)

    def test_screener_candidate_rejected_if_earnings_within_48h(self):
        """Candidate with earnings scheduled in <= 2.0 days is rejected pre-trade."""
        candidate = ScreenerResult(
            ticker="TSLA",
            bias="bullish",
            bias_confidence=0.85,
            setup_score="favorable",
            setup_confidence=0.80,
            catalyst_risk=0.10,
            overall_confidence=0.82,
            metrics={
                "close": 250.0,
                "rsi": 45.0,
                "adx": 28.0,
                "atr": 10.0,
                "earnings_in_days": 1.5,  # <= 2.0 days (48h)
            },
        )

        card = self.orchestrator.deep_analyse(candidate)
        self.assertIsNone(card, "Candidate with impending earnings within 48h must be rejected pre-trade")

    def test_screener_candidate_accepted_if_earnings_beyond_48h(self):
        """Candidate with earnings > 2.0 days passes the pre-trade earnings gate."""
        candidate = ScreenerResult(
            ticker="AAPL",
            bias="bullish",
            bias_confidence=0.85,
            setup_score="favorable",
            setup_confidence=0.80,
            catalyst_risk=0.10,
            overall_confidence=0.82,
            metrics={
                "close": 230.0,
                "rsi": 55.0,
                "adx": 26.0,
                "atr": 4.0,
                "earnings_in_days": 14.0,  # 2 weeks away
            },
        )

        card = self.orchestrator.deep_analyse(candidate)
        self.assertIsNotNone(card)
        self.assertEqual(card.ticker, "AAPL")
        self.assertEqual(card.earnings_in_days, 14.0)


if __name__ == "__main__":
    unittest.main()
