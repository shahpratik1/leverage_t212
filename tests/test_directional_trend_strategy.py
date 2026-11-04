"""Tests for DirectionalTrendStrategy."""

import unittest
from trading_system.strategies.registry import StrategyRegistry
from trading_system.strategies.directional_trend import DirectionalTrendStrategy


class TestDirectionalTrendStrategy(unittest.TestCase):
    def setUp(self):
        self.strategy = StrategyRegistry.get("directional_trend")

    def test_bullish_trend_signal(self):
        metrics = {
            "close": 150.0,
            "sma200": 130.0,
            "ema20": 148.0,
            "ema50": 142.0,
            "ema20_slope_5d": 1.25,
            "adx": 28.0,
            "plus_di": 32.0,
            "minus_di": 14.0,
            "earnings_in_days": 15.0,
        }
        sig = self.strategy.evaluate_candidate("NVDA", metrics)
        self.assertEqual(sig.symbol, "NVDA")
        self.assertEqual(sig.direction, "LONG")
        self.assertEqual(sig.setup_quality, "exceptional")
        self.assertGreaterEqual(sig.conviction, 0.85)
        self.assertTrue(any("Directional Polarity Bullish" in ev for ev in sig.bull_evidence))

    def test_counter_trend_knife_catch_blocked(self):
        # Even though close > sma200, intermediate 20 EMA is sloping down and -DI > +DI
        metrics = {
            "close": 160.0,
            "sma200": 140.0,
            "ema20": 165.0,
            "ema50": 168.0,
            "ema20_slope_5d": -1.5,
            "adx": 30.0,
            "plus_di": 12.0,
            "minus_di": 35.0,  # Bearish impulse!
            "rsi3": 18.0,      # Deeply oversold!
        }
        sig = self.strategy.evaluate_candidate("ABNB", metrics)
        # MUST NOT BUY THE FALLING KNIFE!
        self.assertEqual(sig.direction, "NEUTRAL")
        self.assertEqual(sig.conviction, 0.0)
        self.assertIn("Bear regime active", sig.rationale)

    def test_dual_track_short_on_broad_index(self):
        # QQQ in bearish breakdown
        metrics = {
            "close": 420.0,
            "sma200": 410.0,
            "ema20": 430.0,
            "ema50": 435.0,
            "ema20_slope_5d": -1.2,
            "adx": 26.0,
            "plus_di": 11.0,
            "minus_di": 32.0,
        }
        sig = self.strategy.evaluate_candidate("QQQ", metrics)
        self.assertEqual(sig.symbol, "QQQ")
        self.assertEqual(sig.direction, "SHORT")
        self.assertEqual(sig.setup_quality, "exceptional")
        self.assertGreaterEqual(sig.conviction, 0.85)
        self.assertTrue(any("Dual-track rule" in ev for ev in sig.bear_evidence))

    def test_dual_track_cash_defense_on_single_equity(self):
        # Same bearish conditions on a single equity (e.g. ARM)
        metrics = {
            "close": 120.0,
            "sma200": 115.0,
            "ema20": 128.0,
            "ema50": 130.0,
            "ema20_slope_5d": -1.8,
            "adx": 28.0,
            "plus_di": 10.0,
            "minus_di": 34.0,
        }
        sig = self.strategy.evaluate_candidate("ARM", metrics)
        self.assertEqual(sig.symbol, "ARM")
        # Dual-track cash defense: single stocks are NOT shorted
        self.assertEqual(sig.direction, "NEUTRAL")
        self.assertEqual(sig.conviction, 0.0)
        self.assertIn("Cash Defense", sig.rationale)
        self.assertTrue(any("Dual-track cash defense" in ev for ev in sig.bear_evidence))

    def test_filter_universe(self):
        universe = {
            "NVDA": {
                "close": 150.0, "ema20": 148.0, "ema50": 142.0, "ema20_slope_5d": 1.5,
                "adx": 30.0, "plus_di": 35.0, "minus_di": 12.0
            },
            "QQQ": {
                "close": 420.0, "ema20": 430.0, "ema50": 435.0, "ema20_slope_5d": -1.2,
                "adx": 26.0, "plus_di": 11.0, "minus_di": 32.0
            },
            "CHOP": {
                "close": 100.0, "ema20": 100.0, "ema50": 100.0, "ema20_slope_5d": 0.0,
                "adx": 12.0, "plus_di": 18.0, "minus_di": 18.0
            },
        }
        signals = self.strategy.filter_universe(universe, top_n=5)
        self.assertEqual(len(signals), 2)
        symbols = [s.symbol for s in signals]
        self.assertIn("NVDA", symbols)
        self.assertIn("QQQ", symbols)
        self.assertNotIn("CHOP", symbols)


if __name__ == "__main__":
    unittest.main()
