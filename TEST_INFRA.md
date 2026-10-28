# TEST_INFRA.md — Opaque-Box E2E Testing Infrastructure

## 1. Test Philosophy & Principles

The E2E Test Suite for the Trading 212 Quantitative Trading Alpha Overhaul & Systemic Architectural Harmonization is built on the following foundational tenets:

1. **Opaque-Box & Requirement-Driven**:
   - Tests evaluate system behavior strictly against specifications defined in `ORIGINAL_REQUEST.md` and `PROJECT.md`.
   - Zero reliance on internal private methods, monkey-patching of private attributes, or undocumented class internals.
   - Tests interact exclusively through public APIs, module-level contracts, command-line interfaces, and configuration endpoints.

2. **Authoritative Output Derivation**:
   - All expected outputs are derived from explicit mathematical definitions, regulatory constraints (UCITS/PRIIPs 20% cap), and documented interface contracts.
   - For example:
     - $7,805\text{p} = £78.05$. At EUR/GBP benchmark rate $1.204$, $£78.05 \times 1.204 = €93.97$.
     - On real broker equity of €4,923.05, the 20% max position cap is $0.20 \times €4,923.05 = €984.61$.
     - Expected shares: $\lfloor €984.61 / €93.97 \rfloor = 10\text{ shares}$ ($10 \times €93.97 = €939.70 \approx €940$).
     - The previous synthetic allocation sized to $24\text{ shares}$ ($24 \times €93.97 = €2,256$), which committed $45.8\%$ of account equity and violated the safety cap.

3. **Hermetic & Independent Execution**:
   - Every test case is completely self-contained, initializes its own inputs, cleans up any temporary state, and does not depend on execution order.
   - Safe execution in offline or sandboxed environments: network calls are routed through deterministic mock providers or synthetic fallback contracts without requiring live broker credentials or active internet.

4. **Tiered Quality Gates**:
   - **Tier 1 (Feature Coverage)**: Happy-path isolation testing verifying that each of the 18 features operates as specified ($\ge 5$ test cases per feature).
   - **Tier 2 (Boundary & Corner Cases)**: Edge conditions, empty inputs, extreme ATR/volatility spikes, micro-penny prices, and cap limits ($\ge 5$ test cases per feature).
   - **Tier 3 (Cross-Feature Combinations)**: Pairwise and multi-agent subsystem interactions (e.g. regime transition + profit ratchet + order sizing; high-vol size cut + breakout signal).
   - **Tier 4 (Real-World Application Scenarios)**: Realistic end-to-end trading workflows on 3x leveraged universe assets (`3GISl_EQ`, `3LVEl_EQ`, `QQQ3m_EQ`, etc.).

---

## 2. Test Architecture & Directory Layout

All E2E test files reside exclusively in `tests/e2e/`:

```
tests/e2e/
├── __init__.py
├── runner.py                          # Dedicated multi-tier test runner with reporting
├── test_tier1_feature_coverage.py     # Tier 1: Features 1–18 Happy-Path (>=5 tests per feature)
├── test_tier2_boundary_corner.py      # Tier 2: Features 1–18 Boundaries & Limits (>=5 tests per feature)
├── test_tier3_cross_feature.py        # Tier 3: Subsystem & Pairwise Interactions
└── test_tier4_real_world_scenarios.py  # Tier 4: Realistic 3x ETP Trading Workflows
run_e2e_tests.sh                       # Project-root execution script
```

---

## 3. Test Runner & Execution Commands

### Primary Runner (Shell Script):
```bash
./run_e2e_tests.sh
```

### Standard Python Unittest Discovery:
```bash
python3 -m unittest discover tests/e2e
```

### Detailed Tier-by-Tier Runner with Metrics:
```bash
python3 tests/e2e/runner.py --verbose
```

### Selective Tier Execution:
```bash
python3 -m unittest tests/e2e/test_tier1_feature_coverage.py
python3 -m unittest tests/e2e/test_tier2_boundary_corner.py
python3 -m unittest tests/e2e/test_tier3_cross_feature.py
python3 -m unittest tests/e2e/test_tier4_real_world_scenarios.py
```

