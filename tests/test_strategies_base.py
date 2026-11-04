"""Tests for BaseStrategy interface and StrategySignal contract."""

import unittest
from typing import Any, Dict, List
from trading_system.strategies.base import BaseStrategy, StrategySignal


class DummyStrategy(BaseStrategy):
    def evaluate_candidate(self, ticker: str, metrics: Dict[str, Any]) -> StrategySignal:
        return StrategySignal(
            symbol=ticker,
            direction="LONG",
            conviction=0.85,
            setup_quality="favorable",
            strategy_name=self.name,
            rationale="Test dummy breakout",
            metrics=metrics,
            bull_evidence=["Price above 20 EMA"],
            bear_evidence=[],
        )

    def filter_universe(
        self, universe_metrics: Dict[str, Dict[str, Any]], top_n: int = 5
    ) -> List[StrategySignal]:
        results = [self.evaluate_candidate(k, v) for k, v in universe_metrics.items()]
        return results[:top_n]


class TestBaseStrategy(unittest.TestCase):
    def test_strategy_signal_immutability(self):
        sig = StrategySignal(
            symbol="AAPL",
            direction="LONG",
            conviction=0.90,
            setup_quality="exceptional",
            strategy_name="dummy",
            rationale="Solid test",
        )
        self.assertEqual(sig.symbol, "AAPL")
        self.assertEqual(sig.direction, "LONG")
        self.assertTrue(sig.is_actionable)

        # Check frozen dataclass raises on mutation
        with self.assertRaises(Exception):
            sig.conviction = 0.50  # type: ignore

    def test_base_strategy_instantiation(self):
        # Concrete subclass instantiates fine
        strat = DummyStrategy("test_dummy")
        self.assertEqual(strat.name, "test_dummy")

        metrics = {"close": 150.0}
        sig = strat.evaluate_candidate("AAPL", metrics)
        self.assertEqual(sig.symbol, "AAPL")
        self.assertEqual(sig.direction, "LONG")
        self.assertEqual(sig.strategy_name, "test_dummy")

        universe = {"AAPL": metrics, "MSFT": metrics}
        filtered = strat.filter_universe(universe, top_n=1)
        self.assertEqual(len(filtered), 1)

    def test_base_strategy_cannot_instantiate_abstract(self):
        # BaseStrategy itself cannot be instantiated directly
        with self.assertRaises(TypeError):
            BaseStrategy("abstract")  # type: ignore


if __name__ == "__main__":
    unittest.main()
