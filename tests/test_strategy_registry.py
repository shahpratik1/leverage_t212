"""Tests for StrategyRegistry factory and dynamic lookup."""

import unittest
from typing import Any, Dict, List
from trading_system.strategies.base import BaseStrategy, StrategySignal
from trading_system.strategies.registry import StrategyRegistry


class CustomTestStrategy(BaseStrategy):
    def evaluate_candidate(self, ticker: str, metrics: Dict[str, Any]) -> StrategySignal:
        return StrategySignal(
            symbol=ticker,
            direction="LONG",
            conviction=0.75,
            setup_quality="favorable",
            strategy_name="custom_test",
            rationale="Custom test rationale",
        )

    def filter_universe(
        self, universe_metrics: Dict[str, Dict[str, Any]], top_n: int = 5
    ) -> List[StrategySignal]:
        return [self.evaluate_candidate(k, v) for k, v in universe_metrics.items()][:top_n]


class TestStrategyRegistry(unittest.TestCase):
    def test_register_and_get(self):
        StrategyRegistry.register_class("custom_test", CustomTestStrategy)
        self.assertIn("custom_test", StrategyRegistry.list_available())

        instance = StrategyRegistry.get("custom_test")
        self.assertIsInstance(instance, CustomTestStrategy)
        self.assertEqual(instance.name, "custom_test")

        # Test case-insensitivity
        instance_upper = StrategyRegistry.get("CUSTOM_TEST")
        self.assertIsInstance(instance_upper, CustomTestStrategy)

    def test_get_unknown_strategy_raises(self):
        with self.assertRaises(KeyError):
            StrategyRegistry.get("non_existent_strategy_xyz")


if __name__ == "__main__":
    unittest.main()
