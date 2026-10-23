"""Unit tests for Finnhub, Olostep, and Nixtla data providers with keyless real data & no mocks."""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from trading_system.config import settings
from trading_system.data.finnhub_provider import FinnhubProvider, normalize_finnhub_symbol
from trading_system.data.olostep_provider import OlostepProvider
from trading_system.data.nixtla_provider import NixtlaProvider
from trading_system.data.trading212_client import MarketDataProvider
from trading_system.agents.fundamental_agent import FundamentalAnalyst
from trading_system.agents.technical_agent import TechnicalAnalyst
from trading_system.agents.debate_engine import DebateEngine
from trading_system.agents.antigravity_agent import (
    tool_get_finnhub_fundamentals,
    tool_get_company_news,
    tool_get_olostep_sentiment,
    tool_get_nixtla_forecast,
    tool_get_asset_intelligence,
)


class TestFinnhubProvider(unittest.TestCase):
    """Test Finnhub fundamentals, news, symbol normalization, and keyless real data mechanisms."""

    def test_symbol_normalization(self):
        self.assertEqual(normalize_finnhub_symbol("QQQ3l_EQ"), "QQQ")
        self.assertEqual(normalize_finnhub_symbol("3NVDl_EQ"), "NVDA")
        self.assertEqual(normalize_finnhub_symbol("AAPL_US_EQ"), "AAPL")
        self.assertEqual(normalize_finnhub_symbol("MSFT"), "MSFT")
        self.assertEqual(normalize_finnhub_symbol("3USLl_EQ"), "USL")

    def test_no_mock_data_when_unconfigured_and_offline(self):
        provider = FinnhubProvider(api_key="")
        # When offline or symbol unknown, strictly returns None rather than fake mock numbers
        with patch.object(provider, "_get_keyless_fundamentals", return_value=None):
            data = provider.get_company_fundamentals("UNKNOWN_XYZ", force_refresh=True)
            self.assertIsNone(data)

    def test_keyless_fundamentals_via_yfinance(self):
        provider = FinnhubProvider(api_key="")
        # When keyless yfinance succeeds, returns real data
        mock_info = {
            "symbol": "AAPL",
            "name": "Apple Inc",
            "pe_ratio": 33.2,
            "pb_ratio": 42.1,
            "market_cap": 3200000.0,
            "beta": 1.05,
            "source": "Yahoo Finance (keyless)",
        }
        with patch.object(provider, "_get_keyless_fundamentals", return_value=mock_info):
            data = provider.get_company_fundamentals("AAPL", force_refresh=True)
            self.assertIsNotNone(data)
            self.assertEqual(data["symbol"], "AAPL")
            self.assertEqual(data["source"], "Yahoo Finance (keyless)")
            self.assertEqual(data["pe_ratio"], 33.2)

    @patch.object(FinnhubProvider, "_http_get")
    def test_fundamentals_with_mocked_api(self, mock_http):
        def _mock_resp(path, params):
            if path == "/stock/profile2":
                return {
                    "name": "Apple Inc",
                    "marketCapitalization": 3000000.0,
                    "shareOutstanding": 15000.0,
                    "finnhubIndustry": "Technology",
                    "currency": "USD",
                }
            elif path == "/stock/metric":
                return {
                    "metric": {
                        "peNormalizedAnnual": 28.5,
                        "pbAnnual": 35.2,
                        "evToEbitdaTTM": 21.4,
                        "beta": 1.15,
                        "52WeekHigh": 240.0,
                        "52WeekLow": 165.0,
                    }
                }
            return None

        mock_http.side_effect = _mock_resp

        provider = FinnhubProvider(api_key="mock_finnhub_key")
        data = provider.get_company_fundamentals("AAPL", force_refresh=True)

        self.assertIsNotNone(data)
        self.assertEqual(data["symbol"], "AAPL")
        self.assertEqual(data["name"], "Apple Inc")
        self.assertEqual(data["pe_ratio"], 28.5)
        self.assertEqual(data["beta"], 1.15)
        self.assertEqual(data["source"], "Finnhub API")

    @patch.object(FinnhubProvider, "_http_get")
    def test_company_news_with_mocked_api(self, mock_http):
        mock_http.return_value = [
            {
                "headline": "Apple Beats Q3 Earnings Estimates with Strong iPhone Growth",
                "summary": "Revenue jumped 8% year-over-year as iPhone sales hit new records.",
                "source": "Bloomberg",
                "url": "https://bloomberg.com/news/123",
                "datetime": 1700000000,
            },
            {
                "headline": "Tech Sector Slumps Amid High Interest Rate Commentary",
                "summary": "Investors rotate into defensive assets as yields increase.",
                "source": "Reuters",
                "url": "https://reuters.com/news/456",
                "datetime": 1700000100,
            },
        ]

        provider = FinnhubProvider(api_key="mock_finnhub_key")
        articles = provider.get_company_news("AAPL", days=7, force_refresh=True)

        self.assertEqual(len(articles), 2)
        self.assertEqual(articles[0]["headline"], "Apple Beats Q3 Earnings Estimates with Strong iPhone Growth")
        self.assertEqual(articles[0]["sentiment"], "positive")
        self.assertEqual(articles[1]["sentiment"], "negative")

    def test_formatting_helpers(self):
        provider = FinnhubProvider(api_key="")
        fund_data = {
            "symbol": "NVDA",
            "name": "NVIDIA Corp",
            "market_cap": 2500000.0,
            "pe_ratio": 45.0,
            "pb_ratio": 22.0,
            "ev_to_ebitda": 38.0,
            "beta": 1.75,
            "industry": "Semiconductors",
            "valuation_summary": "P/E: 45.0, Beta: 1.75",
            "source": "Finnhub API",
        }
        report = provider.format_fundamentals_report(fund_data)
        self.assertIn("NVDA", report)
        self.assertIn("P/E=45.0", report)

        none_report = provider.format_fundamentals_report(None)
        self.assertIn("unavailable", none_report)


