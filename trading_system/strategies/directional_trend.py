"""Directional Multi-Timeframe Trend Strategy.

Implements Directional Movement Index (+DI vs -DI) polarity gating combined with
a multi-timeframe Daily 20/50 EMA slope hierarchy and dual-track short execution.
Eliminates counter-trend knife-catching and trades inverse short ETPs exclusively
on broad market indices during verified bear impulses.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set

from trading_system.config import settings
from trading_system.strategies.base import BaseStrategy, StrategySignal
from trading_system.strategies.registry import StrategyRegistry

logger = logging.getLogger(__name__)

# Broad index benchmarks eligible for inverse 3x Short ETP execution
DEFAULT_BROAD_INDICES: Set[str] = {
    "QQQ",
    "SPY",
    "SMH",
    "^NDX",
    "^GSPC",
    "^STOXX50E",
    "3USL",
    "QQQ3",
}


@StrategyRegistry.register("directional_trend")
class DirectionalTrendStrategy(BaseStrategy):
    """Directional trend strategy with DMI polarity gating and dual-track shorting."""

    def __init__(
        self,
        name: str = "directional_trend",
        min_adx_trend: Optional[float] = None,
        max_catalyst_risk: Optional[float] = None,
        broad_indices: Optional[Set[str]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(name=name, **kwargs)
        self.min_adx_trend = (
            min_adx_trend
            if min_adx_trend is not None
            else getattr(settings, "MIN_ADX_TREND", 18.0)
        )
        self.max_catalyst_risk = (
            max_catalyst_risk
            if max_catalyst_risk is not None
            else getattr(settings, "SCREENER_MAX_CATALYST_RISK", 0.25)
        )
        self.broad_indices = broad_indices or set(DEFAULT_BROAD_INDICES)

    def is_broad_index(self, ticker: str) -> bool:
        """Return True if ticker is a broad benchmark index eligible for short ETPs."""
        sym_clean = ticker.upper().replace("^", "")
        return (
            ticker.upper() in self.broad_indices
            or sym_clean in self.broad_indices
            or any(idx in sym_clean for idx in ("QQQ", "SPY", "SMH", "STOXX"))
        )

    def evaluate_candidate(
        self, ticker: str, metrics: Dict[str, Any]
    ) -> StrategySignal:
        """Evaluate an asset using multi-timeframe DMI polarity and EMA slope."""
        close = float(metrics.get("close", 0.0) or 0.0)
        sma200 = float(metrics.get("sma200", 0.0) or 0.0)
        ema20 = float(metrics.get("ema20_daily", metrics.get("ema20", 0.0)) or 0.0)
        ema50 = float(metrics.get("ema50_daily", metrics.get("ema50", 0.0)) or 0.0)
        adx = float(metrics.get("adx", 0.0) or 0.0)
        plus_di = float(metrics.get("plus_di", 25.0 if close > ema20 else 15.0) or 0.0)
        minus_di = float(metrics.get("minus_di", 15.0 if close > ema20 else 25.0) or 0.0)
        slope_raw = metrics.get("ema20_slope_5d")
        if slope_raw is not None:
            slope_20 = float(slope_raw)
        else:
            slope_20 = 0.01 if (close > ema20 or (ema50 > 0 and ema20 > ema50)) else -0.01

        macd = float(metrics.get("macd", 0.0) or 0.0)
        macd_sig = float(metrics.get("macd_signal", 0.0) or 0.0)
        has_macd = "macd" in metrics and "macd_signal" in metrics
        macd_conflicts_bull = has_macd and macd < macd_sig
        macd_conflicts_bear = has_macd and macd > macd_sig

        # Catalyst risk assessment (e.g. impending earnings)
        earnings_val = metrics.get("earnings_in_days")
        is_earnings_blackout = (
            earnings_val is not None
            and isinstance(earnings_val, (int, float))
            and 0.0 <= earnings_val <= 2.0
        )
        catalyst_risk = 0.90 if is_earnings_blackout else 0.05

        bull_evidence: List[str] = []
        bear_evidence: List[str] = []

        is_macro_bull = close > sma200 if sma200 > 0 else True
        is_intermediate_bull = (
            (close > ema50 and ema20 >= ema50 and slope_20 > 0.0)
            if ema50 > 0.0
            else (close > ema20 and slope_20 > 0.0)
        )
        is_dmi_bull = plus_di > minus_di and adx >= self.min_adx_trend
        is_dmi_bear = minus_di > plus_di and adx >= self.min_adx_trend

        # ── 1. Bullish Impulse Setup ─────────────────────────────
        if is_intermediate_bull and is_dmi_bull and not is_dmi_bear and not macd_conflicts_bull:
            conviction = round(min(0.95, 0.70 + (adx / 100.0) + (0.10 if is_macro_bull else 0.0)), 2)
            setup_quality = "exceptional" if adx >= 25.0 else "favorable"
            bull_evidence.append(
                f"Directional Polarity Bullish: +DI ({plus_di:.1f}) > -DI ({minus_di:.1f}) with ADX={adx:.1f} >= {self.min_adx_trend:.1f}."
            )
            bull_evidence.append(
                f"Multi-Timeframe Trend Aligned: Close ({close:.2f}) > 50 EMA ({ema50:.2f}) with 20 EMA upward slope ({slope_20:+.2f}%)."
            )
            if is_macro_bull:
                bull_evidence.append(f"Macro tailwind: Price ({close:.2f}) > 200 SMA ({sma200:.2f}).")

            return StrategySignal(
                symbol=ticker,
                direction="LONG",
                conviction=conviction,
                setup_quality=setup_quality,
                strategy_name=self.name,
                rationale=(
                    f"Directional Trend: Confirmed bullish momentum on {ticker} (+DI={plus_di:.1f} > -DI={minus_di:.1f}, "
                    f"ADX={adx:.1f}, 20 EMA slope={slope_20:+.2f}%)."
                ),
                metrics=metrics,
                bull_evidence=bull_evidence,
                bear_evidence=bear_evidence,
                catalyst_risk=catalyst_risk,
                timeframe="1d",
            )

        # ── 2. Bearish Regime & Dual-Track Short Execution ─────────
        if is_dmi_bear and (close < ema20 or slope_20 < 0.0) and not macd_conflicts_bear:
            bear_evidence.append(
                f"Directional Polarity Bearish: -DI ({minus_di:.1f}) > +DI ({plus_di:.1f}) with ADX={adx:.1f} >= {self.min_adx_trend:.1f}."
            )
            bear_evidence.append(
                f"Trend Breakdown: Close ({close:.2f}) < 20 EMA ({ema20:.2f}) with downward slope ({slope_20:+.2f}%)."
            )

            # Dual-track check: Index underlying vs Single Equity
            if self.is_broad_index(ticker):
                conviction = round(min(0.95, 0.75 + (adx / 100.0)), 2)
                setup_quality = "exceptional" if adx >= 25.0 else "favorable"
                bear_evidence.append(f"Dual-track rule: Benchmark index {ticker} approved for inverse 3x Short ETP execution.")

                return StrategySignal(
                    symbol=ticker,
                    direction="SHORT",
                    conviction=conviction,
                    setup_quality=setup_quality,
                    strategy_name=self.name,
                    rationale=(
                        f"Directional Trend SHORT: Bearish breakdown on benchmark index {ticker} "
                        f"(-DI={minus_di:.1f} > +DI={plus_di:.1f}, ADX={adx:.1f}, 20 EMA slope={slope_20:+.2f}%)."
                    ),
                    metrics=metrics,
                    bull_evidence=bull_evidence,
                    bear_evidence=bear_evidence,
                    catalyst_risk=catalyst_risk,
                    timeframe="1d",
                )
            else:
                # Single-stock cash defense
                bear_evidence.append(
                    f"Dual-track cash defense: Short ETP skipped for single stock {ticker} to protect against short squeeze risk."
                )
                return StrategySignal(
                    symbol=ticker,
                    direction="NEUTRAL",
                    conviction=0.0,
                    setup_quality="unfavorable",
                    strategy_name=self.name,
                    rationale=(
                        f"Cash Defense on {ticker}: Bear regime active (-DI={minus_di:.1f} > +DI={plus_di:.1f}, ADX={adx:.1f}). "
                        f"Long dip-buying locked out; equity shorting suppressed."
                    ),
                    metrics=metrics,
                    bull_evidence=bull_evidence,
                    bear_evidence=bear_evidence,
                    catalyst_risk=catalyst_risk,
                    timeframe="1d",
                )

        # ── 3. Neutral / Counter-Trend Filter ─────────────────────
        neutral_reason = "No clean directional trend impulse"
        if is_dmi_bear:
            neutral_reason = f"Bear regime active (-DI={minus_di:.1f} > +DI={plus_di:.1f}). Long dip-buying disarmed."
        elif slope_20 <= 0.0:
            neutral_reason = f"Intermediate 20 EMA slope ({slope_20:+.2f}%) is not positive. Dip-buying locked out to prevent knife-catching."
        elif close <= ema50:
            neutral_reason = f"Price ({close:.2f}) below 50 EMA ({ema50:.2f}). Waiting for trend reclaim."

        return StrategySignal(
            symbol=ticker,
            direction="NEUTRAL",
            conviction=0.0,
            setup_quality="unfavorable",
            strategy_name=self.name,
            rationale=neutral_reason,
            metrics=metrics,
            bull_evidence=bull_evidence,
            bear_evidence=bear_evidence,
            catalyst_risk=catalyst_risk,
            timeframe="1d",
        )

    def filter_universe(
        self, universe_metrics: Dict[str, Dict[str, Any]], top_n: int = 5
    ) -> List[StrategySignal]:
        """Filter universe and return top actionable directional trend signals."""
        signals: List[StrategySignal] = []

        for ticker, metrics in universe_metrics.items():
            try:
                sig = self.evaluate_candidate(ticker, metrics)
                if sig.is_actionable and sig.catalyst_risk <= self.max_catalyst_risk:
                    signals.append(sig)
            except Exception as exc:
                logger.debug("Directional trend evaluation failed for %s: %s", ticker, exc)

        # Sort by conviction descending, then ADX descending
        def _sort_key(s: StrategySignal) -> tuple[float, float]:
            adx_val = float(s.metrics.get("adx", 0.0) or 0.0)
            return (s.conviction, adx_val)

        signals.sort(key=_sort_key, reverse=True)
        return signals[:top_n]
