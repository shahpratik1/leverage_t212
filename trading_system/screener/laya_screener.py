"""System 1 fast screening layer using the Laya non-autoregressive NLI engine.

This module provides rapid directional bias classification, setup quality
scoring, and catalyst-risk estimation for a universe of assets.  It uses
Laya's ``Router`` (ModernBERT-backed) to evaluate natural-language state
documents derived from technical metrics, returning structured screening
results suitable for downstream portfolio construction.
"""

from __future__ import annotations

import concurrent.futures
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List

try:
    from laya import Router
    HAS_LAYA = True
except ImportError:
    Router = None
    HAS_LAYA = False

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScreenerResult:
    """Immutable result produced by :class:`LayaScreener` for a single asset.

    Attributes:
        ticker: Asset symbol (e.g. ``"AAPL"``).
        bias: Directional classification — ``"bullish"``, ``"bearish"``,
            or ``"neutral"``.
        bias_confidence: Laya confidence score for the bias classification.
        setup_score: Ordinal quality label — ``"unfavorable"``,
            ``"mediocre"``, ``"favorable"``, or ``"exceptional"``.
        setup_confidence: Laya confidence score for the setup classification.
        catalyst_risk: Probability (0.0–1.0) that an upcoming catalyst
            (e.g. earnings) will adversely affect the position.
        overall_confidence: Aggregate confidence across all questions.
        metrics: Original technical-indicator dict that was screened.
        state_document: The formatted context string sent to Laya.
    """

    ticker: str
    bias: str
    bias_confidence: float
    setup_score: str
    setup_confidence: float
    catalyst_risk: float
    overall_confidence: float
    strategy_type: str = "momentum"  # "momentum" or "3rsi_mean_reversion"
    metrics: Dict[str, Any] = field(default_factory=dict)
    state_document: str = ""


# ---------------------------------------------------------------------------
# Screener
# ---------------------------------------------------------------------------


