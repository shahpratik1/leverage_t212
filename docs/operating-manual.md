# Operational Manual: Trading 212 & Leveraged Execution

> **Standard Operating Procedures (SOP)** for live execution, broker quota management, leverage mechanics, and manual dashboard operations.

---

## 1. Trading 212 API Configuration

### 1.1 Environment Setup
The trading engine communicates with Trading 212's modern REST API. Configure credentials in your root `.env` file:

```env
# Trading 212 API Credentials
TRADING212_API_KEY=your_api_key_here
TRADING212_ENV=demo              # Use 'demo' for paper trading, 'live' for real capital
TRADING212_BASE_URL=https://demo.trading212.com/api/v0

# Risk Management Limits
ACCOUNT_RISK_PER_TRADE=0.005    # 0.5% max capital risk per trade
MAX_NOTIONAL_PER_TRADE=0.05     # 5.0% max portfolio exposure per position

# Asymmetric Session Cutoff (UTC)
SESSION_CLOSE_CUTOFF_UTC=16:20
ENFORCE_INVERSE_INTRADAY_ONLY=true
ENFORCE_LONG_BREAKEVEN_OVERNIGHT=true
EARNINGS_BLACKOUT_HOURS=48
```

### 1.2 Required API Key Permissions
When generating your API key in the Trading 212 app (`Settings > API`), enable the following scopes:
- **Orders**: `Read` & `Write` *(required to place spot buys and native stop-loss orders)*
- **Metadata / Instruments**: `Read` *(required to verify ticker symbols, exchange hours, and minimum quantities)*
- **Account Data**: `Read` *(required to read available cash and portfolio equity for sizing)*

### 1.3 Broker Quotas & Rate-Limiting Policy
Trading 212 enforces rate limits on the Invest/Stocks REST API:
- **Order Placement**: Keep request frequency $\le 1$ order per second.
- **Polling**: Poll active positions at intervals $\ge 10$ seconds.
- **Rate Limit Handling**: The `Trading212Client` class automatically intercepts HTTP `429 Too Many Requests` responses, parsing the `Retry-After` header and applying exponential backoff with jitter up to 3 retry attempts.

---

## 2. Order Architecture & OCO Emulation

### 2.1 The Trading 212 OCO Limitation
Trading 212 Invest accounts do not support simultaneous native **One-Cancels-the-Other (OCO)** orders on the same share quantity. Submitting a Limit Sell order locks the underlying shares, preventing the broker from accepting a concurrent Stop Loss order.

### 2.2 The Hybrid Execution Solution
To achieve institutional risk control on Trading 212, the engine employs a **Hybrid Protection Model**:

```
Trade Fill
    │
    ├──► 1. Broker Server: Native GTC Stop Loss (POST /equity/orders/stop)
    │       └── Unconditionally working 24/7 on broker servers (crash-proof)
    │
    └──► 2. Local Engine: Algorithmic TP1 / TP2 / TP3 Polling
            ├── On TP1 Hit (+1.0R): Market sell 50% shares ──► Cancel Stop ──► New Stop at Break-Even (Arm Schmitt Hysteresis)
            ├── On Hysteresis Escape (TP1 + 0.5 ATR): Elevate Stop to TP1 ──► Update Broker Stop
            ├── On TP2 Hit (+2.0R): Market sell 25% shares ──► Ratchet Trailing Stop (TSL)
            └── On TP3 Hit (+3.0R): Market sell remaining 25% runner
```

1. **Catastrophic Stop Protection**: Upon entry fill, the engine immediately submits a **native server-side Stop Sell order** to Trading 212. If the local computer loses power or internet connectivity, the position is guaranteed to be protected at the broker.
2. **Dynamic Profit Management**: Take Profit targets (TP1, TP2, TP3) are actively monitored by `PositionMonitor`. When a target is touched, a market sell is executed, the old broker stop is cancelled, and an updated break-even or ratcheted stop is posted.

---

## 3. Leverage Rules: 3x vs 5x Instruments

