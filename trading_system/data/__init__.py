"""Data ingestion layer for the trading system."""

from trading_system.data.trading212_client import MarketDataProvider, Trading212Client
from trading_system.data.leveraged_universe import LeveragedUniverse

__all__ = [
    "MarketDataProvider",
    "Trading212Client",
    "LeveragedUniverse",
]
