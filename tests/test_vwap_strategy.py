"""Unit tests for VWAP Session Trend-Following Strategy and indicator engine."""

import unittest
from unittest.mock import MagicMock, patch

from trading_system.strategies.registry import StrategyRegistry
from trading_system.strategies.vwap import VWAPStrategy
from trading_system.data.trading212_client import MarketDataProvider


class TestVWAPStrategy(unittest.TestCase):
    """Test suite for VWAP strategy rules, signals, and dual-track risk enforcement."""

    def setUp(self):
        self.strategy = StrategyRegistry.get("vwap")
        self.assertIsInstance(self.strategy, VWAPStrategy)

    def test_registry_registration(self):
        """StrategyRegistry cleanly resolves 'vwap'."""
        self.assertIn("vwap", StrategyRegistry.list_available())
        strat = StrategyRegistry.get("vwap")
        self.assertEqual(strat.name, "vwap")

    def test_long_breakout_signal(self):
        """Price holding above VWAP with macro trend alignment emits actionable LONG."""
        metrics = {
            "close": 102.0,
            "vwap": 100.0,
            "vwap_dist_pct": 2.0,
            "sma200": 95.0,
            "ema20": 98.0,
            "ema50": 96.0,
            "plus_di": 28.0,
            "minus_di": 14.0,
            "adx": 26.0,
            "earnings_in_days": 30.0,
        }
        signal = self.strategy.evaluate_candidate("NVDA", metrics)
        self.assertEqual(signal.direction, "LONG")
        self.assertTrue(signal.is_actionable)
        self.assertGreaterEqual(signal.conviction, 0.70)
        self.assertIn("VWAP Session Long", signal.rationale)
        self.assertIsNone(signal.gate_rejections)

    def test_broad_index_short_signal(self):
        """Broad market benchmark (QQQ) trading below VWAP emits actionable SHORT."""
        metrics = {
            "close": 98.0,
            "vwap": 100.0,
            "vwap_dist_pct": -2.0,
            "sma200": 105.0,
            "ema20": 99.0,
            "ema50": 101.0,
            "plus_di": 15.0,
            "minus_di": 30.0,
            "adx": 25.0,
            "earnings_in_days": None,
        }
        signal = self.strategy.evaluate_candidate("QQQ", metrics)
        self.assertEqual(signal.direction, "SHORT")
        self.assertTrue(signal.is_actionable)
        self.assertGreaterEqual(signal.conviction, 0.75)
        self.assertIn("3x Inverse Short ETP", signal.rationale)
        self.assertIsNone(signal.gate_rejections)

    def test_single_equity_cash_defense_below_vwap(self):
        """Single equity (NVDA) trading below VWAP enforces 100% Cash Defense (NEUTRAL)."""
        metrics = {
            "close": 98.0,
            "vwap": 100.0,
            "vwap_dist_pct": -2.0,
            "sma200": 95.0,
            "ema20": 97.0,
            "ema50": 96.0,
            "plus_di": 15.0,
            "minus_di": 28.0,
            "adx": 24.0,
            "earnings_in_days": 45.0,
        }
        signal = self.strategy.evaluate_candidate("NVDA", metrics)
        self.assertEqual(signal.direction, "NEUTRAL")
        self.assertFalse(signal.is_actionable)
        self.assertEqual(signal.conviction, 0.0)
        self.assertIn("Cash Defense", signal.rationale)
        self.assertIsNotNone(signal.gate_rejections)
        self.assertEqual(signal.gate_rejections[0]["gate_id"], "dual_track_gate:SINGLE_EQUITY_CASH_DEFENSE")

    def test_vwap_dead_zone_rejection(self):
        """Price hovering within min_vwap_dist_pct buffer emits NEUTRAL."""
        metrics = {
            "close": 100.04,
            "vwap": 100.0,
            "vwap_dist_pct": 0.04,  # below default 0.1% min distance
            "sma200": 95.0,
            "ema20": 98.0,
            "ema50": 96.0,
            "earnings_in_days": 20.0,
        }
        signal = self.strategy.evaluate_candidate("AAPL", metrics)
        self.assertEqual(signal.direction, "NEUTRAL")
        self.assertEqual(signal.conviction, 0.0)
        self.assertIn("dead-zone buffer", signal.rationale)
        self.assertIsNotNone(signal.gate_rejections)
        self.assertEqual(signal.gate_rejections[0]["gate_id"], "trend_filter:VWAP_DEAD_ZONE")

    def test_vwap_overextension_rejection(self):
        """Price extending beyond max_vwap_dist_pct (+3.5%) is rejected for exhaustion risk."""
        metrics = {
            "close": 105.0,
            "vwap": 100.0,
            "vwap_dist_pct": 5.0,  # exceeds default 3.5%
            "sma200": 95.0,
            "ema20": 98.0,
            "ema50": 96.0,
            "earnings_in_days": 20.0,
        }
        signal = self.strategy.evaluate_candidate("TSLA", metrics)
        self.assertEqual(signal.direction, "NEUTRAL")
        self.assertEqual(signal.conviction, 0.0)
        self.assertIsNotNone(signal.gate_rejections)
        gate_ids = [g["gate_id"] for g in signal.gate_rejections]
        self.assertIn("trend_filter:VWAP_OVEREXTENDED", gate_ids)

    def test_earnings_blackout_rejection(self):
        """Earnings within blackout window rejects setup even during valid VWAP breakout."""
        metrics = {
            "close": 102.0,
            "vwap": 100.0,
            "vwap_dist_pct": 2.0,
            "sma200": 95.0,
            "ema20": 98.0,
            "ema50": 96.0,
            "earnings_in_days": 1.0,  # within 2.0d blackout
        }
        signal = self.strategy.evaluate_candidate("MSFT", metrics)
        self.assertEqual(signal.direction, "NEUTRAL")
        self.assertFalse(signal.is_actionable)
        self.assertIsNotNone(signal.gate_rejections)
        gate_ids = [g["gate_id"] for g in signal.gate_rejections]
        self.assertIn("catalyst_gate:EARNINGS_BLACKOUT", gate_ids)


class TestVWAPIndicatorEngine(unittest.TestCase):
    """Test suite for MarketDataProvider VWAP indicator calculations."""

    def test_compute_indicators_pure_python_fallback(self):
        """Pure-python candle list computation yields valid VWAP metrics."""
        candles = [
            {"date": f"2026-11-{i:02d}", "open": 100.0, "high": 105.0, "low": 98.0, "close": 102.0, "volume": 1000.0 * i}
            for i in range(1, 40)
        ]
        metrics = MarketDataProvider.compute_indicators(candles, [], symbol="TEST")
        self.assertIn("vwap", metrics)
        self.assertIn("vwap_dist_pct", metrics)
        self.assertIn("vwap_upper_1std", metrics)
        self.assertIn("vwap_lower_1std", metrics)
        self.assertGreater(metrics["vwap"], 0.0)
        self.assertGreaterEqual(metrics["vwap_upper_1std"], metrics["vwap"])
        self.assertLessEqual(metrics["vwap_lower_1std"], metrics["vwap"])


if __name__ == "__main__":
    unittest.main()
