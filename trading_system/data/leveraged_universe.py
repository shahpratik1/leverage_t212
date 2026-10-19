"""3x & 5x Leveraged Asset Universe for Trading 212.

Provides curated pairs and dynamic discovery for 3x & 5x Long and Inverse Short
Exchange Traded Products (ETPs) available on Trading 212 (London Stock Exchange / European listings).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class LeveragedPair:
    """A paired set of 3x (and optionally 5x) Long and Short instruments tied to an underlying asset."""
    underlying_symbol: str      # e.g., 'QQQ', 'NVDA', 'SPY' (used for technicals/yfinance)
    name: str                   # e.g., 'Nasdaq 100', 'NVIDIA', 'S&P 500'
    category: str               # 'Index', 'Tech Equity', 'Commodity', 'Fixed Income'
    long_3x_ticker: str         # Trading 212 ticker for 3x Long (e.g. 'QQQ3l_EQ')
    long_3x_name: str           # Full instrument name
    short_3x_ticker: str        # Trading 212 ticker for 3x Short (e.g. 'QQQSl_EQ')
    short_3x_name: str          # Full instrument name
    currency: str = "USD"
    # 5x Leverage Extensions (Leverage Shares LSE/EU on Trading 212)
    long_5x_ticker: Optional[str] = None   # Trading 212 ticker for 5x Long (e.g. 'QQQ5l_EQ')
    long_5x_name: Optional[str] = None     # Full 5x Long instrument name
    short_5x_ticker: Optional[str] = None  # Trading 212 ticker for -5x Short (e.g. 'QQ3Sl_EQ')
    short_5x_name: Optional[str] = None    # Full -5x Short instrument name
    has_5x: bool = False

    def __post_init__(self) -> None:
        if self.long_5x_ticker or self.short_5x_ticker:
            self.has_5x = True


# ── Curated high-liquidity 3x & 5x pairs on Trading 212 ───────────────────────
# These represent the most actively traded 3x & 5x ETPs on the London / European exchanges
# available via Trading 212 Equity with proven liquidity and tight spreads.
# Single equities max out at 3x under UCITS/PRIIPs concentration rules.
# Broad indices, commodities, and fixed income offer institutional 5x leverage.
CURATED_3X_PAIRS: List[LeveragedPair] = [
    LeveragedPair(
        underlying_symbol="QQQ",
        name="Nasdaq 100",
        category="Index",
        long_3x_ticker="QQQ3l_EQ",
        long_3x_name="WisdomTree NASDAQ 100 3x Daily Leveraged",
        short_3x_ticker="QQQSl_EQ",
        short_3x_name="WisdomTree NASDAQ 100 3x Daily Short",
        currency="USD",
        long_5x_ticker="QQQ5l_EQ",
        long_5x_name="Leverage Shares 5x Long US Tech 100 ETP",
        short_5x_ticker="QQ3Sl_EQ",
        short_5x_name="Leverage Shares -5x Short US Tech 100 ETP",
    ),
    LeveragedPair(
        underlying_symbol="SPY",
        name="S&P 500",
        category="Index",
        long_3x_ticker="3USLl_EQ",
        long_3x_name="WisdomTree S&P 500 3x Daily Leveraged",
        short_3x_ticker="3USSl_EQ",
        short_3x_name="WisdomTree S&P 500 3x Daily Short",
        currency="USD",
        long_5x_ticker="SP5Yl_EQ",
        long_5x_name="Leverage Shares 5x Long US 500 ETP",
        short_5x_ticker="SPYSl_EQ",
        short_5x_name="Leverage Shares -5x Short US 500 ETP",
    ),
    LeveragedPair(
        underlying_symbol="NVDA",
        name="NVIDIA",
        category="Tech Equity",
        long_3x_ticker="3LNVl_EQ",
        long_3x_name="GraniteShares 3x Long NVIDIA",
        short_3x_ticker="3SNVl_EQ",
        short_3x_name="GraniteShares 3x Short NVIDIA",
        currency="USD",
    ),
    LeveragedPair(
        underlying_symbol="TSLA",
        name="Tesla",
        category="Tech Equity",
        long_3x_ticker="3LTSl_EQ",
        long_3x_name="GraniteShares 3x Long Tesla",
        short_3x_ticker="3STSl_EQ",
        short_3x_name="GraniteShares 3x Short Tesla",
        currency="USD",
    ),
    LeveragedPair(
        underlying_symbol="AAPL",
        name="Apple",
        category="Tech Equity",
        long_3x_ticker="AAP3l_EQ",
        long_3x_name="Leverage Shares 3x Apple",
        short_3x_ticker="AAPSl_EQ",
        short_3x_name="Leverage Shares -3x Short Apple AAPL",
        currency="USD",
    ),
    LeveragedPair(
        underlying_symbol="MSFT",
        name="Microsoft",
        category="Tech Equity",
        long_3x_ticker="MSF3l_EQ",
        long_3x_name="Leverage Shares 3x Microsoft",
        short_3x_ticker="MS3Sl_EQ",
        short_3x_name="Leverage Shares -3x Short Microsoft MSFT",
        currency="USD",
    ),
    LeveragedPair(
        underlying_symbol="AMZN",
        name="Amazon",
        category="Tech Equity",
        long_3x_ticker="3LZPl_EQ",
        long_3x_name="GraniteShares 3x Long Amazon",
        short_3x_ticker="3SZNl_EQ",
        short_3x_name="GraniteShares 3x Short Amazon",
        currency="USD",
    ),
    LeveragedPair(
        underlying_symbol="GOOGL",
        name="Alphabet (Google)",
        category="Tech Equity",
        long_3x_ticker="3LALl_EQ",
        long_3x_name="GraniteShares 3x Long Alphabet",
        short_3x_ticker="GGESl_EQ",
        short_3x_name="Leverage Shares -3x Short Alphabet GOOG",
        currency="USD",
    ),
    LeveragedPair(
        underlying_symbol="SOXX",
        name="Semiconductors",
        category="Index",
        long_3x_ticker="3SEMl_EQ",
        long_3x_name="WisdomTree PHLX Semiconductor 3x Daily Leveraged",
        short_3x_ticker="SM3El_EQ",
        short_3x_name="Leverage Shares -3x Short Semiconductors",
        currency="USD",
    ),
    LeveragedPair(
        underlying_symbol="GLD",
        name="Gold",
        category="Commodity",
        long_3x_ticker="3GLEl_EQ",
        long_3x_name="Leverage Shares 3x Long Gold",
        short_3x_ticker="GDMSl_EQ",
        short_3x_name="Leverage Shares -3x Short Gold Miners",
        currency="USD",
        long_5x_ticker="GLD5l_EQ",
        long_5x_name="Leverage Shares 5x Long Gold ETP",
        short_5x_ticker=None,
        short_5x_name=None,
    ),
    LeveragedPair(
        underlying_symbol="SLV",
        name="Silver",
        category="Commodity",
        long_3x_ticker="3SLVl_EQ",
        long_3x_name="Leverage Shares 3x Long Silver",
        short_3x_ticker="SLVSl_EQ",
        short_3x_name="Leverage Shares -3x Short Silver",
        currency="USD",
    ),
    LeveragedPair(
        underlying_symbol="USO",
        name="WTI Crude Oil",
        category="Commodity",
        long_3x_ticker="WTI3l_EQ",
        long_3x_name="Leverage Shares 3x Long WTI Oil",
        short_3x_ticker="SWTOl_EQ",
        short_3x_name="Leverage Shares -3x Short WTI Oil",
        currency="USD",
    ),
    LeveragedPair(
        underlying_symbol="TSM",
        name="Taiwan Semiconductor (TSMC)",
        category="Global Tech",
        long_3x_ticker="3TSMl_EQ",
        long_3x_name="Leverage Shares 3x Long TSMC",
        short_3x_ticker="STSMl_EQ",
        short_3x_name="Leverage Shares -3x Short TSMC",
        currency="USD",
    ),
    LeveragedPair(
        underlying_symbol="TLT",
        name="20+ Year Treasury Bond",
        category="Fixed Income",
        long_3x_ticker="3TYLl_EQ",
        long_3x_name="Leverage Shares 3x Long US Treasuries",
        short_3x_ticker="3TYSl_EQ",
        short_3x_name="Leverage Shares -3x Short US Treasuries",
        currency="USD",
        long_5x_ticker="TLT5l_EQ",
        long_5x_name="Leverage Shares 5x Long 20+ Year Treasury Bond ETP",
        short_5x_ticker="TL5Sl_EQ",
        short_5x_name="Leverage Shares -5x Short 20+ Year Treasury Bond ETP",
    ),
    LeveragedPair(
        underlying_symbol="MAGS",
        name="Magnificent 7 (Big Tech Basket)",
        category="Tech Basket",
        long_3x_ticker="MAG7l_EQ",
        long_3x_name="Leverage Shares 3x Long Magnificent 7",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="USD",
        long_5x_ticker="MAG7l_EQ",
        long_5x_name="Leverage Shares 5x Long Magnificent 7 ETP",
        short_5x_ticker=None,
        short_5x_name=None,
    ),
    LeveragedPair(
        underlying_symbol="^HSI",
        name="Hong Kong Hang Seng",
        category="Global Index",
        long_3x_ticker="3HSl_EQ",
        long_3x_name="GraniteShares 3x Long Hang Seng",
        short_3x_ticker="3SHSl_EQ",
        short_3x_name="GraniteShares 3x Short Hang Seng",
        currency="USD",
    ),
    LeveragedPair(
        underlying_symbol="^GDAXI",
        name="Germany DAX 40",
        category="Global Index",
        long_3x_ticker="3DELl_EQ",
        long_3x_name="WisdomTree DAX 3x Daily Leveraged",
        short_3x_ticker="3DESl_EQ",
        short_3x_name="WisdomTree DAX 3x Daily Short",
        currency="EUR",
        long_5x_ticker="DAX5l_EQ",
        long_5x_name="Leverage Shares 5x Long Germany 40 ETP",
        short_5x_ticker="DAXSl_EQ",
        short_5x_name="Leverage Shares -5x Short Germany 40 ETP",
    ),
    LeveragedPair(
        underlying_symbol="^FTSE",
        name="UK FTSE 100",
        category="Global Index",
        long_3x_ticker="3UKLl_EQ",
        long_3x_name="WisdomTree FTSE 100 3x Daily Leveraged",
        short_3x_ticker="3UKSl_EQ",
        short_3x_name="WisdomTree FTSE 100 3x Daily Short",
        currency="GBP",
        long_5x_ticker="5UKXl_EQ",
        long_5x_name="Leverage Shares 5x Long FTSE 100 ETP",
        short_5x_ticker=None,
        short_5x_name=None,
    ),
    LeveragedPair(
        underlying_symbol="^STOXX50E",
        name="EURO STOXX 50",
        category="Global Index",
        long_3x_ticker="3EULl_EQ",
        long_3x_name="WisdomTree EURO STOXX 50 3x Daily Leveraged",
        short_3x_ticker="3EUSl_EQ",
        short_3x_name="WisdomTree EURO STOXX 50 3x Daily Short",
        currency="EUR",
        long_5x_ticker="5SX5l_EQ",
        long_5x_name="Leverage Shares 5x Long EURO STOXX 50 ETP",
        short_5x_ticker=None,
        short_5x_name=None,
    ),
    LeveragedPair(
        underlying_symbol="SAP",
        name="SAP SE (Germany)",
        category="Global Tech",
        long_3x_ticker="3SAPl_EQ",
        long_3x_name="Leverage Shares 3x Long SAP",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="USD",
    ),
    LeveragedPair(
        underlying_symbol="BTC-USD",
        name="Bitcoin",
        category="Crypto",
        long_3x_ticker="3BTCl_EQ",
        long_3x_name="Leverage Shares 3x Long Bitcoin ETP",
        short_3x_ticker="3BTSl_EQ",
        short_3x_name="Leverage Shares -3x Short Bitcoin ETP",
        currency="USD",
    ),
    LeveragedPair(
        underlying_symbol="ETH-USD",
        name="Ethereum",
        category="Crypto",
        long_3x_ticker="3ETHl_EQ",
        long_3x_name="Leverage Shares 3x Long Ethereum ETP",
        short_3x_ticker="3ETSl_EQ",
        short_3x_name="Leverage Shares -3x Short Ethereum ETP",
        currency="USD",
    ),
    LeveragedPair(
        underlying_symbol="HG=F",
        name="Copper",
        category="Commodity",
        long_3x_ticker="3CPRl_EQ",
        long_3x_name="WisdomTree Copper 3x Daily Leveraged",
        short_3x_ticker="3CPSl_EQ",
        short_3x_name="WisdomTree Copper 3x Daily Short",
        currency="USD",
    ),
    LeveragedPair(
        underlying_symbol="BNO",
        name="Brent Crude Oil",
        category="Commodity",
        long_3x_ticker="3BCOl_EQ",
        long_3x_name="WisdomTree Brent Crude Oil 3x Daily Leveraged",
        short_3x_ticker="3BCSl_EQ",
        short_3x_name="WisdomTree Brent Crude Oil 3x Daily Short",
        currency="USD",
    ),
    # ── Worldwide Sovereign Indices & Thematics ────────────────────
    LeveragedPair(
        underlying_symbol="EWY",
        name="South Korea (MSCI Korea)",
        category="Global Index",
        long_3x_ticker="3KORl_EQ",
        long_3x_name="Leverage Shares 3x Long South Korea ETP",
        short_3x_ticker="SKORl_EQ",
        short_3x_name="Leverage Shares -3x Short South Korea ETP",
        currency="USD",
    ),
    LeveragedPair(
        underlying_symbol="EWJ",
        name="Japan (MSCI Japan / Nikkei 225)",
        category="Global Index",
        long_3x_ticker="JPN3l_EQ",
        long_3x_name="Leverage Shares 3x Long Japan ETP",
        short_3x_ticker="JPNSl_EQ",
        short_3x_name="Leverage Shares -3x Short Japan ETP",
        currency="USD",
    ),
    LeveragedPair(
        underlying_symbol="^FCHI",
        name="France CAC 40",
        category="Global Index",
        long_3x_ticker="3CACl_EQ",
        long_3x_name="WisdomTree CAC 40 3x Daily Leveraged",
        short_3x_ticker="3CSAl_EQ",
        short_3x_name="WisdomTree CAC 40 3x Daily Short",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="FTSEMIB.MI",
        name="Italy FTSE MIB",
        category="Global Index",
        long_3x_ticker="3ITLl_EQ",
        long_3x_name="WisdomTree FTSE MIB 3x Daily Leveraged",
        short_3x_ticker="3ITSl_EQ",
        short_3x_name="WisdomTree FTSE MIB 3x Daily Short",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="INDA",
        name="India (MSCI India)",
        category="Global Index",
        long_3x_ticker="3INDl_EQ",
        long_3x_name="Leverage Shares 3x Long India ETP",
        short_3x_ticker="INDSl_EQ",
        short_3x_name="Leverage Shares -3x Short India ETP",
        currency="USD",
    ),
    LeveragedPair(
        underlying_symbol="BOTZ",
        name="Artificial Intelligence & Robotics",
        category="Thematic",
        long_3x_ticker="GPT3l_EQ",
        long_3x_name="Leverage Shares 3x Long Artificial Intelligence ETP",
        short_3x_ticker="GPTSl_EQ",
        short_3x_name="Leverage Shares -3x Short Artificial Intelligence ETP",
        currency="USD",
    ),
    LeveragedPair(
        underlying_symbol="AMD",
        name="Advanced Micro Devices",
        category="Tech Equity",
        long_3x_ticker="AMD3l_EQ",
        long_3x_name="Leverage Shares 3x Long AMD",
        short_3x_ticker="3SAMl_EQ",
        short_3x_name="GraniteShares 3x Short AMD",
        currency="USD",
    ),
    LeveragedPair(
        underlying_symbol="ASML",
        name="ASML Holding (Lithography)",
        category="Global Tech",
        long_3x_ticker="3ASMl_EQ",
        long_3x_name="Leverage Shares 3x Long ASML",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="EUR",
    ),
]


INDEX_ALIASES: Dict[str, str] = {
    "^N225": "EWJ",
    "N225": "EWJ",
    "NIKKEI": "EWJ",
    "^KS11": "EWY",
    "KS11": "EWY",
    "KOSPI": "EWY",
    "CAC40": "^FCHI",
    "^CAC40": "^FCHI",
    "FCHI": "^FCHI",
    "FTSEMIB": "FTSEMIB.MI",
    "MIB": "FTSEMIB.MI",
    "FTSE_MIB": "FTSEMIB.MI",
    "NIFTY": "INDA",
    "^NSEI": "INDA",
    "NSEI": "INDA",
    "NIFTY50": "INDA",
    "ASML.AS": "ASML",
    "ASML.NA": "ASML",
}


class LeveragedUniverse:
    """Manages 3x and 5x leveraged trading universe for Long & Short opportunities on Trading 212."""

    def __init__(self, pairs: Optional[List[LeveragedPair]] = None):
        self.pairs: List[LeveragedPair] = pairs or CURATED_3X_PAIRS
        self._underlying_map: Dict[str, LeveragedPair] = {
            p.underlying_symbol.upper(): p for p in self.pairs
        }
        self.aliases: Dict[str, str] = dict(INDEX_ALIASES)
        self._5x_to_3x_map: Dict[str, str] = {}
        for p in self.pairs:
            if p.long_5x_ticker and p.long_3x_ticker:
                self._5x_to_3x_map[p.long_5x_ticker] = p.long_3x_ticker
            if p.short_5x_ticker and p.short_5x_ticker != "None" and p.short_3x_ticker and p.short_3x_ticker != "None":
                self._5x_to_3x_map[p.short_5x_ticker] = p.short_3x_ticker

    def replace_5x_ticker(self, ticker: str) -> str:
        """If a given ticker is a known 5x ETP, return its 3x replacement; otherwise return the ticker as-is."""
        return self._5x_to_3x_map.get(ticker, ticker)

    def get_all_underlyings(self) -> List[str]:
        """Return list of all underlying symbols to fetch OHLCV data for."""
        return [p.underlying_symbol for p in self.pairs]

    def get_pair_for_underlying(self, underlying: str) -> Optional[LeveragedPair]:
        """Lookup leveraged pair by underlying ticker or index alias."""
        key = underlying.upper()
        pair = self._underlying_map.get(key)
        if pair:
            return pair
        alias_target = self.aliases.get(key)
        if alias_target:
            return self._underlying_map.get(alias_target.upper())
        return None

    def has_5x_leverage(self, underlying: str, direction: Optional[str] = None) -> bool:
        """Check whether 5x leverage is available for the given underlying asset on Trading 212."""
        pair = self.get_pair_for_underlying(underlying)
        if not pair or not pair.has_5x:
            return False
        if direction is None:
            return pair.has_5x
        direction_upper = direction.upper()
        if direction_upper in ("LONG", "BUY", "BULLISH"):
            return bool(pair.long_5x_ticker)
        elif direction_upper in ("SHORT", "SELL", "BEARISH"):
            return bool(pair.short_5x_ticker and pair.short_5x_ticker != "None")
        return False

    def get_available_leverage_multipliers(self, underlying: str) -> List[int]:
        """Return supported leverage multipliers for an asset.

        Policy: If 5x exists on the asset, it is replaced with 3x, so available leverage is [3].
        """
        pair = self.get_pair_for_underlying(underlying)
        if not pair:
            return [1]
        return [3]

    def get_execution_ticker(
        self,
        underlying: str,
        direction: str,
        leverage: int = 3,
    ) -> Optional[str]:
        """Given an underlying symbol, direction ('LONG' or 'SHORT'), and desired leverage (3 or 5),
        return the appropriate Trading 212 execution ticker.

        Policy: If 5x is requested or exists for this asset, it is replaced with 3x ETP.
        """
        pair = self.get_pair_for_underlying(underlying)
        if not pair:
            return None

        direction_upper = direction.upper()
        is_long = direction_upper in ("LONG", "BUY", "BULLISH")
        is_short = direction_upper in ("SHORT", "SELL", "BEARISH")

        # Policy: If 5x is requested or exists, replace it with 3x
        if leverage >= 5:
            if pair.has_5x:
                logger.info(
                    "5x leverage exists for %s (%s); replacing with 3x ETP per leverage policy",
                    underlying, direction_upper,
                )
            else:
                logger.info(
                    "5x %s ETP unavailable for %s; falling back to 3x ETP",
                    direction_upper, underlying,
                )

        # 3x Resolution (standard)
        if is_long:
            return pair.long_3x_ticker
        elif is_short:
            return pair.short_3x_ticker if pair.short_3x_ticker != "None" else None

        return None

    def get_5x_pairs(self) -> List[LeveragedPair]:
        """Return all pairs in the universe that support 5x leverage."""
        return [p for p in self.pairs if p.has_5x]

    def get_all_execution_tickers(self) -> List[str]:
        """Return list of all active Trading 212 execution tickers (3x only per policy)."""
        tickers = []
        for p in self.pairs:
            if p.long_3x_ticker:
                tickers.append(p.long_3x_ticker)
            if p.short_3x_ticker and p.short_3x_ticker != "None":
                tickers.append(p.short_3x_ticker)
        return list(dict.fromkeys(tickers))