class TestOlostepProvider(unittest.TestCase):
    """Test Olostep AI sentiment search, lexical scoring, and keyless real web search."""

    def test_sentiment_scoring_lexicon(self):
        provider = OlostepProvider(api_key="")
        # Bullish text
        bull_text = "Strong revenue beat and guidance raised as earnings jump to record high. Analysts upgrade to outperform with buy rating."
        label, score, catalysts = provider._analyze_sentiment_text(bull_text)
        self.assertEqual(label, "BULLISH")
        self.assertGreater(score, 0.58)
        self.assertTrue(len(catalysts) > 0)

        # Bearish text
        bear_text = "Revenue missed expectations with deep decline and profit plunge. Analysts downgrade to sell on severe margin compression."
        b_label, b_score, b_catalysts = provider._analyze_sentiment_text(bear_text)
        self.assertEqual(b_label, "BEARISH")
        self.assertLess(b_score, 0.42)

    def test_no_mock_sentiment_when_offline(self):
        provider = OlostepProvider(api_key="")
        with patch.object(provider, "_get_keyless_web_sentiment", return_value=None):
            data = provider.get_sentiment_search("TSLA", force_refresh=True)
            self.assertIsNone(data)

    def test_keyless_web_sentiment(self):
        provider = OlostepProvider(api_key="")
        mock_web = {
            "symbol": "TSLA",
            "sentiment_label": "BULLISH",
            "sentiment_score": 0.65,
            "summary": "Live web & analyst news sentiment: BULLISH",
            "key_catalysts": ["Strong delivery growth"],
            "sources": [{"title": "Reuters", "url": "https://reuters.com"}],
            "source": "Live Web Search (keyless)",
        }
        with patch.object(provider, "_get_keyless_web_sentiment", return_value=mock_web):
            data = provider.get_sentiment_search("TSLA", force_refresh=True)
            self.assertIsNotNone(data)
            self.assertEqual(data["symbol"], "TSLA")
            self.assertEqual(data["sentiment_label"], "BULLISH")

    @patch.object(OlostepProvider, "_call_olostep_api")
    def test_sentiment_with_mocked_api(self, mock_call):
        mock_call.return_value = {
            "answer": "NVIDIA is experiencing strong AI infrastructure demand, with record quarterly revenue and multiple analyst price target upgrades.",
            "results": [
                {
                    "title": "NVIDIA Outperforms Expectations",
                    "snippet": "Strong earnings surge driven by Blackwell architecture sales and accelerating cloud revenue growth.",
                    "url": "https://example.com/nvda-surge",
                }
            ],
        }

        provider = OlostepProvider(api_key="mock_olostep_key")
        result = provider.get_sentiment_search("NVDA", force_refresh=True)

        self.assertIsNotNone(result)
        self.assertEqual(result["symbol"], "NVDA")
        self.assertEqual(result["sentiment_label"], "BULLISH")
        self.assertGreaterEqual(result["sentiment_score"], 0.58)
        self.assertEqual(result["source"], "Olostep Search API")


