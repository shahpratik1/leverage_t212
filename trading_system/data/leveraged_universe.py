"""3x & 5x Leveraged Asset Universe for Trading 212.

Provides curated pairs and dynamic discovery for 3x & 5x Long and Inverse Short
Exchange Traded Products (ETPs) available on Trading 212 (European / London listings).

Currency Preference Policy:
EUR-denominated ETPs (Euronext, Deutsche Börse Xetra, Borsa Italiana) are prioritized as
the first preference whenever available to eliminate currency conversion fees and FX rate risk
for European accounts, falling back to USD and GBP/GBX when no EUR quote exists.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class LeveragedPair:
    """A paired set of 3x (and optionally 5x) Long and Short instruments tied to an underlying asset."""
    underlying_symbol: str      # e.g., 'QQQ', 'NVDA', 'SPY' (used for technicals/yfinance)
    name: str                   # e.g., 'Nasdaq 100', 'NVIDIA', 'S&P 500'
    category: str               # 'Index', 'Tech Equity', 'Commodity', 'Fixed Income'
    long_3x_ticker: str         # Trading 212 ticker for 3x Long (e.g. 'QQQ3m_EQ')
    long_3x_name: str           # Full instrument name
    short_3x_ticker: str        # Trading 212 ticker for 3x Short (e.g. 'QQQSm_EQ')
    short_3x_name: str          # Full instrument name
    currency: str = "EUR"       # Primary quote currency (EUR preferred, fallback USD/GBP)
    # 5x Leverage Extensions (Leverage Shares LSE/EU on Trading 212)
    long_5x_ticker: Optional[str] = None   # Trading 212 ticker for 5x Long (e.g. '5QQEl_EQ')
    long_5x_name: Optional[str] = None     # Full 5x Long instrument name
    short_5x_ticker: Optional[str] = None  # Trading 212 ticker for -5x Short (e.g. 'SQQEl_EQ')
    short_5x_name: Optional[str] = None    # Full -5x Short instrument name
    has_5x: bool = False

    def __post_init__(self) -> None:
        if self.long_5x_ticker or self.short_5x_ticker:
            self.has_5x = True


# ── Curated high-liquidity 3x & 5x pairs on Trading 212 ───────────────────────
# Priority: EUR first preference. Over 100 out of 120 pairs trade directly in EUR.
# Single equities max out at 3x under UCITS/PRIIPs concentration rules.
# Broad indices, commodities, and fixed income offer institutional 5x leverage.
CURATED_3X_PAIRS: List[LeveragedPair] = [
    # ── US Tech & Broad Indices (EUR First Preference) ─────────────────────────
    LeveragedPair(
        underlying_symbol="QQQ",
        name="Nasdaq 100",
        category="Index",
        long_3x_ticker="QQQ3m_EQ",
        long_3x_name="WisdomTree NASDAQ 100 3x Daily Leveraged",
        short_3x_ticker="QQQSm_EQ",
        short_3x_name="WisdomTree NASDAQ 100 3x Daily Short",
        currency="EUR",
        long_5x_ticker="5QQEl_EQ",
        long_5x_name="Leverage Shares 5x Long Nasdaq 100",
        short_5x_ticker="SQQEl_EQ",
        short_5x_name="Leverage Shares -5x Short Nasdaq 100",
    ),
    LeveragedPair(
        underlying_symbol="SPY",
        name="S&P 500",
        category="Index",
        long_3x_ticker="3USLm_EQ",
        long_3x_name="WisdomTree S&P 500 3x Daily Leveraged",
        short_3x_ticker="3USSm_EQ",
        short_3x_name="WisdomTree S&P 500 3x Daily Short",
        currency="EUR",
        long_5x_ticker="5SPEl_EQ",
        long_5x_name="Leverage Shares 5x Long S&P 500",
        short_5x_ticker="SSPEl_EQ",
        short_5x_name="Leverage Shares -5x Short S&P 500",
    ),
    LeveragedPair(
        underlying_symbol="NVDA",
        name="NVIDIA",
        category="Tech Equity",
        long_3x_ticker="3LVEl_EQ",
        long_3x_name="GraniteShares 3x Long NVIDIA",
        short_3x_ticker="3SVEl_EQ",
        short_3x_name="GraniteShares 3x Short NVIDIA",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="TSLA",
        name="Tesla",
        category="Tech Equity",
        long_3x_ticker="3LTEl_EQ",
        long_3x_name="GraniteShares 3x Long Tesla",
        short_3x_ticker="3STEl_EQ",
        short_3x_name="GraniteShares 3x Short Tesla",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="AAPL",
        name="Apple",
        category="Tech Equity",
        long_3x_ticker="3APEl_EQ",
        long_3x_name="Leverage Shares 3x Apple",
        short_3x_ticker="SALEl_EQ",
        short_3x_name="Leverage Shares -3x Short Apple AAPL",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="MSFT",
        name="Microsoft",
        category="Tech Equity",
        long_3x_ticker="3MSEl_EQ",
        long_3x_name="Leverage Shares 3x Microsoft",
        short_3x_ticker="3SMTl_EQ",
        short_3x_name="Leverage Shares -3x Short Microsoft MSFT",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="AMZN",
        name="Amazon",
        category="Tech Equity",
        long_3x_ticker="3LPEl_EQ",
        long_3x_name="GraniteShares 3x Long Amazon",
        short_3x_ticker="3SPEl_EQ",
        short_3x_name="GraniteShares 3x Short Amazon",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="GOOGL",
        name="Alphabet (Google)",
        category="Tech Equity",
        long_3x_ticker="3LGEl_EQ",
        long_3x_name="GraniteShares 3x Long Alphabet",
        short_3x_ticker="3SGEl_EQ",
        short_3x_name="GraniteShares 3x Short Alphabet",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="SOXX",
        name="Semiconductors",
        category="Index",
        long_3x_ticker="3SEMd_EQ",
        long_3x_name="WisdomTree PHLX Semiconductor 3x Daily Leveraged (Acc)",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="GLD",
        name="Gold",
        category="Commodity",
        long_3x_ticker="3GLEl_EQ",
        long_3x_name="Leverage Shares 3x Long Gold",
        short_3x_ticker="SGDEl_EQ",
        short_3x_name="Leverage Shares -3x Short Gold Miners",
        currency="EUR",
        long_5x_ticker="GLD5a_EQ",
        long_5x_name="Leverage Shares 5x Long Gold (Acc)",
        short_5x_ticker=None,
        short_5x_name=None,
    ),
    LeveragedPair(
        underlying_symbol="SLV",
        name="Silver",
        category="Commodity",
        long_3x_ticker="3SLEl_EQ",
        long_3x_name="Leverage Shares 3x Long Silver",
        short_3x_ticker="SSLEl_EQ",
        short_3x_name="Leverage Shares -3x Short Silver",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="USO",
        name="WTI Crude Oil",
        category="Commodity",
        long_3x_ticker="3OILm_EQ",
        long_3x_name="WisdomTree WTI Crude Oil 3x Daily Leveraged",
        short_3x_ticker="3OISm_EQ",
        short_3x_name="WisdomTree WTI Crude Oil 3x Short",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="TSM",
        name="Taiwan Semiconductor (TSMC)",
        category="Global Tech",
        long_3x_ticker="TS3El_EQ",
        long_3x_name="Leverage Shares 3x Long Taiwan Semiconductor TSM",
        short_3x_ticker="STS3l_EQ",
        short_3x_name="Leverage Shares -3x Short Taiwan Semiconductor TSM",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="TLT",
        name="20+ Year Treasury Bond",
        category="Fixed Income",
        long_3x_ticker="3TYLm_EQ",
        long_3x_name="WisdomTree US Treasuries 10Y 3x Leveraged",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="EUR",
        long_5x_ticker="TLT5d_EQ",
        long_5x_name="Leverage Shares 5x Long 20+ Year Treasury Bond",
        short_5x_ticker="5STLd_EQ",
        short_5x_name="Leverage Shares -5x Short 20+ Year Treasury Bond",
    ),
    LeveragedPair(
        underlying_symbol="MAGS",
        name="Magnificent 7 (Big Tech Basket)",
        category="Tech Basket",
        long_3x_ticker="MAG7m_EQ",
        long_3x_name="Leverage Shares 5x Long Magnificent 7",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="EUR",
        long_5x_ticker="MAG7m_EQ",
        long_5x_name="Leverage Shares 5x Long Magnificent 7",
        short_5x_ticker=None,
        short_5x_name=None,
    ),
    LeveragedPair(
        underlying_symbol="FXI",
        name="China Large-Cap (iShares China Large-Cap ETP)",
        category="Global Index",
        long_3x_ticker="3CHEl_EQ",
        long_3x_name="Leverage Shares 3x Long China",
        short_3x_ticker="SCHEl_EQ",
        short_3x_name="Leverage Shares -3x Short China",
        currency="EUR",
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
        long_5x_ticker=None,
        long_5x_name=None,
        short_5x_ticker="SGYEl_EQ",
        short_5x_name="Leverage Shares -5x Short DAX",
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
        long_5x_ticker="5SX5m_EQ",
        long_5x_name="Leverage Shares 5x Long EURO STOXX 50 (Acc)",
        short_5x_ticker=None,
        short_5x_name=None,
    ),
    LeveragedPair(
        underlying_symbol="MBG",
        name="Mercedes-Benz Group (German Industrial & Automotive Leader)",
        category="Global Tech",
        long_3x_ticker="3MBEl_EQ",
        long_3x_name="Leverage Shares 3x Long Mercedes Benz MBG",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="BTC-USD",
        name="Bitcoin",
        category="Crypto",
        long_3x_ticker="BTC3EURs_EQ",
        long_3x_name="Leverage Shares 3x Long Bitcoin BTC (Acc)",
        short_3x_ticker="BTC3SEURs_EQ",
        short_3x_name="Leverage Shares -3x Short Bitcoin BTC (Acc)",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="ETH-USD",
        name="Ethereum",
        category="Crypto",
        long_3x_ticker="ETH3EURs_EQ",
        long_3x_name="Leverage Shares 3x Long Ethereum ETH (Acc)",
        short_3x_ticker="ETH3SEURs_EQ",
        short_3x_name="Leverage Shares -3x Short Ethereum ETH (Acc)",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="HG=F",
        name="Copper",
        category="Commodity",
        long_3x_ticker="3HCLm_EQ",
        long_3x_name="WisdomTree Copper 3x Daily Leveraged",
        short_3x_ticker="3HCSm_EQ",
        short_3x_name="WisdomTree Copper 3x Daily Short",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="BNO",
        name="Brent Crude Oil",
        category="Commodity",
        long_3x_ticker="3BRLm_EQ",
        long_3x_name="WisdomTree Brent Crude Oil 3x Daily Leveraged",
        short_3x_ticker="3BRSm_EQ",
        short_3x_name="WisdomTree Brent Crude Oil 3x Daily Short",
        currency="EUR",
    ),
    # ── Worldwide Sovereign Indices & Thematics (EUR First Preference) ─────────
    LeveragedPair(
        underlying_symbol="EWY",
        name="South Korea (MSCI Korea)",
        category="Global Index",
        long_3x_ticker="3SKEl_EQ",
        long_3x_name="Leverage Shares 3x Long South Korea",
        short_3x_ticker="SKORl_EQ",
        short_3x_name="Leverage Shares -3x Short South Korea ETP",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="EWJ",
        name="Japan (MSCI Japan / Nikkei 225)",
        category="Global Index",
        long_3x_ticker="3JPEl_EQ",
        long_3x_name="Leverage Shares 3x Long Japan",
        short_3x_ticker="JPNSl_EQ",
        short_3x_name="Leverage Shares -3x Short Japan ETP",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="^FCHI",
        name="France CAC 40",
        category="Global Index",
        long_3x_ticker="3LVd_EQ",
        long_3x_name="WisdomTree CAC 40 3x Leveraged",
        short_3x_ticker="3DECd_EQ",
        short_3x_name="WisdomTree CAC 40 3x Short",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="FTSEMIB.MI",
        name="Italy FTSE MIB",
        category="Global Index",
        long_3x_ticker="5ITLm_EQ",
        long_3x_name="WisdomTree FTSE MIB 5x Leveraged (Acc)",
        short_3x_ticker="5ITSm_EQ",
        short_3x_name="WisdomTree FTSE MIB 5x Short (Acc)",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="INDA",
        name="India (MSCI India)",
        category="Global Index",
        long_3x_ticker="3INDd_EQ",
        long_3x_name="Leverage Shares 3x Long India",
        short_3x_ticker="SIN3d_EQ",
        short_3x_name="Leverage Shares -3x Short India",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="BOTZ",
        name="Artificial Intelligence & Robotics",
        category="Thematic",
        long_3x_ticker="GPT3m_EQ",
        long_3x_name="Leverage Shares 3x Long Artificial Intelligence",
        short_3x_ticker="GPTSl_EQ",
        short_3x_name="Leverage Shares -3x Short Artificial Intelligence ETP",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="AMD",
        name="Advanced Micro Devices",
        category="Tech Equity",
        long_3x_ticker="3ADEl_EQ",
        long_3x_name="Leverage Shares 3x AMD",
        short_3x_ticker="3SAMl_EQ",
        short_3x_name="GraniteShares 3x Short AMD",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="ASML",
        name="ASML Holding (Lithography)",
        category="Global Tech",
        long_3x_ticker="3ASMl1_EQ",
        long_3x_name="Leverage Shares 3x Long ASML (Acc)",
        short_3x_ticker="SASLl_EQ",
        short_3x_name="Leverage Shares -3x Short ASML (Acc)",
        currency="GBX",
    ),
    # ── Commodities Expansion (EUR First Preference) ──────────────────────────
    LeveragedPair(
        underlying_symbol="UNG",
        name="Natural Gas",
        category="Commodity",
        long_3x_ticker="3NGLm_EQ",
        long_3x_name="WisdomTree Natural Gas 3x Daily Leveraged",
        short_3x_ticker="3NGSm_EQ",
        short_3x_name="WisdomTree Natural Gas 3x Daily Short",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="URNM",
        name="Uranium & Nuclear Energy",
        category="Commodity",
        long_3x_ticker="U3O8d_EQ",
        long_3x_name="Sprott Uranium Miners (Acc)",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="COFF",
        name="Coffee",
        category="Commodity",
        long_3x_ticker="3CFLm_EQ",
        long_3x_name="WisdomTree Coffee 3x Daily Leveraged",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="WEAT",
        name="Wheat (Agriculture)",
        category="Commodity",
        long_3x_ticker="3WHLm_EQ",
        long_3x_name="WisdomTree Wheat 3x Daily Leveraged",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="EUR",
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
        long_3x_ticker="3BULm_EQ",
        long_3x_name="WisdomTree Bund 10Y 3x",
        short_3x_ticker="3BUSm_EQ",
        short_3x_name="WisdomTree Bund 10Y 3x Daily Short",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="MSTR",
        name="MicroStrategy (Bitcoin Treasury Pioneer)",
        category="Crypto",
        long_3x_ticker="3LMIm_EQ",
        long_3x_name="GraniteShares 3x Long MicroStrategy",
        short_3x_ticker="3SMIm_EQ",
        short_3x_name="GraniteShares 3x Short MicroStrategy",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="SPOT",
        name="Spotify Technology (Audio & AI Monetization)",
        category="Mega-Cap Tech",
        long_3x_ticker="3LPOm_EQ",
        long_3x_name="GraniteShares 3x Long Spotify",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="PYPL",
        name="PayPal Holdings (Global Fintech & Payments)",
        category="Crypto & Fintech",
        long_3x_ticker="3LPPm_EQ",
        long_3x_name="GraniteShares 3x Long PayPal",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="MRNA",
        name="Moderna (mRNA Therapeutics & Vaccines)",
        category="Healthcare",
        long_3x_ticker="3LMOp_EQ",
        long_3x_name="GraniteShares 3x Long Moderna",
        short_3x_ticker="3SMOp_EQ",
        short_3x_name="GraniteShares 3x Short Moderna",
        currency="EUR",
    ),
    # ── Macro Sector SPDRs & Thematic ETFs (EUR First Preference) ──────────────
    LeveragedPair(
        underlying_symbol="XLF",
        name="Financial Select Sector",
        category="Sector ETF",
        long_3x_ticker="3XFEl_EQ",
        long_3x_name="Leverage Shares 3x Long Financials",
        short_3x_ticker="SXFEl_EQ",
        short_3x_name="Leverage Shares -3x Short Financials",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="BP",
        name="BP (Integrated Global Energy & LNG)",
        category="Commodity",
        long_3x_ticker="3BPEl_EQ",
        long_3x_name="Leverage Shares 3x BP",
        short_3x_ticker="3SBPl_EQ",
        short_3x_name="GraniteShares 3x Short BP",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="UNH",
        name="UnitedHealth Group (Healthcare Leader)",
        category="Healthcare",
        long_3x_ticker="3UNHl_EQ",
        long_3x_name="Leverage Shares 3x Long UnitedHealth UNH (Acc)",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="GBX",
    ),
    LeveragedPair(
        underlying_symbol="BAE",
        name="BAE Systems (Aerospace & European Defense)",
        category="Space & Aerospace",
        long_3x_ticker="3LBA1l_EQ",
        long_3x_name="GraniteShares 3x Long BAE Systems",
        short_3x_ticker="3SBAl_EQ",
        short_3x_name="GraniteShares 3x Short BAE Systems",
        currency="GBX",
    ),
    LeveragedPair(
        underlying_symbol="SMH",
        name="VanEck Semiconductor ETF",
        category="Sector ETF",
        long_3x_ticker="SM3El_EQ",
        long_3x_name="Leverage Shares 3x Long Semiconductors",
        short_3x_ticker="3SMSl_EQ",
        short_3x_name="Leverage Shares -3x Short Semiconductors ETP",
        currency="EUR",
    ),
    # ── Worldwide Blue-Chips & European Champions ────────────────────────────
    LeveragedPair(
        underlying_symbol="RR",
        name="Rolls-Royce Holdings (Civil & Defense Aerospace Engines)",
        category="Space & Aerospace",
        long_3x_ticker="3LRRl_EQ",
        long_3x_name="GraniteShares 3x Long Rolls-Royce",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="GBX",
    ),
    LeveragedPair(
        underlying_symbol="SHEL",
        name="Shell plc (Global Energy & LNG)",
        category="Commodity",
        long_3x_ticker="3LRDl_EQ",
        long_3x_name="GraniteShares 3x Long Royal Dutch Shell",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="GBX",
    ),
    LeveragedPair(
        underlying_symbol="AZN",
        name="AstraZeneca (Global Biopharma & Oncology)",
        category="Healthcare",
        long_3x_ticker="3LAZl_EQ",
        long_3x_name="GraniteShares 3x Long AstraZeneca",
        short_3x_ticker="3SAZl_EQ",
        short_3x_name="GraniteShares 3x Short AstraZeneca",
        currency="GBX",
    ),
    # ── Currency ETPs (Spot FX Proxy on Invest) ──────────────────────────────
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
    # ── Quantum Computing Frontier Theme (EUR First Preference) ──────────────
    LeveragedPair(
        underlying_symbol="IONQ",
        name="IonQ (Trapped-Ion Quantum Computing)",
        category="Quantum Computing",
        long_3x_ticker="ION3a_EQ",
        long_3x_name="Leverage Shares 3x Long IONQ (Acc)",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="RGTI",
        name="Rigetti Computing (Superconducting Quantum)",
        category="Quantum Computing",
        long_3x_ticker="RGT3a_EQ",
        long_3x_name="Leverage Shares 3x Long Rigetti Computing RGTI (Acc)",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="IBM",
        name="IBM (Enterprise Quantum Heron & AI Infra)",
        category="Quantum Computing",
        long_3x_ticker="IBMd_EQ",
        long_3x_name="IBM",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="EUR",
    ),
    # ── Space & Aerospace Frontier Theme (EUR First Preference) ──────────────
    LeveragedPair(
        underlying_symbol="RKLB",
        name="Rocket Lab (Orbital Launch & Space Systems)",
        category="Space & Aerospace",
        long_3x_ticker="RKL3a_EQ",
        long_3x_name="Leverage Shares 3x Long Rocket Lab RKLB (Acc)",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="LUNR",
        name="Global X Space Tech & Satellite ETF",
        category="Space & Aerospace",
        long_3x_ticker="LUNAd_EQ",
        long_3x_name="Global X Space Tech (Acc)",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="EUR",
    ),
    # ── Memory & High-Bandwidth Storage (EUR First Preference) ─────────────────
    LeveragedPair(
        underlying_symbol="MU",
        name="Micron Technology (HBM3e Memory Leader)",
        category="Memory & Storage",
        long_3x_ticker="2MUEl_EQ",
        long_3x_name="Leverage Shares 2x Micron Technology",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="WDC",
        name="Western Digital (Enterprise Flash & Storage)",
        category="Memory & Storage",
        long_3x_ticker="WDCd_EQ",
        long_3x_name="Western Digital",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="EUR",
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
        long_3x_ticker="3LPAp_EQ",
        long_3x_name="GraniteShares 3x Long Palantir",
        short_3x_ticker="3SPAp_EQ",
        short_3x_name="GraniteShares 3x Short Palantir",
        currency="EUR",
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
        long_3x_ticker="CBRSd_EQ",
        long_3x_name="First Trust Nasdaq Cybersecurity (Acc)",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="EUR",
    ),
    # ── Space Economy & Private Launch Leaders (EUR First Preference) ──────────
    LeveragedPair(
        underlying_symbol="SPCX",
        name="SpaceX & Space Economy (Procure Space / Launch Infrastructure)",
        category="Space & Aerospace",
        long_3x_ticker="ELONm_EQ",
        long_3x_name="Leverage Shares 3x Long SpaceX (Acc)",
        short_3x_ticker="S3PXd_EQ",
        short_3x_name="Leverage Shares 3x Short SpaceX (Acc)",
        currency="EUR",
    ),
    # ── AI Server Infrastructure & Enterprise Accelerated Hardware ─────────
    LeveragedPair(
        underlying_symbol="SMCI",
        name="Super Micro Computer (Enterprise AI Servers & Liquid Cooling)",
        category="AI Infrastructure",
        long_3x_ticker="SMCIm_EQ",
        long_3x_name="Leverage Shares 2x Long Super Micro Computer",
        short_3x_ticker="SMC2l_EQ",
        short_3x_name="Leverage Shares 2x Long Super Micro Computer (GBX)",
        currency="EUR",
    ),
    # ── Mega-Cap Bluechips & High-Beta Earnings Titans (EUR First Preference) ─
    LeveragedPair(
        underlying_symbol="META",
        name="Meta Platforms (Llama AI & Global Social Infrastructure)",
        category="Mega-Cap Tech",
        long_3x_ticker="3LFEl_EQ",
        long_3x_name="GraniteShares 3x Long Meta Platforms",
        short_3x_ticker="3SFEl_EQ",
        short_3x_name="GraniteShares 3x Short Meta Platforms",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="NFLX",
        name="Netflix (Streaming Entertainment & Ad-Tier Monetization)",
        category="Mega-Cap Tech",
        long_3x_ticker="3NFEl_EQ",
        long_3x_name="Leverage Shares 3x Netflix",
        short_3x_ticker="3SNEl_EQ",
        short_3x_name="GraniteShares 3x Short Netflix",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="COIN",
        name="Coinbase Global (Crypto Infrastructure & Custody)",
        category="Crypto & Fintech",
        long_3x_ticker="3LCOm_EQ",
        long_3x_name="GraniteShares 3x Long Coinbase",
        short_3x_ticker="S3CO1l_EQ",
        short_3x_name="Leverage Shares -3x Short Coinbase",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="ORCL",
        name="Oracle (AI OCI Cloud Infrastructure & Multi-Cloud DB)",
        category="AI Infrastructure",
        long_3x_ticker="ORC3a_EQ",
        long_3x_name="Leverage Shares 3x Long Oracle ORCL (Acc)",
        short_3x_ticker="3ORCl_EQ",
        short_3x_name="Leverage Shares 3x Long Oracle ORCL (GBX)",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="INTC",
        name="Intel (18A Foundry & x86 Enterprise Silicon)",
        category="Semiconductors",
        long_3x_ticker="3INTl_EQ",
        long_3x_name="Leverage Shares 3x Long Intel (Acc)",
        short_3x_ticker="INTSl_EQ",
        short_3x_name="Leverage Shares -3x Short Intel INTC (Acc)",
        currency="GBX",
    ),
    LeveragedPair(
        underlying_symbol="QCOM",
        name="Qualcomm (Oryon NPU & Snapdragon Edge AI)",
        category="Semiconductors",
        long_3x_ticker="QCO3a_EQ",
        long_3x_name="Leverage Shares 3x Long QUALCOMM QCOM (Acc)",
        short_3x_ticker="3QCOl_EQ",
        short_3x_name="Leverage Shares 3x Long QUALCOMM QCOM (GBX)",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="BA",
        name="Boeing (Commercial Aviation & Defense Aerospace)",
        category="Space & Aerospace",
        long_3x_ticker="3BAEl_EQ",
        long_3x_name="Leverage Shares 3x Boeing",
        short_3x_ticker="BAESl_EQ",
        short_3x_name="Leverage Shares -1x Short Boeing",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="UBER",
        name="Uber Technologies (Autonomous Mobility & Logistics)",
        category="Mega-Cap Tech",
        long_3x_ticker="3LUEl_EQ",
        long_3x_name="GraniteShares 3x Long Uber",
        short_3x_ticker="SUBEl_EQ",
        short_3x_name="Leverage Shares -1x Uber",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="BABA",
        name="Alibaba Group (Cloud Intelligence & Global E-Commerce)",
        category="Global Tech",
        long_3x_ticker="3BBEl_EQ",
        long_3x_name="Leverage Shares 3x Alibaba",
        short_3x_ticker="3SBEl_EQ",
        short_3x_name="Leverage Shares -3x Short Alibaba BABA",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="CRM",
        name="Salesforce (Agentforce Autonomous Enterprise Agents)",
        category="Mega-Cap Tech",
        long_3x_ticker="3CREl_EQ",
        long_3x_name="Leverage Shares 3x Salesforce.Com",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="DFNS",
        name="European Aerospace & Defence 3x Basket",
        category="Space & Aerospace",
        long_3x_ticker="3EDFl_EQ",
        long_3x_name="WisdomTree STOXX Europe Aerospace & Defence 3x Daily Leveraged (Acc)",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="EUR",
    ),
    # ── High-Conviction Tech, Energy & Global Leaders Expansion ─────────
    LeveragedPair(
        underlying_symbol="ARKK",
        name="ARK Innovation",
        category="Thematic",
        long_3x_ticker="3AREl_EQ",
        long_3x_name="Leverage Shares 3x Long ARK Innovation",
        short_3x_ticker="SAKEl_EQ",
        short_3x_name="Leverage Shares -3x Short ARK Innovation",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="MARA",
        name="Marathon Digital",
        category="Crypto & Blockchain",
        long_3x_ticker="MAR3a_EQ",
        long_3x_name="GraniteShares 3x Long MARA Daily (Acc)",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="KLAC",
        name="KLA Corp",
        category="Global Tech",
        long_3x_ticker="KLA3a_EQ",
        long_3x_name="Leverage Shares 3x Long KLAC (Acc)",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="OKLO",
        name="Oklo Inc",
        category="Nuclear & Clean Tech",
        long_3x_ticker="OKL3a_EQ",
        long_3x_name="Leverage Shares 3x Long Oklo (Acc)",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="CRWV",
        name="CoreWeave",
        category="AI & Infrastructure",
        long_3x_ticker="CRW3a_EQ",
        long_3x_name="Leverage Shares 3x Long CoreWeave CRWV (Acc)",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="SMR",
        name="NuScale Power",
        category="Nuclear & Clean Tech",
        long_3x_ticker="SMR3a_EQ",
        long_3x_name="Leverage Shares 3x Long NuScale Power SMR (Acc)",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="NBIS",
        name="Nebius Group",
        category="AI & Infrastructure",
        long_3x_ticker="NBI3a_EQ",
        long_3x_name="Leverage Shares 3x Long Nebius NBIS (Acc)",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="QBTS",
        name="D-Wave Quantum",
        category="Quantum Computing",
        long_3x_ticker="QBT3a_EQ",
        long_3x_name="Leverage Shares 3x Long D-Wave Quantum QBTS (Acc)",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="005930.KS",
        name="Samsung Electronics",
        category="Global Tech",
        long_3x_ticker="SME3d_EQ",
        long_3x_name="Leverage Shares 3x Long Samsung Electronics (Acc)",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="000660.KS",
        name="SK Hynix",
        category="Global Tech",
        long_3x_ticker="3HNXd_EQ",
        long_3x_name="Leverage Shares 3x Long SK Hynix (Acc)",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="RACE",
        name="Ferrari",
        category="Consumer & Luxury",
        long_3x_ticker="3RACd_EQ",
        long_3x_name="Leverage Shares 3x Long Ferrari",
        short_3x_ticker="RAS3d_EQ",
        short_3x_name="Leverage Shares -3x Short Ferrari",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="NIO",
        name="NIO Inc",
        category="EV & Clean Tech",
        long_3x_ticker="3NIEl_EQ",
        long_3x_name="Leverage Shares 3x Long NIO",
        short_3x_ticker="3SIEl_EQ",
        short_3x_name="GraniteShares 3x Short NIO",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="SQ",
        name="Block (Square)",
        category="Fintech & Payments",
        long_3x_ticker="3SQEl_EQ",
        long_3x_name="Leverage Shares 3x Square",
        short_3x_ticker="SSQEl_EQ",
        short_3x_name="Leverage Shares -1x Short Square",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="DIS",
        name="Walt Disney",
        category="Media & Entertainment",
        long_3x_ticker="3DIEl_EQ",
        long_3x_name="Leverage Shares 3x Disney",
        short_3x_ticker="SDIEl_EQ",
        short_3x_name="Leverage Shares -1x Short Disney",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="ABNB",
        name="Airbnb",
        category="Consumer & Travel",
        long_3x_ticker="3ABEl_EQ",
        long_3x_name="Leverage Shares 3x AirBNB",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="BIDU",
        name="Baidu",
        category="China Tech",
        long_3x_ticker="3BIEl_EQ",
        long_3x_name="Leverage Shares 3x Baidu",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="XOM",
        name="Exxon Mobil",
        category="Energy & Materials",
        long_3x_ticker="3XOMl_EQ",
        long_3x_name="Leverage Shares 3x Long Exxon",
        short_3x_ticker="XO3Sd_EQ",
        short_3x_name="Leverage Shares -3x Short Exxon",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="HOOD",
        name="Robinhood Markets",
        category="Fintech & Crypto",
        long_3x_ticker="HOD3l_EQ",
        long_3x_name="Leverage Shares 3x Long Robinhood HOOD (Acc)",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="USD",
    ),
    LeveragedPair(
        underlying_symbol="LLY",
        name="Eli Lilly",
        category="Healthcare & Pharma",
        long_3x_ticker="LLY3l_EQ",
        long_3x_name="Leverage Shares 3x Long Eli Lilly (Acc)",
        short_3x_ticker="LLYSl_EQ",
        short_3x_name="Leverage Shares -3x Short Eli Lilly LLY (Acc)",
        currency="USD",
    ),
    LeveragedPair(
        underlying_symbol="RHM.DE",
        name="Rheinmetall",
        category="Space & Aerospace",
        long_3x_ticker="RHM3l_EQ",
        long_3x_name="Leverage Shares 3x Long Rheinmetall RHM Securities (Acc)",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="USD",
    ),
    LeveragedPair(
        underlying_symbol="RIO.L",
        name="Rio Tinto",
        category="Materials & Mining",
        long_3x_ticker="3LRIl_EQ",
        long_3x_name="GraniteShares 3x Long Rio Tinto",
        short_3x_ticker="3SRIl_EQ",
        short_3x_name="GraniteShares 3x Short Rio Tinto",
        currency="GBP",
    ),
    LeveragedPair(
        underlying_symbol="BARC.L",
        name="Barclays",
        category="European Financials",
        long_3x_ticker="3LBC1l_EQ",
        long_3x_name="GraniteShares 3x Long Barclays",
        short_3x_ticker="3SBCl_EQ",
        short_3x_name="GraniteShares 3x Short Barclays",
        currency="GBP",
    ),
    LeveragedPair(
        underlying_symbol="DGE.L",
        name="Diageo",
        category="Consumer Defensive",
        long_3x_ticker="3LDOl_EQ",
        long_3x_name="GraniteShares 3x Long Diageo",
        short_3x_ticker="3SDOl_EQ",
        short_3x_name="GraniteShares 3x Short Diageo",
        currency="GBP",
    ),
    LeveragedPair(
        underlying_symbol="VOD.L",
        name="Vodafone",
        category="Telecom & Tech",
        long_3x_ticker="3LVOl_EQ",
        long_3x_name="GraniteShares 3x Long Vodafone",
        short_3x_ticker="3SVO1l_EQ",
        short_3x_name="GraniteShares 3x Short Vodafone",
        currency="GBP",
    ),
    LeveragedPair(
        underlying_symbol="BTP",
        name="Italy 10Y BTP",
        category="Fixed Income",
        long_3x_ticker="3BTLm_EQ",
        long_3x_name="WisdomTree BTP 10Y 3x Daily Leveraged",
        short_3x_ticker="3BTSm_EQ",
        short_3x_name="WisdomTree BTP 10Y 3x Daily Short",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="GDX",
        name="VanEck Gold Miners",
        category="Commodity Equity",
        long_3x_ticker="3GDEl_EQ",
        long_3x_name="Leverage Shares 3x Long Gold Miners",
        short_3x_ticker="SGDEl_EQ",
        short_3x_name="Leverage Shares -3x Short Gold Miners",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="URA",
        name="Global X Uranium",
        category="Clean Energy & Nuclear",
        long_3x_ticker="URA3a_EQ",
        long_3x_name="Leverage Shares 3x Long Uranium (Acc)",
        short_3x_ticker="URASa_EQ",
        short_3x_name="Leverage Shares -3x Short Uranium (Acc)",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="SILV",
        name="Silver Miners",
        category="Commodity Equity",
        long_3x_ticker="SLM3a_EQ",
        long_3x_name="Leverage Shares 3x Long Silver Miners (Acc)",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="BLC",
        name="Blockchain Leaders",
        category="Crypto & Blockchain",
        long_3x_ticker="BLC3a_EQ",
        long_3x_name="Leverage Shares 3x Long Blockchain Leaders (Acc)",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="KWEB",
        name="China Internet & Tech",
        category="China Tech",
        long_3x_ticker="3KWEl_EQ",
        long_3x_name="Leverage Shares 3x Long China Tech",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="EUR",
    ),
    # ── Earnings-Season Blue-Chip & Thematic Radar (EUR First Preference) ────
    # Pre-positioned for high-beta earnings & macro momentum catalysts.
    # 100% verified tradeable EUR-denominated leveraged ETPs on Trading 212.
    LeveragedPair(
        underlying_symbol="GS",
        name="Goldman Sachs (Global Investment Banking & Capital Markets)",
        category="Financials",
        long_3x_ticker="GS2El_EQ",
        long_3x_name="Leverage Shares 2x Goldman Sachs",
        short_3x_ticker="SGSEl_EQ",
        short_3x_name="Leverage Shares -1x Short Goldman Sachs",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="V",
        name="Visa (Global Payments & Transaction Tech)",
        category="Financials",
        long_3x_ticker="VISEl_EQ",
        long_3x_name="Leverage Shares 2x Visa",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="BRK-B",
        name="Berkshire Hathaway (Warren Buffett Conglomerate)",
        category="Financials",
        long_3x_ticker="2BREl_EQ",
        long_3x_name="Leverage Shares 2x Long Berkshire Hathaway BRK-B",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="ADI",
        name="Analog Devices (High-Performance Mixed-Signal Semis)",
        category="Tech Equity",
        long_3x_ticker="ADI3a_EQ",
        long_3x_name="Leverage Shares 3x Long Analog Devices ADI (Acc)",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="JOBY",
        name="Joby Aviation (Electric Aerial Mobility & Air Taxi)",
        category="Space & Aerospace",
        long_3x_ticker="JOB3a_EQ",
        long_3x_name="Leverage Shares 3x Long Joby Aviation JOBY (Acc)",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="AMAT",
        name="Cerebras Systems (Next-Gen Wafer-Scale AI Chips Benchmark)",
        category="AI Infrastructure",
        long_3x_ticker="3CBRd_EQ",
        long_3x_name="Leverage Shares 3x Long Cerebras Systems CBRS (Acc)",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="SOFI",
        name="Circle Financial (USDC Digital Currency Infrastructure Benchmark)",
        category="Crypto & Fintech",
        long_3x_ticker="CRC3a_EQ",
        long_3x_name="Leverage Shares 3x Long Circle CRCL (Acc)",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="BMNR",
        name="Bitmine Immersion (Immersion Liquid-Cooling AI/Crypto)",
        category="Crypto & Fintech",
        long_3x_ticker="BMN3a_EQ",
        long_3x_name="Leverage Shares 3x Long Bitmine Immersion BMNR (Acc)",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="IREN",
        name="IREN (Next-Gen Renewable Data Centers & AI Cloud)",
        category="AI Infrastructure",
        long_3x_ticker="IRN3a_EQ",
        long_3x_name="Leverage Shares 3x Long IREN (Acc)",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="STX",
        name="Memory DRAM (Next-Gen HBM High-Bandwidth Storage Benchmark)",
        category="Memory & Storage",
        long_3x_ticker="DRM3d_EQ",
        long_3x_name="Leverage Shares 3x Long Memory DRAM (Acc)",
        short_3x_ticker="DRMSd_EQ",
        short_3x_name="Leverage Shares -3x Short Memory DRAM (Acc)",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="TTE",
        name="European Energy Champions (TotalEnergies & Oil Producers)",
        category="Commodity",
        long_3x_ticker="3XEEl_EQ",
        long_3x_name="Leverage Shares 3x Long Oil & Gas",
        short_3x_ticker="3SEEl_EQ",
        short_3x_name="Leverage Shares -3x Short Oil & Gas",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="ADBE",
        name="FAANG Basket (Mega-Cap US Tech Leaders Benchmark)",
        category="Tech Basket",
        long_3x_ticker="3FNEl_EQ",
        long_3x_name="GraniteShares 3x Long FAANG",
        short_3x_ticker="SFNEl_EQ",
        short_3x_name="GraniteShares 1x Short FAANG",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="VT",
        name="Total World Equities (Vanguard World Stock Index)",
        category="Global Index",
        long_3x_ticker="3VTEl_EQ",
        long_3x_name="Leverage Shares 3x Long Total World",
        short_3x_ticker="None",
        short_3x_name="N/A",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="EWT",
        name="Taiwan Equities (iShares MSCI Taiwan Index)",
        category="Global Index",
        long_3x_ticker="3TAEl_EQ",
        long_3x_name="Leverage Shares 3x Long Taiwan",
        short_3x_ticker="STAEl_EQ",
        short_3x_name="Leverage Shares -3x Short Taiwan",
        currency="EUR",
    ),
    LeveragedPair(
        underlying_symbol="TXN",
        name="Magnificent 7 Equal-Weighted (US Tech Leaders Benchmark)",
        category="Tech Basket",
        long_3x_ticker="M73Ld_EQ",
        long_3x_name="WisdomTree Magnificent 7 3x Daily Leveraged (Acc)",
        short_3x_ticker="M73Sd_EQ",
        short_3x_name="WisdomTree Magnificent 7 3x Daily Short (Acc)",
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
    "MERCEDES": "MBG",
    "MERCEDES-BENZ": "MBG",
    "MERCEDES_BENZ": "MBG",
    "DAI": "MBG",
    "SAP": "MBG",
    "SPOTIFY": "SPOT",
    "PAYPAL": "PYPL",
    "MODERNA": "MRNA",
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
    "SHELL": "SHEL",
    "ASTRA": "AZN",
    "ASTRAZENECA": "AZN",
    "ROLLS": "RR",
    "ROLLS-ROYCE": "RR",
    "ROLLS_ROYCE": "RR",
    "MSTR": "MSTR",
    "MICROSTRATEGY": "MSTR",
    "HEALTH": "UNH",
    "UNITEDHEALTH": "UNH",
    "ENERGY": "BP",
    "BRITISH_PETROLEUM": "BP",
    "BAESYSTEMS": "BAE",
    "CHINA": "FXI",
    "HANGSENG": "FXI",
    "^HSI": "FXI",
    "HSI": "FXI",
    "GBPUSD": "GBPUSD=X",
    "MICRON": "MU",
    "BROADCOM": "AVGO",
    "PALANTIR": "PLTR",
    "ROCKETLAB": "RKLB",
    "RIGETTI": "RGTI",
    "SPACE": "SPCX",
    "SPACEX": "SPCX",
    "SMC": "SMCI",
    "SUPERMICRO": "SMCI",
    "SUPER_MICRO": "SMCI",
    "META": "META",
    "FACEBOOK": "META",
    "NETFLIX": "NFLX",
    "COINBASE": "COIN",
    "ORACLE": "ORCL",
    "INTEL": "INTC",
    "QUALCOMM": "QCOM",
    "BOEING": "BA",
    "UBER": "UBER",
    "ALIBABA": "BABA",
    "SALESFORCE": "CRM",
    "DEFENCE": "DFNS",
    "DEFENSE": "DFNS",
    "AEROSPACE": "DFNS",
    "CYBER": "CIBR",
    "CYBERSECURITY": "CIBR",
    "ARK": "ARKK",
    "ARK_INNOVATION": "ARKK",
    "MARATHON": "MARA",
    "MARATHON_DIGITAL": "MARA",
    "KLA": "KLAC",
    "KLATENCOR": "KLAC",
    "OKLO": "OKLO",
    "COREWEAVE": "CRWV",
    "NUSCALE": "SMR",
    "NUSCALE_POWER": "SMR",
    "NEBIUS": "NBIS",
    "DWAVE": "QBTS",
    "D-WAVE": "QBTS",
    "SAMSUNG": "005930.KS",
    "HYNIX": "000660.KS",
    "SKHYNIX": "000660.KS",
    "FERRARI": "RACE",
    "SQUARE": "SQ",
    "BLOCK": "SQ",
    "DISNEY": "DIS",
    "AIRBNB": "ABNB",
    "BAIDU": "BIDU",
    "EXXON": "XOM",
    "EXXONMOBIL": "XOM",
    "ROBINHOOD": "HOOD",
    "LILLY": "LLY",
    "ELI_LILLY": "LLY",
    "RHEINMETALL": "RHM.DE",
    "RHM": "RHM.DE",
    "RIOTINTO": "RIO.L",
    "RIO": "RIO.L",
    "BARCLAYS": "BARC.L",
    "BARC": "BARC.L",
    "DIAGEO": "DGE.L",
    "DGE": "DGE.L",
    "VODAFONE": "VOD.L",
    "VOD": "VOD.L",
    "GOLDMINERS": "GDX",
    "URANIUM": "URA",
    "SILVERMINERS": "SILV",
    "BLOCKCHAIN": "BLC",
    "CHINATECH": "KWEB",
    # Earnings-season blue-chip & thematic aliases
    "GOLDMAN": "GS",
    "GOLDMANSACHS": "GS",
    "VISA": "V",
    "BERKSHIRE": "BRK-B",
    "BRKB": "BRK-B",
    "WARREN": "BRK-B",
    "ANALOG": "ADI",
    "ANALOGDEVICES": "ADI",
    "JOBY": "JOBY",
    "JOBYAVIATION": "JOBY",
    "CEREBRAS": "CBRS",
    "CIRCLE": "CRCL",
    "BITMINE": "BMNR",
    "IREN": "IREN",
    "DRAM": "DRM",
    "MEMORYDRAM": "DRM",
    "OILEUR": "XEE",
    "FAANG": "FAANG",
    "WORLD": "VT",
    "TAIWAN": "EWT",
    "MAG7": "M7",
}

# Cross-currency fallback and historical execution ticker lookup map
# Maps underlying symbol -> {currency: (long_ticker, short_ticker)}
CURRENCY_ALTERNATIVES: Dict[str, Dict[str, Tuple[Optional[str], Optional[str]]]] = {
    "QQQ": {"EUR": ("QQQ3m_EQ", "QQQSm_EQ"), "USD": ("QQQ3l_EQ", "QQQSl_EQ")},
    "SPY": {"EUR": ("3USLm_EQ", "3USSm_EQ"), "USD": ("3USLl_EQ", "3USSl_EQ")},
    "NVDA": {"EUR": ("3LVEl_EQ", "3SVEl_EQ"), "USD": ("3LNVl_EQ", "3SNVl_EQ")},
    "TSLA": {"EUR": ("3LTEl_EQ", "3STEl_EQ"), "USD": ("3LTSl_EQ", "3STSl_EQ")},
    "AAPL": {"EUR": ("3APEl_EQ", "SALEl_EQ"), "USD": ("AAP3l_EQ", "AAPSl_EQ")},
    "MSFT": {"EUR": ("3MSEl_EQ", "3SMTl_EQ"), "USD": ("MSF3l_EQ", "MS3Sl_EQ")},
    "AMZN": {"EUR": ("3LPEl_EQ", "3SPEl_EQ"), "USD": ("3LZPl_EQ", "3SZNl_EQ")},
    "GOOGL": {"EUR": ("3LGEl_EQ", "3SGEl_EQ"), "USD": ("3LALl_EQ", "GGESl_EQ")},
    "BTC-USD": {"EUR": ("BTC3EURs_EQ", "BTC3SEURs_EQ"), "USD": ("BTC3s_EQ", "BTC3Ss_EQ")},
    "ETH-USD": {"EUR": ("ETH3EURs_EQ", "ETH3SEURs_EQ"), "USD": ("ETH3s_EQ", "ETH3Ss_EQ")},
    "HG=F": {"EUR": ("3HCLm_EQ", "3HCSm_EQ"), "USD": ("3HCLl_EQ", "3HCSl_EQ")},
    "BNO": {"EUR": ("3BRLm_EQ", "3BRSm_EQ"), "USD": ("3BRLl_EQ", "3BSRl_EQ")},
    "UNG": {"EUR": ("3NGLm_EQ", "3NGSm_EQ"), "USD": ("3NGLl_EQ", "3NGSl_EQ")},
    "MSTR": {"EUR": ("3LMIm_EQ", "3SMIm_EQ"), "GBX": ("LMI3l_EQ", "SMI3l_EQ")},
    "XLF": {"EUR": ("3XFEl_EQ", "SXFEl_EQ"), "USD": ("3XLFl_EQ", "3SXLl_EQ")},
    "PLTR": {"EUR": ("3LPAp_EQ", "3SPAp_EQ"), "USD": ("3LPAl_EQ", "3SPAl_EQ")},
    "SPCX": {"EUR": ("ELONm_EQ", "S3PXd_EQ"), "USD": ("ELONl_EQ", "3SPSl_EQ")},
    "META": {"EUR": ("3LFEl_EQ", "3SFEl_EQ"), "USD": ("3LFEl_EQ", "3SFPl_EQ")},
    "NFLX": {"EUR": ("3NFEl_EQ", "3SNEl_EQ"), "USD": ("NFL3l_EQ", "3SNFl_EQ")},
    "COIN": {"EUR": ("3LCOm_EQ", "S3CO1l_EQ"), "USD": ("3LCOOl_EQ", "S3CO1l_EQ")},
    "BA": {"EUR": ("3BAEl_EQ", "BAESl_EQ"), "USD": ("BA3l_EQ", "BASl_EQ")},
    "UBER": {"EUR": ("3LUEl_EQ", "SUBEl_EQ"), "USD": ("3LUBl_EQ", "SUBEl_EQ")},
    "BABA": {"EUR": ("3BBEl_EQ", "3SBEl_EQ"), "USD": ("3BABl_EQ", "BA3Sl_EQ")},
    "TSM": {"EUR": ("TS3El_EQ", "STS3l_EQ"), "USD": ("3TSMl_EQ", "STSMl_EQ")},
    "SOXX": {"EUR": ("3SEMd_EQ", "None"), "USD": ("3SEMl_EQ", "None")},
    "SMCI": {"EUR": ("SMCIm_EQ", "None"), "USD": ("SMCIl_EQ", "None")},
    "ORCL": {"EUR": ("ORC3a_EQ", "None"), "USD": ("ORC3l_EQ", "None")},
    "QCOM": {"EUR": ("QCO3a_EQ", "None"), "USD": ("QCO3l_EQ", "None")},
    "CRM": {"EUR": ("3CREl_EQ", "None"), "USD": ("CRM3l_EQ", "None")},
    "DFNS": {"EUR": ("3EDFl_EQ", "None")},
    "AMD": {"EUR": ("3ADEl_EQ", "3SAMl_EQ"), "USD": ("AMD3l_EQ", "3SAMl_EQ")},
    "SLV": {"EUR": ("3SLEl_EQ", "SSLEl_EQ"), "USD": ("3SLVl_EQ", "None")},
    "USO": {"EUR": ("3OILm_EQ", "3OISm_EQ"), "USD": ("WTI3l_EQ", "3OISl_EQ")},
    "GLD": {"EUR": ("3GLEl_EQ", "SGDEl_EQ"), "USD": ("3GLDl_EQ", "None")},
    "WEAT": {"EUR": ("3WHLm_EQ", "None")},
    "COFF": {"EUR": ("3CFLm_EQ", "None")},
    "URNM": {"EUR": ("U3O8d_EQ", "None")},
    "TLT": {"EUR": ("3TYLm_EQ", "None")},
    "BUND": {"EUR": ("3BULm_EQ", "None")},
    "EWG": {"EUR": ("3DELl_EQ", "3DESl_EQ")},
    "EWQ": {"EUR": ("3LVd_EQ", "3DECd_EQ")},
    "EWI": {"EUR": ("5ITLm_EQ", "5ITSm_EQ")},
    "INDA": {"EUR": ("3INDd_EQ", "None")},
    "EWY": {"EUR": ("3SKEl_EQ", "None")},
    "IONQ": {"EUR": ("ION3a_EQ", "None"), "USD": ("IONQl1_EQ", "None")},
    "RGTI": {"EUR": ("RGT3a_EQ", "None")},
    "IBM": {"EUR": ("IBMd_EQ", "None"), "USD": ("IBM_US_EQ", "None")},
    "RKLB": {"EUR": ("RKL3a_EQ", "None")},
    "LUNR": {"EUR": ("LUNAd_EQ", "None")},
    "MU": {"EUR": ("2MUEl_EQ", "None")},
    "WDC": {"EUR": ("WDCd_EQ", "None"), "USD": ("WDC_US_EQ", "None")},
    "CIBR": {"EUR": ("CBRSd_EQ", "None")},
    "BP": {"EUR": ("3BPEl_EQ", "None")},
    "UNH": {"EUR": ("3UNHl_EQ", "None")},
    "ARKK": {"EUR": ("3AREl_EQ", "SAKEl_EQ"), "USD": ("ARK3l_EQ", "SARKl_EQ"), "GBX": ("3ARKl_EQ", "ARKSl_EQ")},
    "MARA": {"EUR": ("MAR3a_EQ", "None"), "USD": ("MAR3l_EQ", "None"), "GBX": ("MARPl_EQ", "None")},
    "KLAC": {"EUR": ("KLA3a_EQ", "None"), "USD": ("KLA3l_EQ", "None"), "GBX": ("3KLAl_EQ", "None")},
    "OKLO": {"EUR": ("OKL3a_EQ", "None"), "USD": ("OKL3l_EQ", "None"), "GBX": ("3OKLl_EQ", "None")},
    "CRWV": {"EUR": ("CRW3a_EQ", "None"), "USD": ("CRW3l_EQ", "None"), "GBX": ("3CRWl_EQ", "None")},
    "SMR": {"EUR": ("SMR3a_EQ", "None"), "USD": ("SMR3l_EQ", "None"), "GBX": ("3SMRl_EQ", "None")},
    "NBIS": {"EUR": ("NBI3a_EQ", "None"), "USD": ("NBI3l_EQ", "None"), "GBX": ("3NBIl_EQ", "None")},
    "QBTS": {"EUR": ("QBT3a_EQ", "None"), "USD": ("QBT3l_EQ", "None"), "GBX": ("3QBTl_EQ", "None")},
    "005930.KS": {"EUR": ("SME3d_EQ", "None"), "USD": ("SMG3l_EQ", "None"), "GBX": ("3SMGl_EQ", "None")},
    "000660.KS": {"EUR": ("3HNXd_EQ", "None"), "USD": ("HNX3l_EQ", "None"), "GBX": ("3HNXl_EQ", "None")},
    "RACE": {"EUR": ("3RACd_EQ", "RAS3d_EQ"), "USD": ("RAC3l_EQ", "RA3Sl_EQ"), "GBX": ("3RACl_EQ", "3SRAl_EQ")},
    "NIO": {"EUR": ("3NIEl_EQ", "3SIEl_EQ"), "USD": ("NIO3l_EQ", "3SNIl_EQ"), "GBX": ("3NIOl_EQ", "3SIPl_EQ")},
    "SQ": {"EUR": ("3SQEl_EQ", "SSQEl_EQ"), "USD": ("3LSQm_EQ", "None"), "GBX": ("SSQ3l_EQ", "None")},
    "DIS": {"EUR": ("3DIEl_EQ", "SDIEl_EQ"), "USD": ("DIS3l_EQ", "DISSl_EQ"), "GBX": ("3DISl_EQ", "SDISl_EQ")},
    "ABNB": {"EUR": ("3ABEl_EQ", "None"), "USD": ("ABN3l_EQ", "None"), "GBX": ("3ABNl_EQ", "None")},
    "BIDU": {"EUR": ("3BIEl_EQ", "None"), "USD": ("BID3l_EQ", "None"), "GBX": ("3BIDl_EQ", "None")},
    "XOM": {"EUR": ("3XOMl_EQ", "XO3Sd_EQ"), "USD": ("XOM3l_EQ", "None"), "GBX": ("3XOMl_EQ", "None")},
    "HOOD": {"USD": ("HOD3l_EQ", "None"), "GBX": ("3HODl_EQ", "None")},
    "LLY": {"USD": ("LLY3l_EQ", "LLYSl_EQ"), "GBX": ("3LLYl_EQ", "SLLYl_EQ")},
    "RHM.DE": {"USD": ("RHM3l_EQ", "None"), "GBX": ("3RHMl_EQ", "None")},
    "RIO.L": {"GBX": ("3LRIl_EQ", "3SRIl_EQ")},
    "BARC.L": {"GBX": ("3LBC1l_EQ", "3SBCl_EQ")},
    "DGE.L": {"GBX": ("3LDOl_EQ", "3SDOl_EQ")},
    "VOD.L": {"GBX": ("3LVOl_EQ", "3SVO1l_EQ")},
    "BTP": {"EUR": ("3BTLm_EQ", "3BTSm_EQ")},
    "GDX": {"EUR": ("3GDEl_EQ", "SGDEl_EQ"), "USD": ("GDX3l_EQ", "GDMSl_EQ"), "GBX": ("3GDXl_EQ", "SGDXl_EQ")},
    "URA": {"EUR": ("URA3a_EQ", "URASa_EQ"), "USD": ("URA3l_EQ", "URASl_EQ"), "GBX": ("3URAl_EQ", "SURAl_EQ")},
    "SILV": {"EUR": ("SLM3a_EQ", "None"), "GBX": ("3SLMl_EQ", "SLMSl_EQ")},
    "BLC": {"EUR": ("BLC3a_EQ", "None"), "USD": ("BLC3l_EQ", "None"), "GBX": ("3BLCl_EQ", "None")},
    "KWEB": {"EUR": ("3KWEl_EQ", "None"), "USD": ("KWE3l_EQ", "None"), "GBX": ("3KWBl_EQ", "None")},
    # Earnings-season blue-chips & thematics
    "GS": {"EUR": ("GS2El_EQ", "SGSEl_EQ")},
    "V": {"EUR": ("VISEl_EQ", "None")},
    "BRK-B": {"EUR": ("2BREl_EQ", "None")},
    "ADI": {"EUR": ("ADI3a_EQ", "None")},
    "JOBY": {"EUR": ("JOB3a_EQ", "None")},
    "CBRS": {"EUR": ("3CBRd_EQ", "None")},
    "CRCL": {"EUR": ("CRC3a_EQ", "None")},
    "BMNR": {"EUR": ("BMN3a_EQ", "None")},
    "IREN": {"EUR": ("IRN3a_EQ", "None")},
    "DRM": {"EUR": ("DRM3d_EQ", "DRMSd_EQ")},
    "XEE": {"EUR": ("3XEEl_EQ", "3SEEl_EQ")},
    "FAANG": {"EUR": ("3FNEl_EQ", "SFNEl_EQ")},
    "VT": {"EUR": ("3VTEl_EQ", "None")},
    "EWT": {"EUR": ("3TAEl_EQ", "STAEl_EQ")},
    "M7": {"EUR": ("M73Ld_EQ", "M73Sd_EQ")},
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
            if p.short_5x_ticker and p.short_5x_ticker != "None":
                if p.short_3x_ticker and p.short_3x_ticker != "None":
                    self._5x_to_3x_map[p.short_5x_ticker] = p.short_3x_ticker
                else:
                    self._5x_to_3x_map[p.short_5x_ticker] = "None"

        # Additional institutional 5x mappings (supporting legacy USD/GBX 5x ticker lookups)
        legacy_5x = {
            "QQQ5l_EQ": "QQQ3m_EQ",
            "QQ3Sl_EQ": "QQQSm_EQ",
            "SP5Yl_EQ": "3USLm_EQ",
            "SPYSl_EQ": "3USSm_EQ",
            "SDAXl_EQ": "3DESl_EQ",
            "5SX5l_EQ": "3EULl_EQ",
            "GLD5l_EQ": "3GLEl_EQ",
            "TLT5d_EQ": "3TYLm_EQ",
            "TLT5l_EQ": "3TYLm_EQ",
            "TL5Sl_EQ": "3TYLm_EQ",
            "5STLd_EQ": "None",
            "MAG7l_EQ": "MAG7m_EQ",
        }
        for k, v in legacy_5x.items():
            self._5x_to_3x_map.setdefault(k, v)

        # Build bidirectional ETP -> LeveragedPair mapping (including alternatives)
        self._etp_map: Dict[str, LeveragedPair] = {}
        for p in self.pairs:
            for t in [p.long_3x_ticker, p.short_3x_ticker, p.long_5x_ticker, p.short_5x_ticker]:
                if t and t != "None":
                    self._etp_map[t.upper()] = p
        for sym, alt_dict in CURRENCY_ALTERNATIVES.items():
            pair = self._underlying_map.get(sym.upper())
            if pair:
                for curr, (l_tkr, s_tkr) in alt_dict.items():
                    if l_tkr:
                        self._etp_map.setdefault(l_tkr.upper(), pair)
                    if s_tkr:
                        self._etp_map.setdefault(s_tkr.upper(), pair)

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

        Multi-Tier Policy: If 5x exists on the asset, both [3, 5] multipliers are supported
        to enable simultaneous tiered allocation based on conviction and volatility.
        Otherwise, standard 3x is supported [3].
        """
        pair = self.get_pair_for_underlying(underlying)
        if not pair:
            return [1]
        return [3, 5] if pair.has_5x else [3]

    def get_execution_ticker(
        self,
        underlying: str,
        direction: str,
        leverage: Optional[int] = None,
        preferred_currency: str = "EUR",
    ) -> Optional[str]:
        """Given an underlying symbol, direction ('LONG' or 'SHORT'), and desired leverage (3 or 5),
        return the appropriate Trading 212 execution ticker prioritizing EUR as first preference.

        Multi-Tier Allocation:
        1. If leverage == 5 (or leverage is None on a 5x asset), return the 5x ETP.
        2. If leverage == 3, return the 3x ETP (even if 5x exists on the asset).
        3. If preferred_currency is specified (default 'EUR'), return that currency's ticker if available.
        """
        pair = self.get_pair_for_underlying(underlying)
        if not pair:
            return None

        direction_upper = direction.upper()
        is_long = direction_upper in ("LONG", "BUY", "BULLISH")
        is_short = direction_upper in ("SHORT", "SELL", "BEARISH")

        # 5x Resolution: only if explicitly enabled in configuration AND requested with leverage >= 5
        from trading_system.config import settings
        allow_5x = getattr(settings, "ENABLE_5X_LEVERAGE", False)
        if allow_5x and pair.has_5x and leverage is not None and leverage >= 5:
            if is_long and pair.long_5x_ticker:
                return pair.long_5x_ticker
            elif is_short and pair.short_5x_ticker and pair.short_5x_ticker != "None":
                return pair.short_5x_ticker

        # Check currency alternatives if non-default currency requested or to match preference
        sym = pair.underlying_symbol.upper()
        alt = CURRENCY_ALTERNATIVES.get(sym, {})
        if preferred_currency and preferred_currency.upper() in alt:
            l_alt, s_alt = alt[preferred_currency.upper()]
            if is_long and l_alt:
                return l_alt
            if is_short and s_alt and s_alt != "None":
                return s_alt

        # 3x Resolution (standard curated pair - already EUR preferred)
        if is_long:
            return pair.long_3x_ticker
        elif is_short:
            return pair.short_3x_ticker if pair.short_3x_ticker != "None" else None

        return None

    def get_5x_pairs(self) -> List[LeveragedPair]:
        """Return all pairs in the universe that support 5x leverage."""
        return [p for p in self.pairs if p.has_5x]

    def get_pair_for_etp(self, etp_ticker: str) -> Optional[LeveragedPair]:
        """Lookup LeveragedPair by any of its primary or alternate currency ETP execution tickers."""
        clean = (etp_ticker or "").strip().upper()
        return self._etp_map.get(clean)

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
        """Return list of all active Trading 212 execution tickers (respecting 5x leverage settings)."""
        from trading_system.config import settings
        allow_5x = getattr(settings, "ENABLE_5X_LEVERAGE", False)
        tickers = []
        for p in self.pairs:
            if allow_5x and p.has_5x:
                if p.long_5x_ticker:
                    tickers.append(p.long_5x_ticker)
                if p.short_5x_ticker and p.short_5x_ticker != "None":
                    tickers.append(p.short_5x_ticker)
            else:
                if p.long_3x_ticker:
                    tickers.append(p.long_3x_ticker)
                if p.short_3x_ticker and p.short_3x_ticker != "None":
                    tickers.append(p.short_3x_ticker)
        return list(dict.fromkeys(tickers))

    def get_ticker_currency(self, ticker: str) -> str:
        """Return authoritative quote currency for an execution ticker ('GBX', 'EUR', 'USD', 'GBP')."""
        return get_ticker_currency(ticker)

    def get_etp_direction(self, etp_ticker: str) -> str:
        """Authoritatively determine if an execution ETP ticker is an inverse SHORT or LONG instrument.

        Returns:
            "SHORT" if the instrument is an inverse / short ETP.
            "LONG" if the instrument is a long ETP or standard long equity.
        """
        if not etp_ticker:
            return "LONG"
        clean = etp_ticker.strip().upper()
        if clean in ("NONE", "N/A"):
            return "LONG"

        # 1. Check curated pairs short tickers
        for p in self.pairs:
            if p.short_3x_ticker and p.short_3x_ticker != "None" and p.short_3x_ticker.upper() == clean:
                return "SHORT"
            if p.short_5x_ticker and p.short_5x_ticker != "None" and p.short_5x_ticker.upper() == clean:
                return "SHORT"
            if p.long_3x_ticker and p.long_3x_ticker.upper() == clean:
                return "LONG"
            if p.long_5x_ticker and p.long_5x_ticker.upper() == clean:
                return "LONG"

        # 2. Check CURRENCY_ALTERNATIVES
        for sym, alt_dict in CURRENCY_ALTERNATIVES.items():
            for curr, (l_tkr, s_tkr) in alt_dict.items():
                if s_tkr and s_tkr != "None" and s_tkr.upper() == clean:
                    return "SHORT"
                if l_tkr and l_tkr != "None" and l_tkr.upper() == clean:
                    return "LONG"

        return "LONG"


