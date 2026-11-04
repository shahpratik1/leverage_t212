# Concepts

> Shared domain vocabulary for this project — entities, named processes, and status concepts with project-specific meaning. Seeded with core domain vocabulary, then accretes as ce-compound and ce-compound-refresh process learnings; direct edits are fine. Glossary only, not a spec or catch-all.

## Trading System Domain

### Leveraged ETP
An exchange-traded product providing daily multiplied (e.g. 3x or 5x) or inverse (-1x, -3x) exposure to an underlying benchmark asset, rebalanced on a daily closing schedule.
*Avoid: Leveraged CFD, margin trade*

Leveraged ETPs trade as spot equities on European exchanges (e.g., LSE) in GBP, EUR, or USD. Long and inverse directions are both executed via spot buy orders, eliminating overnight CFD borrowing and financing fees.

### Mean Reversion
A quantitative trading strategy that identifies extreme short-term statistical overextension (e.g. Connors 3-RSI oversold pullbacks) within structural trends and trades expected snapbacks toward short-term moving averages.

Positions are held for short durations (typically 1 to 4 trading days) to capture prompt relief bounces while minimizing exposure to leveraged volatility drag.

### Volatility Drag
The mathematical compounding decay inherent to daily rebalanced leveraged instruments, where high volatility causes cumulative performance to lag arithmetic multiples of the underlying benchmark.
*Avoid: Beta slippage, decay fee*

Volatility drag is non-linear and escalates rapidly over multi-week holding periods in choppy sideways markets, necessitating strict maximum holding duration limits.

### Catastrophic Stop
An emergency stop-loss placed significantly outside normal market volatility noise (e.g. 3.5x to 5.0x ATR) designed solely to protect capital against regime shifts or gap openings, rather than exiting on ordinary pullbacks.
*Avoid: Operational stop, trailing stop*

Mean reversion strategies rely on indicator reversion (e.g. price crossing the 5-period SMA) for ordinary exits; tight operational stops inadvertently choke valid dip-buying entries before the snapback materializes.

### Noise Buffer
An ATR multiplier (e.g. 2.0x–2.5x) applied to stop-loss placement on high-beta leveraged products to absorb intraday microstructure wicks and bid-ask spread noise without prematurely choking a mean-reversion trade.
*Avoid: Tight stop, scalping stop*

Calibrating the noise buffer to at least 2.2x ATR prevents false-positive exits during peak adverse excursion on 3x leveraged ETPs, allowing short-term relief rallies to develop.

### Schmitt Hysteresis Ratchet
A two-stage protective stop-loss protocol that moves the stop to break-even upon reaching the first take-profit milestone and elevates it to lock in gains only after market price escapes past a volatility dead-band.
*Avoid: Single-tick ratchet, instant break-even lock*

Because wide bid-ask spreads on leveraged vehicles cause immediate resting bid executions, holding the stop at entry until price clears the escape boundary prevents bid-ask bounce from terminating profitable trades prematurely.

### Asymmetric Session Protocol
An execution rule governing overnight risk where leveraged inverse short positions are systematically flattened prior to exchange close, while long positions require positive unrealized cushion to hold overnight.
*Avoid: Universal overnight hold, static session policy*

Inverse leveraged products experience heightened path-dependency and compounding decay during off-market gap openings; liquidating inverse exposure intraday isolates risk to active trading hours.

### Leverage-Scaled ATR Engine
A volatility normalization mechanism that scales underlying benchmark ATR percentage by the instrument leverage multiplier (3x or 5x) to compute true expected price volatility for leveraged ETPs.
*Avoid: Raw underlying stop, fixed percentage stop*

Because leveraged products experience amplified daily range, stop loss distance is calculated using the scaled volatility and clamped between safety thresholds (2.5% to 8.0%) to prevent false-positive noise stop-outs.

### Dual-Trigger Exit Protocol
An execution architecture that couples native broker server-side GTC protective stop and limit orders with algorithmic leading-indicator exits (e.g. Connors 3-RSI exhaustion, Nixtla 5-day forecast reversals).
*Avoid: Client-only stop, price-only limit*

