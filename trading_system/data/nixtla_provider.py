"""Nixtla & Keyless Quantitative 5-Day Time-Series Price Forecasting Provider.

Provides 5-day horizon price forecasting:
1. Nixtla TimeGPT API (when NIXTLA_API_KEY is configured).
2. Keyless quantitative time-series smoothing on real historical candle closes (yfinance).

Strictly avoids mock/simulated data. Requires real historical candle prices.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from datetime import datetime, timezone
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


class NixtlaProvider:
    """Provider for 5-day price trajectory forecasting."""

    BASE_URL: str = "https://api.nixtla.io/v2"

    def __init__(
        self,
        api_key: Optional[str] = None,
        timeout: float = 12.0,
        horizon: int = 5,
        cache_ttl: int = 1800,
    ) -> None:
        """Initialize NixtlaProvider with optional API key, forecast horizon, and cache TTL."""
        self.api_key = (api_key or getattr(settings, "NIXTLA_API_KEY", "") or os.environ.get("NIXTLA_API_KEY", "")).strip()
        self.timeout = timeout
        self.horizon = int(getattr(settings, "NIXTLA_FORECAST_HORIZON", horizon) or 5)
        self.cache_ttl = cache_ttl
        self._cache: Dict[str, Tuple[float, Optional[Dict[str, Any]]]] = {}

    def is_configured(self) -> bool:
        """Check if a Nixtla API key is provided."""
        return bool(self.api_key)

    def _call_nixtla_api(
        self,
        dates: List[str],
        closes: List[float],
    ) -> Optional[List[float]]:
        """Call Nixtla TimeGPT REST API to generate 5-day forecast."""
        url = f"{self.BASE_URL}/forecast"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "TradingEngine/2.0",
        }
        payload = {
            "series": {
                "y": closes[-60:],
                "ds": dates[-60:],
            },
            "h": self.horizon,
            "freq": "D",
            "clean_ex_first": True,
        }

        if HAS_HTTPX and httpx is not None:
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.post(url, headers=headers, json=payload)
                    if resp.status_code == 200:
                        data = resp.json()
                        forecast = self._parse_api_forecast(data)
                        if forecast:
                            return forecast
                    elif resp.status_code == 429:
                        logger.warning("Nixtla TimeGPT API rate limited (429).")
            except Exception as exc:
                logger.debug("Nixtla HTTPX request failed: %s", exc)

        try:
            data_bytes = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(url, data=data_bytes, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                if response.status == 200:
                    data = json.loads(response.read().decode("utf-8"))
                    forecast = self._parse_api_forecast(data)
                    if forecast:
                        return forecast
        except Exception as exc:
            logger.debug("Nixtla urllib request failed: %s", exc)

        return None

    def _parse_api_forecast(self, data: Any) -> Optional[List[float]]:
        """Extract forecasted price sequence from Nixtla response object."""
        if not isinstance(data, dict):
            return None
        if "forecast" in data and isinstance(data["forecast"], list):
            return [float(x) for x in data["forecast"] if x is not None]
        sub = data.get("data")
        if isinstance(sub, dict) and "forecast" in sub and isinstance(sub["forecast"], list):
            return [float(x) for x in sub["forecast"] if x is not None]
        if isinstance(sub, list):
            prices = []
            for item in sub:
                if isinstance(item, dict):
                    v = item.get("TimeGPT") or item.get("forecast") or item.get("y")
                    if v is not None:
                        prices.append(float(v))
                elif isinstance(item, (int, float)):
                    prices.append(float(item))
            if prices:
                return prices
        return None

    def _compute_quantitative_candle_forecast(
        self,
        last_price: float,
        closes: List[float],
    ) -> Optional[List[float]]:
        """Compute real quantitative 5-day trajectory projection on historical candles.

        Uses Holt's double exponential smoothing (level + trend) on real candle closes.
        Requires at least 5 real historical closes. Returns None if data is missing.
        """
        if len(closes) < 5:
            return None

        p0 = max(0.0001, last_price)
        # Level and Trend initialization
        level = closes[0]
        trend = (closes[min(4, len(closes)-1)] - closes[0]) / max(1, min(4, len(closes)-1))

        alpha = 0.35
        beta = 0.15

        for price in closes:
            prev_level = level
            level = alpha * price + (1 - alpha) * (level + trend)
            trend = beta * (level - prev_level) + (1 - beta) * trend

        # Damped trend forward projection
        damping = 0.85
        forecast = []
        curr = p0
        for i in range(self.horizon):
            curr = curr + (trend * (damping ** (i + 1)))
            forecast.append(round(max(0.0001, curr), 4))

        return forecast

    def get_5day_forecast(
        self,
        symbol: str,
        candles: Optional[List[Dict[str, Any]]] = None,
        force_refresh: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Compute or fetch 5-day price forecast and directional bias for a symbol.

        Prioritizes Nixtla TimeGPT if configured; otherwise uses real candle quantitative model.
        Returns None if real candle data cannot be retrieved. Strictly no mocks.
        """
        clean_sym = normalize_finnhub_symbol(symbol)
        now = time.time()

        if not force_refresh and clean_sym in self._cache:
            ts, cached = self._cache[clean_sym]
            if now - ts < self.cache_ttl:
                return cached

        # Prepare dates and closes
        closes: List[float] = []
        dates: List[str] = []

        if candles and len(candles) > 0:
            for c in candles:
                if isinstance(c, dict):
                    close_val = c.get("close") or c.get("c") or c.get("Close")
                    dt_val = c.get("date") or c.get("datetime") or c.get("t")
                    if close_val is not None:
                        closes.append(float(close_val))
                        if dt_val:
                            dates.append(str(dt_val)[:10])
                        else:
                            dates.append(datetime.now(timezone.utc).strftime("%Y-%m-%d"))

        # Fetch real candles from MarketDataProvider if not provided
        if not closes:
            try:
                from trading_system.data.trading212_client import MarketDataProvider
                daily = MarketDataProvider.get_daily_candles(symbol, period="3mo")
                if daily is not None:
                    if hasattr(daily, "empty") and not daily.empty:
                        closes = [float(v) for v in daily["Close"].dropna().values]
                        dates = [str(idx)[:10] for idx in daily.index]
                    elif isinstance(daily, list):
                        for row in daily:
                            if isinstance(row, dict) and "close" in row:
                                closes.append(float(row["close"]))
                                dates.append(str(row.get("date", ""))[:10])
            except Exception as exc:
                logger.debug("Could not fetch daily candles for forecast: %s", exc)

        if not closes or len(closes) < 5:
            # Strictly no mock
            self._cache[clean_sym] = (now, None)
            return None

        last_price = closes[-1]
        forecast_prices: Optional[List[float]] = None
        source = "Quantitative Candle Model (yfinance)"

        # 1. Nixtla API if configured
        if self.is_configured() and getattr(settings, "ENABLE_NIXTLA_FORECAST", True) and len(closes) >= 15:
            try:
                api_forecast = self._call_nixtla_api(dates, closes)
                if api_forecast and len(api_forecast) >= self.horizon:
                    forecast_prices = [round(float(p), 4) for p in api_forecast[:self.horizon]]
                    source = "Nixtla TimeGPT API"
            except Exception as exc:
                logger.debug("Nixtla forecast call failed for %s: %s", clean_sym, exc)

        # 2. Keyless quantitative model on historical candles
        if not forecast_prices:
            forecast_prices = self._compute_quantitative_candle_forecast(last_price, closes)

        if not forecast_prices:
            self._cache[clean_sym] = (now, None)
            return None

        p0 = max(0.0001, last_price)
        p5 = forecast_prices[-1]
        exp_return_pct = round(((p5 - p0) / p0) * 100.0, 2)

        if exp_return_pct >= 1.25:
            directional_bias = "BULLISH"
            confidence = min(0.92, 0.70 + (abs(exp_return_pct) / 20.0))
        elif exp_return_pct <= -1.25:
            directional_bias = "BEARISH"
            confidence = min(0.92, 0.70 + (abs(exp_return_pct) / 20.0))
        else:
            directional_bias = "NEUTRAL"
            confidence = 0.65

        summary = (
            f"5-Day Forecast for {clean_sym} [{source}]: {directional_bias} bias with {exp_return_pct:+.2f}% "
            f"expected return over 5 trading days (Target: ${p5:.2f} vs Current: ${p0:.2f})."
        )

        result = {
            "symbol": clean_sym,
            "horizon_days": self.horizon,
            "last_price": p0,
            "forecast_prices": forecast_prices,
            "expected_return_pct": exp_return_pct,
            "directional_bias": directional_bias,
            "confidence": round(confidence, 2),
            "summary": summary,
            "source": source,
        }

        self._cache[clean_sym] = (now, result)
        return result

    def format_forecast_report(self, data: Optional[Dict[str, Any]]) -> str:
        """Format 5-day forecast into clean summary string."""
        if not data:
            return "5-Day forecast unavailable (insufficient candle data)."
        sym = data.get("symbol", "")
        bias = data.get("directional_bias", "NEUTRAL")
        ret = data.get("expected_return_pct", 0.0)
        p0 = data.get("last_price", 0.0)
        prices = data.get("forecast_prices", [])
        source = data.get("source", "Quantitative Model")

        icon = "🟢" if bias == "BULLISH" else ("🔴" if bias == "BEARISH" else "⚪")
        path_str = " → ".join(f"${p:.2f}" for p in prices)

        return (
            f"5-Day Price Forecast for {sym} [{source}]:\n"
            f"  • Bias: {icon} {bias} ({ret:+.2f}% expected 5d return, Confidence: {data.get('confidence', 0.65):.2f})\n"
            f"  • Trajectory: Current ${p0:.2f} → {path_str}\n"
            f"  • Summary: {data.get('summary', '')}"
        )
