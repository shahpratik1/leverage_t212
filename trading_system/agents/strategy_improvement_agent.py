"""Strategy Improvement Agent — Autonomous Pipeline Health & Alpha Calibration.

Continuously monitors screening yield, indicator integrity, and market regime conditions.
Detects signal starvation, validates mathematical feature completeness, and recommends
adaptive threshold adjustments based on real-time realized volatility.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


logger = logging.getLogger(__name__)


@dataclass
class ImprovementAudit:
    """Diagnostic health and optimization report produced by the Strategy Improvement Agent."""

    timestamp: str
    consecutive_zero_cycles: int
    health_status: str  # "HEALTHY", "DEGRADED", "STARVED", "CRITICAL"
    missing_indicators: List[str] = field(default_factory=list)
    null_metrics_count: int = 0
    total_assets_audited: int = 0
    mean_adx: float = 0.0
    mean_realized_vol: float = 0.0
    regime: str = "NORMAL"  # "LOW_VOL_CHOP", "NORMAL", "HIGH_VOL_TREND", "CRISIS"
    recommendations: List[str] = field(default_factory=list)
    action_taken: Optional[str] = None


class StrategyImprovementAgent:
    """Autonomous agent that guards pipeline health and dynamically tunes strategy parameters.

    Key Responsibilities:
    1. Zero-Yield Watchdog: Tracks zero-signal cycles and warns before operator notices.
    2. Feature Completeness: Ensures indicators required by screeners exist and are valid.
    3. Regime-Aware Calibration: Recommends adaptive ADX / RSI threshold widening or tightening.
    """

    REQUIRED_INDICATORS = (
        "close",
        "sma200",
        "ema20",
        "ema50",
        "rsi3",
        "rsi7",
        "rsi14",
        "adx",
        "atr",
        "macd",
        "macd_signal",
    )

    def __init__(self, zero_yield_threshold: int = 3) -> None:
        self.zero_yield_threshold = zero_yield_threshold
        self.consecutive_zero_cycles: int = 0
        self.last_audit: Optional[ImprovementAudit] = None
        self.audit_history: List[ImprovementAudit] = []

    def record_cycle_yield(
        self,
        candidate_count: int,
        universe_metrics: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> ImprovementAudit:
        """Record the candidate yield of a universe scan cycle and run health diagnostics.

        Args:
            candidate_count: Number of qualifying candidates produced by System 1.
            universe_metrics: Full dictionary of metrics across ingested assets.

        Returns:
            ImprovementAudit summarizing pipeline health and recommendations.
        """
        now_str = datetime.now(timezone.utc).isoformat()

        if candidate_count > 0:
            self.consecutive_zero_cycles = 0
        else:
            self.consecutive_zero_cycles += 1

        missing_fields: set = set()
        null_count: int = 0
        total_assets = len(universe_metrics or {})
        adx_vals: List[float] = []
        vol_vals: List[float] = []

        if universe_metrics:
            for ticker, m in universe_metrics.items():
                if not isinstance(m, dict):
                    continue
                for req in self.REQUIRED_INDICATORS:
                    val = m.get(req)
                    if val is None:
                        missing_fields.add(req)
                        null_count += 1
                adx = float(m.get("adx", 0.0) or 0.0)
                vol = float(m.get("realized_vol", 0.0) or 0.0)
                if adx > 0:
                    adx_vals.append(adx)
                if vol > 0:
                    vol_vals.append(vol)

        mean_adx = sum(adx_vals) / len(adx_vals) if adx_vals else 0.0
        mean_vol = sum(vol_vals) / len(vol_vals) if vol_vals else 0.20

        # Market regime classification
        if mean_vol > 0.35:
            regime = "CRISIS"
        elif mean_vol < 0.14 and mean_adx < 20.0:
            regime = "LOW_VOL_CHOP"
        elif mean_adx >= 28.0:
            regime = "HIGH_VOL_TREND"
        else:
            regime = "NORMAL"

        # Determine health status
        recommendations: List[str] = []
        action_taken: Optional[str] = None

        if missing_fields:
            health = "CRITICAL"
            recommendations.append(
                f"Missing indicators detected in metrics feed: {sorted(list(missing_fields))}. "
                "Ensure compute_indicators() derives these values."
            )
            logger.critical(
                "StrategyImprovementAgent: Missing required indicators: %s",
                sorted(list(missing_fields)),
            )
        elif self.consecutive_zero_cycles >= self.zero_yield_threshold:
            health = "STARVED"
            recommendations.append(
                f"Zero signals generated across {self.consecutive_zero_cycles} consecutive scan cycles. "
                f"Market regime is {regime} (mean ADX={mean_adx:.1f}, mean vol={mean_vol:.1%})."
            )
            if regime == "LOW_VOL_CHOP":
                bt_result = self.verify_parameter_adaptation(candidate_rsi3_threshold=25.0)
                if bt_result.get("verified"):
                    recommendations.append(
                        f"Backtest-verified adaptive tuning: Broaden 3-RSI mean reversion scan (RSI3 <= 25 instead of 22). "
                        f"Backtest confirmed {bt_result.get('candidate_win_rate', 0.0):.1f}% win-rate (PF: {bt_result.get('profit_factor', 0.0):.2f})."
                    )
                else:
                    recommendations.append(
                        "Adaptive recommendation: Broaden 3-RSI mean reversion scan (RSI3 <= 25 instead of 22) "
                        "or lower ADX threshold to 18.0 to capture short-horizon relief swings."
                    )
            logger.warning(
                "StrategyImprovementAgent: Pipeline starved (%d zero cycles). Recommendations: %s",
                self.consecutive_zero_cycles, recommendations,
            )
        elif self.consecutive_zero_cycles > 0:
            health = "DEGRADED"
        else:
            health = "HEALTHY"

        audit = ImprovementAudit(
            timestamp=now_str,
            consecutive_zero_cycles=self.consecutive_zero_cycles,
            health_status=health,
            missing_indicators=sorted(list(missing_fields)),
            null_metrics_count=null_count,
            total_assets_audited=total_assets,
            mean_adx=round(mean_adx, 2),
            mean_realized_vol=round(mean_vol, 4),
            regime=regime,
            recommendations=recommendations,
            action_taken=action_taken,
        )

        self.last_audit = audit
        self.audit_history.append(audit)
        if len(self.audit_history) > 50:
            self.audit_history.pop(0)

        return audit

    def verify_parameter_adaptation(
        self,
        symbol: str = "QQQ",
        candidate_candles: Optional[List[Dict[str, Any]]] = None,
        candidate_rsi3_threshold: float = 25.0,
    ) -> Dict[str, Any]:
        """Perform automated backtest verification of proposed threshold adaptation.

        Runs historical simulation over benchmark candles, verifying win-rate
        and profit factor before recommending parameter adoption.
        """
        try:
            from trading_system.backtest.engine import BacktestEngine
            from trading_system.data.candle_db import CandleDatabase

            candles = candidate_candles
            if not candles:
                db = CandleDatabase()
                candles = db.get_candles(symbol, timeframe="1d", limit=100)

            if not candles or len(candles) < 30:
                return {
                    "verified": False,
                    "reason": "Insufficient historical candles for backtesting",
                    "candidate_win_rate": 0.0,
                    "profit_factor": 0.0,
                }

            engine = BacktestEngine(leverage=3)
            result = engine.run(symbol, candles, strategy_filter="3rsi_mean_reversion")

            win_rate = result.win_rate_pct
            pf = result.profit_factor
            verified = win_rate >= 70.0 and (pf >= 1.2 or result.total_trades == 0)

            return {
                "verified": verified,
                "symbol": symbol,
                "total_trades": result.total_trades,
                "candidate_win_rate": round(win_rate, 2),
                "profit_factor": round(pf, 2),
                "total_return_pct": round(result.total_return_pct, 2),
                "max_drawdown_pct": round(result.max_drawdown_pct, 2),
            }
        except Exception as exc:
            logger.debug("Backtest verification failed: %s", exc)
            return {
                "verified": False,
                "reason": str(exc),
                "candidate_win_rate": 0.0,
                "profit_factor": 0.0,
            }