Broker-side orders ensure capital protection persists across network drops and off-hours, while algorithmic signals capture peak momentum trims before price reversal degrades unrealized gains.

### Multi-Factor Forecast Trimming
An algorithmic exit mechanism that evaluates rolling zero-lookahead forward price projections (e.g. Nixtla 5-day Holt-Winters exponential smoothing forecasts) on profitable positions, executing a partial trim (e.g. 50%) when expected returns deteriorate sharply ahead of a market turning point.
*Avoid: Fixed target exit, static price limit*

By locking in accrued gains prior to projected negative momentum shifts, forecast trimming prevents sudden adverse swings from eroding leveraged gains.

### Micro-Runner Dynamic Host Discovery
An automated probing mechanism for deploying to physical micro-runners (e.g. Raspberry Pi) across local area networks subject to DHCP lease drift, sequentially testing candidate hostnames and active subnet IPs before initiating synchronization.
*Avoid: Hardcoded static IP, manual target entry*

### Session-Day Decay Trailing Stop Envelope
A multi-dimensional trailing stop loss protocol that tightens allowable ATR distance by a fixed daily decrement (e.g. 0.35x ATR per session) across completed exchange trading days on leveraged ETPs, forcing stagnant positions to Break-Even or liquidating them before daily rebalancing volatility drag (beta slippage) erodes capital.
*Avoid: Static percentage stop, perpetual holding stop*

### Zero-Alpha Evacuation
An automated risk exit triggered on stagnant leveraged positions that fail to achieve positive directional expansion after a defined holding duration (e.g. 4 market sessions), liquidating exposure at 16:20 UTC before market close to eliminate compounding beta decay.
*Avoid: Indefinite chop holding, passive drift*

### Tenured Runner Immunity
A risk management rule that exempts strongly trending, profitable positions (e.g. >= +3.0% unrealized gain with Cushion locked) from time-decay stop tightening, preserving full ATR trailing distance for multi-week momentum moves.
*Avoid: Universal decay choking, premature profit choking*

### Pluggable Strategy Engine
An architectural pattern decoupling quantitative signal generation, technical indicator computation, and setup qualification from the orchestrator, risk manager, and broker layers into self-contained strategy modules conforming to a unified `BaseStrategy` interface.
*Avoid: Monolithic indicator screening, tightly coupled strategy logic*

New quantitative strategies can be developed, backtested, and activated via configuration (`ACTIVE_STRATEGY`) without altering broker transport, position monitoring, or stop-loss governance.

### Directional Polarity Gating
A regime filtering protocol using Directional Movement Index ($+DI$ vs $-DI$) and intermediate moving average slope (e.g. Daily 20/50 EMA) to determine directional market regime before allowing dip-buying or breakout trades.
*Avoid: Non-directional ADX attribution, static 200 SMA dip-buying*

Forbids buying oversold dips when $-DI > +DI$ or when the 20 EMA slope is negative, preventing counter-trend knife catching during sustained intermediate sell-offs.

### Dual-Track Bear Regime Execution
A risk-managed short execution protocol that trades 3x inverse short ETPs (`QQQS`, `3USS`, `3SES`) exclusively on highly liquid benchmark indices during confirmed bear impulses ($-DI > +DI$, $ADX \ge 18$), while enforcing 100% Cash Defense for single equities.
*Avoid: Full equity shorting, unhedged bear drift*

By confining inverse short exposure to broad market indices, the portfolio captures downside liquidity while eliminating borrow costs and violent short-squeeze gap risks on individual high-beta stocks.

### Pre-Flight Order Queue Reconciliation
A cycle initialization safeguard that queries the broker's resting order book (`get_orders()`) before evaluating new signals, canceling stale market orders ($> 5$ minutes old or placed outside continuous market hours) and suppressing duplicate in-flight orders for the same instrument.
*Avoid: Blind order dispatch, open-loop execution*


