import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
from datetime import datetime
from io import BytesIO
from unittest.mock import MagicMock, patch

from trading_system.data.trading212_client import (
    Trading212Client,
    Trading212Error,
    _atomic_write_json,
    _read_cross_process_state,
    _write_cross_process_state,
)


def _make_urllib_response(payload, headers=None):
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode("utf-8")
    resp.headers = headers or {}
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda s, *args: None
    return resp


def _make_urllib_429(headers=None):
    hdrs = headers if headers is not None else {"Retry-After": "1"}
    return urllib.error.HTTPError(
        url="https://demo.trading212.com/api/v0/equity/account/cash",
        code=429,
        msg="Too Many Requests",
        hdrs=hdrs,
        fp=BytesIO(b'{"error":"rate limit"}'),
    )


class TestTrading212RateLimitTelemetry(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._rate_file = os.path.join(self._tmpdir.name, ".t212_rate_limit.json")
        self._rate_path_patcher = patch(
            "trading_system.data.trading212_client._get_rate_limit_state_path",
            return_value=self._rate_file,
        )
        self._rate_path_patcher.start()
        Trading212Client.reset_rate_limit_telemetry(reset_timers=True)

    def tearDown(self):
        Trading212Client.reset_rate_limit_telemetry(reset_timers=True)
        self._rate_path_patcher.stop()
        self._tmpdir.cleanup()

    def _new_client(self, rate_limit_delay=0.0):
        client = Trading212Client(
            api_key="test-key",
            base_url="https://demo.trading212.com/api/v0",
            rate_limit_delay=rate_limit_delay,
        )
        # Default to urllib transport unless a test explicitly injects a mock httpx client
        client.client = None
        return client

    def test_initial_telemetry_schema_and_reset(self):
        snap = Trading212Client.get_rate_limit_telemetry()
        expected_keys = {
            "wait_count",
            "cumulative_wait_ms",
            "max_wait_ms",
            "rate_limit_429_count",
            "last_429_at",
            "endpoint_cooldown_remaining",
            "global_cooldown_remaining",
            "trade_active",
            "last_request_at",
            "captured_at",
        }
        self.assertEqual(set(snap.keys()), expected_keys)
        self.assertEqual(snap["wait_count"], 0)
        self.assertEqual(snap["cumulative_wait_ms"], 0.0)
        self.assertEqual(snap["max_wait_ms"], 0.0)
        self.assertEqual(snap["rate_limit_429_count"], 0)
        self.assertIsNone(snap["last_429_at"])
        self.assertEqual(snap["endpoint_cooldown_remaining"], {})
        self.assertEqual(snap["global_cooldown_remaining"], 0.0)
        self.assertFalse(snap["trade_active"])
        self.assertIsNone(snap["last_request_at"])
        datetime.fromisoformat(snap["captured_at"])

        with Trading212Client._global_rate_lock:
            Trading212Client._telemetry_wait_count = 3
            Trading212Client._telemetry_cumulative_wait_ms = 450.0
            Trading212Client._telemetry_max_wait_ms = 200.0
            Trading212Client._telemetry_429_count = 1
            Trading212Client._telemetry_last_429_at = "2026-01-01T00:00:00+00:00"
            Trading212Client._global_next_allowed_time = time.time() + 25.0
            Trading212Client._endpoint_next_allowed_time["account_cash"] = time.time() + 12.0

        Trading212Client.reset_rate_limit_telemetry(reset_timers=False)
        after_soft = Trading212Client.get_rate_limit_telemetry()
        self.assertEqual(after_soft["wait_count"], 0)
        self.assertEqual(after_soft["cumulative_wait_ms"], 0.0)
        self.assertEqual(after_soft["max_wait_ms"], 0.0)
        self.assertEqual(after_soft["rate_limit_429_count"], 0)
        self.assertIsNone(after_soft["last_429_at"])
        self.assertGreater(after_soft["global_cooldown_remaining"], 10.0)
        self.assertGreater(after_soft["endpoint_cooldown_remaining"]["account_cash"], 5.0)

        Trading212Client.reset_rate_limit_telemetry(reset_timers=True)
        after_hard = Trading212Client.get_rate_limit_telemetry()
        self.assertEqual(after_hard["global_cooldown_remaining"], 0.0)
        self.assertEqual(after_hard["endpoint_cooldown_remaining"], {})

    def test_pacing_wait_telemetry_and_lock_released_during_sleep(self):
        client = self._new_client(rate_limit_delay=0.0)

        # Force an endpoint pacing wait of ~0.35s on next account_cash request
        with Trading212Client._global_rate_lock:
            now = time.time()
            Trading212Client._endpoint_next_allowed_time["account_cash"] = now + 0.35
            Trading212Client._global_last_request_time = now - 5.0

        entered_sleep = threading.Event()
        release_sleep = threading.Event()
        sleep_calls = []

        def fake_sleep(seconds):
            sleep_calls.append(seconds)
            entered_sleep.set()
            # Block until reader thread confirms _global_rate_lock is unlocked during sleep
            self.assertTrue(
                release_sleep.wait(timeout=2.0),
                "Reader thread blocked waiting for _global_rate_lock during sleep",
            )

        telemetry_during_sleep = {}
        reader_error = []

        def reader_worker():
            try:
                self.assertTrue(entered_sleep.wait(timeout=2.0))
                # If _global_rate_lock were held across time.sleep, this call would deadlock.
                telemetry_during_sleep.update(Trading212Client.get_rate_limit_telemetry())
            except Exception as exc:
                reader_error.append(exc)
            finally:
                release_sleep.set()

        reader = threading.Thread(target=reader_worker)
        reader.start()

        with patch("trading_system.data.trading212_client.time.sleep", side_effect=fake_sleep), patch(
            "urllib.request.urlopen",
            return_value=_make_urllib_response({"free": 500.0, "total": 500.0}),
        ):
            cash = client.get_account_cash(force_refresh=True)

        reader.join(timeout=3.0)
        self.assertFalse(reader_error, f"Reader thread failed: {reader_error}")
        self.assertIsNotNone(cash)
        self.assertEqual(cash.free, 500.0)
        self.assertTrue(telemetry_during_sleep, "Telemetry was not read while sleeping")
        self.assertEqual(len(sleep_calls), 1)
        self.assertGreater(sleep_calls[0], 0.1)

        snap = Trading212Client.get_rate_limit_telemetry()
        self.assertEqual(snap["wait_count"], 1)
        self.assertGreaterEqual(snap["cumulative_wait_ms"], sleep_calls[0] * 1000.0 - 1.0)
        self.assertEqual(snap["max_wait_ms"], snap["cumulative_wait_ms"])
        self.assertEqual(snap["rate_limit_429_count"], 0)
        self.assertIsNotNone(snap["last_request_at"])
        datetime.fromisoformat(snap["last_request_at"])

    def test_429_telemetry_cooldown_remaining_and_proactive_reset(self):
        client = self._new_client(rate_limit_delay=1.25)

        # First call returns 429 with Retry-After=4, second succeeds with proactive 0 remaining header
        responses = [
            _make_urllib_429({"Retry-After": "4"}),
            _make_urllib_response(
                {"free": 1234.5, "total": 1234.5},
                headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(time.time()) + 8)},
            ),
        ]
        sleep_calls = []

        def fake_urlopen(req, timeout=None):
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        with patch("trading_system.data.trading212_client.time.sleep", side_effect=sleep_calls.append), patch(
            "urllib.request.urlopen", side_effect=fake_urlopen
        ):
            cash = client.get_account_cash(force_refresh=True)

        self.assertIsNotNone(cash)
        self.assertEqual(cash.free, 1234.5)
        self.assertGreaterEqual(len(sleep_calls), 1)

        snap = Trading212Client.get_rate_limit_telemetry()
        self.assertEqual(snap["rate_limit_429_count"], 1)
        self.assertIsNotNone(snap["last_429_at"])
        datetime.fromisoformat(snap["last_429_at"])
        self.assertGreaterEqual(snap["wait_count"], 1)
        self.assertGreater(snap["cumulative_wait_ms"], 0.0)
        self.assertGreater(snap["max_wait_ms"], 0.0)
        self.assertGreater(snap["endpoint_cooldown_remaining"]["account_cash"], 0.0)
        self.assertGreater(snap["global_cooldown_remaining"], 0.0)

        # When cooldown expires in the past, remaining must clamp to 0.0
        with Trading212Client._global_rate_lock:
            Trading212Client._endpoint_next_allowed_time["account_cash"] = time.time() - 5.0
            Trading212Client._global_next_allowed_time = time.time() - 5.0

        expired = Trading212Client.get_rate_limit_telemetry()
        self.assertEqual(expired["endpoint_cooldown_remaining"]["account_cash"], 0.0)
        self.assertEqual(expired["global_cooldown_remaining"], 0.0)

    def test_429_exhaustion_httpx_records_telemetry_without_wait_inflation(self):
        client = self._new_client(rate_limit_delay=1.25)

        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.headers = {"Retry-After": "2"}
        mock_httpx = MagicMock()
        mock_httpx.request.return_value = mock_resp
        client.client = mock_httpx

        sleep_calls = []
        with patch("trading_system.data.trading212_client.time.sleep", side_effect=sleep_calls.append):
            with self.assertRaises(Trading212Error):
                client._request("GET", "/equity/account/cash")

        snap = Trading212Client.get_rate_limit_telemetry()
        self.assertEqual(snap["rate_limit_429_count"], 4)
        self.assertIsNotNone(snap["last_429_at"])
        self.assertEqual(snap["wait_count"], len(sleep_calls))
        self.assertGreaterEqual(snap["wait_count"], 3)
        self.assertGreater(snap["cumulative_wait_ms"], 0.0)
        self.assertGreater(snap["endpoint_cooldown_remaining"]["account_cash"], 0.0)

    def test_get_rate_limit_telemetry_performs_zero_http_and_zero_disk_io(self):
        Trading212Client.set_trade_active(True)
        with patch(
            "urllib.request.urlopen",
            side_effect=AssertionError("HTTP urlopen must not be called"),
        ), patch(
            "trading_system.data.trading212_client._read_cross_process_state",
            side_effect=AssertionError("Disk read must not happen during telemetry snapshot"),
        ), patch(
            "trading_system.data.trading212_client._write_cross_process_state",
            side_effect=AssertionError("Disk write must not happen during telemetry snapshot"),
        ):
            snap = Trading212Client.get_rate_limit_telemetry()

        self.assertTrue(snap["trade_active"])
        self.assertEqual(snap["wait_count"], 0)

        Trading212Client.set_trade_active(False)
        snap2 = Trading212Client.get_rate_limit_telemetry()
        self.assertFalse(snap2["trade_active"])

    def test_concurrent_rate_limit_file_persistence_no_fnf_or_torn_json(self):
        _atomic_write_json(
            self._rate_file,
            {
                "global_next_allowed_time": 100.0,
                "trade_executing": False,
                "trade_timestamp": 0.0,
                "endpoints": {},
                "writer_seq": 0,
            },
        )

        errors = []
        stop_event = threading.Event()
        write_iterations = 80

        def writer_worker(worker_id):
            try:
                for i in range(1, write_iterations + 1):
                    payload = {
                        "global_next_allowed_time": time.time(),
                        "trade_executing": (i % 2 == 0),
                        "trade_timestamp": time.time(),
                        "endpoints": {"account_cash": time.time() + 1.0, "orders_query": time.time() + 2.0},
                        "writer_id": worker_id,
                        "writer_seq": i,
                        "padding": "x" * 256,
                    }
                    _write_cross_process_state(self._rate_file, payload)
            except Exception as exc:
                errors.append(("writer", worker_id, type(exc).__name__, str(exc)))

        def state_reader_worker(reader_id):
            try:
                while not stop_event.is_set():
                    data = _read_cross_process_state(self._rate_file)
                    if not isinstance(data, dict) or "endpoints" not in data:
                        errors.append(("state_reader", reader_id, "InvalidState", repr(data)))
            except Exception as exc:
                errors.append(("state_reader", reader_id, type(exc).__name__, str(exc)))

        def raw_json_reader_worker(reader_id):
            try:
                while not stop_event.is_set():
                    with open(self._rate_file, "r", encoding="utf-8") as handle:
                        parsed = json.load(handle)
                    if not isinstance(parsed, dict) or "global_next_allowed_time" not in parsed:
                        errors.append(("raw_reader", reader_id, "InvalidJSON", repr(parsed)))
            except Exception as exc:
                errors.append(("raw_reader", reader_id, type(exc).__name__, str(exc)))

        writers = [threading.Thread(target=writer_worker, args=(w,)) for w in range(8)]
        readers = [threading.Thread(target=state_reader_worker, args=(r,)) for r in range(4)] + [
            threading.Thread(target=raw_json_reader_worker, args=(r + 10,)) for r in range(4)
        ]

        for r in readers:
            r.start()
        for w in writers:
            w.start()
        for w in writers:
            w.join(timeout=15.0)
        stop_event.set()
        for r in readers:
            r.join(timeout=5.0)

        self.assertEqual(errors, [], f"Concurrent persistence errors detected: {errors}")
        leftover_files = sorted(os.listdir(self._tmpdir.name))
        self.assertEqual(leftover_files, [".t212_rate_limit.json"])

    def test_concurrent_set_trade_active_and_is_trade_active(self):
        errors = []

        def toggle_worker(tid):
            try:
                for i in range(40):
                    Trading212Client.set_trade_active(i % 2 == 0)
                    _ = Trading212Client.is_trade_active()
                    _ = Trading212Client.get_rate_limit_telemetry()
            except Exception as exc:
                errors.append((tid, type(exc).__name__, str(exc)))

        threads = [threading.Thread(target=toggle_worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15.0)

        self.assertEqual(errors, [])

    def test_zero_http_cached_getters_and_broker_snapshot(self):
        client = self._new_client(rate_limit_delay=0.05)

        # Unpopulated empty caches
        self.assertIsNone(client.get_cached_account_cash())
        self.assertIsNone(client.get_cached_portfolio())
        self.assertIsNone(client.get_cached_orders())
        self.assertIsNone(client.get_cached_historical_orders())

        with patch("urllib.request.urlopen", side_effect=AssertionError("No HTTP allowed")):
            self.assertIsNone(client.get_account_cash(cached_only=True))
            fallback_cash = client.get_account_cash(fallback_equity=777.0, cached_only=True)
            self.assertEqual(fallback_cash.free, 777.0)
            self.assertEqual(client.get_portfolio(cached_only=True), [])
            self.assertEqual(client.get_orders(cached_only=True), [])
            self.assertEqual(client.get_historical_orders(cached_only=True), {"items": []})
            self.assertEqual(client.get_instruments(cached_only=True), [])
            empty_snap = client.get_cached_broker_snapshot()

        self.assertIsNone(empty_snap["cash"])
        self.assertIsNone(empty_snap["portfolio"])
        self.assertIsNone(empty_snap["orders"])
        self.assertIsNone(empty_snap["historical_orders"])
        self.assertFalse(empty_snap["historical_summary"]["cached"])
        self.assertEqual(empty_snap["historical_summary"]["item_count"], 0)
        self.assertIsNone(empty_snap["open_orders_count"])
        self.assertIsNone(empty_snap["active_server_stops_count"])

        # Populate caches via mocked HTTP calls
        cash_payload = {"free": 2500.0, "invested": 7500.0, "total": 10200.0, "blocked": 50.0, "result": 200.0, "ppl": 200.0}
        portfolio_payload = [
            {
                "ticker": "AAPL_US_EQ",
                "quantity": 10.0,
                "averagePrice": 150.0,
                "currentPrice": 170.0,
                "ppl": 200.0,
                "maxBuy": 100.0,
                "maxSell": 10.0,
                "currency": "USD",
            }
        ]
        orders_payload = [
            {"id": 101, "ticker": "AAPL_US_EQ", "type": "STOP", "status": "NEW", "quantity": -10.0, "stopPrice": 142.0},
            {"id": 102, "ticker": "MSFT_US_EQ", "type": "LIMIT", "status": "NEW", "quantity": 5.0, "limitPrice": 400.0},
        ]
        hist_payload = {
            "items": [
                {
                    "order": {
                        "id": 1,
                        "ticker": "NVDA_US_EQ",
                        "status": "FILLED",
                        "createdAt": "2026-11-10T15:30:00Z",
                    },
                    "fill": {"walletImpact": {"realisedProfitLoss": 125.5}},
                },
                {
                    "order": {
                        "id": 2,
                        "ticker": "AMD_US_EQ",
                        "status": "CANCELLED",
                        "createdAt": "2026-11-09T10:00:00Z",
                    },
                    "fill": None,
                },
            ],
            "nextPagePath": "/api/v0/equity/history/orders?cursor=abc",
        }
        instruments_payload = [
            {
                "ticker": "AAPL_US_EQ",
                "name": "Apple Inc",
                "shortName": "AAPL",
                "type": "STOCK",
                "currencyCode": "USD",
                "minTradeQuantity": 0.0001,
                "maxOpenQuantity": 10000.0,
            }
        ]

        def route_urlopen(req, timeout=None):
            url = req.full_url
            if url.endswith("/equity/account/cash"):
                return _make_urllib_response(cash_payload)
            if url.endswith("/equity/portfolio"):
                return _make_urllib_response(portfolio_payload)
            if url.endswith("/equity/orders"):
                return _make_urllib_response(orders_payload)
            if "/equity/history/orders" in url:
                return _make_urllib_response(hist_payload)
            if url.endswith("/equity/metadata/instruments"):
                return _make_urllib_response(instruments_payload)
            raise AssertionError(f"Unexpected URL: {url}")

        with patch("trading_system.data.trading212_client.time.sleep"), patch(
            "urllib.request.urlopen", side_effect=route_urlopen
        ):
            client.get_account_cash()
            client.get_portfolio()
            client.get_orders()
            client.get_historical_orders(limit=50)
            client.get_instruments()

        # Age all caches far beyond normal TTLs to prove cached_only & snapshot ignore TTL staleness
        stale_ts = time.time() - 600.0
        with client._cache_lock:
            client._account_cash_cache_time = stale_ts
            client._portfolio_cache_time = stale_ts
            client._orders_cache_time = stale_ts
            client._history_orders_cache_time = stale_ts
            client._instruments_cache_time = stale_ts

        # Strictly block ALL network I/O and _request calls
        with patch.object(
            client, "_request", side_effect=AssertionError("_request must not be called")
        ), patch(
            "urllib.request.urlopen", side_effect=AssertionError("urlopen must not be called")
        ):
            self.assertEqual(client.get_cached_account_cash().free, 2500.0)
            self.assertEqual(client.get_account_cash(cached_only=True).total, 10200.0)

            cached_port = client.get_cached_portfolio()
            self.assertEqual(len(cached_port), 1)
            self.assertEqual(cached_port[0].ticker, "AAPL_US_EQ")
            self.assertEqual(len(client.get_portfolio(cached_only=True)), 1)

            cached_orders = client.get_cached_orders()
            self.assertEqual(len(cached_orders), 2)
            cached_orders.append({"id": 999})
            self.assertEqual(len(client.get_orders(cached_only=True)), 2)

            cached_hist = client.get_cached_historical_orders()
            self.assertEqual(len(cached_hist["items"]), 2)
            self.assertEqual(
                client.get_historical_orders(cached_only=True)["nextPagePath"],
                "/api/v0/equity/history/orders?cursor=abc",
            )

            cached_instruments = client.get_instruments(cached_only=True)
            self.assertEqual(len(cached_instruments), 1)
            self.assertEqual(cached_instruments[0].ticker, "AAPL_US_EQ")

            snap = client.get_cached_broker_snapshot()

        self.assertEqual(snap["cash"]["free"], 2500.0)
        self.assertEqual(snap["cash"]["ppl_result"], 200.0)
        self.assertEqual(len(snap["portfolio"]), 1)
        self.assertEqual(snap["portfolio"][0]["ticker"], "AAPL_US_EQ")
        self.assertEqual(snap["open_orders_count"], 2)
        self.assertEqual(snap["active_server_stops_count"], 1)
        self.assertTrue(snap["historical_summary"]["cached"])
        self.assertEqual(snap["historical_summary"]["item_count"], 2)
        self.assertEqual(snap["historical_summary"]["filled_count"], 1)
        self.assertAlmostEqual(snap["historical_summary"]["realised_pnl_total"], 125.5)
        self.assertEqual(snap["historical_summary"]["latest_order_at"], "2026-11-10T15:30:00Z")
        self.assertGreaterEqual(snap["cache_ages_seconds"]["cash"], 590.0)
        self.assertIsNotNone(snap["cache_timestamps"]["cash_at"])
        self.assertIn("rate_limit_telemetry", snap)
        datetime.fromisoformat(snap["captured_at"])

        # Combination guard: force_refresh=True and cached_only=True must raise ValueError
        with self.assertRaises(ValueError):
            client.get_account_cash(force_refresh=True, cached_only=True)
        with self.assertRaises(ValueError):
            client.get_portfolio(force_refresh=True, cached_only=True)
        with self.assertRaises(ValueError):
            client.get_orders(force_refresh=True, cached_only=True)
        with self.assertRaises(ValueError):
            client.get_historical_orders(force_refresh=True, cached_only=True)
        with self.assertRaises(ValueError):
            client.get_instruments(force_refresh=True, cached_only=True)


if __name__ == "__main__":
    unittest.main()