# ── Authoritative Currency Resolution for Trading 212 Instruments ──────────────
# Tickers trading in British Pence / Penny (GBX / p) on the London Stock Exchange
GBX_TICKERS = {
    '3ABNl_EQ', '3ARKl_EQ', '3ASMl1_EQ', '3BABl_EQ', '3BIDl_EQ', '3BLCl_EQ', '3BSRl_EQ',
    '3CRWl_EQ', '3DISl_EQ', '3GDXl_EQ', '3GILl_EQ', '3GISl_EQ', '3GLDl_EQ', '3HNXl_EQ',
    '3HODl_EQ', '3INTl_EQ', '3KLAl_EQ', '3KWEl_EQ', '3LAZl_EQ', '3LBA1l_EQ', '3LBC1l_EQ',
    '3LDOl_EQ', '3LLYl_EQ', '3LRDl_EQ', '3LRIl_EQ', '3LRRl_EQ', '3LVOl_EQ', '3LZPl_EQ',
    '3NBIl_EQ', '3NIOl_EQ', '3OKLl_EQ', '3ORCl_EQ', '3QBTl_EQ', '3QCOl_EQ', '3RACl_EQ',
    '3RHMl_EQ', '3SAMl_EQ', '3SAZl_EQ', '3SBAl_EQ', '3SBCl_EQ', '3SBPl_EQ', '3SDOl_EQ',
    '3SFPl_EQ', '3SIPl_EQ', '3SLMl_EQ', '3SLVl_EQ', '3SMGl_EQ', '3SMRl_EQ', '3SPSl_EQ',
    '3SRAl_EQ', '3SRIl_EQ', '3SVO1l_EQ', '3SXLl_EQ', '3TSMl_EQ', '3UKLl_EQ', '3UKSl_EQ',
    '3UNHl_EQ', '3URAl_EQ', '3XLFl_EQ', '3XOMl_EQ', '5UKXl_EQ', 'ARKSl_EQ', 'GBUSl_EQ',
    'LMI3l_EQ', 'MARPl_EQ', 'S3CO1l_EQ', 'SASLl_EQ', 'SAVGl_EQ', 'SDISl_EQ', 'SGDXl_EQ',
    'SKORl_EQ', 'SLLYl_EQ', 'SMC2l_EQ', 'SMI3l_EQ', 'SSQ3l_EQ', 'STSMl_EQ', 'SURAl_EQ',
    'USGBl_EQ'
}

