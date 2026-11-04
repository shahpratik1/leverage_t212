"""Tests for Rsi3MeanReversionStrategy."""

import unittest
from trading_system.strategies.registry import StrategyRegistry
from trading_system.strategies.rsi3_mean_reversion import Rsi3MeanReversionStrategy


class TestRsi3MeanReversionStrategy(unittest.TestCase):
    def setUp(self):
        self.strategy = StrategyRegistry.get("3rsi_mean_reversion")

    def test_bullish_dip_signal(self):
        metrics = {
            "close": 150.0,
            "sma200": 130.0,
            "rsi3": 20.0,
            "rsi7": 35.0,
            "sma5": 152.0,
            "earnings_in_days": 10.0,
        }
        sig = self.strategy.evaluate_candidate("AAPL", metrics)
        self.assertEqual(sig.symbol, "AAPL")
        self.assertEqual(sig.direction, "LONG")
        self.assertEqual(sig.setup_quality, "exceptional")
        self.assertGreaterEqual(sig.conviction, 0.90)
        self.assertTrue(len(sig.bull_evidence) >= 2)

    def test_bearish_rip_signal(self):
        metrics = {
            "close": 110.0,
            "sma200": 130.0,
            "rsi3": 85.0,
            "rsi7": 65.0,
            "earnings_in_days": 10.0,
        }
        sig = self.strategy.evaluate_candidate("AAPL", metrics)
        self.assertEqual(sig.direction, "SHORT")
        self.assertEqual(sig.setup_quality, "exceptional")
        self.assertTrue(len(sig.bear_evidence) >= 1)

    def test_neutral_signal(self):
        metrics = {
            "close": 150.0,
            "sma200": 130.0,
            "rsi3": 55.0,
            "rsi7": 50.0,
        }
        sig = self.strategy.evaluate_candidate("AAPL", metrics)
        self.assertEqual(sig.direction, "NEUTRAL")
        self.assertEqual(sig.conviction, 0.0)

    def test_filter_universe(self):
        universe = {
            "AAPL": {"close": 150.0, "sma200": 130.0, "rsi3": 22.0, "rsi7": 35.0},
            "MSFT": {"close": 400.0, "sma200": 350.0, "rsi3": 15.0, "rsi7": 30.0},
            "GOOGL": {"close": 170.0, "sma200": 160.0, "rsi3": 50.0, "rsi7": 50.0},
        }
        results = self.strategy.filter_universe(universe, top_n=5)
        self.assertEqual(len(results), 2)
        # MSFT (rsi3=15) should be ranked before AAPL (rsi3=22)
        self.assertEqual(results[0].symbol, "MSFT")
        self.assertEqual(results[1].symbol, "AAPL")


if __name__ == "__main__":
    unittest.main()
