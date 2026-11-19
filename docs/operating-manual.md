# Operational Manual: Institutional Leveraged Execution & Control Plane

> **Standard Operating Procedures (SOP)** for live broker execution, multi-provider market data routing, 100% Pure Trailing Stop Loss management, Web Workbench control plane, and cloud/edge production runners.

---

## 1. Trading 212 API Configuration & Authentication

### 1.1 Environment Setup
The trading engine communicates with Trading 212's REST API. Configure credentials in your root `.env` file:

```ini
# ── Trading 212 Broker Credentials ──
TRADING212_API_KEY=your_trading212_api_key_here
TRADING212_API_SECRET=your_trading212_api_secret_here
USE_LIVE=false                     # 'false' for Practice/Demo account; 'true' for Real Money live trading

# ── Quantitative Risk Management ──
DEFAULT_ACCOUNT_EQUITY=10000.0     # Baseline equity fallback if broker API offline
MAX_RISK_PER_TRADE=0.005           # 0.5% max capital risk per trade
MAX_POSITION_PCT=0.20              # 20.0% max portfolio exposure per position
MAX_PORTFOLIO_BETA_HEAT=6.0        # Combined portfolio beta leverage limit
DAILY_MAX_DRAWDOWN_PCT=0.02        # 2.0% daily circuit breaker limit

# ── Session & Timing Protocols (UTC) ──
SESSION_CLOSE_CUTOFF_UTC=16:20     # 16:20 UTC Asymmetric Session Gap Evacuation
OPENING_AUCTION_BUFFER_MINUTES=15  # 15-minute opening auction spread settlement buffer
ENTRY_EARNINGS_BLACKOUT_DAYS=2.0   # 48-hour earnings blackout window
```

### 1.2 Required Broker API Key Scopes
When generating your API key in the Trading 212 app (`Settings > API`):
- **Orders**: `Read` & `Write` *(Required to place spot buy entries and native broker GTC stop-loss orders)*.
- **Metadata / Instruments**: `Read` *(Required to query ticker mappings, exchange hours, minimum order quantities, and currency codes)*.
- **Account Data / Portfolio**: `Read` *(Required to read live portfolio cash, unrealized P&L, and open positions)*.

### 1.3 Broker Quota Management & Rate Limiting
Trading 212 enforces rate limits across its REST API:
- **Order Placement**: Kept $\le 1$ order per second.
- **Position & Portfolio Inquiries**: Sliding rate-limiting managed by `Trading212Client` with persistence in `.t212_rate_limit.json`.
- **429 Handling**: Automatic interception of HTTP `429 Too Many Requests`, parsing `Retry-After` headers and applying jittered exponential backoff.
- **Pre-Flight Order Queue Reconciliation**: At the start of every cycle, `Orchestrator.reconcile_pending_orders()` audits resting broker orders. Stale market orders ($> 300\text{s}$ old or submitted while exchange is closed) are automatically cancelled, and duplicate entries for in-flight tickers are suppressed.

---

## 2. Multi-Provider Market Data Waterfall Configuration

The engine uses a thread-safe **MultiProviderRouter** waterfall with per-provider sliding-window rate limiters, auto-cooldown on rate limits, and keyless public fallback.

### 2.1 Provider Configuration (`.env`)
```ini
# Optional API keys for premium providers (with zero-config fallback to keyless feeds)
FMP_API_KEY=your_fmp_api_key           # Financial Modeling Prep (250 req/day free)
TIINGO_API_KEY=your_tiingo_api_key     # Tiingo (500 req/day, 50/hr free)
POLYGON_API_KEY=your_polygon_api_key   # Polygon.io (5 req/min free)
ALPHA_VANTAGE_API_KEY=your_av_api_key  # Alpha Vantage (5 req/min, 25/day free)
```

### 2.2 Waterfall Priority & Auto-Cooldown
1. **Tier 1 (Direct API Keys)**: Queries `FMP` $\to$ `Tiingo` $\to$ `Polygon.io` $\to$ `Alpha Vantage`.
2. **Rate Limit / 429 Trip**: If any provider exhausts its quota or encounters an HTTP 429, the router immediately places that provider into cooldown (60–120 seconds) and cascades seamlessly to the next tier without stalling the execution scan.
3. **Tier 2 (Keyless Direct Fallback)**: If API keys are absent or exhausted, the router seamlessly queries:
   - **Direct Yahoo Chart API** (fast JSON OHLCV)
   - **Stooq CSV Feed** (direct EOD/intraday historical bars)
   - **yfinance client** (resilient historical candle extraction)
4. **SQLite WAL Persistence**: Ingested candles are stored in `data/candles.db` using SQLite with Write-Ahead Logging (`PRAGMA journal_mode=WAL`). Incremental delta sync checks `MAX(timestamp)` and fetches only new bars, cutting network load by 90%+.

---

## 3. Hybrid Broker Execution & 100% Pure Trailing Stop Loss (TSL)

