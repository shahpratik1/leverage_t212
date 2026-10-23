"""System 2 Fundamental & Catalyst Analysis agent."""

import logging
from dataclasses import dataclass
from datetime import date as DateType
from typing import Any, Dict, Optional

# Set up module logger
logger = logging.getLogger(__name__)


@dataclass
class FundamentalAnalysis:
    """Dataclass representing fundamental and catalyst analysis results."""
    ticker: str
    valuation_rating: str  # 'undervalued'/'fair'/'overvalued'
    catalyst_assessment: str  # 'positive'/'neutral'/'negative'
    macro_environment: str  # 'favorable'/'neutral'/'headwind'
    earnings_drift: str  # 'positive_drift'/'no_drift'/'negative_drift'
    summary: str
    confidence: float
    raw_analysis: Optional[str] = None


class FundamentalAnalyst:
    """System 2 Fundamental & Catalyst Analyst agent evaluating valuation, macro, and catalysts."""

    def __init__(self, openai_api_key: str, model: str = 'gpt-4o'):
        """
        Initialize the FundamentalAnalyst with TradingAgents configuration.

        Args:
            openai_api_key: OpenAI API key for LLM calls.
            model: Model name for deep thinking LLM (default: 'gpt-4o').
        """
        self.openai_api_key = openai_api_key
        self.model = model
        self.config = {
            'llm_provider': 'openai',
            'deep_think_llm': model,
            'quick_think_llm': 'gpt-4o-mini',
            'openai_api_key': openai_api_key,
        }
        logger.debug(f"Initialized FundamentalAnalyst with model={model}")

    def _fallback_analysis(self, ticker: str, metrics: Dict[str, Any]) -> FundamentalAnalysis:
        """
        Evaluate fundamental valuation, Finnhub news, and Olostep sentiment.

        Args:
            ticker: Stock ticker symbol.
            metrics: Dictionary of financial and technical metrics.

        Returns:
            FundamentalAnalysis with quantitative and sentiment assessments.
        """
        logger.info(f"Synthesizing Finnhub fundamentals & news with Olostep sentiment for {ticker}")

        finnhub_data = metrics.get("finnhub_fundamentals")
        finnhub_news = metrics.get("finnhub_news")
        olostep_data = metrics.get("olostep_sentiment")

        try:
            from trading_system.data.trading212_client import MarketDataProvider
            if finnhub_data is None:
                finnhub_data = MarketDataProvider.get_company_fundamentals(ticker)
            if finnhub_news is None:
                finnhub_news = MarketDataProvider.get_company_news(ticker, days=7)
            if olostep_data is None:
                olostep_data = MarketDataProvider.get_sentiment_search(ticker)
        except Exception as exc:
            logger.debug("Failed to retrieve provider data in FundamentalAnalyst: %s", exc)

        has_fundamentals = bool(finnhub_data)
        has_sentiment = bool(olostep_data)

        finnhub_data = finnhub_data or {}
        finnhub_news = finnhub_news or []
        olostep_data = olostep_data or {}

        # 1. Valuation rating from real multiples
        pe = finnhub_data.get("pe_ratio")
        ev = finnhub_data.get("ev_to_ebitda")
        if (pe is not None and pe < 18.0) or (ev is not None and ev < 10.0):
            valuation_rating = "undervalued"
        elif (pe is not None and pe > 38.0) or (ev is not None and ev > 25.0):
            valuation_rating = "overvalued"
        else:
            valuation_rating = "fair"

        # 2. Catalyst assessment from real sentiment search & news
        if has_sentiment:
            olostep_label = olostep_data.get("sentiment_label", "NEUTRAL").upper()
            olostep_score = float(olostep_data.get("sentiment_score", 0.50) or 0.50)
            pos_news = sum(1 for n in finnhub_news if n.get("sentiment") == "positive")
            neg_news = sum(1 for n in finnhub_news if n.get("sentiment") == "negative")

            if olostep_label == "BULLISH" or (olostep_score >= 0.58 and pos_news >= neg_news):
                catalyst_assessment = "positive"
            elif olostep_label == "BEARISH" or (olostep_score <= 0.42 and neg_news >= pos_news):
                catalyst_assessment = "negative"
            else:
                catalyst_assessment = "neutral"
        else:
            olostep_label = "NEUTRAL"
            olostep_score = 0.50
            catalyst_assessment = "neutral"

        # 3. Macro environment
        if has_sentiment and olostep_score >= 0.55 and finnhub_data.get("beta", 1.0) < 1.4:
            macro_environment = "favorable"
        elif has_sentiment and olostep_score <= 0.42:
            macro_environment = "headwind"
        else:
            macro_environment = "neutral"

        # 4. Earnings drift
        earnings_raw = metrics.get("earnings_in_days")
        earnings_days: Optional[float] = None
        if earnings_raw is not None:
            try:
                earnings_days = float(earnings_raw)
            except (ValueError, TypeError):
                earnings_days = None

        if earnings_days is not None and 3.0 <= earnings_days <= 21.0:
            if catalyst_assessment == "positive":
                earnings_drift = "positive_drift"
            elif catalyst_assessment == "negative":
                earnings_drift = "negative_drift"
            else:
                earnings_drift = "no_drift"
        else:
            earnings_drift = "no_drift"

        # 5. Synthesis Summary & Confidence
        val_source = finnhub_data.get("source", "Market Data") if has_fundamentals else "None"
        val_summary = finnhub_data.get("valuation_summary", f"P/E: {pe or 'N/A'}") if has_fundamentals else "Data unavailable"
        catalysts_list = olostep_data.get("key_catalysts", [])
        cat_snippet = f" Key Catalysts: {'; '.join(catalysts_list[:2])}." if catalysts_list else ""
        news_count = len(finnhub_news)
        news_snippet = f" {news_count} live news articles analyzed." if news_count > 0 else ""

        if has_fundamentals or has_sentiment:
            summary = (
                f"Valuation [{val_source}]: {valuation_rating.title()} ({val_summary}). "
                f"Web Sentiment: {olostep_label} (Score: {olostep_score:.2f})."
                f"{cat_snippet}{news_snippet}"
            )
            confidence = 0.70 + (0.05 if has_fundamentals else 0.0) + (0.05 if has_sentiment else 0.0)
        else:
            summary = (
                f"Fundamental and live sentiment data unavailable for {ticker}. "
                "Defaulted to conservative neutral assessments for valuation, catalyst, and macro."
            )
            confidence = 0.50

        return FundamentalAnalysis(
            ticker=ticker,
            valuation_rating=valuation_rating,
            catalyst_assessment=catalyst_assessment,
            macro_environment=macro_environment,
            earnings_drift=earnings_drift,
            summary=summary,
            confidence=round(confidence, 2),
            raw_analysis=None,
        )

    def _extract_analysis(self, ticker: str, result: Any, metrics: Dict[str, Any]) -> FundamentalAnalysis:
        """
        Extract structured FundamentalAnalysis from the result of TradingAgentsGraph.

        Args:
            ticker: Stock ticker symbol.
            result: Result output from TradingAgentsGraph.propagate.
            metrics: Underlying metrics dictionary.

        Returns:
            FundamentalAnalysis dataclass instance.
        """
        raw_text = str(result)
        data: Dict[str, Any] = {}

        if isinstance(result, dict):
            for key in ('fundamental_analysis', 'fundamentals', 'fundamental', 'catalyst_analysis'):
                sub = result.get(key)
                if isinstance(sub, dict):
                    data = sub
                    break
            if not data:
                data = result
        elif isinstance(result, str):
            data = {'summary': result}
            res_lower = result.lower()
            if 'undervalued' in res_lower:
                data['valuation_rating'] = 'undervalued'
            elif 'overvalued' in res_lower:
                data['valuation_rating'] = 'overvalued'

            if 'positive' in res_lower and 'catalyst' in res_lower:
                data['catalyst_assessment'] = 'positive'
            elif 'negative' in res_lower and 'catalyst' in res_lower:
                data['catalyst_assessment'] = 'negative'

            if 'favorable' in res_lower and 'macro' in res_lower:
                data['macro_environment'] = 'favorable'
            elif 'headwind' in res_lower or ('negative' in res_lower and 'macro' in res_lower):
                data['macro_environment'] = 'headwind'

        # Valuation rating: 'undervalued' / 'fair' / 'overvalued'
        val_raw = str(data.get('valuation_rating') or data.get('valuation') or '').lower()
        if 'under' in val_raw:
            valuation_rating = 'undervalued'
        elif 'over' in val_raw:
            valuation_rating = 'overvalued'
        else:
            valuation_rating = 'fair'

        # Catalyst assessment: 'positive' / 'neutral' / 'negative'
        cat_raw = str(data.get('catalyst_assessment') or data.get('catalyst') or '').lower()
        if 'pos' in cat_raw or 'bull' in cat_raw:
            catalyst_assessment = 'positive'
        elif 'neg' in cat_raw or 'bear' in cat_raw:
            catalyst_assessment = 'negative'
        else:
            catalyst_assessment = 'neutral'

        # Macro environment: 'favorable' / 'neutral' / 'headwind'
        macro_raw = str(data.get('macro_environment') or data.get('macro') or '').lower()
        if 'favor' in macro_raw or 'tail' in macro_raw:
            macro_environment = 'favorable'
        elif 'head' in macro_raw or 'unfavor' in macro_raw:
            macro_environment = 'headwind'
        else:
            macro_environment = 'neutral'

        # Earnings drift: 'positive_drift' / 'no_drift' / 'negative_drift'
        drift_raw = str(data.get('earnings_drift') or data.get('drift') or '').lower()
        if 'pos' in drift_raw:
            earnings_drift = 'positive_drift'
        elif 'neg' in drift_raw:
            earnings_drift = 'negative_drift'
        else:
            earnings_drift = 'no_drift'

        # Confidence
        conf_val = data.get('confidence', 0.8)
        try:
            confidence = float(conf_val)
            confidence = max(0.0, min(1.0, confidence))
        except (ValueError, TypeError):
            confidence = 0.8

        # Summary
        summary = (
            data.get('summary')
            or (result.get('summary') if isinstance(result, dict) else None)
            or data.get('fundamental_report')
            or f"Fundamental analysis completed for {ticker}. Valuation: {valuation_rating}, Catalyst: {catalyst_assessment}."
        )
        if not isinstance(summary, str):
            summary = str(summary)

        return FundamentalAnalysis(
            ticker=ticker,
            valuation_rating=valuation_rating,
            catalyst_assessment=catalyst_assessment,
            macro_environment=macro_environment,
            earnings_drift=earnings_drift,
            summary=summary,
            confidence=confidence,
            raw_analysis=raw_text,
        )

    def analyze(self, ticker: str, metrics: Dict[str, Any], date: Optional[str] = None) -> FundamentalAnalysis:
        """
        Analyze fundamental and catalyst drivers for a given ticker.

        Tries to use TradingAgentsGraph for deliberative fundamental analysis.
        Falls back gracefully to a conservative baseline if unavailable or upon error.

        Args:
            ticker: Stock ticker symbol.
            metrics: Dictionary of financial and technical metrics.
            date: Optional date string (YYYY-MM-DD) for point-in-time analysis.

        Returns:
            FundamentalAnalysis dataclass instance.
        """
        target_date = date or DateType.today().isoformat()
        logger.info(f"Starting fundamental analysis for {ticker} on {target_date}")

        try:
            from tradingagents.graph.trading_graph import TradingAgentsGraph
            from tradingagents.default_config import DEFAULT_CONFIG

            logger.info(f"Instantiating TradingAgentsGraph for {ticker}")
            config = DEFAULT_CONFIG.copy()
            config.update({
                'llm_provider': self.config.get('llm_provider', 'openai'),
                'deep_think_llm': self.config.get('deep_think_llm', 'gpt-4o'),
                'quick_think_llm': self.config.get('quick_think_llm', 'gpt-4o-mini'),
                'max_debate_rounds': 1,
                'max_risk_discuss_rounds': 1,
            })
            # Only run fundamentals analyst for this stage
            ta = TradingAgentsGraph(
                selected_analysts=['fundamentals'],
                config=config,
            )

            logger.info(f"Running LLM fundamental analysis propagate for {ticker}")
            state, decision = ta.propagate(ticker=ticker, date=target_date)

            # Merge state and decision for extraction
            result = {}
            if isinstance(state, dict):
                result.update(state)
            if isinstance(decision, dict):
                result.update(decision)

            analysis = self._extract_analysis(ticker, result, metrics)
            logger.info(
                f"Fundamental analysis successful for {ticker}: "
                f"valuation={analysis.valuation_rating}, catalyst={analysis.catalyst_assessment}, "
                f"macro={analysis.macro_environment}, drift={analysis.earnings_drift}, "
                f"confidence={analysis.confidence:.2f}"
            )
            return analysis

        except ImportError as e:
            logger.warning(
                f"TradingAgentsGraph import failed ({e}). Using conservative fallback analysis for {ticker}."
            )
            return self._fallback_analysis(ticker, metrics)
        except Exception as e:
            logger.error(
                f"TradingAgents fundamental analysis failed for {ticker}: {e}. Using conservative fallback analysis."
            )
            return self._fallback_analysis(ticker, metrics)

    def format_report(self, analysis: FundamentalAnalysis) -> str:
        """
        Format a clean, human-readable text report of the fundamental analysis.

        Args:
            analysis: FundamentalAnalysis dataclass instance.

        Returns:
            Formatted multiline string summary.
        """
        return (
            f"Fundamental Analysis Report: {analysis.ticker}\n"
            f"============================================\n"
            f"Valuation Rating    : {analysis.valuation_rating.replace('_', ' ').title()}\n"
            f"Catalyst Assessment : {analysis.catalyst_assessment.replace('_', ' ').title()}\n"
            f"Macro Environment   : {analysis.macro_environment.replace('_', ' ').title()}\n"
            f"Earnings Drift      : {analysis.earnings_drift.replace('_', ' ').title()}\n"
            f"Confidence          : {analysis.confidence:.2f}\n"
            f"--------------------------------------------\n"
            f"Summary:\n{analysis.summary}\n"
        )