### 3.1 Single-Stock vs Index Leverage Constraints
Under European UCITS and PRIIPs regulatory frameworks:
- **Single-Stock ETPs (GraniteShares & Leverage Shares)**: Leverage is capped at **3x** (e.g. `3LNVl_EQ` for NVIDIA, `3LTSl_EQ` for Tesla, `3TSMl_EQ` for TSMC).
- **Broad Market Indices & Commodities**: Institutional **5x leverage** is available on Trading 212 (e.g. `QQQ5l_EQ` for Nasdaq 100, `SP5Yl_EQ` for S&P 500, `GLD5l_EQ` for Gold, `TLT5l_EQ` for 20+ Year Treasuries).

### 3.2 Volatility Drag Mathematics
Daily rebalanced leveraged ETPs suffer from path-dependent compounding decay formulated by Avellaneda & Zhang (2010):

$$\frac{L_t}{L_0} \approx \left(\frac{S_t}{S_0}\right)^\beta \cdot \exp\left( \frac{\beta - \beta^2}{2} \cdot V_t \right)$$

For $\beta = 3$:
- The drag coefficient is $\frac{3 - 9}{2} = -3.0 \cdot V_t$.
- For $\beta = 5$:
- The drag coefficient is $\frac{5 - 25}{2} = -10.0 \cdot V_t$ (more than **3.3x more volatile decay** than 3x).

**Operational Rule**: 5x instruments must only be traded with strict short-term holding caps ($\le 2$ days) during trending regimes ($\text{ADX} \ge 25$). Never hold 5x positions through choppy sideways consolidations.

---

## 4. 16:20 UTC Asymmetric Session Gap Protocol

### 4.1 The Exchange Time Mismatch
European ETPs on the London Stock Exchange close at **16:30 UTC**, while the underlying US benchmark equities continue trading in New York until **21:00 UTC**. Any open position held overnight carries 4.5 hours of unhedged price discovery plus overnight earnings/macro gap risk.

### 4.2 The Evacuation Rules (Executed daily at 16:20 UTC)
1. **3x Inverse / Short Positions**: **100% liquidated**. Inverse ETPs decay exponentially if held through opening gaps. All shorts are strictly intraday.
2. **3x Long Positions without TP1**: **100% liquidated**. If a position has not produced a $+1.0\text{R}$ cushion to move its stop to break-even before LSE close, it is closed at the market.
3. **3x Long Positions with TP1 Hit**: **Retained for overnight swing**. Because the stop is already moved to break-even, the worst-case outcome is a scratch trade.
4. **Earnings Blackout**: Any position where the underlying company reports earnings within 24 hours is liquidated at 16:20 UTC, regardless of profit.

---

## 5. Manual Execution SOP via Dashboard

When running in **Manual Execution Mode**, operators can execute high-conviction signals directly in the Trading 212 mobile or desktop app using the interactive dashboard (`http://localhost:8888`):

1. **Check Live Radar**:
   - Open `http://localhost:8888/signals_dashboard.html`.
   - Verify connection badge: `Trading 212 Demo: Connected`.
2. **Review Actionable Cards**:
   - Verify direction: `BUY (3x LONG)` or `SELL (3x SHORT)`.
   - Note the exact Trading 212 ticker (e.g. `QQQ3l_EQ` or `3TSMl_EQ`).
3. **Copy Order Parameters**:
   - Click **Copy Order** on the card.
   - The formatted clipboard text contains:
     - Entry price
     - Recommended share quantity (scaled to 0.5% risk)
     - Initial Stop Loss price
     - Target levels (TP1, TP2, TP3)
4. **Execute in Broker**:
   - In Trading 212, search for the ETP ticker.
   - Submit a Market Buy for the specified share quantity.
   - Immediately submit a **Stop Sell Order** at the specified Stop Loss price.
5. **Manage Exits**:
   - When the dashboard signals `TP1 HIT`, sell 50% of shares in Trading 212 and edit the Stop Loss order price to match your Entry Price (Break-Even). The engine's Schmitt Hysteresis safely ratchets the stop to TP1 once price clears the escape threshold.
   - At `TP2 HIT`, sell 25% of shares and ratchet Trailing Stop to TP1. The remaining 25% runner rides to TP3.