### 3.1 Order Contention Elimination: Why 0% TP Limits
On Trading 212 Invest accounts, submitting a Take Profit Limit order locks the underlying shares. Placing a concurrent Stop Loss order on the same shares results in `400 Bad Request: 'Selling more equities than owned'`. Furthermore, capping winners at arbitrary static multiples (+1.0R / +2.0R) prematurely curtails high-beta momentum trends.

The system enforces **100% Pure Trailing Stop Loss**:
- **Zero Take Profit Limit Orders**: No competing limit orders are ever placed.
- **100% Position Quantity Intact**: The entire position rides the trend to capture outlier right-tail distribution gains (+20% to +50%+).
- **Single Monotonic Server-Side GTC Stop**: A single native Stop Order (`POST /equity/orders/stop`) is registered on Trading 212 servers immediately upon entry fill, updating monotonically upward.

### 3.2 Progressive TSL Ratchet Lifecycle

```
Trade Entry Fill
     │
     ├──► 1. Immediate Native Broker GTC Stop Loss (POST /equity/orders/stop)
     │       └── Distance: Entry - 2.2 * ATR (clamped between 2.5% and 8.0%)
     │
     ├──► 2. Phase 1: Breakeven Lock (+1.5% Unrealized Gain)
     │       └── Stop ratcheted to Entry Price (Downside risk becomes 0.00%)
     │
     ├──► 3. Phase 2: Profit Cushion Lock (+3.0% Unrealized Gain)
     │       └── Stop ratcheted to Entry + 1.5% (Guarantees +1.5% net profit minimum)
     │
     ├──► 4. Phase 3: Dynamic ATR Chandelier Trail (> +3.0% Gain)
     │       └── Stop continuously tracks: P_peak - (1.2 * ATR)
     │       └── Stops only ratchet upward on new price highs; never loosen
     │
     ├──► 5. Session-Day Decay & Zero-Alpha Evacuation
     │       └── For stagnant trades: ATR distance tightens by 0.35x ATR per session
     │       └── Liquidates exposure after 4 stagnant sessions (Zero-Alpha exit)
     │       └── Tenured Runner Immunity: Positions >= +3.0% profit are exempt
     │
     └──► 6. Exit Execution
             └── 100% of position liquidated when price touches the tightened broker GTC stop
```

---

## 4. Leverage Governance: 3x vs 5x ETP Mechanics

### 4.1 European UCITS Regulatory Framework
Under European PRIIPs/UCITS rules:
- **Single-Stock ETPs (GraniteShares & Leverage Shares)**: Capped at **3x leverage** (e.g., `3LVEl_EQ` for NVIDIA, `3LTEl_EQ` for Tesla, `3APEl_EQ` for Apple).
- **Broad Market Indices & Commodities**: Institutional **5x leverage** is permitted on Trading 212 (e.g., `QQQ5l_EQ` for Nasdaq 100, `SP5Yl_EQ` for S&P 500, `GLD5l_EQ` for Gold, `TLT5l_EQ` for 20+ Year Treasuries).
- **5x Leverage Priority Policy**: When 5x leverage is available for an index or commodity asset, 5x leverage takes precedence over 3x leverage (`available_leverage=[5]`).

### 4.2 Spot ETP Mechanics (Zero Borrow Fees & Spot-Only Execution)
- On Trading 212 Invest, all orders are spot equity purchases ($Q > 0$).
- **Long Market Exposure**: Executed by buying spot 3x/5x Long ETPs (`QQQ3l_EQ`, `3USLl_EQ`, `3LVEl_EQ`).
- **Short Market Exposure**: Executed by buying spot 3x Inverse Short ETPs (`QQQSl_EQ`, `3USSl_EQ`, `3SESl_EQ`).
- **Zero CFD Financing Fees**: Because positions are spot shares, there are zero overnight borrow or swap fees (saving 8%–15% annualized drag).
- **Dual-Track Bear Shield**: Broad market indices are shorted via inverse ETPs during downtrends. Single tech equities route to **100% Cash Defense** to eliminate violent short-squeeze risk.

### 4.3 Volatility Decay Mathematics ($ADX \ge 18.0$)
Leveraged products rebalance daily, subjecting capital to compounding volatility drag ($-\frac{1}{2} L^2 \sigma^2$).
- In choppy sideways regimes ($\text{ADX} < 18.0$), the system strictly stays in **100% Cash Defense**.
- Entries require verified trend momentum ($\text{ADX} \ge 18.0$ with $+DI > -DI$ for longs and positive 20 EMA slope).

### 4.4 Currency Normalization (LSE Pence GBX to EUR/USD Base)
- LSE quotes in British Pence (`GBX` / `p`) are divided by 100 to convert to GBP, then normalized to account base currency (EUR) before calculating position sizing.
- Maximum notional position cap is enforced: $\text{Notional} \le 0.20 \times \text{Account Equity}$.

---

## 5. Risk Management: The 9 Non-Negotiable Safety Gates

Before any order is dispatched, `RiskManager` asserts 9 deterministic safety gates:

