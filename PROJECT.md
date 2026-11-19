# Project: Quantitative Trading Strategy Alpha Overhaul & Systemic Architectural Harmonization

## Architecture
The Trading 212 algorithmic system is an autonomous multi-agent quantitative trading engine for leveraged equities and ETPs.
- **Data & Indicators Layer**: `trading_system/data/` (`trading212_client.py`, `leveraged_universe.py`, `candle_db.py`, `multi_provider.py`). Provides multi-provider waterfall routing (FMP, Tiingo, Polygon.io, Alpha Vantage, Yahoo Chart, Stooq CSV), SQLite WAL delta caching, indicator computation (RSI, EMA, ADX, ATR, Donchian, Bollinger, Volatility, DMI +/-DI, 20 EMA slope), and live broker connectivity.
- **Quantitative Strategy Engine**: `trading_system/strategies/` (`base.py`, `registry.py`, `directional_trend.py`, `vwap.py`). Decoupled strategy architecture with `BaseStrategy` Protocol, `StrategySignal`, and `StrategyRegistry`. Supports `DirectionalTrendStrategy` (DMI polarity gating + 20/50 EMA slope hierarchy + MACD divergence filtering + Dual-Track Bear Regime execution) and `VWAPStrategy`.
- **Screener & Regime Layer**: `trading_system/screener/` (`laya_screener.py`). Filters universe candidates, runs parallel multi-threaded screening across 120+ pairs, and generates structured decision funnel attribution reports.
- **Agent Deliberation Layer**: `trading_system/agents/` (`technical_agent.py`, `risk_manager.py`, `debate_engine.py`, `strategy_improvement_agent.py`). Pure System 2 deliberative multi-agent debate (technical + fundamental + dialectic arbiter) with fast reflex bypass completely removed.
- **Execution & Position Monitoring Layer**: `trading_system/orchestrator.py`, `trading_system/execution/position_monitor.py`. Pre-flight broker order queue reconciliation (stale order purge & in-flight deduplication), 15-minute market opening auction buffer, pre-trade drift & spread gates, 100% Pure Trailing Stop Loss engine (0% TP limits), and native Trading 212 GTC stop synchronization.
- **Backtesting & Simulation Layer**: `trading_system/backtest/` (`engine.py`). Simulates strategy execution across historical OHLCV data and verifies statistical edge (Sharpe, Profit Factor, Win Rate).
- **Control Plane, Dashboard & Cloud/Edge Layer**: `trading_system/dashboard/server.py`, `trading_system/config_bus.py`, `scripts/deploy_to_pi.sh`, `.github/workflows/market_runner.yml`. Live Web Workbench with dynamic ConfigBus profiles and epochs, token auth, SSE telemetry, Cloudflare Quick Tunnel, and Raspberry Pi systemd daemon.

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| 1 | `MarketDataProvider.get_quote` | Implement live & synthetic quote getter for execution tickers | M1 | Survey 3 / R3 |
| 2 | Live ETP Pricing in Position Sizing | Query `card.trading212_ticker` quote & currency before calculating `position_size_shares` | M1 | Survey 3 / R3 |
| 3 | LSE GBX Pence Normalization | Divide GBX pence by 100 to GBP and convert to base EUR; enforce 20% equity cap | M1 | Survey 3 / R3 |
| 4 | `3GISl_EQ` Sizing Verification | Verify 3GISl_EQ (7805p) sizes to ~10 shares (~€940) rather than 24 shares (~€2,256) | M1 | Survey 3 / R3 |
| 5 | Live Broker Equity in Dashboard | Replace synthetic $10k with `client.get_account_cash().total` (€4,923.05) & update `€` UI | M1 | Survey 3 / R3 |
| 6 | Hermetic Test Suite Baseline | Fix `test_asymmetric_session.py` fixture isolation and keyword bug in `strategy_improvement_agent.py` | M1 | Survey 2 & 3 / R2 |
| 7 | Extended Technical Indicators | Add Donchian 20d, Bollinger Bands (%B, Bandwidth), Keltner Channels, and ATR Spike Ratio to `compute_indicators()` | M2 | Survey 1 / R1 |
| 8 | Market Regime Classifier | Implement ADX < 22 -> Chop, ADX >= 25 & 20 EMA > 50 EMA -> Trending, Vol > 65% / ATR Spike -> High-Vol | M2 | Survey 1 / R1 |
| 9 | Multi-Regime Signal Routing | Route Chop to 3-RSI Mean Reversion, Trending to Donchian/EMA Breakout, High-Vol to size cut / cash preservation | M2 | Survey 1 / R1 |
| 10 | Dynamic Profit-Taking Ratchets | Calibrate +1.5R (50%), +2.5R (25%), +3.5R (25%) profit milestones in config, risk manager, position monitor | M2 | Survey 1 / R1 |
| 11 | Progressive Trailing Stops | Lock in gains progressively at +1.5% (breakeven) and +3.0% (+1.5% profit cushion) unrealized profit | M2 | Survey 1 / R1 |
| 12 | Backtest Multi-Regime Simulation | Extend `BacktestEngine` with ADX, EMA, Donchian, Bollinger, and 3-regime execution logic | M3 | Survey 2 / R2 |
| 13 | Universe Backtesting Pipeline | Batch backtest across 120-asset 3x universe (`CURATED_3X_PAIRS`) using daily candles | M3 | Survey 2 / R2 |
| 14 | Quantitative Edge Verification | Implement `meets_benchmark_targets` enforcing Sharpe >= 1.5, Profit Factor >= 1.75, Win Rate >= 60% | M3 | Survey 2 / R2 |
| 15 | 100% Zero-Regression Test Suite | Ensure 192+ unit tests pass with zero errors | M3 | Survey 2 / R2 |
| 16 | E2E Test Suite Pass (Tiers 1-4) | Pass 100% of opaque-box requirement-driven E2E tests published by testing track | M4 | Project Pattern / R1-R3 |
| 17 | Adversarial Coverage Hardening | Tier 5 white-box stress testing and edge-case bug hunting | M4 | Project Pattern |
| 18 | Raspberry Pi Deployment Sync | Verify deployment scripts and environment sync to Raspberry Pi service | M4 | Survey 3 / R3 |
| 19 | Pure 100% Trailing Stop Loss Engine | Replace partial trims with 100% position retention, progressive breakeven (+1.5%), cushion (+3.0%), dynamic ATR trail, and native T212 GTC stop synchronization | M4 | User Directive |
| 20 | Backtest Pure TSL Modernization | Modernize BacktestEngine with pure TSL simulation; verify statistical edge (Sharpe 3.40, PF 6.86, Win Rate 75%) | M4 | R2 / Backtest |
| 21 | Pluggable Strategy Engine | Abstract `BaseStrategy` Protocol & `StrategyRegistry` for decoupled strategy isolation | M5 | User Directive |
| 22 | Directional Trend Strategy | DMI (+DI/-DI) polarity gating, 20/50 EMA slope hierarchy, and MACD divergence gating | M5 | User Directive |
| 23 | Dual-Track Bear Execution | Short inverse 3x ETPs on indices (`QQQS`, `3USS`, `3SES`), 100% Cash Defense for single equities | M5 | User Directive |
| 24 | Pre-Flight Order Queue Reconciliation | Audit resting broker orders, purge stale market orders (>300s or closed market), deduplicate in-flight orders | M5 | User Directive |
| 25 | Sole Strategy Standardization | Excise 3-RSI mean reversion and legacy momentum heuristics; standardize active strategy on Directional Trend | M5 | User Directive |
| 26 | Multi-Provider Waterfall Router | Thread-safe sliding rate-limiting and auto-cooldown across FMP, Tiingo, Polygon.io, Alpha Vantage, Yahoo, Stooq | M6 | System Scaling |
| 27 | Dynamic ConfigBus & Epoch Tracking | Pinned epoch snapshots (`config_epoch_id`), built-in profiles, and live parameter calibration | M6 | Control Plane |
| 28 | Web Workbench & API Auth | Browser workbench UI (`/workbench`), bearer token authentication, SSE 1-second telemetry push | M6 | UI/UX |
| 29 | 1-Click Cloud Runner & Cloudflare | GitHub Actions automated market hours live runner with Cloudflare Quick Tunnel and atomic state persistence | M6 | Cloud Infra |
| 30 | 566 Automated Test Verification | 100% passing across 566 unit and integration test assertions with zero regressions | M6 | Quality Assurance |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| M1 | Architectural Discrepancy & Currency Scale Patching | Features 1-6: Live ETP quote retrieval, GBX pence normalization, 20% cap enforcement, dashboard broker cash integration (€4,923.05), and hermetic test fixture fixes | none | COMPLETED |
| M2 | Multi-Regime Strategy Ensemble & Dynamic Ratchets | Features 7-11: Indicators (Donchian, Bollinger, ATR spikes), 3-regime classifier (Chop, Trending, High-Vol), strategy routing (3-RSI vs Breakout), and progressive trailing stops | M1 | COMPLETED |
| M3 | Quantitative Backtest Engine & Walk-Forward Validation | Features 12-15, 20: BacktestEngine pure TSL & multi-regime simulation, 120-asset 3x universe evaluation, statistical edge verification (Sharpe >= 1.5, PF >= 1.75, Win Rate >= 60%), 405 unit test zero regression | M2 | COMPLETED |
| M4 | Final Milestone: 100% E2E Test Pass, Pure TSL & Pi Deployment | Features 16-19: Pass all 201 E2E tests (Tiers 1-4), 100% Pure TSL engine activation, broker order contention elimination, and 24/7 Raspberry Pi deployment | M3, TEST_READY.md | COMPLETED |
| M5 | Isolated Strategy Engine & Directional Trend Overhaul | Features 21-25: Pluggable strategy architecture, DirectionalTrendStrategy with DMI polarity, dual-track bear shorting, pre-flight order queue reconciliation, full test pass | M4 | COMPLETED |
| M6 | Multi-Provider Market Data, ConfigBus & Cloud Control Plane | Features 26-30: Multi-provider waterfall, dynamic ConfigBus & profiles, Web Workbench, GitHub Actions 1-click cloud runner, 566 tests 100% pass | M5 | COMPLETED |

