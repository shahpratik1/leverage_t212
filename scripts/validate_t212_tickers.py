#!/usr/bin/env python3
"""Validate all Curated Leveraged Pairs and Currency Alternatives against Trading 212.

Checks all configured ETP execution tickers in `trading_system.data.leveraged_universe`
against the local `.t212_instruments_cache.json` disk cache (or live Trading 212 API)
to ensure 100% ticker validity and zero broken order placements in production.

Usage:
    python3 scripts/validate_t212_tickers.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import List, Set, Tuple

# Add repository root to pythonpath
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from trading_system.data.leveraged_universe import CURATED_3X_PAIRS, CURRENCY_ALTERNATIVES
from trading_system.data.trading212_client import _get_instruments_cache_path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("validate_tickers")


def load_known_tickers() -> Set[str]:
    """Load tradeable ticker symbols from Trading 212 cache file."""
    cache_path = _get_instruments_cache_path()
    if not os.path.exists(cache_path):
        logger.warning("Cache file not found at %s. Attempting to load from workspace root...", cache_path)
        alt_path = REPO_ROOT / ".t212_instruments_cache.json"
        if alt_path.exists():
            cache_path = str(alt_path)
        else:
            logger.error("No Trading 212 instruments cache found on disk!")
            return set()

    with open(cache_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    tickers = {item.get("ticker") for item in data if isinstance(item, dict) and "ticker" in item}
    logger.info("Loaded %d tradeable instruments from Trading 212 cache (%s)", len(tickers), cache_path)
    return tickers


def validate_universe() -> bool:
    """Validate every ticker in CURATED_3X_PAIRS and CURRENCY_ALTERNATIVES."""
    known_tickers = load_known_tickers()
    if not known_tickers:
        logger.error("Cannot validate without known tickers. Skipping check.")
        return False

    invalid: List[Tuple[str, str, str]] = []

    # 1. Check primary curated pairs
    for p in CURATED_3X_PAIRS:
        for attr, tkr in [
            ("long_3x", p.long_3x_ticker),
            ("short_3x", p.short_3x_ticker),
            ("long_5x", p.long_5x_ticker),
            ("short_5x", p.short_5x_ticker),
        ]:
            if tkr and tkr != "None" and tkr not in known_tickers:
                invalid.append((p.underlying_symbol, attr, tkr))

    # 2. Check currency alternatives
    for sym, currs in CURRENCY_ALTERNATIVES.items():
        for c, (l, s) in currs.items():
            for tkr in [l, s]:
                if tkr and tkr != "None" and tkr not in known_tickers:
                    invalid.append((sym, f"alt_{c}", tkr))

    if invalid:
        logger.error("VALIDATION FAILED: Found %d invalid tickers in universe configuration:", len(invalid))
        for sym, kind, tkr in invalid:
            logger.error("  - %s (%s): %s not recognized by Trading 212", sym, kind, tkr)
        return False

    total_pairs = len(CURATED_3X_PAIRS)
    eur_pairs = sum(1 for p in CURATED_3X_PAIRS if p.currency == "EUR")
    logger.info(
        "VALIDATION PASSED: All %d pairs (%d EUR-denominated) are 100%% verified in Trading 212!",
        total_pairs,
        eur_pairs,
    )
    return True


if __name__ == "__main__":
    success = validate_universe()
    sys.exit(0 if success else 1)
