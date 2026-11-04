"""3-Period RSI Mean Reversion Strategy.

Encapsulates Connors 3-RSI pullback rules into an isolated strategy class
conforming to the BaseStrategy protocol.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from trading_system.config import settings
from trading_system.strategies.base import BaseStrategy, StrategySignal
from trading_system.strategies.registry import StrategyRegistry

logger = logging.getLogger(__name__)


@StrategyRegistry.register("3rsi_mean_reversion")
class Rsi3MeanReversionStrategy(BaseStrategy):
    """Connors 3-period RSI pullback mean reversion strategy."""

    def __init__(
        self,
        name: str = "3rsi_mean_reversion",
        rsi3_oversold: Optional[float] = None,
        rsi7_oversold: Optional[float] = None,
        max_catalyst_risk: Optional[float] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(name=name, **kwargs)
        self.rsi3_oversold = (
            rsi3_oversold
            if rsi3_oversold is not None
            else getattr(settings, "RSI3_OVERSOLD", 28.0)
        )
        self.rsi7_oversold = (
            rsi7_oversold
            if rsi7_oversold is not None
            else getattr(settings, "RSI7_OVERSOLD", 45.0)
        )
        self.max_catalyst_risk = (
            max_catalyst_risk
            if max_catalyst_risk is not None
            else getattr(settings, "SCREENER_MAX_CATALYST_RISK", 0.25)
        )

    def evaluate_candidate(
        self, ticker: str, metrics: Dict[str, Any]
    ) -> StrategySignal:
        """Evaluate a single asset for 3-RSI mean reversion entry."""
        close = float(metrics.get("close", 0.0) or 0.0)
        sma200 = float(metrics.get("sma200", 0.0) or 0.0)
        rsi3 = float(metrics.get("rsi3", metrics.get("rsi", 50.0)) or 50.0)
        rsi7 = float(metrics.get("rsi7", metrics.get("rsi", 50.0)) or 50.0)
        sma5 = float(metrics.get("sma5", close) or close)

        # Catalyst risk from earnings proximity
        earnings_val = metrics.get("earnings_in_days")
        is_earnings_blackout = (
            earnings_val is not None
            and isinstance(earnings_val, (int, float))
            and 0.0 <= earnings_val <= 2.0
        )
        catalyst_risk = 0.90 if is_earnings_blackout else 0.05

        bull_evidence: List[str] = []
        bear_evidence: List[str] = []

        # 1. Bullish Dip: Macro bull (close > sma200), short-term pullback (rsi3 <= oversold)
        if close > sma200 and rsi3 <= self.rsi3_oversold and rsi7 <= self.rsi7_oversold:
            bull_evidence.append(
                f"Short-term 3-RSI pullback (RSI3={rsi3:.1f} <= {self.rsi3_oversold:.1f}) with price above 200 SMA."
            )
            bull_evidence.append(
                f"5-day SMA at {sma5:.2f} provides strong magnetic reversion target."
            )
            bull_evidence.append(
                f"Macro uptrend intact: Price ({close:.2f}) > 200 SMA ({sma200:.2f})."
            )
            return StrategySignal(
                symbol=ticker,
                direction="LONG",
                conviction=0.95,
                setup_quality="exceptional",
                strategy_name=self.name,
                rationale=(
                    f"3-RSI Mean Reversion: Confirmed pullback on {ticker} (RSI3={rsi3:.1f} <= {self.rsi3_oversold:.1f}) "
                    f"with price above 200 SMA."
                ),
                metrics=metrics,
                bull_evidence=bull_evidence,
                bear_evidence=bear_evidence,
                catalyst_risk=catalyst_risk,
                timeframe="1d",
            )

        # 2. Bearish Rip: Macro bear (close < sma200), short-term overbought
        rsi3_overbought = 100.0 - self.rsi3_oversold
        rsi7_overbought = 100.0 - self.rsi7_oversold
        if close < sma200 and rsi3 >= rsi3_overbought and rsi7 >= rsi7_overbought:
            bear_evidence.append(
                f"Short-term 3-RSI overbought (RSI3={rsi3:.1f} >= {rsi3_overbought:.1f}) with price below 200 SMA."
            )
            bear_evidence.append(
                f"Macro downtrend intact: Price ({close:.2f}) < 200 SMA ({sma200:.2f})."
            )
            return StrategySignal(
                symbol=ticker,
                direction="SHORT",
                conviction=0.95,
                setup_quality="exceptional",
                strategy_name=self.name,
                rationale=(
                    f"3-RSI Mean Reversion: Overbought snapback on {ticker} (RSI3={rsi3:.1f} >= {rsi3_overbought:.1f}) "
                    f"with price below 200 SMA."
                ),
                metrics=metrics,
                bull_evidence=bull_evidence,
                bear_evidence=bear_evidence,
                catalyst_risk=catalyst_risk,
                timeframe="1d",
            )

        # 3. Neutral / No Signal
        return StrategySignal(
            symbol=ticker,
            direction="NEUTRAL",
            conviction=0.0,
            setup_quality="unfavorable",
            strategy_name=self.name,
            rationale=f"RSI(3)={rsi3:.1f} is within normal bounds. No mean reversion extreme.",
            metrics=metrics,
            bull_evidence=bull_evidence,
            bear_evidence=bear_evidence,
            catalyst_risk=catalyst_risk,
            timeframe="1d",
        )

    def filter_universe(
        self, universe_metrics: Dict[str, Dict[str, Any]], top_n: int = 5
    ) -> List[StrategySignal]:
        """Screen universe metrics and return top-ranked actionable signals."""
        signals: List[StrategySignal] = []

        for ticker, metrics in universe_metrics.items():
            try:
                sig = self.evaluate_candidate(ticker, metrics)
                if sig.is_actionable and sig.catalyst_risk <= self.max_catalyst_risk:
                    signals.append(sig)
            except Exception as exc:
                logger.debug("3-RSI evaluation failed for %s: %s", ticker, exc)

        # Prioritize extreme pullbacks (lowest RSI3 for Long, highest for Short)
        def _sort_key(s: StrategySignal) -> float:
            r3 = float(s.metrics.get("rsi3", 50.0) or 50.0)
            return r3 if s.direction == "LONG" else (100.0 - r3)

        signals.sort(key=_sort_key)
        return signals[:top_n]
