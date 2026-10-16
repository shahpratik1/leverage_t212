"""Central configuration module for an institutional-grade hybrid quantitative trading engine.

Provides strongly typed configuration settings, environment variable loading,
risk and universe management parameters, and API base URL resolution.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import List, Optional


logger = logging.getLogger(__name__)


def _load_env_file() -> None:
    """Load environment variables from .env file if present."""
    env_paths = [
        os.path.join(os.getcwd(), ".env"),
        os.path.join(os.path.dirname(__file__), ".env"),
        os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"),
    ]
    for p in env_paths:
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            k, v = k.strip(), v.strip().strip("'\"")
                            if k and k not in os.environ:
                                os.environ[k] = v
            except Exception:
                pass
            break


_load_env_file()


@dataclass
class Settings:
    """Central configuration settings for the hybrid quantitative trading engine.

    Attributes:
        TRADING212_API_KEY: Trading 212 API key (loaded from TRADING212_API_KEY env var).
        TRADING212_API_SECRET: Trading 212 API secret (loaded from TRADING212_API_SECRET env var).
        TRADING212_BASE_URL: Trading 212 Demo API base URL.
        TRADING212_LIVE_URL: Trading 212 Live API base URL.
        USE_LIVE: Whether to trade in live environment (False = demo for safety).
        OPENAI_API_KEY: OpenAI API key (loaded from OPENAI_API_KEY env var).
        POLYGON_API_KEY: Polygon.io API key (loaded from POLYGON_API_KEY env var).
        FINNHUB_API_KEY: Finnhub API key (loaded from FINNHUB_API_KEY env var).
        LLM_DEEP_MODEL: LLM model name for deep reasoning debate.
        LLM_QUICK_MODEL: LLM model name for rapid analysis.
        MAX_RISK_PER_TRADE: Maximum fractional equity risk per trade (e.g. 0.005 = 0.5%).
        MAX_POSITION_PCT: Maximum fractional equity notional per position (e.g. 0.05 = 5%).
        ATR_SL_MULTIPLIER: Multiplier for ATR when computing stop-loss distance.
        MIN_SL_PCT: Minimum stop-loss percentage distance from entry price.
        TP1_R_MULTIPLE: Risk-multiple target for Take Profit 1.
        TP2_R_MULTIPLE: Risk-multiple target for Take Profit 2.
        TP3_R_MULTIPLE: Risk-multiple target for Take Profit 3.
        SCREENER_MIN_SETUP_SCORE: Minimum acceptable setup quality for candidate screening.
        SCREENER_MAX_CATALYST_RISK: Maximum acceptable catalyst-risk probability.
        SCREENER_TOP_N: Number of top-ranked candidates to select from screener.
        LOG_LEVEL: Logging level for application (INFO, DEBUG, etc.).
        TRADES_LOG_PATH: Filepath for persisting trade records in JSONL format.
        UNIVERSE_SIZE: Maximum number of candidate assets in trading universe.
        WATCHLIST: Optional list of tickers to override universe scanning.
    """

    # Broker settings
    TRADING212_API_KEY: str = ""
    TRADING212_API_SECRET: str = ""
    TRADING212_BASE_URL: str = "https://demo.trading212.com/api/v0"
    TRADING212_LIVE_URL: str = "https://live.trading212.com/api/v0"
    USE_LIVE: bool = False
    DEFAULT_ACCOUNT_EQUITY: float = 10000.0

    # AI / LLM API settings
    OPENAI_API_KEY: str = ""
    LLM_DEEP_MODEL: str = "gpt-4o"
    LLM_QUICK_MODEL: str = "gpt-4o-mini"

    # Parallel Concurrency & Intelligent Model Routing
    PARALLEL_INGESTION_WORKERS: int = 12
    PARALLEL_DELIBERATION_WORKERS: int = 5
    ENABLE_SPECULATIVE_ROUTING: bool = True
    SPECULATIVE_CONFIDENCE_THRESHOLD: float = 0.75

    # Market Data API keys
    POLYGON_API_KEY: str = ""
    FINNHUB_API_KEY: str = ""

    # Risk parameters
    MAX_RISK_PER_TRADE: float = 0.005  # 0.5% of equity
    MAX_POSITION_PCT: float = 0.05  # 5% of equity max
    ATR_SL_MULTIPLIER: float = 2.2
    MIN_SL_PCT: float = 0.005  # 0.5% minimum stop distance
    TP1_R_MULTIPLE: float = 1.0
    TP2_R_MULTIPLE: float = 2.0
    TP3_R_MULTIPLE: float = 3.0

    # Screener parameters
    SCREENER_MIN_SETUP_SCORE: str = "favorable"
    SCREENER_MAX_CATALYST_RISK: float = 0.25
    SCREENER_TOP_N: int = 5
    MIN_ADX_TREND: float = 20.0  # Trend strength gate for 3x leverage

    # 3-RSI Mean Reversion Strategy (High Win-Rate Institutional Dip Buying)
    ENABLE_3RSI_MEAN_REVERSION: bool = True
    RSI3_OVERSOLD: float = 15.0      # Extreme oversold trigger (Connors / 3-RSI)
    RSI3_OVERBOUGHT: float = 85.0    # Extreme overbought trigger for inverse short
    RSI7_OVERSOLD: float = 35.0      # Medium-term momentum confirmation
    RSI7_OVERBOUGHT: float = 60.0
    RSI14_OVERSOLD: float = 48.0     # Macro trend pullback confirmation
    RSI14_OVERBOUGHT: float = 52.0
    MR_MAX_HOLD_DAYS: int = 4        # Fast mean reversion holding period (eliminates beta decay)

    # Trailing Stop Loss
    TSL_ATR_MULTIPLIER: float = 1.2

    # Asymmetric Session Protocol & API Rate Quota Management
    SESSION_CLOSE_CUTOFF_UTC: str = "16:20"  # 10m before 16:30 LSE close
    ENFORCE_INVERSE_INTRADAY_ONLY: bool = True
    ENFORCE_LONG_BREAKEVEN_OVERNIGHT: bool = True
    EARNINGS_BLACKOUT_HOURS: int = 24
    RATE_LIMIT_INTER_CALL_DELAY: float = 1.25  # seconds between Trading 212 API calls (strictly obeys 1 req/sec quota)

    # Notifications
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""
    DISCORD_WEBHOOK_URL: str = ""
    ENABLE_DESKTOP_NOTIFICATIONS: bool = True
    NOTIFICATIONS_LOG_PATH: str = "notifications.jsonl"
    ACTIVE_TRADES_PATH: str = "active_trades.json"

    # Universe & Mode
    USE_LEVERAGED_3X: bool = True
    ENABLE_5X_LEVERAGE: bool = True
    PREFER_5X_LEVERAGE: bool = False
    LOG_LEVEL: str = "INFO"
    TRADES_LOG_PATH: str = "trades.jsonl"
    UNIVERSE_SIZE: int = 100
    WATCHLIST: Optional[List[str]] = field(default=None)

    def __post_init__(self) -> None:
        """Load configuration from environment variables if not explicitly provided."""
        # Load API keys from environment variables using os.environ.get()
        if not self.TRADING212_API_KEY:
            self.TRADING212_API_KEY = os.environ.get("TRADING212_API_KEY", "")

        if not self.TRADING212_API_SECRET:
            self.TRADING212_API_SECRET = os.environ.get("TRADING212_API_SECRET", "")

        if not self.OPENAI_API_KEY:
            self.OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

        if not self.POLYGON_API_KEY:
            self.POLYGON_API_KEY = os.environ.get("POLYGON_API_KEY", "")

        if not self.FINNHUB_API_KEY:
            self.FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "")

        # Optional environment overrides for URLs, models, and flags
        env_use_live = os.environ.get("USE_LIVE")
        if env_use_live is not None and not self.USE_LIVE:
            self.USE_LIVE = env_use_live.strip().lower() in ("1", "true", "yes", "t")

        env_base_url = os.environ.get("TRADING212_BASE_URL")
        if env_base_url:
            self.TRADING212_BASE_URL = env_base_url.strip()

        env_live_url = os.environ.get("TRADING212_LIVE_URL")
        if env_live_url:
            self.TRADING212_LIVE_URL = env_live_url.strip()

        env_deep_model = os.environ.get("LLM_DEEP_MODEL")
        if env_deep_model:
            self.LLM_DEEP_MODEL = env_deep_model.strip()

        env_quick_model = os.environ.get("LLM_QUICK_MODEL")
        if env_quick_model:
            self.LLM_QUICK_MODEL = env_quick_model.strip()

        env_log_level = os.environ.get("LOG_LEVEL")
        if env_log_level:
            self.LOG_LEVEL = env_log_level.strip().upper()

        env_trades_log_path = os.environ.get("TRADES_LOG_PATH")
        if env_trades_log_path:
            self.TRADES_LOG_PATH = env_trades_log_path.strip()

        if not self.TELEGRAM_BOT_TOKEN:
            self.TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

        if not self.TELEGRAM_CHAT_ID:
            self.TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

        if not self.DISCORD_WEBHOOK_URL:
            self.DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

        env_3x = os.environ.get("USE_LEVERAGED_3X")
        if env_3x is not None:
            self.USE_LEVERAGED_3X = env_3x.strip().lower() in ("1", "true", "yes", "t")

        env_5x = os.environ.get("ENABLE_5X_LEVERAGE")
        if env_5x is not None:
            self.ENABLE_5X_LEVERAGE = env_5x.strip().lower() in ("1", "true", "yes", "t")

        env_prefer_5x = os.environ.get("PREFER_5X_LEVERAGE")
        if env_prefer_5x is not None:
            self.PREFER_5X_LEVERAGE = env_prefer_5x.strip().lower() in ("1", "true", "yes", "t")

        if self.WATCHLIST is None:
            env_watchlist = os.environ.get("WATCHLIST")
            if env_watchlist:
                self.WATCHLIST = [s.strip() for s in env_watchlist.split(",") if s.strip()]

    @property
    def api_base_url(self) -> str:
        """Return the active Trading 212 API base URL based on the USE_LIVE flag.

        Returns:
            str: TRADING212_LIVE_URL if USE_LIVE is True, otherwise TRADING212_BASE_URL.
        """
        return self.TRADING212_LIVE_URL if self.USE_LIVE else self.TRADING212_BASE_URL

    def validate(
        self,
        required_keys: Optional[List[str]] = None,
        require_openai: bool = False,
    ) -> None:
        """Validate that required configuration keys and parameters are properly set.

        Args:
            required_keys: Optional list of attribute names that must be set and non-empty.
                Defaults to ['TRADING212_API_KEY']. To also require OpenAI or other keys,
                pass them in required_keys or set require_openai=True.
            require_openai: If True, adds 'OPENAI_API_KEY' to required keys check.

        Raises:
            ValueError: If any required key is missing/empty, or if parameter values are invalid.
        """
        keys_to_check: List[str] = list(required_keys) if required_keys is not None else ["TRADING212_API_KEY"]
        if require_openai and "OPENAI_API_KEY" not in keys_to_check:
            keys_to_check.append("OPENAI_API_KEY")

        missing: List[str] = [
            k for k in keys_to_check
            if not getattr(self, k, None) or not str(getattr(self, k)).strip()
        ]
        if missing:
            raise ValueError(
                f"Missing required configuration key(s): {', '.join(missing)}. "
                f"Please ensure they are set in environment variables or Settings."
            )

        # Validate risk parameters
        if not (0.0 < self.MAX_RISK_PER_TRADE <= 1.0):
            raise ValueError(
                f"MAX_RISK_PER_TRADE must be in (0.0, 1.0], got {self.MAX_RISK_PER_TRADE}"
            )
        if not (0.0 < self.MAX_POSITION_PCT <= 1.0):
            raise ValueError(
                f"MAX_POSITION_PCT must be in (0.0, 1.0], got {self.MAX_POSITION_PCT}"
            )
        if self.MAX_RISK_PER_TRADE > self.MAX_POSITION_PCT:
            raise ValueError(
                f"MAX_RISK_PER_TRADE ({self.MAX_RISK_PER_TRADE}) cannot exceed "
                f"MAX_POSITION_PCT ({self.MAX_POSITION_PCT})"
            )
        if self.ATR_SL_MULTIPLIER <= 0:
            raise ValueError(
                f"ATR_SL_MULTIPLIER must be positive, got {self.ATR_SL_MULTIPLIER}"
            )
        if self.MIN_SL_PCT <= 0:
            raise ValueError(
                f"MIN_SL_PCT must be positive, got {self.MIN_SL_PCT}"
            )
        if not (self.TP1_R_MULTIPLE < self.TP2_R_MULTIPLE < self.TP3_R_MULTIPLE):
            raise ValueError(
                f"Take-profit multiples must satisfy TP1 < TP2 < TP3, "
                f"got TP1={self.TP1_R_MULTIPLE}, TP2={self.TP2_R_MULTIPLE}, TP3={self.TP3_R_MULTIPLE}"
            )
        if self.UNIVERSE_SIZE <= 0:
            raise ValueError(
                f"UNIVERSE_SIZE must be positive, got {self.UNIVERSE_SIZE}"
            )
        if not (0.0 <= self.SCREENER_MAX_CATALYST_RISK <= 1.0):
            raise ValueError(
                f"SCREENER_MAX_CATALYST_RISK must be in [0.0, 1.0], got {self.SCREENER_MAX_CATALYST_RISK}"
            )
        if self.SCREENER_TOP_N <= 0:
            raise ValueError(
                f"SCREENER_TOP_N must be positive, got {self.SCREENER_TOP_N}"
            )

        if not self.OPENAI_API_KEY and not require_openai:
            logger.warning(
                "OPENAI_API_KEY is not set. System 2 LLM debate will fall back to rule-based analysis."
            )


# Module-level singleton instance
settings = Settings()
