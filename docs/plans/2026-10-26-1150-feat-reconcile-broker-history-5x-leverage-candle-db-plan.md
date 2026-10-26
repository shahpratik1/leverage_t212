# Plan: Reconcile Live Broker History, Prioritize 5x Leverage, and Incremental Candle Backend Database

## 1. Problem Statement & Motivation
1. **Trade & Alert History Out of Sync**: The dashboard's "Recent Trade & Alert History" was reading solely from `notifications.jsonl` (stale from Oct 22), failing to reflect actual Trading 212 order executions (e.g. today's live fills on `IONQl1_EQ`, `3LFEl_EQ`, `3SEMd_EQ`).
2. **5x Leverage Prioritization**: Where 5x ETPs exist on Trading 212 for an asset (e.g. QQQ, SPY, TLT), the system previously suppressed them to 3x. The user requested: replace 3x completely with 5x whenever 5x is present, adapting execution, risk management, and sizing.
3. **Redundant API Candle Calls**: Currently `MarketDataProvider` requests 1 full year (`1y`) of candles repeatedly on every cycle just for 1 new candle. We need an incremental SQLite candle database backend that caches history, identifies the latest candle timestamp, fetches only new/missing bars, merges them, and eliminates redundant API consumption and computation.

## 2. Proposed Changes

### Component 1: Live Broker Order & Trade Reconciliation
- In `trading_system/data/trading212_client.py`:
  - Add/ensure `get_historical_orders(limit=50)` cleanly queries `/equity/history/orders?limit=...`.
- In `trading_system/dashboard/server.py`:
  - In `build_dashboard_data()` and `load_recent_notifications()`: fetch live historical order fills from Trading 212 via `client.get_historical_orders()` and merge with internal notifications so the "Recent Trade & Alert History" reflects live broker fills with fill price, realized P&L, and timestamps.
- In `trading_system/execution/position_monitor.py` and `orchestrator.py`:
  - When positions are closed externally, stopped out, or executed, emit a `TradeNotification` to `notifications.jsonl`.

### Component 2: Prioritize 5x Leverage Over 3x When Available
- In `trading_system/data/leveraged_universe.py`:
  - Update `get_available_leverage_multipliers()`: return `[5]` if `pair.has_5x` else `[3]`.
  - Update `get_execution_ticker()`: if `pair.has_5x`, return the 5x ticker (`long_5x_ticker` / `short_5x_ticker`) instead of 3x.
  - Update `get_all_execution_tickers()` to include 5x tickers.
- In `trading_system/agents/risk_manager.py`:
  - Allow `leverage=5` when `universe.has_5x_leverage()`.
  - Remove prohibited 5x ticker validation check.
  - Scale volatility sizing and ATR stop distances proportionally for 5x leverage.
- In `trading_system/orchestrator.py`:
  - Pass 5x leverage to risk manager when underlying supports 5x.
- In `tests/test_5x_leveraged_universe.py`:
  - Update tests to assert 5x is selected and executed when available.

### Component 3: Incremental SQLite Candle Database (`candles.db`)
- Create `trading_system/data/candle_db.py`:
  - Persistent SQLite storage with WAL mode.
  - Table `candles(symbol, timeframe, date, open, high, low, close, volume, PRIMARY KEY (symbol, timeframe, date))`.
  - Methods: `get_latest_date`, `get_candles`, `upsert_candles`, `get_candle_count`.
- In `trading_system/data/trading212_client.py` (`MarketDataProvider`):
  - Integrate `CandleDatabase` in `get_daily_candles` and `get_hourly_candles`.
  - Check latest date in DB:
    - If up to date, return directly from DB (0 network calls).
    - If slightly outdated, fetch only recent delta (e.g. `period="5d"`), upsert to DB, and return full series.
    - If empty, fetch full initial history (`period="1y"`), seed DB, and return.
- Create `tests/test_candle_db.py`:
  - Test initial seed, delta fetch, deduplication, and recovery.

## 3. Verification & Deployment Plan
1. Run local tests: `tests/test_candle_db.py`, `tests/test_5x_leveraged_universe.py`, `tests/test_dashboard.py`.
2. Run full test suite: `./venv/bin/python3 -m unittest discover tests`.
3. Deploy to Raspberry Pi: `./scripts/deploy_to_pi.sh`.
4. Verify Pi runner service status and dashboard at `http://192.168.68.50:8080`.
5. Verify live "Recent Trade & Alert History" displays actual broker executions.
