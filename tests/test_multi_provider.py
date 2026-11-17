"""Unit tests for MultiProviderRouter with rate limiting, quota tracking, and failover cascades."""

from __future__ import annotations

import time
import unittest
from unittest.mock import MagicMock, patch

from trading_system.data.multi_provider import MultiProviderRouter, ProviderQuota


class TestMultiProviderRouter(unittest.TestCase):
    """Test MultiProviderRouter rate limits, quotas, and waterfall cascades."""

    def setUp(self):
        self.router = MultiProviderRouter()
        # Use fresh quotas for clean test isolation
        for q in self.router.quotas.values():
            q._request_timestamps.clear()
            q._day_request_count = 0
            q._cooldown_until = 0.0
            q._total_success = 0
            q._total_errors = 0
            q._total_429 = 0

    def test_missing_api_key_check(self):
        """Keyed providers without an API key return no_api_key."""
        can_call, reason = self.router.can_request("fmp", api_key="")
        self.assertFalse(can_call)
        self.assertEqual(reason, "no_api_key")

    def test_minute_rate_limit_enforcement(self):
        """Enforces minute-level request limit (e.g. 5 req/min for Polygon)."""
        quota = self.router.quotas["polygon"]
        quota.rate_limit_per_min = 3

        # First 3 requests succeed
        for _ in range(3):
            can_call, reason = self.router.can_request("polygon", api_key="test_key")
            self.assertTrue(can_call)
            self.router.record_request("polygon")

        # 4th request must be blocked
        can_call, reason = self.router.can_request("polygon", api_key="test_key")
        self.assertFalse(can_call)
        self.assertIn("minute_limit", reason)

    def test_daily_budget_enforcement(self):
        """Enforces daily request budget."""
        quota = self.router.quotas["alpha_vantage"]
        quota.daily_budget = 2

        # Make 2 requests
        for _ in range(2):
            self.router.record_request("alpha_vantage")

        can_call, reason = self.router.can_request("alpha_vantage", api_key="test_key")
        self.assertFalse(can_call)
        self.assertIn("daily_budget_exhausted", reason)

    def test_http_429_triggers_cooldown(self):
        """Receiving HTTP 429 activates provider cooldown."""
        self.router.record_response("fmp", 429, error_msg="Rate limit exceeded")
        quota = self.router.quotas["fmp"]
        self.assertGreater(quota._cooldown_until, time.time())
        self.assertEqual(quota._total_429, 1)

        # Subsequent check is blocked due to cooldown
        can_call, reason = self.router.can_request("fmp", api_key="test_key")
        self.assertFalse(can_call)
        self.assertIn("cooldown_active", reason)

    def test_waterfall_cascade_when_first_provider_rate_limited(self):
        """When FMP is in cooldown, cascade automatically queries Tiingo."""
        mock_settings = MagicMock()
        mock_settings.FMP_API_KEY = "fmp_test"
        mock_settings.TIINGO_API_KEY = "tiingo_test"
        mock_settings.POLYGON_API_KEY = ""
        mock_settings.ALPHA_VANTAGE_API_KEY = ""

        # Put FMP in cooldown
        self.router.record_response("fmp", 429)

        # Mock Tiingo returning candles
        expected_candles = [{"date": "2026-11-16", "open": 100.0, "high": 105.0, "low": 99.0, "close": 104.0, "volume": 1000.0}]
        with patch.object(self.router, "fetch_tiingo_daily", return_value=expected_candles) as mock_tiingo:
            with patch.object(self.router, "fetch_fmp_daily") as mock_fmp:
                res = self.router.get_daily_candles_cascade("NVDA", mock_settings)
                self.assertEqual(res, expected_candles)
                mock_fmp.assert_not_called()  # FMP skipped due to cooldown
                mock_tiingo.assert_called_once_with("NVDA", "tiingo_test")

    def test_alpha_vantage_rate_limit_detection(self):
        """Alpha Vantage rate-limit message in response payload triggers cooldown."""
        note_response = {"Note": "Thank you for using Alpha Vantage! Standard call frequency is 5 calls per minute."}
        with patch.object(self.router, "_http_get_json", return_value=(200, note_response)):
            res = self.router.fetch_alphavantage_daily("AAPL", "av_key")
            self.assertIsNone(res)
            quota = self.router.quotas["alpha_vantage"]
            self.assertEqual(quota._total_429, 1)
            self.assertGreater(quota._cooldown_until, time.time())

    def test_non_equity_earnings_bypassed_without_api_calls(self):
        """Non-equity assets (ARKK, GDX, QQQ, SPY, GLD) bypass earnings check without consuming quota."""
        mock_settings = MagicMock()
        mock_settings.FMP_API_KEY = "fmp_key"
        mock_settings.ALPHA_VANTAGE_API_KEY = "av_key"

        with patch.object(self.router, "fetch_fmp_earnings_days") as mock_fmp:
            with patch.object(self.router, "fetch_alphavantage_earnings_days") as mock_av:
                for sym in ("ARKK", "GDX", "QQQ", "SPY", "BOTZ", "GLD", "SLV", "BTC-USD"):
                    res = self.router.get_earnings_days_cascade(sym, mock_settings)
                    self.assertIsNone(res)
                    mock_fmp.assert_not_called()
                    mock_av.assert_not_called()

    def test_telemetry_reporting(self):
        """Telemetry reports diagnostic metrics for all providers."""
        self.router.record_response("fmp", 200)
        self.router.record_response("polygon", 429)
        telemetry = self.router.get_telemetry()
        self.assertIn("fmp", telemetry)
        self.assertIn("polygon", telemetry)
        self.assertEqual(telemetry["fmp"]["total_success"], 1)
        self.assertEqual(telemetry["polygon"]["total_429"], 1)
        self.assertGreater(telemetry["polygon"]["cooldown_remaining_sec"], 0)


if __name__ == "__main__":
    unittest.main()
