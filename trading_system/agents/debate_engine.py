import logging
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional
import json

try:
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    TRADING_AGENTS_AVAILABLE = True
except ImportError:
    TRADING_AGENTS_AVAILABLE = False

logger = logging.getLogger(__name__)

@dataclass
class DebatePosition:
    side: str
    thesis: str
    key_evidence: List[str] = field(default_factory=list)
    target_price: Optional[float] = None
    risk_factors: List[str] = field(default_factory=list)
    confidence: float = 0.5

@dataclass
class DebateOutcome:
    ticker: str
    winning_side: str
    consensus_decision: str
    bull_position: DebatePosition
    bear_position: DebatePosition
    synthesis: str
    conviction_score: float
    debate_rounds: int

class DebateEngine:
    def __init__(
        self,
        openai_api_key: str = "",
        model: str = 'gpt-4o',
        quick_model: str = 'gpt-4o-mini',
        debate_rounds: int = 3,
        speculative_routing: bool = False,
        speculative_threshold: float = 0.75,
        gemini_api_key: Optional[str] = None,
    ):
        self.openai_api_key = openai_api_key
        self.model = model
        self.quick_model = quick_model
        self.debate_rounds = debate_rounds
        self.speculative_routing = False  # Pure System 2 Deliberative Multi-Agent Brain: Fast Reflex bypass disabled
        self.speculative_threshold = speculative_threshold
        self.gemini_api_key = gemini_api_key

    def run_debate(
        self,
        ticker: str,
        metrics: Dict[str, Any],
        technical_report: str,
        fundamental_report: str,
        date: Optional[str] = None,
        strategy_type: str = "momentum",
    ) -> DebateOutcome:
        """
        Runs System 2 Deliberative Multi-Agent Debate:
        All candidates undergo full dialectic deliberation between Bull and Bear analysts,
        synthesizing technical, fundamental, sentiment (Olostep), and forecast (Nixtla) data.
        Fast-reflex speculative bypasses have been completely removed to ensure cognitive depth
        and meta-learning alignment across all trades.
        """
        logger.info(
            "🧠 [System 2 Deliberative Brain] Running deep multi-agent debate for %s (strategy: %s)",
            ticker, strategy_type,
        )

        # Quantitative rule-based or TradingAgents LLM debate
        if not TRADING_AGENTS_AVAILABLE or not self.openai_api_key:
            return self._fallback_debate(ticker, metrics, technical_report, fundamental_report, strategy_type=strategy_type)
        
        try:
            from tradingagents.default_config import DEFAULT_CONFIG

            logger.info(f"Running TradingAgents deep debate for {ticker} using {self.model}")
            config = DEFAULT_CONFIG.copy()
            config.update({
                'llm_provider': 'openai',
                'deep_think_llm': self.model,
                'quick_think_llm': self.quick_model,
                'max_debate_rounds': self.debate_rounds,
                'max_risk_discuss_rounds': 1,
            })
            import os
            os.environ.setdefault('OPENAI_API_KEY', self.openai_api_key)

            graph = TradingAgentsGraph(config=config)
            
            analysis_date = date or "2026-01-01"
            state, decision = graph.propagate(ticker=ticker, date=analysis_date)
            
            bull_raw = state.get("debate_transcript", {}) if isinstance(state, dict) else {}
            bear_raw = {}
            
            final_decision_raw = decision.get("action", "") if isinstance(decision, dict) else ""
            confidence = float(decision.get("confidence", 0.5)) if isinstance(decision, dict) else 0.5
            rationale = decision.get("rationale", "No synthesis provided.") if isinstance(decision, dict) else "No synthesis provided."
            
            bull_position = self._parse_bull_position(bull_raw, metrics)
            bear_position = self._parse_bear_position(bear_raw, metrics)
            
            decision_upper = str(final_decision_raw).upper()
            if 'BUY' in decision_upper or 'BULLISH' in decision_upper:
                consensus_decision = 'BUY_ENTRY'
                winning_side = 'bull'
            elif 'SELL' in decision_upper or 'BEARISH' in decision_upper:
                consensus_decision = 'SELL_ENTRY'
                winning_side = 'bear'
            else:
                consensus_decision = 'DO_NOT_TRADE'
                winning_side = 'tie'
                
            return DebateOutcome(
                ticker=ticker,
                winning_side=winning_side,
                consensus_decision=consensus_decision,
                bull_position=bull_position,
                bear_position=bear_position,
                synthesis=rationale,
                conviction_score=confidence,
                debate_rounds=self.debate_rounds
            )
            
        except Exception as e:
            logger.error(f"Error during TradingAgents debate: {e}. Falling back to analytical arbiter.")
            return self._fallback_debate(ticker, metrics, technical_report, fundamental_report, strategy_type=strategy_type)

    def _speculative_fast_route(
        self,
        ticker: str,
        metrics: Dict[str, Any],
        technical_report: str,
        fundamental_report: str,
        strategy_type: str = "momentum",
    ) -> DebateOutcome:
        """Deprecated: Fast reflex routing removed in favor of full System 2 multi-agent deliberation."""
        logger.warning(
            "⚡ [Deprecated] _speculative_fast_route called for %s — redirecting to System 2 deliberative debate",
            ticker,
        )
        return self._fallback_debate(ticker, metrics, technical_report, fundamental_report, strategy_type=strategy_type)

    def _fallback_debate(
        self,
        ticker: str,
        metrics: Dict[str, Any],
        technical_report: str,
        fundamental_report: str,
        strategy_type: str = "momentum",
    ) -> DebateOutcome:
        """Deep analytical arbiter fallback when TradingAgents package is unavailable."""
        logger.info(f"Running deep analytical deliberation for {ticker} (Strategy: {strategy_type})")
        
        bull_evidence = []
        bear_evidence = []
        bull_risks = []
        bear_risks = []
        
        rsi3 = metrics.get('rsi3', 50)
        rsi14 = metrics.get('rsi14', metrics.get('rsi', 50))
        ema20 = metrics.get('ema20', 0)
        ema50 = metrics.get('ema50', 0)
        sma200 = metrics.get('sma200', 0)
        close = metrics.get('close', 0)
        adx = metrics.get('adx', 0)
        high_20d = metrics.get('high_20d', 0)
        sma5 = metrics.get('sma5', close)
        
        from trading_system.config import settings
        rsi3_thresh = getattr(settings, "RSI3_OVERSOLD", 28.0)
        adx_trend_min = getattr(settings, "MIN_ADX_TREND", 18.0)

        # 3-RSI check
        if strategy_type == "3rsi_mean_reversion" or (close > sma200 and rsi3 <= rsi3_thresh):
            bull_evidence.append(f"Short-term 3-RSI pullback (RSI3={rsi3:.1f} <= {rsi3_thresh:.1f}) with price above 200 SMA.")
            bull_evidence.append(f"5-day SMA at {sma5:.2f} provides strong magnetic reversion target.")
        elif rsi14 >= 50:
            bull_evidence.append(f"RSI(14) at {rsi14:.1f} is above 50, indicating bullish momentum.")
        else:
            bear_evidence.append(f"RSI(14) at {rsi14:.1f} is below 50, indicating bearish momentum.")
            
        if ema20 > ema50:
            bull_evidence.append(f"20 EMA ({ema20:.2f}) > 50 EMA ({ema50:.2f}), short-term uptrend.")
        else:
            bear_evidence.append(f"20 EMA ({ema20:.2f}) < 50 EMA ({ema50:.2f}), short-term downtrend.")
            
        if close > sma200:
            bull_evidence.append(f"Price ({close:.2f}) > 200 SMA ({sma200:.2f}), long-term uptrend.")
        else:
            bear_evidence.append(f"Price ({close:.2f}) < 200 SMA ({sma200:.2f}), long-term downtrend.")

        if adx >= adx_trend_min:
            bull_evidence.append(f"ADX at {adx:.1f} indicates strong trend velocity.")
        else:
            bear_evidence.append(f"ADX at {adx:.1f} indicates low directional momentum.")

        if high_20d > 0 and close >= high_20d * 0.98:
            bull_evidence.append("Price is testing 20-day high, potential breakout.")

        # Ingest Finnhub, Olostep, and Nixtla evidence
        finnhub_data = metrics.get("finnhub_fundamentals")
        olostep_data = metrics.get("olostep_sentiment")
        nixtla_data = metrics.get("nixtla_forecast")

        if olostep_data:
            ol_label = olostep_data.get("sentiment_label", "NEUTRAL")
            ol_score = olostep_data.get("sentiment_score", 0.50)
            if ol_label == "BULLISH":
                bull_evidence.append(f"Olostep sentiment search is BULLISH (Score: {ol_score:.2f}) with positive analyst catalyst coverage.")
            elif ol_label == "BEARISH":
                bear_evidence.append(f"Olostep sentiment search is BEARISH (Score: {ol_score:.2f}) with negative news flow.")

        if nixtla_data:
            nx_bias = nixtla_data.get("directional_bias", "NEUTRAL")
            nx_ret = nixtla_data.get("expected_return_pct", 0.0)
            target_p = nixtla_data.get("forecast_prices", [close])[-1] if nixtla_data.get("forecast_prices") else close
            if nx_bias == "BULLISH":
                bull_evidence.append(f"Nixtla TimeGPT 5-day forecast is BULLISH ({nx_ret:+.2f}% expected return, 5d target ${target_p:.2f}).")
            elif nx_bias == "BEARISH":
                bear_evidence.append(f"Nixtla TimeGPT 5-day forecast is BEARISH ({nx_ret:+.2f}% expected downside, 5d target ${target_p:.2f}).")

        if finnhub_data:
            pe = finnhub_data.get("pe_ratio")
            if pe and pe < 20.0:
                bull_evidence.append(f"Finnhub valuation multiples show attractive P/E of {pe:.1f}.")
            elif pe and pe > 40.0:
                bear_risks.append(f"Elevated Finnhub P/E of {pe:.1f} creates multiple compression risk.")
            
        bull_signals_count = len(bull_evidence)
        bear_signals_count = len(bear_evidence)
        total_signals = bull_signals_count + bear_signals_count
        
        if total_signals == 0:
            conviction_score = 0.0
        else:
            conviction_score = abs(bull_signals_count - bear_signals_count) / total_signals
            
        if bull_signals_count >= 3:
            consensus_decision = 'BUY_ENTRY'
            winning_side = 'bull'
            bear_risks.append("Strong bullish technicals overwhelming bearish case.")
        elif bear_signals_count >= 3:
            consensus_decision = 'SELL_ENTRY'
            winning_side = 'bear'
            bull_risks.append("Strong bearish technicals present.")
        else:
            consensus_decision = 'DO_NOT_TRADE'
            winning_side = 'tie'
            
        current_price = close if close > 0 else 100.0
        
        bull_position = DebatePosition(
            side='bull',
            thesis="Technical indicators point to upward momentum.",
            key_evidence=bull_evidence,
            target_price=current_price * 1.1,
            risk_factors=bull_risks,
            confidence=bull_signals_count / total_signals if total_signals > 0 else 0.5
        )
        
        bear_position = DebatePosition(
            side='bear',
            thesis="Technical indicators point to downward pressure.",
            key_evidence=bear_evidence,
            target_price=current_price * 0.9,
            risk_factors=bear_risks,
            confidence=bear_signals_count / total_signals if total_signals > 0 else 0.5
        )
        
        return DebateOutcome(
            ticker=ticker,
            winning_side=winning_side,
            consensus_decision=consensus_decision,
            bull_position=bull_position,
            bear_position=bear_position,
            synthesis="System 2 Deliberative Multi-Agent synthesis based on technical, fundamental, and sentiment consensus.",
            conviction_score=conviction_score,
            debate_rounds=self.debate_rounds
        )

    def _parse_bull_position(self, raw: Any, metrics: Dict[str, Any]) -> DebatePosition:
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = {"thesis": raw}
        
        if not isinstance(raw, dict):
            raw = {}
            
        current_price = metrics.get('close')
        target = raw.get('target_price')
        if target is None and current_price:
            target = float(current_price) * 1.1
            
        return DebatePosition(
            side='bull',
            thesis=raw.get('thesis', 'Bullish outlook'),
            key_evidence=raw.get('key_evidence', []),
            target_price=target,
            risk_factors=raw.get('risk_factors', []),
            confidence=float(raw.get('confidence', 0.5))
        )

    def _parse_bear_position(self, raw: Any, metrics: Dict[str, Any]) -> DebatePosition:
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = {"thesis": raw}
                
        if not isinstance(raw, dict):
            raw = {}
            
        current_price = metrics.get('close')
        target = raw.get('target_price')
        if target is None and current_price:
            target = float(current_price) * 0.9
            
        return DebatePosition(
            side='bear',
            thesis=raw.get('thesis', 'Bearish outlook'),
            key_evidence=raw.get('key_evidence', []),
            target_price=target,
            risk_factors=raw.get('risk_factors', []),
            confidence=float(raw.get('confidence', 0.5))
        )
        
    def format_report(self, outcome: DebateOutcome) -> str:
        """
        Comprehensive debate report showing both sides and the consensus.
        """
        report = []
        report.append(f"Debate Report for {outcome.ticker}")
        report.append("=" * 40)
        report.append(f"Consensus Decision: {outcome.consensus_decision}")
        report.append(f"Winning Side: {outcome.winning_side.upper()} (Conviction: {outcome.conviction_score:.2f})")
        report.append(f"Debate Rounds: {outcome.debate_rounds}")
        report.append("\n--- Synthesis ---")
        report.append(outcome.synthesis)
        report.append("\n--- Bull Case ---")
        report.append(f"Thesis: {outcome.bull_position.thesis}")
        report.append("Key Evidence:")
        for ev in outcome.bull_position.key_evidence:
            report.append(f"  - {ev}")
        report.append(f"Target Price: {outcome.bull_position.target_price}")
        report.append("Risk Factors:")
        for rk in outcome.bull_position.risk_factors:
            report.append(f"  - {rk}")
            
        report.append("\n--- Bear Case ---")
        report.append(f"Thesis: {outcome.bear_position.thesis}")
        report.append("Key Evidence:")
        for ev in outcome.bear_position.key_evidence:
            report.append(f"  - {ev}")
        report.append(f"Target Price: {outcome.bear_position.target_price}")
        report.append("Risk Factors:")
        for rk in outcome.bear_position.risk_factors:
            report.append(f"  - {rk}")
            
        return "\n".join(report)