## Interface Contracts

### 1. `MarketDataProvider.get_quote` ↔ `RiskManager` / `Orchestrator`
- **Method**: `MarketDataProvider.get_quote(symbol: str) -> dict`
- **Returns**: `{"symbol": str, "price": float, "currency": str, "bid": float, "ask": float, "timestamp": str}`
- **Fallback**: If offline/sandboxed, generates deterministic quote consistent with candle close.

### 2. Currency Normalization ↔ Position Sizing
- **Helper**: `LeveragedUniverse.normalize_quote_to_eur(ticker: str, price: float, currency: str) -> float`
- **Rule**: If `currency == "GBX"` or `ticker in GBX_TICKERS` or `(currency == "GBP" and price > 500)`: price is in pence, divide by 100 to obtain GBP. Convert GBP to EUR using benchmark rate (1.204) or live FX.
- **Cap**: Position notional `shares * price_eur` $\le 0.20 \times \text{account\_equity\_eur}$.
- **Verification**: `3GISl_EQ` with quote 7805p on €4,923.05 equity sizes to exactly 10 shares (~€940).

### 3. Multi-Provider Router Waterfall Contract
- **Class**: `MultiProviderRouter` (`trading_system/data/multi_provider.py`)
- **Waterfall**: `FMP` $\to$ `Tiingo` $\to$ `Polygon.io` $\to$ `Alpha Vantage` $\to$ `Yahoo Direct Chart` $\to$ `Stooq CSV` $\to$ `yfinance`.
- **Rate-Limiter**: Sliding window tracking timestamps; automatic 60s–120s cooldown upon HTTP 429 without failing caller queries.

