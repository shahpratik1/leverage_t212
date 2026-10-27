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
        speculative_routing: bool = True,
        speculative_threshold: float = 0.75,
        gemini_api_key: Optional[str] = None,
    ):
        self.openai_api_key = openai_api_key
        self.model = model
        self.quick_model = quick_model
        self.debate_rounds = debate_rounds
        self.speculative_routing = speculative_routing
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
        Runs speculative model routing:
        1. Fast Tier (Tier 1): Checks quantitative evidence clarity & conviction.
           If speculative_routing is True and conviction >= speculative_threshold,
           returns the fast consensus decision immediately (< 10ms) without
           launching expensive multi-round deep reasoning.
        2. Deep Tier (Tier 2): If the setup is contested or ambiguous (conviction < speculative_threshold),
           escalates to deep multi-agent debate (TradingAgentsGraph / LLM_DEEP_MODEL) to resolve risks.
        """
        # ── 1. Tier 1: Speculative Fast Tier Evaluation ────────────────────────
        if self.speculative_routing:
            fast_outcome = self._speculative_fast_route(
                ticker=ticker,
                metrics=metrics,
                technical_report=technical_report,
                fundamental_report=fundamental_report,
                strategy_type=strategy_type,
            )
            if fast_outcome.conviction_score >= self.speculative_threshold:
                logger.info(
                    "⚡ [Speculative Router] Fast Tier consensus for %s: %s (Conviction: %.2f >= %.2f) [<10ms]",
                    ticker, fast_outcome.consensus_decision, fast_outcome.conviction_score, self.speculative_threshold
                )
                return fast_outcome
            else:
                logger.info(
                    "🔍 [Speculative Router] %s is contested (Conviction: %.2f < %.2f) — escalating to Deep Arbiter (%s)",
                    ticker, fast_outcome.conviction_score, self.speculative_threshold, self.model
                )

        # ── 2. Tier 2: Deep Arbiter Debate ──────────────────────────────────────
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
        """Tier 1 Fast Speculative Router: High-speed quantitative conviction evaluator."""
        rsi3 = float(metrics.get("rsi3", 50.0))
        rsi7 = float(metrics.get("rsi7", 50.0))
        rsi14 = float(metrics.get("rsi14", metrics.get("rsi", 50.0)))
        close = float(metrics.get("close", 0.0))
        sma200 = float(metrics.get("sma200", 0.0))
        sma5 = float(metrics.get("sma5", close))
        ema20 = float(metrics.get("ema20", 0.0))
        ema50 = float(metrics.get("ema50", 0.0))
        adx = float(metrics.get("adx", 0.0))
        high_20d = float(metrics.get("high_20d", close))

        # Check for 3-RSI Mean Reversion Pattern (85%+ Win-Rate Edge)
        if strategy_type == "3rsi_mean_reversion" or (close > sma200 and rsi3 <= 22.0):
            if close > sma200 and rsi3 <= 22.0:
                conviction = 0.94 if rsi3 <= 18.0 else 0.88
                target_p = sma5 if sma5 > close else close * 1.05
                return DebateOutcome(
                    ticker=ticker,
                    winning_side="bull",
                    consensus_decision="BUY_ENTRY",
                    bull_position=DebatePosition(
                        side="bull",
                        thesis="Institutional 3-RSI mean-reversion dip buying with 85%+ win rate expectation.",
                        key_evidence=[
                            f"RSI(3)={rsi3:.1f} indicates extreme short-term liquidity flush inside secular bull trend.",
                            f"Price ({close:.2f}) remains safely above 200 SMA ({sma200:.2f}).",
                            f"Immediate mean-reversion magnet: 5-Day SMA at {sma5:.2f} (+{((sma5 - close) / close) * 100:.1f}%).",
                            "Larry Connors 3-RSI model historically demonstrates 85%+ win rate across large-cap pullbacks.",
                        ],
                        target_price=target_p,
                        risk_factors=["Downside break of 200-day moving average support."],
                        confidence=conviction,
                    ),
                    bear_position=DebatePosition(
                        side="bear",
                        thesis="Short-term liquidation pressure could test lower support before reversing.",
                        key_evidence=[
                            f"Momentum across RSI(7)={rsi7:.1f} remains negative.",
                            "Potential broad index volatility.",
                        ],
                        target_price=close * 0.96,
                        risk_factors=["Overwhelming statistical edge favoring immediate rebound."],
                        confidence=round(1.0 - conviction, 2),
                    ),
                    synthesis=f"Speculative Fast Router: Confirmed high-probability 3-RSI mean reversion entry on {ticker}. Short-term sellers exhausted above 200 SMA.",
                    conviction_score=conviction,
                    debate_rounds=1,
                )

        # Check for Decisive Trend Momentum Breakout
        bull_ev = []
        bear_ev = []

        if close > sma200:
            bull_ev.append(f"Price ({close:.2f}) > 200 SMA ({sma200:.2f}), confirming primary bull market.")
        else:
            bear_ev.append(f"Price ({close:.2f}) < 200 SMA ({sma200:.2f}), primary trend is downward.")

        if ema20 > ema50:
            bull_ev.append(f"20 EMA ({ema20:.2f}) > 50 EMA ({ema50:.2f}), directional moving average expansion.")
        else:
            bear_ev.append(f"20 EMA ({ema20:.2f}) < 50 EMA ({ema50:.2f}), short-term moving average compression.")

        if rsi14 >= 52.0:
            bull_ev.append(f"RSI(14) at {rsi14:.1f} confirms active upward momentum.")
        elif rsi14 <= 45.0:
            bear_ev.append(f"RSI(14) at {rsi14:.1f} indicates negative momentum pressure.")

        if adx >= 24.0:
            bull_ev.append(f"ADX at {adx:.1f} confirms decisive directional trend strength.")
        else:
            bear_ev.append(f"ADX at {adx:.1f} reflects weak/range-bound trend.")

        if high_20d > 0 and close >= high_20d * 0.985:
            bull_ev.append(f"Price is pressing 20-day high ({high_20d:.2f}), breakout in progress.")

        n_bull = len(bull_ev)
        n_bear = len(bear_ev)
        total = n_bull + n_bear

        if n_bull >= 4 and n_bear <= 1:
            conviction = 0.86
            consensus = "BUY_ENTRY"
            winner = "bull"
        elif n_bear >= 4 and n_bull <= 1:
            conviction = 0.84
            consensus = "SELL_ENTRY"
            winner = "bear"
        else:
            # Contested / ambiguous setup
            conviction = round(abs(n_bull - n_bear) / total, 2) if total > 0 else 0.40
            consensus = "BUY_ENTRY" if n_bull > n_bear else ("SELL_ENTRY" if n_bear > n_bull else "DO_NOT_TRADE")
            winner = "bull" if n_bull > n_bear else ("bear" if n_bear > n_bull else "tie")

        return DebateOutcome(
            ticker=ticker,
            winning_side=winner,
            consensus_decision=consensus,
            bull_position=DebatePosition(
                side="bull",
                thesis="Trend momentum and moving average alignment point upward.",
                key_evidence=bull_ev,
                target_price=close * 1.08,
                risk_factors=["Overbought exhaustion", "Macro volatility"],
                confidence=round(n_bull / total, 2) if total > 0 else 0.5,
            ),
            bear_position=DebatePosition(
                side="bear",
                thesis="Overhead resistance and momentum drag pose downside risk.",
                key_evidence=bear_ev,
                target_price=close * 0.92,
                risk_factors=["Trend continuation momentum"],
                confidence=round(n_bear / total, 2) if total > 0 else 0.5,
            ),
            synthesis="Speculative Fast Tier analysis based on multi-timeframe moving averages, RSI, and ADX.",
            conviction_score=conviction,
            debate_rounds=1,
        )

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
        
        # 3-RSI check
        if strategy_type == "3rsi_mean_reversion" or (close > sma200 and rsi3 <= 22.0):
            bull_evidence.append(f"Extreme 3-RSI oversold condition (RSI3={rsi3:.1f} <= 22.0) with price above 200 SMA.")
            bull_evidence.append(f"5-day SMA at {sma5:.2f} provides strong magnetic reversion target.")
        elif rsi14 > 50:
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

        if adx > 25:
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
            synthesis="Rule-based synthesis based on simple moving averages and RSI.",
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
