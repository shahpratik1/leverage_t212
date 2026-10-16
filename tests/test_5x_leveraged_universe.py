"""Unit tests for 5x & 3x Leveraged Asset Universe on Trading 212."""

import unittest
from unittest.mock import MagicMock

from trading_system.data.leveraged_universe import LeveragedPair, LeveragedUniverse, CURATED_3X_PAIRS
from trading_system.agents.risk_manager import DecisionCard, RiskManager
from trading_system.config import settings


class Test5xLeveragedUniverse(unittest.TestCase):
    """Test suite verifying 5x Leverage Shares ETP discovery, ticker mapping, and fallbacks."""

    def setUp(self):
        self.universe = LeveragedUniverse()
        self.risk_manager = RiskManager()

    def test_5x_pairs_presence_and_flag(self):
        """Major indices, commodities, and fixed income must support 5x leverage."""
        expected_5x = ["QQQ", "SPY", "GLD", "TLT", "MAGS", "^FTSE", "^GDAXI", "^STOXX50E"]
        for symbol in expected_5x:
            pair = self.universe.get_pair_for_underlying(symbol)
            self.assertIsNotNone(pair, f"Missing pair for {symbol}")
            self.assertTrue(pair.has_5x, f"Expected has_5x=True for {symbol}")
            self.assertIn(5, self.universe.get_available_leverage_multipliers(symbol))

    def test_single_stocks_capped_at_3x(self):
        """Single equities must remain 3x due to European UCITS concentration regulations."""
        single_stocks = ["NVDA", "TSLA", "AAPL", "MSFT", "AMZN", "GOOGL", "TSM"]
        for symbol in single_stocks:
            pair = self.universe.get_pair_for_underlying(symbol)
            self.assertIsNotNone(pair, f"Missing pair for {symbol}")
            self.assertFalse(pair.has_5x, f"Expected has_5x=False for single stock {symbol}")
            self.assertEqual(self.universe.get_available_leverage_multipliers(symbol), [3])

    def test_5x_ticker_resolution_long_and_short(self):
        """Resolution of 5x tickers for both Long and Inverse Short."""
        # QQQ (Nasdaq 100)
        self.assertEqual(self.universe.get_execution_ticker("QQQ", "LONG", leverage=5), "QQQ5l_EQ")
        self.assertEqual(self.universe.get_execution_ticker("QQQ", "SHORT", leverage=5), "QQ3Sl_EQ")

        # SPY (S&P 500)
        self.assertEqual(self.universe.get_execution_ticker("SPY", "LONG", leverage=5), "SP5Yl_EQ")
        self.assertEqual(self.universe.get_execution_ticker("SPY", "SHORT", leverage=5), "SPYSl_EQ")

        # TLT (20+ Year Treasury Bond)
        self.assertEqual(self.universe.get_execution_ticker("TLT", "LONG", leverage=5), "TLT5l_EQ")
        self.assertEqual(self.universe.get_execution_ticker("TLT", "SHORT", leverage=5), "TL5Sl_EQ")

        # MAGS (Magnificent 7)
        self.assertEqual(self.universe.get_execution_ticker("MAGS", "LONG", leverage=5), "MAG7l_EQ")

    def test_5x_fallback_to_3x_when_unavailable(self):
        """Requesting 5x on a single stock gracefully falls back to 3x."""
        # NVDA has 3x Long (3LNV) and 3x Short (3SNV)
        self.assertEqual(self.universe.get_execution_ticker("NVDA", "LONG", leverage=5), "3LNVl_EQ")
        self.assertEqual(self.universe.get_execution_ticker("NVDA", "SHORT", leverage=5), "3SNVl_EQ")

        # TSM has 3x Long (3TSM)
        self.assertEqual(self.universe.get_execution_ticker("TSM", "LONG", leverage=5), "3TSMl_EQ")

    def test_standard_3x_resolution_preserved(self):
        """Default leverage=3 resolution must remain identical to prior behavior."""
        self.assertEqual(self.universe.get_execution_ticker("QQQ", "LONG", leverage=3), "QQQ3l_EQ")
        self.assertEqual(self.universe.get_execution_ticker("QQQ", "SHORT", leverage=3), "QQQSl_EQ")
        self.assertEqual(self.universe.get_execution_ticker("SPY", "LONG", leverage=3), "3USLl_EQ")
        self.assertEqual(self.universe.get_execution_ticker("SPY", "SHORT", leverage=3), "3USSl_EQ")

    def test_decision_card_leverage_tagging(self):
        """DecisionCard properly tags leverage=5 when 5x execution ticker is passed."""
        debate_mock = MagicMock()
        debate_mock.consensus_decision = "BUY_ENTRY"
        debate_mock.conviction_score = 0.90
        debate_mock.synthesis = "High conviction 5x tech breakout."

        card_5x = self.risk_manager.calculate_decision_card(
            ticker="QQQ",
            trading212_ticker="QQQ5l_EQ",
            direction="LONG",
            metrics={"close": 120.0, "atr": 2.5},
            debate_outcome=debate_mock,
            account_equity=10000.0,
            leverage=5,
        )
        self.assertEqual(card_5x.leverage, 5)
        self.assertEqual(card_5x.trading212_ticker, "QQQ5l_EQ")

        card_3x = self.risk_manager.calculate_decision_card(
            ticker="QQQ",
            trading212_ticker="QQQ3l_EQ",
            direction="LONG",
            metrics={"close": 415.0, "atr": 8.5},
            debate_outcome=debate_mock,
            account_equity=10000.0,
            leverage=3,
        )
        self.assertEqual(card_3x.leverage, 3)
        self.assertEqual(card_3x.trading212_ticker, "QQQ3l_EQ")


if __name__ == "__main__":
    unittest.main()