### 4. 100% Pure Trailing Stop Loss Engine & Broker Synchronization
- **Complete Elimination of Take Profit Targets (TP1 / TP2 / TP3)**: Static profit targets and partial quantity trims (50%/25%/25%) are fully excised. 100% of shares ride the trend intact without artificial profit caps.
- **Progressive Ratchet Rules**:
  - At +1.5% unrealized profit: move Stop Loss to Entry Price (Breakeven Lock — risk-free trade).
  - At +3.0% unrealized profit: move Stop Loss to Entry Price + 1.5% (Lock Profit Cushion — guaranteed net gain).
  - Above +3.0% unrealized profit: dynamic ATR Chandelier trail follows peak price ($P_{\text{peak}} - 1.2 \times \text{ATR}$).
- **Single GTC Stop Protection**: Eliminates broker order contention (`400 Bad Request: 'Selling more equities than owned'`). 100% of shares are protected under a single server-side GTC Stop on Trading 212.
- **Market Opening Auction Buffer & Drift Gates**: 15-minute execution buffer on market open (08:00–08:15 UTC for EU/LSE, 14:30–14:45 UTC for US), pre-trade spread gate (<1.00%), and overnight price drift gate (<1.50%).
- **Asymmetric Session Isolation**: Intraday shorts flatten at 16:20 UTC (Winter Time / GMT aware); longs with locked breakeven stops carry overnight.

