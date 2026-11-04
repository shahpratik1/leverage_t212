"""Quantitative trading strategies package for modest-bose."""

from trading_system.strategies.base import BaseStrategy, StrategySignal
from trading_system.strategies.registry import StrategyRegistry

__all__ = ["BaseStrategy", "StrategySignal", "StrategyRegistry"]
