"""Unit tests for Persistent SQLite Candle Database and Incremental Data Caching."""

import os
import tempfile
import unittest

from trading_system.data.candle_db import CandleDatabase
from trading_system.data.trading212_client import MarketDataProvider


class TestCandleDatabase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_candles.db")
        self.db = CandleDatabase(self.db_path)

    def tearDown(self):
        self.db.close()
        self.temp_dir.cleanup()

    def test_upsert_and_retrieve_candles(self):
        """Verify upserting raw candle dictionaries and retrieving them chronologically."""
        raw_candles = [
            {"date": "2026-10-20", "open": 100.0, "high": 105.0, "low": 99.0, "close": 104.0, "volume": 1000000},
            {"date": "2026-10-21", "open": 104.0, "high": 108.0, "low": 103.0, "close": 107.5, "volume": 1200000},
            {"date": "2026-10-22", "open": 107.0, "high": 109.0, "low": 106.0, "close": 108.0, "volume": 900000},
        ]
        count = self.db.upsert_candles("QQQ", "1d", raw_candles)
        self.assertEqual(count, 3)
        self.assertEqual(self.db.get_candle_count("QQQ", "1d"), 3)
        self.assertEqual(self.db.get_latest_date("QQQ", "1d"), "2026-10-22")

        retrieved = self.db.get_candles("QQQ", "1d")
        self.assertEqual(len(retrieved), 3)
        self.assertEqual(retrieved[0]["date"], "2026-10-20")
        self.assertEqual(retrieved[-1]["date"], "2026-10-22")
        self.assertAlmostEqual(retrieved[-1]["close"], 108.0)

    def test_deduplication_on_conflict(self):
        """Verify that duplicate candle dates are updated without expanding row count."""
        initial = [
            {"date": "2026-10-25", "open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0, "volume": 500000},
        ]
        self.db.upsert_candles("NVDA", "1d", initial)
        self.assertEqual(self.db.get_candle_count("NVDA", "1d"), 1)

        # Updated close for same date
        updated = [
            {"date": "2026-10-25", "open": 100.0, "high": 104.0, "low": 99.0, "close": 103.5, "volume": 750000},
        ]
        self.db.upsert_candles("NVDA", "1d", updated)
        self.assertEqual(self.db.get_candle_count("NVDA", "1d"), 1)

        candles = self.db.get_candles("NVDA", "1d")
        self.assertEqual(candles[0]["close"], 103.5)
        self.assertEqual(candles[0]["high"], 104.0)

    def test_limit_retrieval(self):
        """Verify limit parameter returns the most recent N candles in chronological order."""
        from datetime import date, timedelta
        base_dt = date(2025, 1, 1)
        many_candles = [
            {
                "date": (base_dt + timedelta(days=i)).isoformat(),
                "open": 100.0 + i,
                "high": 105.0 + i,
                "low": 95.0 + i,
                "close": 101.0 + i,
                "volume": 1000,
            }
            for i in range(50)
        ]
        self.db.upsert_candles("SPY", "1d", many_candles)
        self.assertEqual(self.db.get_candle_count("SPY", "1d"), 50)

        limit_20 = self.db.get_candles("SPY", "1d", limit=20)
        self.assertEqual(len(limit_20), 20)
        # Oldest of the 20 should be index 30
        self.assertAlmostEqual(limit_20[0]["close"], 101.0 + 30)
        # Newest should be index 49
        self.assertAlmostEqual(limit_20[-1]["close"], 101.0 + 49)

    def test_clear_symbol(self):
        """Verify clearing records for a symbol."""
        self.db.upsert_candles("AAPL", "1d", [{"date": "2026-10-20", "open": 150, "high": 152, "low": 149, "close": 151, "volume": 1000}])
        self.db.upsert_candles("AAPL", "1h", [{"date": "2026-10-20 10:00:00", "open": 150, "high": 152, "low": 149, "close": 151, "volume": 1000}])
        self.assertEqual(self.db.get_candle_count("AAPL", "1d"), 1)
        self.assertEqual(self.db.get_candle_count("AAPL", "1h"), 1)

        self.db.clear_symbol("AAPL", "1d")
        self.assertEqual(self.db.get_candle_count("AAPL", "1d"), 0)
        self.assertEqual(self.db.get_candle_count("AAPL", "1h"), 1)

    def test_reset_instance_and_connection_tracking(self):
        """Verify get_instance, connection tracking in _all_conns, and reset_instance cleanly closing connections."""
        instance = CandleDatabase.get_instance(self.db_path)
        self.assertIsNotNone(instance)
        # Query via instance to open connection
        _ = instance.get_candle_count("SPY", "1d")
        self.assertGreaterEqual(len(instance._all_conns), 1)

        CandleDatabase.reset_instance()
        self.assertIsNone(CandleDatabase._instance)
        self.assertEqual(len(instance._all_conns), 0)


if __name__ == "__main__":
    unittest.main()
