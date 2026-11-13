"""Compatibility shim for legacy technical agent imports."""
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class TechnicalAnalysis:
    ticker: str
    trend_structure: str
    ma_alignment: str
    momentum_rating: str
    volume_assessment: str
    key_levels: Dict[str, float]
    summary: str
    confidence: float
    raw_analysis: str = ""
    market_regime: str = "TRENDING_UP"


class TechnicalAnalyst:
    """Legacy stub for TechnicalAnalyst."""

    def __init__(self, openai_api_key: Optional[str] = None):
        self.openai_api_key = openai_api_key

    def analyze(self, ticker: str, candles: Any = None) -> TechnicalAnalysis:
        return TechnicalAnalysis(
            ticker=ticker,
            trend_structure="BULLISH",
            ma_alignment="ALIGNED",
            momentum_rating="STRONG",
            volume_assessment="AVERAGE",
            key_levels={"support": 90.0, "resistance": 110.0},
            summary="Trend signal",
            confidence=0.8,
            raw_analysis="",
            market_regime="TRENDING_UP",
        )
