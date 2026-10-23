"""Finnhub & Keyless Fundamental Data Provider for Company Fundamentals and News.

Provides real company fundamentals and news from:
1. Finnhub API (when FINNHUB_API_KEY is configured).
2. Keyless real market data via yfinance and Yahoo Finance RSS (when no API key is provided).

Strictly avoids mock/simulated data. If real data is unavailable, returns None or empty results.
"""

from __future__ import annotations

import logging
import os
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    httpx = None
    HAS_HTTPX = False

import json

from trading_system.config import settings

logger = logging.getLogger(__name__)


def normalize_finnhub_symbol(symbol: str) -> str:
    """Extract clean equity ticker from broker symbols.

    Examples:
        'QQQ3l_EQ' -> 'QQQ'
        '3NVDl_EQ' -> 'NVDA'
        'AAPL_US_EQ' -> 'AAPL'
        'MSFT' -> 'MSFT'
        '3USLl_EQ' -> 'USL'
    """
    clean = (symbol or "").strip().upper()
    if not clean:
        return ""

    if clean.endswith("_US_EQ"):
        clean = clean[:-6]
    elif clean.endswith("L_EQ"):
        base = clean[:-4]
        m = re.match(r"^[0-9]+([A-Z]+)$", base)
        if m:
            clean = m.group(1)
        elif base.endswith("3") or base.endswith("5"):
            clean = base[:-1]
        else:
            clean = base
    elif clean.endswith("D_EQ") or clean.endswith("_DE_EQ"):
        clean = clean.split("_")[0]
    elif clean.endswith("M_EQ") or clean.endswith("P_EQ") or clean.endswith("A_EQ"):
        clean = clean[:-4]

    ticker_remaps = {
        "NVD": "NVDA",
        "TSL": "TSLA",
        "AMZ": "AMZN",
        "MSF": "MSFT",
        "GOO": "GOOGL",
        "APP": "AAPL",
        "MET": "META",
        "MIC": "MU",
        "AMD": "AMD",
        "QQQ": "QQQ",
        "SPY": "SPY",
    }
    return ticker_remaps.get(clean, clean)


