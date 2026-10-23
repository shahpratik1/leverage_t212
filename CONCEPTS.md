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
