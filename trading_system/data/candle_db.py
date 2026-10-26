"""Persistent SQLite Backend Database for Incremental Historical Candle Storage.

Provides fast, incremental caching of daily and hourly OHLCV candles to:
1. Prevent repetitive, slow full-period (1y) API calls across scanning cycles.
2. Store candles locally with atomic WAL (Write-Ahead Logging) transactions.
3. Fetch only newly closed bars from upstream data providers and merge them seamlessly.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger(__name__)


class CandleDatabase:
    """Thread-safe SQLite database manager for OHLCV candles."""

    _instance: Optional["CandleDatabase"] = None
    _lock = threading.Lock()

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            try:
                from trading_system.config import settings
                db_path = getattr(settings, "CANDLE_DB_PATH", "data/candles.db")
            except Exception:
                db_path = "data/candles.db"

        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        self._init_lock = threading.Lock()
        self._local = threading.local()
        self._init_db()

    @classmethod
    def get_instance(cls, db_path: Optional[str] = None) -> "CandleDatabase":
        """Singleton accessor."""
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(db_path)
            return cls._instance

    def _get_connection(self) -> sqlite3.Connection:
        """Get thread-local SQLite connection with WAL mode enabled."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(self.db_path, timeout=30.0)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA busy_timeout=15000;")
            self._local.conn = conn
        return self._local.conn

    def _init_db(self) -> None:
        """Initialize the candles schema."""
        with self._init_lock:
            conn = self._get_connection()
            with conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS candles (
                        symbol TEXT NOT NULL,
                        timeframe TEXT NOT NULL,
                        date TEXT NOT NULL,
                        open REAL NOT NULL,
                        high REAL NOT NULL,
                        low REAL NOT NULL,
                        close REAL NOT NULL,
                        volume REAL NOT NULL,
                        PRIMARY KEY (symbol, timeframe, date)
                    ) WITHOUT ROWID;
                """)

    def get_latest_date(self, symbol: str, timeframe: str = "1d") -> Optional[str]:
        """Return the ISO date string of the most recent candle for (symbol, timeframe)."""
        sym = symbol.upper().strip()
        conn = self._get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT MAX(date) FROM candles WHERE symbol = ? AND timeframe = ?",
            (sym, timeframe),
        )
        row = cur.fetchone()
        return row[0] if row and row[0] else None

    def get_candle_count(self, symbol: str, timeframe: str = "1d") -> int:
        """Return total number of candles stored for (symbol, timeframe)."""
        sym = symbol.upper().strip()
        conn = self._get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM candles WHERE symbol = ? AND timeframe = ?",
            (sym, timeframe),
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0

    def get_candles(
        self,
        symbol: str,
        timeframe: str = "1d",
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Retrieve chronological candle records for (symbol, timeframe)."""
        sym = symbol.upper().strip()
        conn = self._get_connection()
        cur = conn.cursor()
        if limit:
            query = """
                SELECT date, open, high, low, close, volume
                FROM (
                    SELECT date, open, high, low, close, volume
                    FROM candles
                    WHERE symbol = ? AND timeframe = ?
                    ORDER BY date DESC
                    LIMIT ?
                )
                ORDER BY date ASC
            """
            cur.execute(query, (sym, timeframe, limit))
        else:
            query = """
                SELECT date, open, high, low, close, volume
                FROM candles
                WHERE symbol = ? AND timeframe = ?
                ORDER BY date ASC
            """
            cur.execute(query, (sym, timeframe))

        rows = cur.fetchall()
        return [
            {
                "date": str(r[0]),
                "open": float(r[1]),
                "high": float(r[2]),
                "low": float(r[3]),
                "close": float(r[4]),
                "volume": float(r[5]),
            }
            for r in rows
        ]

    def upsert_candles(
        self,
        symbol: str,
        timeframe: str,
        candles: List[Union[Dict[str, Any], Any]],
    ) -> int:
        """Insert or update candles for (symbol, timeframe). Returns count of upserted rows."""
        if not candles:
            return 0

        sym = symbol.upper().strip()
        records = []
        for c in candles:
            # Handle dictionary or Pandas Series
            if isinstance(c, dict):
                dt = str(c.get("date", c.get("Date", "")))
                o = float(c.get("open", c.get("Open", 0.0)))
                h = float(c.get("high", c.get("High", 0.0)))
                l = float(c.get("low", c.get("Low", 0.0)))
                cl = float(c.get("close", c.get("Close", 0.0)))
                v = float(c.get("volume", c.get("Volume", 0.0)))
            else:
                # Row-like object
                dt = str(getattr(c, "date", getattr(c, "Date", "")))
                o = float(getattr(c, "open", getattr(c, "Open", 0.0)))
                h = float(getattr(c, "high", getattr(c, "High", 0.0)))
                l = float(getattr(c, "low", getattr(c, "Low", 0.0)))
                cl = float(getattr(c, "close", getattr(c, "Close", 0.0)))
                v = float(getattr(c, "volume", getattr(c, "Volume", 0.0)))

            # Strip timestamp details if standard daily date
            if timeframe == "1d" and " " in dt:
                dt = dt.split(" ")[0]
            if dt and cl > 0:
                records.append((sym, timeframe, dt, o, h, l, cl, v))

        if not records:
            return 0

        conn = self._get_connection()
        with conn:
            conn.executemany(
                """
                INSERT INTO candles (symbol, timeframe, date, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, timeframe, date) DO UPDATE SET
                    open = excluded.open,
                    high = excluded.high,
                    low = excluded.low,
                    close = excluded.close,
                    volume = excluded.volume;
                """,
                records,
            )
        return len(records)

    def clear_symbol(self, symbol: str, timeframe: Optional[str] = None) -> None:
        """Clear cached records for a symbol."""
        sym = symbol.upper().strip()
        conn = self._get_connection()
        with conn:
            if timeframe:
                conn.execute(
                    "DELETE FROM candles WHERE symbol = ? AND timeframe = ?",
                    (sym, timeframe),
                )
            else:
                conn.execute("DELETE FROM candles WHERE symbol = ?", (sym,))

    def close(self) -> None:
        """Close thread-local connection."""
        if hasattr(self._local, "conn") and self._local.conn is not None:
            try:
                self._local.conn.close()
            except Exception:
                pass
            self._local.conn = None
