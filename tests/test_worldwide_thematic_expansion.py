"""Unit tests for worldwide sovereign indices, thematic 3x pairs, and session-gated ingestion.

Validates:
1. Lookup and pairing of all 8 worldwide & thematic 3x instruments:
   - South Korea (EWY -> 3KORl_EQ / SKORl_EQ)
   - Japan (EWJ -> JPN3l_EQ / JPNSl_EQ)
   - France CAC 40 (^FCHI -> 3CACl_EQ / 3CSAl_EQ)
   - Italy FTSE MIB (FTSEMIB.MI -> 3ITLl_EQ / 3ITSl_EQ)
   - India (INDA -> 3INDl_EQ / INDSl_EQ)
   - AI & Robotics (BOTZ -> GPT3l_EQ / GPTSl_EQ)
   - AMD (AMD -> AMD3l_EQ / 3SAMl_EQ)
   - ASML (ASML -> 3ASMl_EQ)
2. Index symbol aliasing (^N225 -> EWJ, ^KS11 -> EWY, CAC40 -> ^FCHI, FTSEMIB -> FTSEMIB.MI, NIFTY -> INDA).
3. Market session resolution across Tokyo, Seoul, Mumbai, Paris, Milan, New York, London, and 24/7 Crypto.
4. Session-active gating logic with 30-minute pre-market buffer and dead-hour filtration.
5. Ingestion worker concurrency scaling (24 ingestion, 8 deliberation).
"""

import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from trading_system.config import settings
from trading_system.data.leveraged_universe import CURATED_3X_PAIRS, INDEX_ALIASES, LeveragedUniverse
from trading_system.data.trading212_client import Trading212Client
from trading_system.orchestrator import Orchestrator


