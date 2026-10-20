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
    # ── Commodities Expansion (Natural Gas, Uranium, Coffee, Wheat) ──────────
    LeveragedPair(
        underlying_symbol="UNG",
        name="Natural Gas",
        category="Commodity",
        long_3x_ticker="3NGLl_EQ",
        long_3x_name="WisdomTree Natural Gas 3x Daily Leveraged",
        short_3x_ticker="3NGSl_EQ",
        short_3x_name="WisdomTree Natural Gas 3x Daily Short",
        currency="USD",
    ),
    LeveragedPair(
        underlying_symbol="URNM",
        name="Uranium & Nuclear Energy",
        category="Commodity",
        long_3x_ticker="URNMl_EQ",
        long_3x_name="Global X Uranium ETF (ETP)",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="USD",
    ),
    LeveragedPair(
        underlying_symbol="COFF",
        name="Coffee",
        category="Commodity",
        long_3x_ticker="3COFl_EQ",
        long_3x_name="WisdomTree Coffee 3x Daily Leveraged",
        short_3x_ticker="3COSVl_EQ",
        short_3x_name="WisdomTree Coffee 3x Daily Short",
        currency="USD",
    ),
    LeveragedPair(
        underlying_symbol="WEAT",
        name="Wheat (Agriculture)",
        category="Commodity",
        long_3x_ticker="3WHLl_EQ",
        long_3x_name="WisdomTree Wheat 3x Daily Leveraged",
        short_3x_ticker="3WETl_EQ",
        short_3x_name="WisdomTree Wheat 3x Daily Short",
        currency="USD",
    ),
    # ── Sovereign Bonds & Fixed Income Expansion ──────────────────────────────
    LeveragedPair(
        underlying_symbol="3GIL",
        name="UK Gilts (10Y Gilt)",
        category="Fixed Income",
        long_3x_ticker="3GILl_EQ",
        long_3x_name="WisdomTree UK Gilts 10Y 3x Daily Leveraged",
        short_3x_ticker="3GISl_EQ",
        short_3x_name="WisdomTree UK Gilts 10Y 3x Daily Short",
        currency="GBP",
    ),
    LeveragedPair(
        underlying_symbol="BUND",
        name="German Euro-Bund (10Y Bund)",
        category="Fixed Income",
        long_3x_ticker="3BUNl_EQ",
        long_3x_name="WisdomTree Euro-Bund 10Y 3x Daily Leveraged",
        short_3x_ticker="3BUSl_EQ",
        short_3x_name="WisdomTree Euro-Bund 10Y 3x Daily Short",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="HYG",
        name="High Yield Corporate Bonds",
        category="Fixed Income",
        long_3x_ticker="HYGl_EQ",
        long_3x_name="iShares $ High Yield Corp Bond UCITS ETF",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="USD",
    ),
    # ── Expanded 24/7 Crypto ──────────────────────────────────────────────────
    LeveragedPair(
        underlying_symbol="SOL-USD",
        name="Solana",
        category="Crypto",
        long_3x_ticker="3SOLl_EQ",
        long_3x_name="Leverage Shares 3x Long Solana ETP",
        short_3x_ticker="3SOSl_EQ",
        short_3x_name="Leverage Shares -3x Short Solana ETP",
        currency="USD",
    ),
    LeveragedPair(
        underlying_symbol="XRP-USD",
        name="Ripple (XRP)",
        category="Crypto",
        long_3x_ticker="XRPl_EQ",
        long_3x_name="21Shares Ripple XRP ETP",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="USD",
    ),
    LeveragedPair(
        underlying_symbol="ADA-USD",
        name="Cardano (ADA)",
        category="Crypto",
        long_3x_ticker="ADAl_EQ",
        long_3x_name="21Shares Cardano ETP",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="USD",
    ),
    # ── Macro Sector SPDRs & Thematic ETFs ────────────────────────────────────
    LeveragedPair(
        underlying_symbol="XLF",
        name="Financial Select Sector",
        category="Sector ETF",
        long_3x_ticker="3LFLl_EQ",
        long_3x_name="GraniteShares 3x Long Financials ETP",
        short_3x_ticker="3SFLl_EQ",
        short_3x_name="GraniteShares 3x Short Financials ETP",
        currency="USD",
    ),
    LeveragedPair(
        underlying_symbol="XLE",
        name="Energy Select Sector",
        category="Sector ETF",
        long_3x_ticker="3LENl_EQ",
        long_3x_name="GraniteShares 3x Long Energy ETP",
        short_3x_ticker="3SENl_EQ",
        short_3x_name="GraniteShares 3x Short Energy ETP",
        currency="USD",
    ),
    LeveragedPair(
        underlying_symbol="XLV",
        name="Health Care Select Sector",
        category="Sector ETF",
        long_3x_ticker="3LHLl_EQ",
        long_3x_name="GraniteShares 3x Long Healthcare ETP",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="USD",
    ),
    LeveragedPair(
        underlying_symbol="XLI",
        name="Industrial Select Sector",
        category="Sector ETF",
        long_3x_ticker="XLIl_EQ",
        long_3x_name="Industrial Select Sector SPDR Fund",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="USD",
    ),
    LeveragedPair(
        underlying_symbol="SMH",
        name="VanEck Semiconductor ETF",
        category="Sector ETF",
        long_3x_ticker="3SMHl_EQ",
        long_3x_name="Leverage Shares 3x Long Semiconductors ETP",
        short_3x_ticker="3SMSl_EQ",
        short_3x_name="Leverage Shares -3x Short Semiconductors ETP",
        currency="USD",
    ),
    # ── Worldwide Blue-Chips & Global Equities ────────────────────────────────
    LeveragedPair(
        underlying_symbol="NVO",
        name="Novo Nordisk (Denmark)",
        category="Global Equity",
        long_3x_ticker="3NONl_EQ",
        long_3x_name="Leverage Shares 3x Long Novo Nordisk",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="USD",
    ),
    LeveragedPair(
        underlying_symbol="MC.PA",
        name="LVMH Moet Hennessy (France)",
        category="Global Equity",
        long_3x_ticker="3LVHl_EQ",
        long_3x_name="Leverage Shares 3x Long LVMH",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="TM",
        name="Toyota Motor (Japan)",
        category="Global Equity",
        long_3x_ticker="3TYTl_EQ",
        long_3x_name="Leverage Shares 3x Long Toyota Motor",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="USD",
    ),
    # ── Currency ETPs (Spot FX Proxy on Invest) ──────────────────────────────
    LeveragedPair(
        underlying_symbol="EURUSD=X",
        name="USD / EUR Currency Pair",
        category="Currency",
        long_3x_ticker="USEUl_EQ",
        long_3x_name="WisdomTree Long USD Short EUR ETP",
        short_3x_ticker="EUUSl_EQ",
        short_3x_name="WisdomTree Long EUR Short USD ETP",
        currency="USD",
    ),
    LeveragedPair(
        underlying_symbol="GBPUSD=X",
        name="USD / GBP Currency Pair",
        category="Currency",
        long_3x_ticker="GBUSl_EQ",
        long_3x_name="WisdomTree Long USD Short GBP ETP",
        short_3x_ticker="USGBl_EQ",
        short_3x_name="WisdomTree Long GBP Short USD ETP",
        currency="USD",
    ),
    # ── Quantum Computing Frontier Theme ────────────────────────────────────
    LeveragedPair(
        underlying_symbol="IONQ",
        name="IonQ (Trapped-Ion Quantum Computing)",
        category="Quantum Computing",
        long_3x_ticker="IONQl1_EQ",
        long_3x_name="Leverage Shares 3x Long IONQ ETP",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="USD",
    ),
    LeveragedPair(
        underlying_symbol="RGTI",
        name="Rigetti Computing (Superconducting Quantum)",
        category="Quantum Computing",
        long_3x_ticker="RGT3l_EQ",
        long_3x_name="Leverage Shares 3x Long Rigetti Computing ETP",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="USD",
    ),
    LeveragedPair(
        underlying_symbol="IBM",
        name="IBM (Enterprise Quantum Heron & AI Infra)",
        category="Quantum Computing",
        long_3x_ticker="IBM_US_EQ",
        long_3x_name="International Business Machines Corp",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="USD",
    ),
    # ── Space & Aerospace Frontier Theme ────────────────────────────────────
    LeveragedPair(
        underlying_symbol="RKLB",
        name="Rocket Lab (Orbital Launch & Space Systems)",
        category="Space & Aerospace",
        long_3x_ticker="RKL3l_EQ",
        long_3x_name="Leverage Shares 3x Long Rocket Lab ETP",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="USD",
    ),
    LeveragedPair(
        underlying_symbol="LUNR",
        name="Global X Space Tech & Satellite ETF",
        category="Space & Aerospace",
        long_3x_ticker="LUNRl_EQ",
        long_3x_name="Global X Space Tech ETP",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="USD",
    ),
    # ── Memory & High-Bandwidth Storage (HBM & AI Data) ─────────────────────
    LeveragedPair(
        underlying_symbol="MU",
        name="Micron Technology (HBM3e Memory Leader)",
        category="Memory & Storage",
        long_3x_ticker="MU2l_EQ",
        long_3x_name="Leverage Shares 2x Micron Technology ETP",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="USD",
    ),
    LeveragedPair(
        underlying_symbol="WDC",
        name="Western Digital (Enterprise Flash & Storage)",
        category="Memory & Storage",
        long_3x_ticker="WDC_US_EQ",
        long_3x_name="Western Digital Corp",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="USD",
    ),
    # ── Trillion-Dollar Mega-Caps & Enterprise AI Infrastructure ───────────
    LeveragedPair(
        underlying_symbol="AVGO",
        name="Broadcom (AI Custom Silicon & XPU Networking)",
        category="AI Infrastructure",
        long_3x_ticker="AVG3l_EQ",
        long_3x_name="Leverage Shares 3x Long Broadcom ETP",
        short_3x_ticker="SAVGl_EQ",
        short_3x_name="Leverage Shares -3x Short Broadcom ETP",
        currency="USD",
    ),
    LeveragedPair(
        underlying_symbol="PLTR",
        name="Palantir (Enterprise AI & Defense Operating System)",
        category="AI Infrastructure",
        long_3x_ticker="3LPAl_EQ",
        long_3x_name="GraniteShares 3x Long Palantir ETP",
        short_3x_ticker="3SPAl_EQ",
        short_3x_name="GraniteShares 3x Short Palantir ETP",
        currency="USD",
    ),
    LeveragedPair(
        underlying_symbol="ARM",
        name="ARM Holdings (Global AI Architecture)",
        category="AI Infrastructure",
        long_3x_ticker="ARM3l_EQ",
        long_3x_name="Leverage Shares 3x Long ARM ETP",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="USD",
    ),
    LeveragedPair(
        underlying_symbol="CIBR",
        name="Cybersecurity & Cloud Defense ETF",
        category="Cybersecurity",
        long_3x_ticker="CIBRl_EQ",
        long_3x_name="First Trust Nasdaq Cybersecurity ETP",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="USD",
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
    "SOL": "SOL-USD",
    "XRP": "XRP-USD",
    "ADA": "ADA-USD",
    "NATGAS": "UNG",
    "NG=F": "UNG",
    "KC=F": "COFF",
    "COFFEE": "COFF",
    "WHEAT": "WEAT",
    "ZW=F": "WEAT",
    "GILTS": "3GIL",
    "GLTS": "3GIL",
    "FGBL": "BUND",
    "GERMAN_BUND": "BUND",
    "LVMH": "MC.PA",
    "LVMUY": "MC.PA",
    "NOVO": "NVO",
    "NOVO-B.CO": "NVO",
    "TOYOTA": "TM",
    "7203.T": "TM",
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "MICRON": "MU",
    "BROADCOM": "AVGO",
    "PALANTIR": "PLTR",
    "ROCKETLAB": "RKLB",
    "RIGETTI": "RGTI",
    "SPACE": "LUNR",
    "CYBER": "CIBR",
    "CYBERSECURITY": "CIBR",
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

    def get_pair_for_etp(self, etp_ticker: str) -> Optional[LeveragedPair]:
        """Lookup LeveragedPair by any of its ETP execution tickers."""
        clean = (etp_ticker or "").strip().upper()
        for p in self.pairs:
            matches = [
                (p.long_3x_ticker or "").upper(),
                (p.short_3x_ticker or "").upper(),
                (p.long_5x_ticker or "").upper(),
                (p.short_5x_ticker or "").upper(),
            ]
            if clean in matches:
                return p
        return None

    def get_category_for_symbol(self, symbol: str) -> str:
        """Return the market category/sector cluster for an underlying asset or ETP ticker.

        Returns:
            str: Category such as 'Index', 'Tech Equity', 'Commodity', 'Fixed Income',
                 'Global Tech', 'Global Index', 'Thematic', or fallback 'General Equity'.
        """
        # 1. Direct underlying lookup
        pair = self.get_pair_for_underlying(symbol)
        if pair:
            return pair.category

        # 2. ETP ticker lookup
        pair_etp = self.get_pair_for_etp(symbol)
        if pair_etp:
            return pair_etp.category

        # 3. Heuristic matching
        clean = (symbol or "").upper()
        if any(c in clean for c in ("BTC", "ETH", "SOL", "XRP", "ADA")):
            return "Crypto"
        if any(curr in clean for curr in ("EURUSD", "GBPUSD", "USEU", "EUUS", "GBUS", "USGB")):
            return "Currency"
        if any(s in clean for s in ("XLF", "XLE", "XLV", "XLI", "SMH", "3LFL", "3LEN", "3LHL", "3SMH")):
            return "Sector ETF"
        if any(comm in clean for comm in ("GLD", "SLV", "USO", "WTI", "GOLD", "UNG", "URNM", "COFF", "WEAT", "WHEAT", "CPR")):
            return "Commodity"
        if any(fi in clean for fi in ("TLT", "BOND", "TYL", "TREASURY", "GILT", "BUND", "HYG", "3GIL", "3BUN")):
            return "Fixed Income"
        if any(tech in clean for tech in ("NVDA", "AAPL", "MSFT", "TSLA", "AMZN", "GOOG", "AMD", "TSM", "MAGS", "QQQ")):
            return "Tech Equity"
        if any(idx in clean for idx in ("SPY", "DAX", "FTSE", "HSI", "CAC", "MIB", "KOR", "JPN")):
            return "Index"
        return "General Equity"

    def get_all_execution_tickers(self) -> List[str]:
        """Return list of all active Trading 212 execution tickers (3x only per policy)."""
        tickers = []
        for p in self.pairs:
            if p.long_3x_ticker:
                tickers.append(p.long_3x_ticker)
            if p.short_3x_ticker and p.short_3x_ticker != "None":
                tickers.append(p.short_3x_ticker)
        return list(dict.fromkeys(tickers))