# Tickers trading in US Dollars (USD / $)
USD_TICKERS = {
    '3BRLl_EQ', '3HCLl_EQ', '3HCSl_EQ', '3LALl_EQ', '3LCOOl_EQ', '3LNVl_EQ', '3LPAl_EQ',
    '3LTSl_EQ', '3LUBl_EQ', '3NGLl_EQ', '3NGSl_EQ', '3OISl_EQ', '3SEMl_EQ', '3SMSl_EQ',
    '3SNFl_EQ', '3SNIl_EQ', '3SNVl_EQ', '3SPAl_EQ', '3STSl_EQ', '3SZNl_EQ', '3USLl_EQ',
    '3USSl_EQ', 'AAP3l_EQ', 'AAPSl_EQ', 'ABN3l_EQ', 'AMD3l_EQ', 'ARK3l_EQ', 'ARM3l_EQ',
    'AVG3l_EQ', 'BA3Sl_EQ', 'BA3l_EQ', 'BASl_EQ', 'BID3l_EQ', 'BLC3l_EQ', 'BTC3Ss_EQ',
    'BTC3s_EQ', 'CRM3l_EQ', 'CRW3l_EQ', 'DIS3l_EQ', 'DISSl_EQ', 'ELONl_EQ', 'ETH3Ss_EQ',
    'ETH3s_EQ', 'GDMSl_EQ', 'GDX3l_EQ', 'GPTSl_EQ', 'HNX3l_EQ', 'HOD3l_EQ', 'IBM_US_EQ',
    'INTSl_EQ', 'IONQl1_EQ', 'JPNSl_EQ', 'KLA3l_EQ', 'KWE3l_EQ', 'LLY3l_EQ', 'LLYSl_EQ', 'MAR3l_EQ',
    'MS3Sl_EQ', 'MSF3l_EQ', 'NBI3l_EQ', 'NFL3l_EQ', 'NIO3l_EQ', 'OKL3l_EQ', 'ORC3l_EQ',
    'QBT3l_EQ', 'QCO3l_EQ', 'QQQ3l_EQ', 'QQQSl_EQ', 'RA3Sl_EQ', 'RAC3l_EQ', 'RHM3l_EQ',
    'SARKl_EQ', 'SLMSl_EQ', 'SMCIl_EQ', 'SMG3l_EQ', 'SMR3l_EQ', 'URA3l_EQ', 'URASl_EQ',
    'WDC_US_EQ', 'WTI3l_EQ', 'XOM3l_EQ'
}

