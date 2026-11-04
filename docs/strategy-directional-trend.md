# Quantitative Strategy Guide: Directional Trend Strategy on 3x Leveraged ETPs

> **Mathematical Formulation, Polarity Gating, Multi-Timeframe Hierarchy, and Dual-Track Short Execution**

---

## 1. Executive Summary

The **Directional Trend Strategy** is an institutional quantitative model engineered specifically for daily-leveraged (3x) Exchange-Traded Products (ETPs) on Trading 212. It eliminates counter-trend knife-catching and volatility drag by requiring verified directional momentum before capital commitment.

The strategy resolves the root cause of historical drawdowns:
1. **No Counter-Trend Dip Buying**: Forbids buying oversold dips ($\text{RSI}_3 \le 28$) when the intermediate trend is down ($\text{EMA}_{20} < \text{EMA}_{50}$ or negative EMA slope).
2. **Directional Polarity Gating**: Uses the Directional Movement Index (+DI vs -DI) to confirm that directional momentum aligns with trade direction.
3. **Dual-Track Bear Regime Execution**:
   - **Broad Benchmark Indices** (`QQQ`, `SPY`, `SMH`): Executed as active `SHORT` entries via inverse 3x Short ETPs (`QQQS`, `3USS`, `3SES`).
   - **Single Equities** (`NVDA`, `TSLA`, `ABNB`): Routed to **100% Cash Defense** (`direction = "NEUTRAL"`), eliminating borrow fees and short squeeze risks.

---

## 2. Core Quantitative Criteria

| Component | Indicator / Parameter | Threshold / Rule | Rationale |
|---|---|---|---|
| **Macro Trend Alignment** | 200-day Simple Moving Average ($\text{SMA}_{200}$) | $\text{Price} > \text{SMA}_{200}$ for Longs | Secular bull market confirmation. |
| **Intermediate Trend Hierarchy** | Daily 20 EMA & 50 EMA | $\text{Price} > \text{EMA}_{50}$ & $\text{EMA}_{20} \ge \text{EMA}_{50}$ | Ensures multi-timeframe alignment across daily bars. |
| **EMA Slope Confirmation** | 5-day EMA 20 Slope ($\text{slope}_{20}$) | $\text{slope}_{20} > 0.0\%$ | Verifies positive directional velocity, rejecting falling knives. |
| **Directional Polarity** | Directional Movement Index ($\text{DMI}_{14}$) | $+\text{DI} > -\text{DI}$ (Long) or $-\text{DI} > +\text{DI}$ (Short) | Confirms dominant buyers vs sellers over 14 bars. |
| **Trend Strength Gate** | Average Directional Index ($\text{ADX}_{14}$) | $\text{ADX} \ge 18.0$ | Filters out low-volatility chop to avoid leveraged beta decay. |
| **MACD Divergence Gate** | MACD Line & Signal Line | $\text{MACD} \ge \text{Signal}$ (Long) or $\text{MACD} \le \text{Signal}$ (Short) | Rejects entry on momentum divergence. |
| **Impending Catalyst Filter** | Earnings Blackout Proximity | $\text{Earnings} > 2.0\text{ days}$ | Rejects entries within 48h of binary earnings announcements. |

---

## 3. Dual-Track Bear Regime Architecture

```
                  ┌───────────────────────────────┐
                  │ Bearish Breakdown Detected    │
                  │ -DI > +DI and ADX >= 18.0     │
                  │ Price < EMA20 or Slope20 < 0  │
                  └──────────────┬────────────────┘
                                 │
                 Is asset a benchmark index?
                 (QQQ, SPY, SMH, ^NDX, ^GSPC)
                                 │
                   ┌─────────────┴─────────────┐
                  YES                          NO
                   │                           │
                   ▼                           ▼
        ┌─────────────────────┐    ┌─────────────────────┐
        │ Active Short ETP    │    │ 100% Cash Defense   │
        │ Execution           │    │ (direction=NEUTRAL) │
        │ (QQQS, 3USS, 3SES)  │    │ Preserves Capital   │
        └─────────────────────┘    └─────────────────────┘
```

### 3.1 Benchmark Index Shorting
Major indices exhibit broad liquidity, orderly trend trends during macro sell-offs, and deep inverse ETP liquidity (`QQQS`, `3USS`, `3SES`). Shorting is safe and profitable with tight Trailing Stop Loss protection.

### 3.2 Single Equity Cash Defense
Individual equities are prone to extreme short squeezes, earnings gaps, and unhedged headline volatility. During bear impulses, single equities are placed in 100% cash defense rather than shorted, preserving capital until a verified bullish impulse emerges.

---

## 4. Lifecycle & Exit Protocols

1. **Initial Protective Stop**: Placed at fill as a native server-side GTC stop on Trading 212 at $P_{\text{entry}} - 2.2 \times \text{ATR}_{14}$ (minimum $0.5\%$).
2. **Progressive Trailing Stops**:
   - $+1.5\%$ Unrealized Gain $\to$ Stop ratcheted to Breakeven ($P_{\text{entry}}$).
   - $+3.0\%$ Unrealized Gain $\to$ Stop ratcheted to $+1.5\%$ Cushion Lock.
   - Above $+3.0\%$ Gain $\to$ Dynamic ATR Chandelier trail follows trend peak.
3. **Session Close Protocol (16:20 UTC)**:
   - Inverse Short ETPs flatten 100% at 16:20 UTC to prevent overnight compounding drag.
   - Long positions with locked breakeven stops carry overnight.