1. **Daily Drawdown Circuit Breaker (-2.0%)**: If portfolio equity drops $\ge 2.0\%$ from session start, system halts all new order submissions for 24 hours.
2. **15-Minute Market Opening Auction Settlement Buffer**: Blocks trade entry during 08:00–08:15 UTC (LSE/European open) and 14:30–14:45 UTC (US open) while opening auctions uncross and spreads normalize.
3. **Pre-Trade Bid-Ask Spread Gate (< 1.00%)**: Rejects execution if live broker bid-ask spread exceeds $1.00\%$, eliminating adverse fill slippage.
4. **Stale Overnight Price Drift Gate (< 1.50%)**: Validates live quote against candidate setup price. If price has drifted $> 1.50\%$, order is rejected. If $\le 1.50\%$, entry and stop levels are recalibrated to the live quote.
5. **Stop Invariant Protection Gate**: Strictly asserts $P_{\text{stop}} < P_{\text{bid}}$ for long spot holdings, preventing inverted stops from triggering instant market liquidations.
6. **Pre-Trade Earnings Blackout Gate (48 Hours)**: Rejects trades if company earnings are scheduled within $\le 48\text{ hours}$ (eliminates binary earnings-gap risk).
7. **Direction Integrity Safety Gate**: Validates that a `SHORT` signal maps to an inverse short ETP. Forbids buying a Long ETP on a Short signal. Rejects trade if no inverse ETP exists.
8. **Synthetic Market Data Gate**: Intercepts and blocks live broker execution if incoming price feeds were generated synthetically.
9. **16:20 UTC Asymmetric Session Gap Protocol**:
   - Liquidates all 3x Inverse Short ETPs at 16:20 UTC (10 minutes before LSE close) to prevent overnight compounding drag.
   - Liquidates any Long positions that have not reached Breakeven (+1.5% gain).
   - Long positions with locked Breakeven stops are permitted to carry overnight.

---

## 6. Web Workbench & Live Radar Operations

### 6.1 Accessing the Live Telemetry Server
The orchestrator hosts an embedded multi-threaded HTTP server on port 8080 (or 8888):
- **Live Workbench UI**: `http://localhost:8080/workbench`
- **Classic Signals Dashboard**: `http://localhost:8080/signals_dashboard.html`
- **Real-Time Telemetry Stream**: `GET /api/live` (1-second Server-Sent Events push)
- **Fast-Cached Data API**: `GET /api/data` (Active trades, broker balance, funnel attribution)

### 6.2 Token Authentication
When `DASHBOARD_AUTH_ENABLED=true` in `.env`:
- API and Workbench endpoints require bearer token authentication.
- Provide token via URL parameter (`?token=your_secret_token`) or HTTP header (`Authorization: Bearer your_secret_token`).
- Failed authentication attempts are rate-limited to prevent brute-force attacks.

### 6.3 Dynamic Configuration Bus (`ConfigBus`)
Operators can inspect and recalibrate parameters dynamically without restarting the engine:
- **Built-in Profiles**: Switch between `default`, `conservative`, `aggressive`, and `benchmark`.
- **Config Epochs**: Every parameter change increments `config_epoch_id`. In-flight positions maintain their entry epoch rules unless explicitly migrated.
- **Decision Funnel Attribution**: Visualizes candidates passing/failing each stage of the quantitative pipeline, with deep links to the responsible configuration keys.

---

## 7. Production Deployment Topologies

### Option A: 1-Click GitHub Actions Cloud Runner ⭐ *(Recommended)*
Run the entire engine in GitHub's cloud infrastructure during London Stock Exchange market hours:

1. In GitHub, navigate to **Actions** $\to$ **[Trading 212 Market Hours Live Runner](.github/workflows/market_runner.yml)**.
2. Click **Run workflow**.
3. Select parameters:
   - **Duration**: `270` minutes (Morning session) or `245` minutes (Afternoon session).
   - **USE_LIVE**: Set via repository secrets / inputs (`false` for Demo, `true` for Live).
4. View the run logs to locate your public **Cloudflare Live Dashboard URL** (`https://xxx.trycloudflare.com`) to monitor live trades securely from any device.

### Option B: 24/7 Raspberry Pi 5 Linux Daemon
Deploy to a dedicated local physical runner (e.g., Raspberry Pi 5 on Debian 13 arm64):

```bash
# 1. Automated deployment and service restart
./scripts/deploy_to_pi.sh

# 2. Check service status
ssh pi "sudo systemctl status trading-bot.service"

# 3. View live systemd logs
ssh pi "sudo journalctl -u trading-bot.service -f"

# 4. Fetch live state back to local workstation for offline audit
./scripts/fetch_live_state.sh
```

### Option C: Local CLI Runner
```bash
# Paper trading / dry-run mode
python3 -m trading_system.orchestrator --dry-run

# Live automated trading engine
python3 -m trading_system.orchestrator --interval 5 --monitor-interval 10 --dashboard-port 8080
```