_INSTRUMENTS_CACHE_MAP: Optional[Dict[str, str]] = None


def _load_instruments_cache() -> Dict[str, str]:
    """Load cached T212 instruments currency mapping if file exists."""
    global _INSTRUMENTS_CACHE_MAP
    if _INSTRUMENTS_CACHE_MAP is not None:
        return _INSTRUMENTS_CACHE_MAP

    mapping: Dict[str, str] = {}
    possible_paths = [
        ".t212_instruments_cache.json",
        os.path.join(os.path.dirname(__file__), "..", "..", ".t212_instruments_cache.json"),
    ]
    for p in possible_paths:
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        for item in data:
                            tk = item.get("ticker")
                            cc = item.get("currencyCode")
                            if tk and cc:
                                mapping[tk] = cc.upper()
                break
            except Exception:
                pass
    _INSTRUMENTS_CACHE_MAP = mapping
    return mapping


def get_ticker_currency(ticker: str) -> str:
    """Return authoritative quote currency for any Trading 212 execution ticker ('GBX', 'EUR', 'USD', 'GBP')."""
    if not ticker:
        return "EUR"
    clean = str(ticker).strip()

    # 1. Authoritative broker cache check
    cache = _load_instruments_cache()
    if clean in cache:
        return cache[clean]

    # 2. Known sets
    if clean in GBX_TICKERS or clean.endswith(".L"):
        return "GBX"
    if clean in USD_TICKERS or any(clean.endswith(sfx) for sfx in ("_US_EQ", "_US", ".US")):
        return "USD"

    # 3. European exchange suffix heuristics
    if any(clean.endswith(sfx) for sfx in ("p_EQ", "m_EQ", "e_EQ", "d_EQ", "a_EQ", "fp_EQ")):
        return "EUR"

    return "EUR"


