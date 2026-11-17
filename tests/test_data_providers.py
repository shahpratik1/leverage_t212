"""Unit tests for MarketDataProvider and self-contained quantitative data methods."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from trading_system.data.trading212_client import MarketDataProvider


class TestMarketDataProvider(unittest.TestCase):
    """Test MarketDataProvider quantitative data retrieval and caching."""

    def test_legacy_provider_stubs(self):
        """Verify excised third-party provider calls safely return empty stubs without crashing."""
        self.assertEqual(MarketDataProvider.get_company_fundamentals("AAPL"), {})
        self.assertEqual(MarketDataProvider.get_company_news("AAPL"), [])
        self.assertEqual(MarketDataProvider.get_sentiment_search("AAPL"), {})
        self.assertEqual(MarketDataProvider.get_5day_forecast("AAPL"), {})

    def test_synthetic_ticker_detection(self):
        """Test crypto and commodity synthetic underlying detection."""
        MarketDataProvider.SYNTHETIC_TICKERS.add("BTC-USD")
        self.assertTrue(MarketDataProvider.is_ticker_synthetic("BTC-USD"))
        self.assertFalse(MarketDataProvider.is_ticker_synthetic("AAPL"))
        MarketDataProvider.SYNTHETIC_TICKERS.discard("BTC-USD")

    def test_clear_candle_cache(self):
        """Test candle cache clear operation."""
        MarketDataProvider._CANDLE_CACHE["TEST"] = (0.0, [{"close": 100}])
        MarketDataProvider._EARNINGS_CACHE["TEST"] = (0.0, 5.0)
        MarketDataProvider.clear_candle_cache()
        self.assertNotIn("TEST", MarketDataProvider._CANDLE_CACHE)
        self.assertNotIn("TEST", MarketDataProvider._EARNINGS_CACHE)

    def test_non_equity_earnings_skip(self):
        """Verify non-equity universe tickers (ETFs, indices, commodities) skip earnings calendar without network/404."""
        for sym in ("ARKK", "GDX", "QQQ", "SPY", "BOTZ", "URA", "GLD", "SLV", "^GDAXI", "BTC-USD"):
            with patch("trading_system.data.trading212_client.yf") as mock_yf:
                res = MarketDataProvider.get_days_to_earnings(sym)
                self.assertIsNone(res)
                mock_yf.Ticker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