class TestNixtlaProvider(unittest.TestCase):
    """Test Nixtla TimeGPT 5-day forecast and keyless candle quantitative model."""

    def test_quantitative_candle_forecast(self):
        provider = NixtlaProvider(api_key="")
        # Upward trending closes
        closes = [100.0 + i * 1.5 for i in range(20)]
        forecast = provider._compute_quantitative_candle_forecast(130.0, closes)
        self.assertIsNotNone(forecast)
        self.assertEqual(len(forecast), 5)
        # Verify upward trajectory
        self.assertGreater(forecast[-1], 130.0)

    def test_no_mock_forecast_when_no_candles(self):
        provider = NixtlaProvider(api_key="")
        with patch("trading_system.data.trading212_client.MarketDataProvider.get_daily_candles", return_value=None):
            data = provider.get_5day_forecast("UNKNOWN_NO_CANDLES", candles=[], force_refresh=True)
            self.assertIsNone(data)

    def test_keyless_candle_forecast_generation(self):
        provider = NixtlaProvider(api_key="")
        candles = [{"close": 100.0 + i * 2.0, "date": f"2026-01-{i+1:02d}"} for i in range(20)]
        data = provider.get_5day_forecast("MSFT", candles=candles, force_refresh=True)
        self.assertIsNotNone(data)
        self.assertEqual(data["symbol"], "MSFT")
        self.assertEqual(data["horizon_days"], 5)
        self.assertEqual(len(data["forecast_prices"]), 5)
        self.assertEqual(data["source"], "Quantitative Candle Model (yfinance)")
        self.assertGreater(data["expected_return_pct"], 0.0)
        self.assertEqual(data["directional_bias"], "BULLISH")

    @patch.object(NixtlaProvider, "_call_nixtla_api")
    def test_forecast_with_mocked_api(self, mock_call):
        mock_call.return_value = [125.5, 127.0, 128.5, 130.0, 132.0]

        provider = NixtlaProvider(api_key="mock_nixtla_key")
        candles = [{"close": 100.0 + i, "date": f"2026-01-{i+1:02d}"} for i in range(25)]
        data = provider.get_5day_forecast("QQQ", candles=candles, force_refresh=True)

        self.assertIsNotNone(data)
        self.assertEqual(data["symbol"], "QQQ")
        self.assertEqual(data["forecast_prices"], [125.5, 127.0, 128.5, 130.0, 132.0])
        self.assertEqual(data["source"], "Nixtla TimeGPT API")
        self.assertGreater(data["expected_return_pct"], 0.0)
        self.assertEqual(data["directional_bias"], "BULLISH")


class TestMarketDataProviderIntegration(unittest.TestCase):
    """Test MarketDataProvider unified methods and cache clearing."""

    def test_market_data_provider_unified_accessors(self):
        MarketDataProvider.clear_candle_cache()

        # Should not crash even when offline (strictly returns None or data)
        fund = MarketDataProvider.get_company_fundamentals("AAPL")
        if fund is not None:
            self.assertEqual(fund["symbol"], "AAPL")

        news = MarketDataProvider.get_company_news("AAPL", days=5)
        self.assertIsInstance(news, list)

        sentiment = MarketDataProvider.get_sentiment_search("AAPL")
        if sentiment is not None:
            self.assertIn("sentiment_score", sentiment)

        # Forecast with candles
        candles = [{"close": 150.0 + i, "date": f"2026-01-{i+1:02d}"} for i in range(15)]
        forecast = MarketDataProvider.get_5day_forecast("AAPL", candles=candles)
        self.assertIsNotNone(forecast)
        self.assertEqual(forecast["horizon_days"], 5)

        # Cache clearing
        MarketDataProvider.clear_candle_cache()


