"""System 2 Technical Analysis agent that evaluates individual stock candidates."""

import logging
from dataclasses import dataclass
from typing import Dict, Any, Optional
from datetime import date as DateType

logger = logging.getLogger(__name__)

@dataclass
class TechnicalAnalysis:
    ticker: str
    trend_structure: str  # 'bullish'/'bearish'/'neutral'
    ma_alignment: str  # 'aligned_bullish'/'aligned_bearish'/'mixed'
    momentum_rating: str  # 'strong'/'moderate'/'weak'/'divergent'
    volume_assessment: str  # 'confirming'/'diverging'/'neutral'
    key_levels: Dict[str, float]  # support, resistance, breakout_level
    summary: str
    confidence: float
    raw_analysis: Optional[str] = None


class TechnicalAnalyst:
    def __init__(self, openai_api_key: str, model: str = 'gpt-4o'):
        """Initialize the TechnicalAnalyst with TradingAgents config."""
        # Store config for TradingAgents
        self.config = {
            'llm_provider': 'openai',
            'deep_think_llm': model,
            'quick_think_llm': 'gpt-4o-mini',
            'openai_api_key': openai_api_key,
        }

    def _determine_trend_structure(self, metrics: Dict[str, Any]) -> str:
        close = metrics.get('close', 0.0)
        sma200 = metrics.get('sma200', 0.0)
        ema20 = metrics.get('ema20', 0.0)
        ema50 = metrics.get('ema50', 0.0)
        
        if close > sma200 and ema20 > ema50:
            return 'bullish'
        elif close < sma200 and ema20 < ema50:
            return 'bearish'
        return 'neutral'

    def _determine_ma_alignment(self, metrics: Dict[str, Any]) -> str:
        close = metrics.get('close', 0.0)
        sma200 = metrics.get('sma200', 0.0)
        ema20 = metrics.get('ema20', 0.0)
        ema50 = metrics.get('ema50', 0.0)
        
        if ema20 > ema50 and close > sma200:
            return 'aligned_bullish'
        elif ema20 < ema50 and close < sma200:
            return 'aligned_bearish'
        return 'mixed'

    def _determine_momentum(self, metrics: Dict[str, Any]) -> str:
        rsi3 = metrics.get('rsi3', 50.0)
        rsi14 = metrics.get('rsi14', metrics.get('rsi', 50.0))
        close = metrics.get('close', 0.0)
        sma200 = metrics.get('sma200', 0.0)

        # 3-RSI Mean Reversion conditions
        if close > sma200 and rsi3 <= 22.0:
            return 'extreme_oversold_dip (3-RSI Mean Reversion - High Win Rate)'
        elif close < sma200 and rsi3 >= 78.0:
            return 'extreme_overbought_exhaustion (3-RSI Mean Reversion - High Win Rate)'

        if rsi14 > 60:
            return 'strong'
        elif rsi14 > 50:
            return 'moderate'
        return 'weak'

    def _determine_volume_assessment(self, metrics: Dict[str, Any]) -> str:
        volume = metrics.get('volume', 0)
        avg_volume = metrics.get('avg_volume', 1)
        if volume > avg_volume * 1.5:
            return 'confirming'
        elif volume < avg_volume * 0.5:
            return 'diverging'
        return 'neutral'

    def _fallback_analysis(self, ticker: str, metrics: Dict[str, Any]) -> TechnicalAnalysis:
        """Pure rule-based technical analysis from the metrics dict."""
        logger.info(f"Using fallback analysis for {ticker}")
        
        trend = self._determine_trend_structure(metrics)
        ma_alignment = self._determine_ma_alignment(metrics)
        momentum = self._determine_momentum(metrics)
        volume_assessment = self._determine_volume_assessment(metrics)
        rsi3 = metrics.get('rsi3', 50.0)
        rsi7 = metrics.get('rsi7', 50.0)
        rsi14 = metrics.get('rsi14', metrics.get('rsi', 50.0))
        
        key_levels = {
            'support': metrics.get('support', 0.0),
            'resistance': metrics.get('resistance', 0.0),
            'breakout_level': metrics.get('breakout_level', 0.0),
            'sma5_mr_target': metrics.get('sma5', 0.0),
        }
        
        is_mr = "3-RSI Mean Reversion" in momentum
        summary_text = (
            f"3-RSI Mean Reversion Dip: RSI3={rsi3:.1f}, RSI7={rsi7:.1f}, RSI14={rsi14:.1f}. "
            f"Asset in macro {trend} trend testing deep oversold support (Target: 5-SMA @ {metrics.get('sma5', 0.0):.2f})."
            if is_mr else
            f"Trend Momentum: {trend} trend structure with {momentum} momentum. RSI14={rsi14:.1f}, ADX={metrics.get('adx', 0.0):.1f}."
        )

        return TechnicalAnalysis(
            ticker=ticker,
            trend_structure=trend,
            ma_alignment=ma_alignment,
            momentum_rating=momentum,
            volume_assessment=volume_assessment,
            key_levels=key_levels,
            summary=summary_text,
            confidence=0.88 if is_mr else 0.70,
            raw_analysis=None
        )

    def analyze(self, ticker: str, metrics: Dict[str, Any], date: Optional[str] = None) -> TechnicalAnalysis:
        """Analyze a ticker using TradingAgents with fallback to metrics-based logic."""
        target_date = date or DateType.today().isoformat()
        
        try:
            from tradingagents.graph.trading_graph import TradingAgentsGraph
            from tradingagents.default_config import DEFAULT_CONFIG
            
            logger.info(f"Running LLM technical analysis for {ticker} on {target_date}")
            config = DEFAULT_CONFIG.copy()
            config.update({
                'llm_provider': self.config['llm_provider'],
                'deep_think_llm': self.config['deep_think_llm'],
                'quick_think_llm': self.config['quick_think_llm'],
                'max_debate_rounds': 1,
                'max_risk_discuss_rounds': 1,
            })
            # Only run market (technical) analyst for this stage
            ta = TradingAgentsGraph(
                selected_analysts=['market'],
                config=config,
            )
            state, decision = ta.propagate(ticker=ticker, date=target_date)
            
            trend = self._determine_trend_structure(metrics)
            ma_alignment = self._determine_ma_alignment(metrics)
            momentum = self._determine_momentum(metrics)
            volume_assessment = self._determine_volume_assessment(metrics)
            
            key_levels = {
                'support': metrics.get('support', 0.0),
                'resistance': metrics.get('resistance', 0.0),
                'breakout_level': metrics.get('breakout_level', 0.0)
            }
            
            # Extract summary from state if available
            summary = state.get('market_report', "LLM Analysis successful.") if isinstance(state, dict) else "LLM Analysis successful."
            raw_text = str(state)
            confidence = float(decision.get('confidence', 0.8)) if isinstance(decision, dict) else 0.8
            
            analysis = TechnicalAnalysis(
                ticker=ticker,
                trend_structure=trend,
                ma_alignment=ma_alignment,
                momentum_rating=momentum,
                volume_assessment=volume_assessment,
                key_levels=key_levels,
                summary=summary,
                confidence=confidence,
                raw_analysis=raw_text
            )
            logger.info(f"Completed LLM analysis for {ticker}")
            return analysis
            
        except Exception as e:
            logger.error(f"TradingAgents analysis failed for {ticker}: {e}")
            return self._fallback_analysis(ticker, metrics)

    def format_report(self, analysis: TechnicalAnalysis) -> str:
        """Format a clean text report of the technical analysis."""
        levels_str = ", ".join(f"{k}: {v}" for k, v in analysis.key_levels.items())
        return (
            f"Technical Analysis Report: {analysis.ticker}\n"
            f"=========================================\n"
            f"Trend Structure  : {analysis.trend_structure.title()}\n"
            f"MA Alignment     : {analysis.ma_alignment.replace('_', ' ').title()}\n"
            f"Momentum Rating  : {analysis.momentum_rating.title()}\n"
            f"Volume Assessment: {analysis.volume_assessment.title()}\n"
            f"Key Levels       : {levels_str}\n"
            f"Confidence       : {analysis.confidence:.2f}\n"
            f"-----------------------------------------\n"
            f"Summary:\n{analysis.summary}\n"
        )