class LayaScreener:
    """System 1 screener powered by the Laya NLI engine.

    Parameters:
        min_setup_score: Minimum acceptable setup quality label.  Assets
            scoring below this are filtered out.  Default ``"favorable"``.
        max_catalyst_risk: Maximum acceptable catalyst-risk probability.
            Assets above this threshold are filtered out.  Default ``0.25``.
        top_n: Number of top-ranked candidates to return from
            :meth:`scan_universe`.  Default ``5``.
    """

    # Canonical ordering for the ``score``-type setup labels.
    _SCORE_RANK: Dict[str, int] = {
        "unfavorable": 0,
        "mediocre": 1,
        "favorable": 2,
        "exceptional": 3,
    }

    def __init__(
        self,
        min_setup_score: str = "favorable",
        max_catalyst_risk: float = 0.25,
        top_n: int = 5,
    ) -> None:
        self.router: Any = Router(preload=True) if (HAS_LAYA and Router is not None) else None
        self.min_setup_score: str = min_setup_score
        self.max_catalyst_risk: float = max_catalyst_risk
        self.top_n: int = top_n
        self._score_rank: Dict[str, int] = dict(self._SCORE_RANK)

        # ----- Laya question bank -----
        self.questions: Dict[str, Dict[str, Any]] = {
            "bias": {
                "type": "choice",
                "instructions": "What is the technical market structure and momentum bias?",
                "criteria": {
                    "bullish": "Price above Daily 200 SMA, 20 EMA > 50 EMA, RSI > 50, breaking out or pulling back to support",
                    "bearish": "Price below 200 SMA, breakdown below support, RSI < 45, heavy sell volume",
                    "neutral": "Choppy sideways consolidation, range-bound, conflicting moving averages",
                },
            },
            "setup_score": {
                "type": "score",
                "instructions": "Rate the breakout or trend-following quality and reward-to-risk asymmetry.",
                "criteria": [
                    "unfavorable",
                    "mediocre",
                    "favorable",
                    "exceptional",
                ],
            },
            "catalyst_risk": {
                "type": "noul",
                "instructions": (
                    "Is there an imminent earnings release, FDA decision, "
                    "or macro binary event within 48 hours?"
                ),
            },
        }

    # ------------------------------------------------------------------
    # State-document formatting
    # ------------------------------------------------------------------

    @staticmethod
    def format_asset_state(ticker: str, metrics: Dict[str, Any]) -> str:
        """Build a natural-language state document for Laya.

        The document summarises price structure, trend, momentum, volatility,
        and upcoming catalysts in a deterministic, human-readable format.

        Args:
            ticker: Asset symbol.
            metrics: Dictionary of pre-computed technical indicators.  Expected
                keys (from ``MarketDataProvider.compute_indicators``):
                ``close``, ``sma200``, ``sma200_dist``, ``ema20``,
                ``ema50``, ``trend``, ``rsi``, ``adx``, ``atr``,
                ``high_20d``, and optionally ``earnings_in_days``.

        Returns:
            Formatted multi-line context string.
        """
        close: float = metrics.get("close", 0.0)
        sma200: float = metrics.get("sma200", 0.0)
        sma200_dist: float = metrics.get("sma200_dist", 0.0)
        rsi3: float = metrics.get("rsi3", metrics.get("rsi", 50.0))
        rsi7: float = metrics.get("rsi7", metrics.get("rsi", 50.0))
        rsi14: float = metrics.get("rsi14", metrics.get("rsi", 50.0))
        sma5: float = metrics.get("sma5", close)

        earnings_val = metrics.get("earnings_in_days")
        earnings_info: str = (
            f"{earnings_val:.1f}d"
            if isinstance(earnings_val, (int, float))
            else (str(earnings_val) if earnings_val is not None else "None within 7d")
        )

        state: str = (
            f"Asset: {ticker}\n"
            f"Daily Close: {close:.2f} | 200 SMA: {sma200:.2f} "
            f"(Distance: {sma200_dist:+.1f}%)\n"
            f"1H Trend: 20 EMA={metrics.get('ema20', 0.0):.2f}, "
            f"50 EMA={metrics.get('ema50', 0.0):.2f} | "
            f"Trend: {metrics.get('trend', 'unknown')}\n"
            f"Momentum: RSI(3)={rsi3:.1f}, RSI(7)={rsi7:.1f}, "
            f"RSI(14)={rsi14:.1f}, ADX(14)={metrics.get('adx', 0.0):.1f}\n"
            f"Mean Reversion Benchmarks: 5-Day SMA={sma5:.2f}, "
            f"ATR(14)={metrics.get('atr', 0.0):.2f}\n"
            f"Upcoming Earnings: {earnings_info}"
        )
        return state

    # ------------------------------------------------------------------
    # Single-asset evaluation
    # ------------------------------------------------------------------

    def evaluate_single(
        self, ticker: str, metrics: Dict[str, Any]
    ) -> ScreenerResult:
        """Screen a single asset and return a :class:`ScreenerResult`.

        On any error the method returns a *neutral* result with zero
        confidence so that downstream filters safely exclude it.

        Args:
            ticker: Asset symbol.
            metrics: Technical-indicator dictionary.

        Returns:
            Populated :class:`ScreenerResult`.
        """
        state: str = self.format_asset_state(ticker, metrics)

        close = float(metrics.get("close", 0.0))
        sma200 = float(metrics.get("sma200", 0.0))
        ema20 = float(metrics.get("ema20", 0.0))
        ema50 = float(metrics.get("ema50", 0.0))
        rsi3 = float(metrics.get("rsi3", 50.0))
        rsi7 = float(metrics.get("rsi7", 50.0))
        rsi14 = float(metrics.get("rsi14", metrics.get("rsi", 50.0)))
        adx = float(metrics.get("adx", 20.0))

        # ── 1. Priority High Win-Rate Check: 3-RSI Multi-Timeframe Mean Reversion ──
        earnings_val = metrics.get("earnings_in_days")
        is_earnings_blackout = (
            earnings_val is not None
            and isinstance(earnings_val, (int, float))
            and 0.0 <= earnings_val <= 2.0
        )
        base_catalyst_risk = 0.90 if is_earnings_blackout else 0.05

        # Bullish Dip: Macro bull (close > sma200), extreme short-term liquidation (rsi3 <= 22, rsi7 <= 42)
        if close > sma200 and rsi3 <= 22.0 and rsi7 <= 42.0:
            return ScreenerResult(
                ticker=ticker,
                bias="bullish",
                bias_confidence=0.95,
                setup_score="exceptional",
                setup_confidence=0.95,
                catalyst_risk=base_catalyst_risk,
                overall_confidence=0.95,
                strategy_type="3rsi_mean_reversion",
                metrics=metrics,
                state_document=state,
            )
        # Bearish Overbought: Macro bear (close < sma200), short-squeeze exhaustion (rsi3 >= 78, rsi7 >= 58)
        elif close < sma200 and rsi3 >= 78.0 and rsi7 >= 58.0:
            return ScreenerResult(
                ticker=ticker,
                bias="bearish",
                bias_confidence=0.92,
                setup_score="exceptional",
                setup_confidence=0.92,
                catalyst_risk=base_catalyst_risk,
                overall_confidence=0.92,
                strategy_type="3rsi_mean_reversion",
                metrics=metrics,
                state_document=state,
            )

        # ── 2. Rule-based evaluation fallback when Laya engine is unavailable ──
        if self.router is None:
            if close > sma200 and ema20 > ema50 and rsi14 > 50.0:
                bias = "bullish"
                setup_score = "exceptional" if (rsi14 > 58.0 and adx >= 25.0) else "favorable"
            elif close < sma200 and ema20 < ema50 and rsi14 < 48.0:
                bias = "bearish"
                setup_score = "exceptional" if (rsi14 < 40.0 and adx >= 25.0) else "favorable"
            else:
                bias = "neutral"
                setup_score = "mediocre"

            return ScreenerResult(
                ticker=ticker,
                bias=bias,
                bias_confidence=0.85,
                setup_score=setup_score,
                setup_confidence=0.85,
                catalyst_risk=base_catalyst_risk,
                overall_confidence=0.85,
                strategy_type="momentum",
                metrics=metrics,
                state_document=state,
            )

        try:
            raw: Dict[str, Any] = self.router.predict(state, self.questions)
        except Exception:
            logger.exception("Laya prediction failed for %s", ticker)
            return ScreenerResult(
                ticker=ticker,
                bias="neutral",
                bias_confidence=0.0,
                setup_score="unfavorable",
                setup_confidence=0.0,
                catalyst_risk=1.0,
                overall_confidence=0.0,
                metrics=metrics,
                state_document=state,
            )

        answers: Dict[str, Any] = raw.get("answers", {})
        overall_confidence: float = float(raw.get("answer_confidence", 0.0))

        # --- Parse bias (choice question) ---
        bias_answer: Dict[str, Any] = answers.get("bias", {})
        bias: str = str(bias_answer.get("choice", "neutral"))
        bias_confidence: float = float(bias_answer.get("confidence", 0.0))

        # --- Parse setup_score (score question) ---
        setup_answer: Dict[str, Any] = answers.get("setup_score", {})
        setup_score: str = str(setup_answer.get("choice", "unfavorable"))
        setup_confidence: float = float(setup_answer.get("confidence", 0.0))

        # --- Parse catalyst_risk (noul question) ---
        catalyst_answer: Dict[str, Any] = answers.get("catalyst_risk", {})
        catalyst_risk: float = float(catalyst_answer.get("noul", 1.0))

        return ScreenerResult(
            ticker=ticker,
            bias=bias,
            bias_confidence=bias_confidence,
            setup_score=setup_score,
            setup_confidence=setup_confidence,
            catalyst_risk=catalyst_risk,
            overall_confidence=overall_confidence,
            metrics=metrics,
            state_document=state,
        )

    # ------------------------------------------------------------------
    # Universe scan
    # ------------------------------------------------------------------

    def scan_universe(
        self, universe_metrics: Dict[str, Dict[str, Any]]
    ) -> List[ScreenerResult]:
        """Screen an entire universe of assets in parallel.

        Assets are evaluated concurrently (up to 4 threads).  Results are
        filtered by :meth:`passes_filter`, sorted by setup quality and
        confidence, and trimmed to :attr:`top_n`.

        Args:
            universe_metrics: Mapping of ``{ticker: metrics_dict}``.

        Returns:
            Sorted list of the top *N* qualifying :class:`ScreenerResult`
            instances (may be shorter than *N* if fewer assets pass).
        """
        n_assets: int = len(universe_metrics)
        logger.info("Screening %d assets...", n_assets)

        results: List[ScreenerResult] = []

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            future_to_ticker: Dict[
                concurrent.futures.Future[ScreenerResult], str
            ] = {
                executor.submit(self.evaluate_single, ticker, metrics): ticker
                for ticker, metrics in universe_metrics.items()
            }

            for future in concurrent.futures.as_completed(future_to_ticker):
                ticker = future_to_ticker[future]
                try:
                    result: ScreenerResult = future.result()
                except Exception:
                    logger.exception(
                        "Unexpected error evaluating %s", ticker
                    )
                    continue
                results.append(result)

        # Filter ----------------------------------------------------------
        passed: List[ScreenerResult] = [
            r for r in results if self.passes_filter(r)
        ]

        for r in passed:
            logger.info(
                "Passed: %s (score=%s, risk=%.3f)",
                r.ticker,
                r.setup_score,
                r.catalyst_risk,
            )

        # Sort: best setup first, then highest confidence ----------------
        passed.sort(
            key=lambda r: (
                self._score_rank.get(r.setup_score, 0),
                r.overall_confidence,
            ),
            reverse=True,
        )

        # Diversified selection across market categories/sectors
        # Ensures balanced representation across Tech, Indices, Commodities, Bonds, Crypto, Currencies
        try:
            from trading_system.data.leveraged_universe import LeveragedUniverse
            univ = LeveragedUniverse()
            category_counts: Dict[str, int] = {}
            diversified: List[ScreenerResult] = []
            overflow: List[ScreenerResult] = []
            max_per_category = 2

            for r in passed:
                cat = univ.get_category_for_symbol(r.ticker)
                count = category_counts.get(cat, 0)
                if count < max_per_category:
                    diversified.append(r)
                    category_counts[cat] = count + 1
                else:
                    overflow.append(r)

            return (diversified + overflow)[: self.top_n]
        except Exception as exc:
            logger.debug("Sector-diversified selection fallback: %s", exc)
            return passed[: self.top_n]

    # ------------------------------------------------------------------
    # Filter predicate
    # ------------------------------------------------------------------

    def passes_filter(self, result: ScreenerResult) -> bool:
        """Check whether a screening result meets minimum quality criteria.

        A result passes if **all** of the following hold:

        * Bias is ``"bullish"`` or ``"bearish"`` (neutral is rejected).
        * ADX is >= 20.0 to prevent volatility decay in choppy markets.
        * Setup score is at or above :attr:`min_setup_score`.
        * Catalyst risk is strictly below :attr:`max_catalyst_risk`.

        Args:
            result: The :class:`ScreenerResult` to evaluate.

        Returns:
            ``True`` if the result passes all filters.
        """
        if result.bias not in ("bullish", "bearish"):
            return False

        # Volatility decay protection for 3x leverage
        adx: float = result.metrics.get("adx", 0.0)
        if result.strategy_type == "3rsi_mean_reversion":
            if adx < 15.0:
                return False
        else:
            if adx < 20.0:
                return False

        result_rank: int = self._score_rank.get(result.setup_score, 0)
        min_rank: int = self._score_rank.get(self.min_setup_score, 0)
        if result_rank < min_rank:
            return False

        if result.catalyst_risk >= self.max_catalyst_risk:
            return False

        return True