class TestWorldwideThematicExpansion(unittest.TestCase):
    """Test suite verifying worldwide sovereign and thematic 3x asset expansion."""

    def setUp(self):
        self.universe = LeveragedUniverse()

    def test_all_8_worldwide_and_thematic_pairs_exist(self):
        """Verify all 8 worldwide sovereign and thematic pairs are present in LeveragedUniverse."""
        expected_pairs = {
            "EWY": ("3KORl_EQ", "SKORl_EQ"),
            "EWJ": ("JPN3l_EQ", "JPNSl_EQ"),
            "^FCHI": ("3CACl_EQ", "3CSAl_EQ"),
            "FTSEMIB.MI": ("3ITLl_EQ", "3ITSl_EQ"),
            "INDA": ("3INDl_EQ", "INDSl_EQ"),
            "BOTZ": ("GPT3l_EQ", "GPTSl_EQ"),
            "AMD": ("AMD3l_EQ", "3SAMl_EQ"),
            "ASML": ("3ASMl_EQ", "None"),
        }

        all_underlyings = self.universe.get_all_underlyings()

        for underlying, (expected_long, expected_short) in expected_pairs.items():
            self.assertIn(underlying, all_underlyings, f"Missing underlying {underlying} in universe")
            pair = self.universe.get_pair_for_underlying(underlying)
            self.assertIsNotNone(pair, f"Could not find LeveragedPair for {underlying}")
            self.assertEqual(pair.long_3x_ticker, expected_long)
            if expected_short != "None":
                self.assertEqual(pair.short_3x_ticker, expected_short)

    def test_execution_tickers_resolution(self):
        """Verify get_execution_ticker returns the right ETP for LONG and SHORT."""
        self.assertEqual(self.universe.get_execution_ticker("EWY", "LONG"), "3KORl_EQ")
        self.assertEqual(self.universe.get_execution_ticker("EWY", "SHORT"), "SKORl_EQ")

        self.assertEqual(self.universe.get_execution_ticker("EWJ", "LONG"), "JPN3l_EQ")
        self.assertEqual(self.universe.get_execution_ticker("EWJ", "SHORT"), "JPNSl_EQ")

        self.assertEqual(self.universe.get_execution_ticker("^FCHI", "LONG"), "3CACl_EQ")
        self.assertEqual(self.universe.get_execution_ticker("^FCHI", "SHORT"), "3CSAl_EQ")

        self.assertEqual(self.universe.get_execution_ticker("FTSEMIB.MI", "LONG"), "3ITLl_EQ")
        self.assertEqual(self.universe.get_execution_ticker("FTSEMIB.MI", "SHORT"), "3ITSl_EQ")

        self.assertEqual(self.universe.get_execution_ticker("INDA", "LONG"), "3INDl_EQ")
        self.assertEqual(self.universe.get_execution_ticker("INDA", "SHORT"), "INDSl_EQ")

        self.assertEqual(self.universe.get_execution_ticker("BOTZ", "LONG"), "GPT3l_EQ")
        self.assertEqual(self.universe.get_execution_ticker("BOTZ", "SHORT"), "GPTSl_EQ")

        self.assertEqual(self.universe.get_execution_ticker("AMD", "LONG"), "AMD3l_EQ")
        self.assertEqual(self.universe.get_execution_ticker("AMD", "SHORT"), "3SAMl_EQ")

        self.assertEqual(self.universe.get_execution_ticker("ASML", "LONG"), "3ASMl_EQ")
        self.assertIsNone(self.universe.get_execution_ticker("ASML", "SHORT"))

    def test_index_aliases_resolution(self):
        """Verify index aliases resolve correctly to underlyings and execution tickers."""
        # Japan Nikkei
        pair_nikkei = self.universe.get_pair_for_underlying("^N225")
        self.assertIsNotNone(pair_nikkei)
        self.assertEqual(pair_nikkei.underlying_symbol, "EWJ")
        self.assertEqual(self.universe.get_execution_ticker("NIKKEI", "LONG"), "JPN3l_EQ")

        # South Korea KOSPI
        pair_kospi = self.universe.get_pair_for_underlying("^KS11")
        self.assertIsNotNone(pair_kospi)
        self.assertEqual(pair_kospi.underlying_symbol, "EWY")
        self.assertEqual(self.universe.get_execution_ticker("KOSPI", "LONG"), "3KORl_EQ")

        # France CAC 40
        pair_cac = self.universe.get_pair_for_underlying("CAC40")
        self.assertIsNotNone(pair_cac)
        self.assertEqual(pair_cac.underlying_symbol, "^FCHI")
        self.assertEqual(self.universe.get_execution_ticker("CAC40", "LONG"), "3CACl_EQ")

        # Italy FTSE MIB
        pair_mib = self.universe.get_pair_for_underlying("FTSEMIB")
        self.assertIsNotNone(pair_mib)
        self.assertEqual(pair_mib.underlying_symbol, "FTSEMIB.MI")
        self.assertEqual(self.universe.get_execution_ticker("MIB", "LONG"), "3ITLl_EQ")

        # India Nifty
        pair_nifty = self.universe.get_pair_for_underlying("NIFTY")
        self.assertIsNotNone(pair_nifty)
        self.assertEqual(pair_nifty.underlying_symbol, "INDA")
        self.assertEqual(self.universe.get_execution_ticker("^NSEI", "LONG"), "3INDl_EQ")

        # ASML Amsterdam alias
        pair_asml = self.universe.get_pair_for_underlying("ASML.AS")
        self.assertIsNotNone(pair_asml)
        self.assertEqual(pair_asml.underlying_symbol, "ASML")

    def test_market_session_resolution_across_exchanges(self):
        """Verify get_market_session_info returns correct exchange and session hours."""
        wed_tokyo_open = datetime(2026, 10, 14, 2, 0, tzinfo=timezone.utc)
        wed_seoul_open = datetime(2026, 10, 14, 3, 0, tzinfo=timezone.utc)
        wed_mumbai_open = datetime(2026, 10, 14, 5, 0, tzinfo=timezone.utc)
        wed_paris_open = datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)
        wed_milan_open = datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)

        # Tokyo
        info_tse = Trading212Client.get_market_session_info("^N225", now_utc=wed_tokyo_open)
        self.assertEqual(info_tse["exchange"], "TSE")
        self.assertTrue(info_tse["is_open"])
        self.assertEqual(info_tse["open_time_utc"], "00:00")
        self.assertEqual(info_tse["close_time_utc"], "06:00")

        # Seoul
        info_krx = Trading212Client.get_market_session_info("^KS11", now_utc=wed_seoul_open)
        self.assertEqual(info_krx["exchange"], "KRX")
        self.assertTrue(info_krx["is_open"])
        self.assertEqual(info_krx["open_time_utc"], "00:00")
        self.assertEqual(info_krx["close_time_utc"], "06:30")

        # Mumbai
        info_nse = Trading212Client.get_market_session_info("^NSEI", now_utc=wed_mumbai_open)
        self.assertEqual(info_nse["exchange"], "NSE")
        self.assertTrue(info_nse["is_open"])
        self.assertEqual(info_nse["open_time_utc"], "03:45")
        self.assertEqual(info_nse["close_time_utc"], "10:00")

        # Paris
        info_euronext = Trading212Client.get_market_session_info("^FCHI", now_utc=wed_paris_open)
        self.assertEqual(info_euronext["exchange"], "EURONEXT")
        self.assertTrue(info_euronext["is_open"])
        self.assertEqual(info_euronext["open_time_utc"], "08:00")
        self.assertEqual(info_euronext["close_time_utc"], "16:30")

        # Milan
        info_mib = Trading212Client.get_market_session_info("FTSEMIB.MI", now_utc=wed_milan_open)
        self.assertEqual(info_mib["exchange"], "BORSA_ITALIANA")
        self.assertTrue(info_mib["is_open"])
        self.assertEqual(info_mib["open_time_utc"], "08:00")
        self.assertEqual(info_mib["close_time_utc"], "16:30")

        # Crypto (24/7)
        sat_crypto = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
        info_crypto = Trading212Client.get_market_session_info("BTC-USD", now_utc=sat_crypto)
        self.assertEqual(info_crypto["exchange"], "CRYPTO")
        self.assertTrue(info_crypto["is_open"])
        self.assertFalse(info_crypto["is_weekend"])

    def test_is_asset_session_active_filtering(self):
        """Verify is_asset_session_active recognizes active hours and filters off-hours."""
        # Active in London morning (10:00 UTC): European ETP 3KORl_EQ is trading on LSE
        wed_10am = datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc)
        self.assertTrue(Trading212Client.is_asset_session_active("EWY", now_utc=wed_10am))
        self.assertTrue(Trading212Client.is_asset_session_active("^FCHI", now_utc=wed_10am))
        self.assertTrue(Trading212Client.is_asset_session_active("SPY", now_utc=wed_10am))

        # Active within 30 min of London open (07:45 UTC)
        wed_pre_open = datetime(2026, 10, 14, 7, 45, tzinfo=timezone.utc)
        self.assertTrue(Trading212Client.is_asset_session_active("SPY", now_utc=wed_pre_open))

        # Dead hours on Wednesday night (22:30 UTC): US closed at 21:00, Europe closed at 16:30, Asia opens at 00:00
        wed_dead_hours = datetime(2026, 10, 14, 22, 30, tzinfo=timezone.utc)
        self.assertFalse(Trading212Client.is_asset_session_active("SPY", now_utc=wed_dead_hours))
        self.assertFalse(Trading212Client.is_asset_session_active("^FCHI", now_utc=wed_dead_hours))
        self.assertFalse(Trading212Client.is_asset_session_active("FTSEMIB.MI", now_utc=wed_dead_hours))

        # Weekend (Saturday 14:00 UTC): all equity markets closed, only crypto active
        sat_midday = datetime(2026, 10, 17, 14, 0, tzinfo=timezone.utc)
        self.assertFalse(Trading212Client.is_asset_session_active("SPY", now_utc=sat_midday))
        self.assertFalse(Trading212Client.is_asset_session_active("EWY", now_utc=sat_midday))
        self.assertFalse(Trading212Client.is_asset_session_active("EWJ", now_utc=sat_midday))
        self.assertTrue(Trading212Client.is_asset_session_active("BTC-USD", now_utc=sat_midday))

    def test_concurrency_settings_and_session_gated_ingestion(self):
        """Verify worker scaling settings and orchestrator session-gated ingestion."""
        self.assertEqual(settings.PARALLEL_INGESTION_WORKERS, 24)
        self.assertEqual(settings.PARALLEL_DELIBERATION_WORKERS, 8)

        orch = Orchestrator(dry_run=True, enable_dashboard=False)
        self.assertEqual(orch.parallel_ingestion_workers, 24)
        self.assertEqual(orch.parallel_deliberation_workers, 8)

        # Test session-gated ingestion filters dormant assets
        with patch.object(Trading212Client, "is_asset_session_active") as mock_active:
            # Simulate only 2 assets active
            mock_active.side_effect = lambda sym: sym in ("SPY", "QQQ")
            with patch("trading_system.orchestrator.MarketDataProvider.get_daily_candles", return_value=[]):
                with patch("trading_system.orchestrator.MarketDataProvider.get_hourly_candles", return_value=[]):
                    with patch("trading_system.orchestrator.MarketDataProvider.compute_indicators", return_value={"close": 100.0}):
                        metrics = orch.ingest_universe_data(filter_active_sessions=True)
                        # Should have ingested only the 2 active assets
                        self.assertEqual(set(metrics.keys()), {"SPY", "QQQ"})


if __name__ == "__main__":
    unittest.main()