---

## 4. Feature Inventory & Tier Mapping

All 18 features from `PROJECT.md` are mapped across Tiers 1–4:

| Feature # | Feature Name | Tier 1 (Happy Path) | Tier 2 (Boundaries) | Tier 3 (Cross-Feature) | Tier 4 (Real-World E2E) |
|---|---|---|---|---|---|
| **F01** | `MarketDataProvider.get_quote` | Quote retrieval for EUR, USD, GBX; required schema; deterministic fallback | Penny quotes, zero volume, wide spreads, rate limits, missing ticker | Quote feed into RiskManager & PositionMonitor | Real-time quote streaming during market open |
| **F02** | Live ETP Pricing in Sizing | Query `card.trading212_ticker` quote & currency before sizing | ETP vs underlying price divergence (10x ratio), micro-price, zero quote | Interaction with volatility sizing and conviction scores | Sizing 3x Long vs Short ETPs simultaneously |
| **F03** | LSE GBX Pence Normalization | `normalize_quote_to_eur` converts pence to GBP (/100) and EUR | Boundary pence values (0.5p, 100p, 500p cutoff, 100000p), negative | Sizing cap enforcement (shares * price_eur <= 0.20 * equity) | Mixed EUR/USD/GBX portfolio margin compliance |
| **F04** | `3GISl_EQ` Sizing Verification | 7805p on €4,923.05 sizes to ~10 shares (~€940) | Equity sensitivity (€1k, €4.9k, €10k, €100k), 1-share tolerance | 3GISl_EQ sizing under stop distance risk budget vs 20% cap | End-to-end 3GISl_EQ order generation & placement |
| **F05** | Live Broker Equity in Dashboard | Dashboard queries `client.get_account_cash().total` (€4,923.05) | Zero cash, missing cash fields, disconnected broker fallback | Trajectory sparkline anchoring with active trade PnL | Full SSE dashboard payload delivery |
| **F06** | Hermetic Test Suite Baseline | Fixture isolation, environment independence, clean state | Multi-run determinism, no leaked state across test files | Coexistence with active monitor threads | CI execution check without environment bleed |
| **F07** | Extended Technical Indicators | Donchian 20d, Bollinger Bands, Keltner, ATR Spike Ratio | Flat prices, NaN candles, zero volatility, sudden 100% price jump | Indicator pipeline feeding regime classifier & entry triggers | Streaming indicators on multi-month historical data |
| **F08** | Market Regime Classifier | ADX < 22 (Chop), ADX >= 25 & EMA20>50 (Trend), Vol>65% (High-Vol) | Boundary values (ADX 22.0, 24.9, EMA20==50, ATR ratio 1.50) | Regime state transitions across consecutive bars | Universe regime distribution across 120 pairs |
| **F09** | Multi-Regime Signal Routing | Chop -> 3-RSI; Trending -> Donchian/EMA Breakout; High-Vol -> Cut | Conflicting signals (RSI3<=28 during trend, breakout during high-vol) | Pipeline routing: Screener -> TechnicalAgent -> RiskManager | Multi-day trading cycle routing diverse assets |
| **F10** | Dynamic Profit Ratchets | +1.5R (50%), +2.5R (25%), +3.5R (25%) milestone targets | Micro-ATR distances, inverted stops, fractional share trims | Schmitt hysteresis and trailing stop coordination | Complete position lifecycle from entry to TP1->TP2->TP3 |
| **F11** | Progressive Trailing Stops | +1.5% unrealized -> Breakeven, +3.0% unrealized -> Entry + 1.5% | Whipsaw protection, threshold touch, immediate sharp reversal | Pre-TP1 progressive stops coordinating with ATR trailing | Intraday price simulation verifying profit lock |
| **F12** | Backtest Multi-Regime Sim | BacktestEngine simulates Chop, Trending, and High-Vol rules | Sparse candles, market price gaps, zero trading days | Commission, slippage, and dynamic ratchets in backtest | 1-year historical backtest on core 3x leveraged assets |
| **F13** | Universe Backtest Pipeline | Batch backtest across 120-asset 3x universe (`CURATED_3X_PAIRS`) | Corrupted candle data, missing symbols, mixed currencies | Universe portfolio equity curve and drawdown simulation | Walk-forward validation over 252 trading days |
| **F14** | Quantitative Edge Verification | `meets_benchmark_targets` (Sharpe >= 1.5, PF >= 1.75, WR >= 60%) | Boundary metrics (Sharpe 1.499 vs 1.500, PF 1.749 vs 1.750, WR 59.9%) | Edge metrics combined with max drawdown constraints | Statistical significance testing (Monte Carlo resamplings) |
| **F15** | Zero-Regression Test Suite | All existing unit test modules discoverable and executable | Isolation check: no global state leakage or unclosed sockets | Regression verification on core risk and order subsystems | Automated project test suite execution |
| **F16** | E2E Test Suite Pass (Tiers 1-4)| Self-verifying runner executes and produces structured reports | Runner error handling (timeouts, fatal exceptions, unhandled exits) | Dynamic milestone tracking reporting progress against M1–M4 | Comprehensive multi-tier test execution report |
| **F17** | Adversarial Hardening | Malformed quotes, negative prices, NaN indicators | Zero cash balance, flash crash 50% drop, broker API outage | Order bursts, concurrent trade validations, active trade locks | Black swan market crash simulation across leveraged ETPs |
| **F18** | Raspberry Pi Deployment Sync | Deploy script syntax (`deploy_to_pi.sh`), systemd unit structure | Host unreachable, SSH key missing, invalid port | Syncing `active_trades.json`, `.env`, and service restart | Simulated remote deployment pipeline execution |