def get_currency_display(currency: str) -> Tuple[str, str, str]:
    """Returns (currency_code, currency_prefix, currency_suffix).

    Examples:
        'GBX' -> ('GBX', '', 'p')
        'EUR' -> ('EUR', '€', '')
        'GBP' -> ('GBP', '£', '')
        'USD' -> ('USD', '$', '')
    """
    c = (currency or "").upper().strip()
    if c == "GBX":
        return ("GBX", "", "p")
    elif c == "GBP":
        return ("GBP", "£", "")
    elif c == "USD":
        return ("USD", "$", "")
    return ("EUR", "€", "")


def format_price(price: float, currency: str = "EUR") -> str:
    """Format a numeric price with the appropriate currency prefix or suffix.

    Examples:
        format_price(3190.0, 'GBX') -> '3190.00p'
        format_price(45.2, 'EUR')   -> '€45.20'
        format_price(150.5, 'USD')  -> '$150.50'
    """
    _, prefix, suffix = get_currency_display(currency)
    return f"{prefix}{price:.2f}{suffix}"


_DEFAULT_UNIVERSE: Optional[LeveragedUniverse] = None


def get_etp_direction(etp_ticker: str) -> str:
    """Authoritative module-level helper to determine if an ETP ticker is SHORT or LONG."""
    global _DEFAULT_UNIVERSE
    if _DEFAULT_UNIVERSE is None:
        _DEFAULT_UNIVERSE = LeveragedUniverse()
    return _DEFAULT_UNIVERSE.get_etp_direction(etp_ticker)

