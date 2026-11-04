"""Base strategy interface and standardized signal data structures.

This module defines the abstract contract that all quantitative trading strategies
must implement, decoupling strategy algorithms from screening, multi-agent debate,
risk sizing, and broker execution layers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class StrategySignal:
    """Standardized trading signal emitted by a BaseStrategy implementation."""

    symbol: str
    direction: str  # "LONG", "SHORT", or "NEUTRAL"
    conviction: float  # 0.0 to 1.0
    setup_quality: str  # "unfavorable", "mediocre", "favorable", "exceptional"
    strategy_name: str
    rationale: str
    metrics: Dict[str, Any] = field(default_factory=dict)
    bull_evidence: List[str] = field(default_factory=list)
    bear_evidence: List[str] = field(default_factory=list)
    catalyst_risk: float = 0.0
    timeframe: str = "1d"

    @property
    def is_actionable(self) -> bool:
        """Return True if the signal indicates an actionable entry."""
        return self.direction in ("LONG", "SHORT") and self.conviction > 0.0


class BaseStrategy(ABC):
    """Abstract Base Class for quantitative trading strategies.

    Subclasses encapsulate all signal generation, technical indicator thresholds,
    trend filtering, and market regime heuristics.
    """

    def __init__(self, name: str, **kwargs: Any) -> None:
        self.name = name

    @abstractmethod
    def evaluate_candidate(
        self, ticker: str, metrics: Dict[str, Any]
    ) -> StrategySignal:
        """Evaluate technical indicators for a single asset and emit a StrategySignal.

        Args:
            ticker: Underlying asset symbol (e.g. "QQQ", "AAPL", "BTC-USD").
            metrics: Pre-computed technical and fundamental indicators.

        Returns:
            StrategySignal with direction, conviction, and dialectic evidence.
        """
        raise NotImplementedError

    @abstractmethod
    def filter_universe(
        self, universe_metrics: Dict[str, Dict[str, Any]], top_n: int = 5
    ) -> List[StrategySignal]:
        """Screen and rank universe metrics, returning top-ranked actionable signals.

        Args:
            universe_metrics: Mapping of ticker to indicator metrics.
            top_n: Maximum number of candidate signals to return.

        Returns:
            List of top-ranked StrategySignal instances.
        """
        raise NotImplementedError
