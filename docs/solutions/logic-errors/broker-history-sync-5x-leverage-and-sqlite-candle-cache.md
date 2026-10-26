---
title: Live Broker Order Reconciliation, 5x Leverage Prioritization, and Incremental SQLite Candle Database
date: 2026-10-26
category: logic-errors
module: data-and-dashboard
problem_type: data_desynchronization_and_api_quota_exhaustion
component: trading_system/dashboard/server.py, trading_system/data/candle_db.py, trading_system/data/leveraged_universe.py
symptoms:
  - "Recent Trade & Alert History table on dashboard detached from actual Trading 212 order fills"
  - "Position stop outs and fills occurring on broker were invisible on dashboard activity table"
  - "Assets with 5x leverage availability were artificially suppressed and capped at 3x"
  - "Repetitive, high-latency 1y candle API requests causing 403/429 rate limit errors on each scan cycle"
root_cause: static_notifications_file_source_and_missing_incremental_candle_store
resolution_type: live_broker_order_merge_and_wal_sqlite_candle_cache
severity: high
tags:
  - trading212
  - broker-reconciliation
  - 5x-leverage
  - sqlite-candle-db
  - rate-limiting
---

# Live Broker Order Reconciliation, 5x Leverage Prioritization, and Incremental SQLite Candle Database

## Problem Context
1. **Recent Trade & Alert History Desynchronization**:
   The dashboard's `🔔 Recent Trade & Alert History` section loaded solely from local `notifications.jsonl`. When orders were filled, stopped out, or executed on the broker (Trading 212), the table remained outdated and omitted real fills (e.g., `IONQl1_EQ`, `3LFEl_EQ`, `3SEMd_EQ`).
2. **5x Leverage Prioritization**:
   The universe previously capped all assets at 3x leverage even when high-conviction 5x instruments (e.g., `5QQEl_EQ`, `SQQEl_EQ`, `5SPEl_EQ`, `SSPEl_EQ`) were available on Trading 212.
3. **Redundant API Candle Ingestion**:
   Every scanning cycle re-requested 1 year (`period="1y"`) of historical bars for dozens of assets repeatedly across Yahoo Finance, Stooq, and Finnhub, incurring high latency and rate-limit blocks (HTTP 403 / 429).

## Root Cause & Solution

### 1. Live Trading 212 History Stream Merging (`trading_system/dashboard/server.py`)
Implemented `fetch_live_broker_notifications(client, universe, limit=25)`:
- Calls `GET /equity/history/orders?limit=25` on Trading 212 via `Trading212Client.get_historical_orders()`.
- Extracts fills (`fill.price`, `fill.quantity`, `walletImpact.realisedProfitLoss`, `filledAt`), mapping order types (`STOP`, `LIMIT`, `MARKET`) into standardized trade events (`BUY`, `SELL`, `SL`, `TP1`).
- Merges broker executions with local alert events in `build_dashboard_data()`, sorting chronologically descending so live executions appear immediately.
- Added alias `notify_stop_loss()` to `Notifier` to guarantee external closures in `position_monitor.py` record to audit logs.

### 2. 5x Leverage Prioritization System Adaptation
- `trading_system/data/leveraged_universe.py`:
  - `get_available_leverage_multipliers()` returns `[5]` if `pair.has_5x` else `[3]`.
  - `get_execution_ticker()` maps directly to `long_5x_ticker` / `short_5x_ticker` when available.
  - `get_all_execution_tickers()` includes 5x tickers in active monitoring.
- `trading_system/agents/risk_manager.py`:
  - Removed prohibited 5x ticker restrictions.
  - Set `MAX_LEVERAGE = 5`.
  - Dynamically budgets volatility notional for 5x leverage: $Effective Vol = \max(0.10, 5 \times Realized Vol)$.
- `trading_system/orchestrator.py`:
  - Routes orders with `target_leverage = 5` when `has_5x_leverage()` is true.

### 3. Incremental SQLite Candle Backend (`trading_system/data/candle_db.py`)
- Created `CandleDatabase` using thread-local SQLite connections with WAL mode (`PRAGMA journal_mode=WAL;`).
- Schema: `candles (symbol TEXT, timeframe TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, PRIMARY KEY (symbol, timeframe, date)) WITHOUT ROWID`.
- In `MarketDataProvider.get_daily_candles` & `get_hourly_candles`:
  - Checks `get_latest_date()` and `get_candle_count()`.
  - If today's bar is already stored locally, returns directly from SQLite (0 network API calls).
  - If delta is missing, requests a minimal 5-day delta (`fetch_period = "5d"`) instead of 1 year, upserts into SQLite via `executemany`, and returns the merged contiguous history.

## Verification
- `tests/test_candle_db.py`: 4 passing tests (upsert, conflict deduplication, limit ordering, clear).
- `tests/test_5x_leveraged_universe.py`: 7 passing tests (5x presence, ticker resolution, risk manager tagging, trade validation).
- `tests/test_embedded_dashboard.py`: 7 passing tests including live historical order parsing.
- Full suite: 183 tests passing in 37.6s.
- Live deployment to Raspberry Pi (`192.168.68.50`): `trading-bot.service` active and `/api/data` verified returning live fills for `IONQl1_EQ`, `3LFEl_EQ`, and `3SEMd_EQ`.