---

## 5. Traceability Matrix

| Requirement | Acceptance Criteria | E2E Test Coverage |
|---|---|---|
| **R1. Multi-Regime Alpha** | Regime classification (Chop vs Trend vs High-Vol) | `test_tier1_feature_coverage.py` (F07–F09), `test_tier3_cross_feature.py` |
| | Dynamic ratchets (+1.5R, +2.5R, +3.5R) | `test_tier1_feature_coverage.py` (F10), `test_tier4_real_world_scenarios.py` |
| | Progressive trailing stops (+1.5%, +3.0%) | `test_tier1_feature_coverage.py` (F11), `test_tier3_cross_feature.py` |
| **R2. Backtesting & Edge** | Multi-regime historical simulation | `test_tier1_feature_coverage.py` (F12), `test_tier4_real_world_scenarios.py` |
| | 120-asset universe pipeline | `test_tier1_feature_coverage.py` (F13), `test_tier3_cross_feature.py` |
| | Benchmark edge targets (Sharpe $\ge 1.5$, PF $\ge 1.75$, WR $\ge 60\%$) | `test_tier1_feature_coverage.py` (F14), `test_tier2_boundary_corner.py` |
| | 100% zero-regression test suite | `test_tier1_feature_coverage.py` (F15), `test_tier3_cross_feature.py` |
| **R3. Architecture & Safety** | ETP live quote & currency query in sizing | `test_tier1_feature_coverage.py` (F01–F02), `test_tier2_boundary_corner.py` |
| | LSE GBX pence normalization (/100) & 20% cap | `test_tier1_feature_coverage.py` (F03), `test_tier2_boundary_corner.py` |
| | `3GISl_EQ` sizes to ~10 shares (~€940) | `test_tier1_feature_coverage.py` (F04), `test_tier4_real_world_scenarios.py` |
| | Dashboard broker cash (€4,923.05) & € display | `test_tier1_feature_coverage.py` (F05), `test_tier3_cross_feature.py` |
| | Raspberry Pi deployment synchronization | `test_tier1_feature_coverage.py` (F18), `test_tier2_boundary_corner.py` |