class FinnhubProvider:
    """Data provider for real company fundamentals and news."""

    BASE_URL: str = "https://finnhub.io/api/v1"

    def __init__(
        self,
        api_key: Optional[str] = None,
        timeout: float = 8.0,
        fundamentals_ttl: int = 3600,
        news_ttl: int = 900,
    ) -> None:
        """Initialize FinnhubProvider with optional API key and caching TTLs."""
        self.api_key = (api_key or getattr(settings, "FINNHUB_API_KEY", "") or os.environ.get("FINNHUB_API_KEY", "")).strip()
        self.timeout = timeout
        self.fundamentals_ttl = fundamentals_ttl
        self.news_ttl = news_ttl

        self._fundamentals_cache: Dict[str, Tuple[float, Optional[Dict[str, Any]]]] = {}
        self._news_cache: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}

    def is_configured(self) -> bool:
        """Check if a valid Finnhub API key is present."""
        return bool(self.api_key)

    def _http_get(self, path: str, params: Dict[str, str]) -> Optional[Dict[str, Any] | List[Any]]:
        """Perform HTTP GET request to Finnhub with API key and timeout."""
        query_params = dict(params)
        query_params["token"] = self.api_key
        url = f"{self.BASE_URL}{path}?{urllib.parse.urlencode(query_params)}"

        if HAS_HTTPX and httpx is not None:
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.get(url, headers={"User-Agent": "TradingEngine/2.0"})
                    if resp.status_code == 200:
                        return resp.json()
                    elif resp.status_code == 429:
                        logger.warning("Finnhub API rate limited (429).")
                        return None
                    else:
                        logger.debug("Finnhub API returned status %d for %s", resp.status_code, path)
                        return None
            except Exception as exc:
                logger.debug("Finnhub HTTPX request failed for %s: %s", path, exc)

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "TradingEngine/2.0"})
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                if response.status == 200:
                    return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            logger.debug("Finnhub urllib request failed for %s: %s", path, exc)

        return None

    def _get_keyless_fundamentals(self, clean_sym: str) -> Optional[Dict[str, Any]]:
        """Fetch real fundamentals without requiring an API key via yfinance."""
        try:
            import yfinance as yf
            t = yf.Ticker(clean_sym)
            info = getattr(t, "info", None) or {}
            if not info:
                # Try fast_info
                fast = getattr(t, "fast_info", None)
                if fast:
                    mcap = getattr(fast, "market_cap", None)
                    if mcap:
                        info = {"marketCap": mcap}

            if info:
                pe = info.get("trailingPE") or info.get("forwardPE")
                pb = info.get("priceToBook")
                ev_ebitda = info.get("enterpriseToEbitda")
                beta = info.get("beta")
                mcap = info.get("marketCap")
                name = info.get("shortName") or info.get("longName") or clean_sym
                industry = info.get("industry") or info.get("sector") or "Equity"
                high_52w = info.get("fiftyTwoWeekHigh")
                low_52w = info.get("fiftyTwoWeekLow")
                roe = info.get("returnOnEquity")
                div_yield = info.get("dividendYield")

                if pe or pb or mcap or beta:
                    val_summary_parts = []
                    if pe:
                        val_summary_parts.append(f"P/E: {float(pe):.1f}")
                    if pb:
                        val_summary_parts.append(f"P/B: {float(pb):.1f}")
                    if ev_ebitda:
                        val_summary_parts.append(f"EV/EBITDA: {float(ev_ebitda):.1f}")
                    if beta:
                        val_summary_parts.append(f"Beta: {float(beta):.2f}")

                    val_summary = ", ".join(val_summary_parts) if val_summary_parts else "Metrics in normal range"

                    return {
                        "symbol": clean_sym,
                        "name": name,
                        "market_cap": float(mcap / 1e6) if mcap else 0.0,
                        "shares_outstanding": float(info.get("sharesOutstanding", 0.0) or 0.0) / 1e6,
                        "industry": industry,
                        "currency": info.get("currency", "USD"),
                        "pe_ratio": float(pe) if pe is not None else None,
                        "pb_ratio": float(pb) if pb is not None else None,
                        "ev_to_ebitda": float(ev_ebitda) if ev_ebitda is not None else None,
                        "beta": float(beta) if beta is not None else 1.0,
                        "high_52w": float(high_52w) if high_52w is not None else None,
                        "low_52w": float(low_52w) if low_52w is not None else None,
                        "roe_ttm": float(roe) * 100.0 if roe is not None else None,
                        "dividend_yield": float(div_yield) * 100.0 if div_yield is not None else None,
                        "valuation_summary": val_summary,
                        "source": "Yahoo Finance (keyless)",
                    }
        except Exception as exc:
            logger.debug("Keyless yfinance fundamentals fetch failed for %s: %s", clean_sym, exc)

        return None

    def get_company_fundamentals(
        self, symbol: str, force_refresh: bool = False
    ) -> Optional[Dict[str, Any]]:
        """Fetch real fundamental metrics for a symbol.

        Prioritizes Finnhub if configured; otherwise uses keyless Yahoo Finance data.
        Returns None if real data cannot be retrieved. Strictly no mocks.
        """
        clean_sym = normalize_finnhub_symbol(symbol)
        now = time.time()

        if not force_refresh and clean_sym in self._fundamentals_cache:
            ts, cached = self._fundamentals_cache[clean_sym]
            if now - ts < self.fundamentals_ttl:
                return cached

        # 1. Finnhub API if configured
        if self.is_configured() and getattr(settings, "ENABLE_FINNHUB_FUNDAMENTALS", True):
            try:
                profile = self._http_get("/stock/profile2", {"symbol": clean_sym}) or {}
                metrics_resp = self._http_get("/stock/metric", {"symbol": clean_sym, "metric": "all"}) or {}
                m = metrics_resp.get("metric", {}) if isinstance(metrics_resp, dict) else {}

                if profile or m:
                    pe = m.get("peNormalizedAnnual") or m.get("peBasicExclExtraTTM")
                    pb = m.get("pbAnnual") or m.get("pbQuarterly")
                    ev_ebitda = m.get("evToEbitdaTTM")
                    beta = m.get("beta")
                    high_52w = m.get("52WeekHigh")
                    low_52w = m.get("52WeekLow")
                    roe = m.get("roeTTM")
                    div_yield = m.get("dividendYieldIndicatedAnnual")

                    val_summary_parts = []
                    if pe:
                        val_summary_parts.append(f"P/E: {float(pe):.1f}")
                    if pb:
                        val_summary_parts.append(f"P/B: {float(pb):.1f}")
                    if ev_ebitda:
                        val_summary_parts.append(f"EV/EBITDA: {float(ev_ebitda):.1f}")
                    if beta:
                        val_summary_parts.append(f"Beta: {float(beta):.2f}")

                    val_summary = ", ".join(val_summary_parts) if val_summary_parts else "Metrics in normal range"

                    result = {
                        "symbol": clean_sym,
                        "name": profile.get("name", clean_sym),
                        "market_cap": float(profile.get("marketCapitalization", 0.0) or 0.0),
                        "shares_outstanding": float(profile.get("shareOutstanding", 0.0) or 0.0),
                        "industry": profile.get("finnhubIndustry", "Equity"),
                        "currency": profile.get("currency", "USD"),
                        "pe_ratio": float(pe) if pe is not None else None,
                        "pb_ratio": float(pb) if pb is not None else None,
                        "ev_to_ebitda": float(ev_ebitda) if ev_ebitda is not None else None,
                        "beta": float(beta) if beta is not None else 1.0,
                        "high_52w": float(high_52w) if high_52w is not None else None,
                        "low_52w": float(low_52w) if low_52w is not None else None,
                        "roe_ttm": float(roe) if roe is not None else None,
                        "dividend_yield": float(div_yield) if div_yield is not None else None,
                        "valuation_summary": val_summary,
                        "source": "Finnhub API",
                    }
                    self._fundamentals_cache[clean_sym] = (now, result)
                    return result
            except Exception as exc:
                logger.debug("Failed to fetch Finnhub fundamentals for %s: %s", clean_sym, exc)

        # 2. Keyless real data via yfinance
        keyless_result = self._get_keyless_fundamentals(clean_sym)
        if keyless_result:
            self._fundamentals_cache[clean_sym] = (now, keyless_result)
            return keyless_result

        # Strictly no mock baseline
        self._fundamentals_cache[clean_sym] = (now, None)
        return None

    def _get_keyless_news(self, clean_sym: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Fetch real news articles without an API key using Yahoo Finance RSS."""
        articles: List[Dict[str, Any]] = []
        try:
            url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={clean_sym}"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                xml_data = resp.read()
            root = ET.fromstring(xml_data)
            items = root.findall(".//item")
            for it in items[:limit]:
                title = (it.find("title").text or "").strip() if it.find("title") is not None else ""
                desc = (it.find("description").text or "").strip() if it.find("description") is not None else ""
                link = (it.find("link").text or "").strip() if it.find("link") is not None else ""
                if not title:
                    continue

                lower_txt = f"{title} {desc}".lower()
                pos_words = ["surge", "jump", "record", "beat", "rally", "growth", "upgrade", "outperform", "profit", "gain"]
                neg_words = ["drop", "plunge", "fall", "miss", "slump", "loss", "downgrade", "probe", "lawsuit", "decline"]
                pos_count = sum(1 for w in pos_words if w in lower_txt)
                neg_count = sum(1 for w in neg_words if w in lower_txt)
                sentiment = "positive" if pos_count > neg_count else ("negative" if neg_count > pos_count else "neutral")

                articles.append({
                    "headline": title,
                    "summary": desc,
                    "source": "Yahoo Finance News",
                    "url": link,
                    "datetime": int(time.time()),
                    "sentiment": sentiment,
                    "source_type": "Yahoo Finance RSS (keyless)",
                })
        except Exception as exc:
            logger.debug("Keyless news fetch failed for %s: %s", clean_sym, exc)

        return articles

    def get_company_news(
        self,
        symbol: str,
        days: int = 7,
        limit: int = 5,
        force_refresh: bool = False,
    ) -> List[Dict[str, Any]]:
        """Fetch real company news articles.

        Prioritizes Finnhub if configured; otherwise uses keyless Yahoo Finance RSS.
        Returns empty list if no real articles are found. Strictly no mocks.
        """
        clean_sym = normalize_finnhub_symbol(symbol)
        now = time.time()

        if not force_refresh and clean_sym in self._news_cache:
            ts, cached = self._news_cache[clean_sym]
            if now - ts < self.news_ttl:
                return cached[:limit]

        today = datetime.now(timezone.utc).date()
        from_date = (today - timedelta(days=days)).isoformat()
        to_date = today.isoformat()

        if self.is_configured() and getattr(settings, "ENABLE_FINNHUB_NEWS", True):
            try:
                news_raw = self._http_get(
                    "/company-news",
                    {"symbol": clean_sym, "from": from_date, "to": to_date},
                )
                if isinstance(news_raw, list) and len(news_raw) > 0:
                    parsed_articles: List[Dict[str, Any]] = []
                    for item in news_raw:
                        if not isinstance(item, dict):
                            continue
                        headline = item.get("headline", "").strip()
                        summary = item.get("summary", "").strip()
                        if not headline:
                            continue

                        lower_txt = f"{headline} {summary}".lower()
                        pos_words = ["surge", "jump", "record", "beat", "rally", "growth", "upgrade", "outperform", "profit"]
                        neg_words = ["drop", "plunge", "fall", "miss", "slump", "loss", "downgrade", "probe", "lawsuit", "decline"]
                        pos_count = sum(1 for w in pos_words if w in lower_txt)
                        neg_count = sum(1 for w in neg_words if w in lower_txt)
                        sentiment = "positive" if pos_count > neg_count else ("negative" if neg_count > pos_count else "neutral")

                        parsed_articles.append({
                            "headline": headline,
                            "summary": summary,
                            "source": item.get("source", "Finnhub"),
                            "url": item.get("url", ""),
                            "datetime": item.get("datetime", int(now)),
                            "sentiment": sentiment,
                            "source_type": "Finnhub API",
                        })

                    if parsed_articles:
                        self._news_cache[clean_sym] = (now, parsed_articles)
                        return parsed_articles[:limit]
            except Exception as exc:
                logger.debug("Finnhub news fetch failed for %s: %s", clean_sym, exc)

        # Keyless real news fallback
        keyless_news = self._get_keyless_news(clean_sym, limit=limit)
        self._news_cache[clean_sym] = (now, keyless_news)
        return keyless_news

    def format_fundamentals_report(self, data: Optional[Dict[str, Any]]) -> str:
        """Format fundamentals dictionary into clean, human-readable text."""
        if not data:
            return "Company fundamentals data unavailable."
        sym = data.get("symbol", "")
        name = data.get("name", sym)
        pe = data.get("pe_ratio")
        pb = data.get("pb_ratio")
        ev = data.get("ev_to_ebitda")
        beta = data.get("beta")
        mcap = data.get("market_cap", 0.0)
        source = data.get("source", "Market Data")

        pe_str = f"{pe:.1f}" if pe is not None else "N/A"
        pb_str = f"{pb:.1f}" if pb is not None else "N/A"
        ev_str = f"{ev:.1f}" if ev is not None else "N/A"
        beta_str = f"{beta:.2f}" if beta is not None else "1.00"

        return (
            f"Fundamentals for {name} ({sym}) [{source}]:\n"
            f"  • Valuation: P/E={pe_str}, P/B={pb_str}, EV/EBITDA={ev_str}\n"
            f"  • Profile: Beta={beta_str}, Market Cap=${mcap:,.0f}M, Industry={data.get('industry', 'N/A')}\n"
            f"  • Summary: {data.get('valuation_summary', 'Normal valuation')}"
        )

    def format_news_report(self, news: List[Dict[str, Any]]) -> str:
        """Format recent company news articles into clean summary text."""
        if not news:
            return "No recent company news articles available."
        lines = []
        for n in news[:3]:
            sent_icon = "🟢" if n.get("sentiment") == "positive" else ("🔴" if n.get("sentiment") == "negative" else "⚪")
            lines.append(f"  {sent_icon} [{n.get('source', 'News')}] {n.get('headline', '')}")
        return "Recent Company News:\n" + "\n".join(lines)
