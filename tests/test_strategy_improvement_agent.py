"""Unit tests for StrategyImprovementAgent."""

from __future__ import annotations

import unittest
from trading_system.agents.strategy_improvement_agent import StrategyImprovementAgent


class TestStrategyImprovementAgent(unittest.TestCase):
    """Test suite for autonomous StrategyImprovementAgent."""

    def setUp(self) -> None:
        self.agent = StrategyImprovementAgent(zero_yield_threshold=3)

    def test_healthy_cycle_records_success(self) -> None:
        """When candidates are produced, status is HEALTHY and consecutive count resets to 0."""
        metrics = {
            "NVDA": {
                "close": 150.0,
                "sma200": 130.0,
                "ema20": 145.0,
                "ema50": 140.0,
                "rsi3": 45.0,
                "rsi7": 50.0,
                "rsi14": 55.0,
                "adx": 28.0,
                "atr": 3.5,
                "macd": 1.2,
                "macd_signal": 0.8,
                "realized_vol": 0.25,
            }
        }
        audit = self.agent.record_cycle_yield(candidate_count=2, universe_metrics=metrics)
        self.assertEqual(audit.health_status, "HEALTHY")
        self.assertEqual(audit.consecutive_zero_cycles, 0)
        self.assertEqual(len(audit.missing_indicators), 0)

    def test_missing_indicator_triggers_critical_alert(self) -> None:
        """When required indicators (e.g. MACD) are missing from metrics, health status is CRITICAL."""
        metrics = {
            "TSLA": {
                "close": 200.0,
                "sma200": 190.0,
                # Missing 'macd' and 'macd_signal'
            }
        }
        audit = self.agent.record_cycle_yield(candidate_count=0, universe_metrics=metrics)
        self.assertEqual(audit.health_status, "CRITICAL")
        self.assertIn("macd", audit.missing_indicators)
        self.assertIn("macd_signal", audit.missing_indicators)

    def test_starvation_triggers_when_zero_threshold_reached(self) -> None:
        """When 0 candidates persist past threshold, health status becomes STARVED with recommendations."""
        metrics = {
            "SPY": {
                "close": 500.0,
                "sma200": 480.0,
                "ema20": 495.0,
                "ema50": 490.0,
                "rsi3": 50.0,
                "rsi7": 50.0,
                "rsi14": 50.0,
                "adx": 16.0,
                "atr": 4.0,
                "macd": 0.5,
                "macd_signal": 0.5,
                "realized_vol": 0.12,
            }
        }
        self.agent.record_cycle_yield(candidate_count=0, universe_metrics=metrics)
        self.agent.record_cycle_yield(candidate_count=0, universe_metrics=metrics)
        audit = self.agent.record_cycle_yield(candidate_count=0, universe_metrics=metrics)

        self.assertEqual(audit.consecutive_zero_cycles, 3)
        self.assertEqual(audit.health_status, "STARVED")
        self.assertEqual(audit.regime, "LOW_VOL_CHOP")
        self.assertGreater(len(audit.recommendations), 0)

    def test_verify_parameter_adaptation(self) -> None:
        """Verify StrategyImprovementAgent can run automated backtest verification."""
        candles = []
        for i in range(40):
            d_str = f"2026-01-{i+1:02d}" if i < 31 else f"2026-02-{i-30:02d}"
            candles.append({
                "date": d_str,
                "open": 100.0 + i * 0.5,
                "high": 102.0 + i * 0.5,
                "low": 99.0 + i * 0.5,
                "close": 101.0 + i * 0.5,
                "volume": 1000000.0,
            })
        result = self.agent.verify_parameter_adaptation(
            symbol="QQQ",
            candidate_candles=candles,
            candidate_rsi3_threshold=25.0,
        )
        self.assertIn("verified", result)
        self.assertIn("candidate_win_rate", result)
        self.assertEqual(result["symbol"], "QQQ")



if __name__ == "__main__":
    unittest.main()
