"""Olostep Data Provider for Market & AI Sentiment Search.

Queries Olostep Search & Answers API (https://api.olostep.com/v1) to gather real-time
web intelligence, analyst consensus, and social/market sentiment for candidate assets.
Provides structured sentiment scoring (bullish/bearish/neutral), score (0.0 to 1.0),
and key catalyst snippets.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.request
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    httpx = None
    HAS_HTTPX = False

from trading_system.config import settings
from trading_system.data.finnhub_provider import normalize_finnhub_symbol

logger = logging.getLogger(__name__)


class OlostepProvider:
    """Provider for real-time sentiment search and analyst commentary via Olostep."""

    BASE_URL: str = "https://api.olostep.com/v1"

    def __init__(
        self,
        api_key: Optional[str] = None,
        timeout: float = 10.0,
        cache_ttl: int = 900,
    ) -> None:
        """Initialize OlostepProvider with optional API key and caching TTL."""
        self.api_key = (api_key or getattr(settings, "OLOSTEP_API_KEY", "") or os.environ.get("OLOSTEP_API_KEY", "")).strip()
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self._cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}

    def is_configured(self) -> bool:
        """Check if an API key is provided."""
        return bool(self.api_key)

    def _analyze_sentiment_text(self, text: str) -> Tuple[str, float, List[str]]:
        """Compute structured sentiment rating, score [0.0, 1.0], and catalysts from text."""
        lower = text.lower()
        bull_words = [
            "bullish", "upgrade", "outperform", "buy", "beat", "strong", "growth",
            "surge", "rally", "record high", "dividend increase", "expansion",
            "profit jump", "revenue beat", "breakout", "positive outlook", "guidance raised"
        ]
        bear_words = [
            "bearish", "downgrade", "underperform", "sell", "miss", "weak", "plunge",
            "slump", "loss", "headwind", "lawsuit", "investigation", "probe", "debt",
            "margin compression", "negative outlook", "guidance cut", "drop"
        ]

        bull_count = sum(lower.count(w) for w in bull_words)
        bear_count = sum(lower.count(w) for w in bear_words)

        total = bull_count + bear_count
        if total == 0:
            score = 0.50
            label = "NEUTRAL"
        else:
            net = (bull_count - bear_count) / total
            score = max(0.0, min(1.0, 0.50 + 0.50 * net))
            if score >= 0.58:
                label = "BULLISH"
            elif score <= 0.42:
                label = "BEARISH"
            else:
                label = "NEUTRAL"

        # Extract catalyst snippets
        sentences = re.split(r"[.\n;]+", text)
        catalysts: List[str] = []
        for s in sentences:
            s_clean = s.strip()
            if len(s_clean) > 20 and any(w in s_clean.lower() for w in bull_words + bear_words):
                catalysts.append(s_clean)
                if len(catalysts) >= 3:
                    break

        return label, round(score, 2), catalysts

    def _call_olostep_api(self, query: str) -> Optional[Dict[str, Any]]:
        """Call Olostep Search/Answers API with bearer token."""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "TradingEngine/2.0",
        }
        payload = {
            "query": query,
            "max_results": 5,
        }

        # Try /searches endpoint first
        endpoints = [
            f"{self.BASE_URL}/searches",
            f"{self.BASE_URL}/answers",
        ]

        for url in endpoints:
            if HAS_HTTPX and httpx is not None:
                try:
                    with httpx.Client(timeout=self.timeout) as client:
                        resp = client.post(url, headers=headers, json=payload)
                        if resp.status_code == 200:
                            return resp.json()
                        elif resp.status_code == 429:
                            logger.warning("Olostep API rate limited (429).")
                            return None
                except Exception as exc:
                    logger.debug("Olostep HTTPX call to %s failed: %s", url, exc)

            # urllib fallback
            try:
                data_bytes = json.dumps(payload).encode("utf-8")
                req = urllib.request.Request(url, data=data_bytes, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=self.timeout) as response:
                    if response.status == 200:
                        return json.loads(response.read().decode("utf-8"))
            except Exception as exc:
                logger.debug("Olostep urllib call to %s failed: %s", url, exc)

        return None

    def get_sentiment_search(
        self,
        symbol: str,
        query_override: Optional[str] = None,
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        """Perform real-time sentiment search for a stock ticker.

        Returns:
            Dict containing:
                - symbol: str
                - sentiment_label: "BULLISH" | "BEARISH" | "NEUTRAL"
                - sentiment_score: float (0.0=extreme bearish, 0.5=neutral, 1.0=extreme bullish)
                - summary: str text synthesis
                - key_catalysts: List[str]
                - sources: List[Dict[str, str]]
                - is_simulated: bool
        """
        clean_sym = normalize_finnhub_symbol(symbol)
        now = time.time()

        if not force_refresh and clean_sym in self._cache:
            ts, cached = self._cache[clean_sym]
            if now - ts < self.cache_ttl:
                return cached

        query = query_override or f"{clean_sym} stock news sentiment financial analyst catalysts rating"

        if self.is_configured() and getattr(settings, "ENABLE_OLOSTEP_SENTIMENT", True):
            try:
                api_result = self._call_olostep_api(query)
                if isinstance(api_result, dict):
                    # Combine text across answer, snippets, results
                    text_corpus = []
                    sources: List[Dict[str, str]] = []

                    answer = api_result.get("answer") or api_result.get("summary")
                    if answer and isinstance(answer, str):
                        text_corpus.append(answer)

                    results = api_result.get("results") or api_result.get("data") or []
                    if isinstance(results, list):
                        for r in results:
                            if isinstance(r, dict):
                                title = r.get("title", "")
                                snippet = r.get("snippet", "") or r.get("text", "")
                                url = r.get("url", "") or r.get("link", "")
                                text_corpus.append(f"{title}. {snippet}")
                                if url:
                                    sources.append({"title": title or "Source", "url": url})

                    combined_text = " ".join(text_corpus)
                    if combined_text:
                        label, score, catalysts = self._analyze_sentiment_text(combined_text)
                        summary = answer if answer else (combined_text[:280] + "...")

                        sentiment_data = {
                            "symbol": clean_sym,
                            "sentiment_label": label,
                            "sentiment_score": score,
                            "summary": summary,
                            "key_catalysts": catalysts or [f"Active analyst coverage for {clean_sym}"],
                            "sources": sources[:3],
                            "is_simulated": False,
                        }
                        self._cache[clean_sym] = (now, sentiment_data)
                        return sentiment_data
            except Exception as exc:
                logger.debug("Olostep sentiment search failed for %s: %s", clean_sym, exc)

        # Baseline fallback
        baseline_sentiment = {
            "symbol": clean_sym,
            "sentiment_label": "NEUTRAL",
            "sentiment_score": 0.50,
            "summary": f"Olostep sentiment search: Balanced market sentiment and analyst consensus for {clean_sym}.",
            "key_catalysts": [
                f"Consolidation and institutional liquidity support for {clean_sym}",
                "Macro earnings visibility within expected quarterly bands"
            ],
            "sources": [],
            "is_simulated": True,
        }
        self._cache[clean_sym] = (now, baseline_sentiment)
        return baseline_sentiment

    def format_sentiment_report(self, data: Dict[str, Any]) -> str:
        """Format sentiment data into clean summary string."""
        sym = data.get("symbol", "")
        label = data.get("sentiment_label", "NEUTRAL")
        score = data.get("sentiment_score", 0.50)
        sim = " (Simulated Baseline)" if data.get("is_simulated") else ""

        icon = "🟢" if label == "BULLISH" else ("🔴" if label == "BEARISH" else "⚪")
        lines = [
            f"Olostep Sentiment Search for {sym}{sim}:",
            f"  • Rating: {icon} {label} (Score: {score:.2f} / 1.00)",
            f"  • Summary: {data.get('summary', 'Balanced sentiment.')}"
        ]
        catalysts = data.get("key_catalysts", [])
        if catalysts:
            lines.append("  • Key Catalysts:")
            for c in catalysts[:2]:
                lines.append(f"    - {c}")
        return "\n".join(lines)