### 5. Backtest Benchmark Targets
- **Method**: `BacktestResult.meets_benchmark_targets() -> tuple[bool, dict]`
- **Targets**: `sharpe_ratio >= 1.5`, `profit_factor >= 1.75`, `win_rate >= 60.0`.
- **Verified Benchmark Performance (Multi-Factor Ensemble with Pure TSL)**:
  - Sharpe Ratio: **3.40** (Target: $\ge 1.5$)
  - Profit Factor: **6.86** (Target: $\ge 1.75$)
  - Win Rate: **75.0%** (Target: $\ge 60.0\%$)

## Code Layout
- `trading_system/data/multi_provider.py`: Thread-safe multi-provider waterfall with sliding-window rate limiters.
- `trading_system/data/trading212_client.py`: `MarketDataProvider` indicators and quote methods.
- `trading_system/data/leveraged_universe.py`: Curated 3x/5x universe (120+ pairs), currency metadata, and normalization helpers.
- `trading_system/screener/laya_screener.py`: Screener regime classification, candidate routing, and funnel attribution.
- `trading_system/strategies/`: Pluggable strategy engine (`base.py`, `registry.py`, `directional_trend.py`, `vwap.py`).
- `trading_system/agents/technical_agent.py`: Technical analysis regime detection and strategy recommendations.
- `trading_system/agents/risk_manager.py`: Position sizing with live ETP quote, GBX normalization, 20% cap, directional safety gate.
- `trading_system/orchestrator.py`: Deterministic 5-step pipeline coordination, pre-flight order queue reconciliation, live quote passing, order execution.
- `trading_system/execution/position_monitor.py`: Pure 100% Trailing Stop Loss engine, progressive ratchets (+1.5% BE, +3.0% Cushion, ATR trail), native T212 GTC stop synchronization.
- `trading_system/backtest/engine.py`: Multi-regime backtester, universe batch simulation, benchmark edge verification with Pure TSL.
- `trading_system/config_bus.py`: Dynamic configuration snapshots, epoch tracking, built-in profiles.
- `trading_system/dashboard/server.py`: Embedded web workbench, live broker equity anchoring (€4,923.05), EUR dashboard display, and real-time SSE telemetry.
- `tests/`: Complete unit and opaque-box E2E suites containing **566 automated tests (100% passing)**.