class TestAgentIntegration(unittest.TestCase):
    """Test agents consuming Finnhub, Olostep, and Nixtla data."""

    def test_fundamental_analyst_integration(self):
        analyst = FundamentalAnalyst(openai_api_key="")
        metrics = {
            "close": 150.0,
            "finnhub_fundamentals": {
                "pe_ratio": 16.5,
                "ev_to_ebitda": 9.5,
                "beta": 1.10,
                "valuation_summary": "P/E: 16.5, EV/EBITDA: 9.5",
                "source": "Yahoo Finance (keyless)",
            },
            "finnhub_news": [
                {"headline": "Strong quarterly performance", "sentiment": "positive"},
            ],
            "olostep_sentiment": {
                "sentiment_label": "BULLISH",
                "sentiment_score": 0.75,
                "key_catalysts": ["Accelerating growth"],
                "source": "Live Web Search (keyless)",
            },
        }
        res = analyst.analyze("AAPL", metrics)
        self.assertEqual(res.valuation_rating, "undervalued")
        self.assertEqual(res.catalyst_assessment, "positive")
        self.assertEqual(res.macro_environment, "favorable")
        self.assertIn("Valuation [Yahoo Finance (keyless)]", res.summary)
        self.assertIn("Web Sentiment", res.summary)

    def test_technical_analyst_integration(self):
        analyst = TechnicalAnalyst(openai_api_key="")
        metrics = {
            "close": 200.0,
            "sma200": 180.0,
            "ema20": 195.0,
            "ema50": 190.0,
            "rsi14": 58.0,
            "adx": 28.0,
            "nixtla_forecast": {
                "forecast_prices": [202.0, 204.0, 206.0, 208.0, 210.0],
                "expected_return_pct": 5.0,
                "directional_bias": "BULLISH",
                "source": "Quantitative Candle Model (yfinance)",
            },
        }
        res = analyst.analyze("NVDA", metrics)
        self.assertEqual(res.trend_structure, "bullish")
        self.assertEqual(res.key_levels["nixtla_5d_target"], 210.0)
        self.assertEqual(res.key_levels["nixtla_expected_return_pct"], 5.0)
        self.assertIn("5-Day Forecast", res.summary)

    def test_debate_engine_integration(self):
        engine = DebateEngine(openai_api_key="", speculative_routing=False)
        metrics = {
            "close": 100.0,
            "sma200": 90.0,
            "ema20": 98.0,
            "ema50": 95.0,
            "rsi14": 55.0,
            "adx": 26.0,
            "finnhub_fundamentals": {"pe_ratio": 17.0},
            "olostep_sentiment": {"sentiment_label": "BULLISH", "sentiment_score": 0.70},
            "nixtla_forecast": {
                "directional_bias": "BULLISH",
                "expected_return_pct": 3.5,
                "forecast_prices": [101.0, 102.0, 102.5, 103.0, 103.5],
            },
        }
        outcome = engine.run_debate(
            ticker="QQQ",
            metrics=metrics,
            technical_report="Bullish trend",
            fundamental_report="Strong fundamentals",
        )
        self.assertEqual(outcome.winning_side, "bull")
        evidence_text = " ".join(outcome.bull_position.key_evidence)
        self.assertIn("Olostep sentiment search is BULLISH", evidence_text)
        self.assertIn("Nixtla TimeGPT 5-day forecast is BULLISH", evidence_text)

    def test_antigravity_agent_tools(self):
        fh_json = tool_get_finnhub_fundamentals("NVDA")
        fh_data = json.loads(fh_json)
        self.assertIn(fh_data.get("status"), [None, "unavailable"])

        news_json = tool_get_company_news("NVDA", days=3)
        news_data = json.loads(news_json)
        self.assertIn("articles", news_data)

        olo_json = tool_get_olostep_sentiment("NVDA")
        olo_data = json.loads(olo_json)
        self.assertIn(olo_data.get("status"), [None, "unavailable"])

        nxt_json = tool_get_nixtla_forecast("NVDA")
        nxt_data = json.loads(nxt_json)
        self.assertIn(nxt_data.get("status"), [None, "unavailable"])

        intel_json = tool_get_asset_intelligence("NVDA")
        intel_data = json.loads(intel_json)
        self.assertIn("finnhub_valuation", intel_data)
        self.assertIn("olostep_sentiment", intel_data)
        self.assertIn("nixtla_5d_forecast", intel_data)


if __name__ == "__main__":
    unittest.main()
