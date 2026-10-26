# Trading 212 Spot ETP Mechanics: Direction Resolution and P&L Dynamics

## Problem
A trade on `IONQ` was initiated with a strategy `SHORT` signal, but because `IONQ` had no inverse short ETP listed on Trading 212, the orchestrator fell back to `get_instrument_by_symbol("IONQ")`, which returned `IONQl1_EQ` ("Leverage Shares 3x Long IONQ ETP", a spot 3x Long security). The bot purchased the 3x Long ETP, recorded it in `active_trades.json` as `direction: "SHORT"`, and the dashboard computed P&L inversely:
$$\text{pnl\_amount} = (\text{entry} - \text{current\_price}) \times \text{quantity}$$
When the market price of `IONQl1_EQ` dropped from \$1.68 to \$1.66, the dashboard reported a false profit of +\$0.02 (+1.13%), while the broker actually held a financial loss of -\$0.02 (-1.19%). The user identified this severe discrepancy between the dashboard and Trading 212.

## Root Cause
1. **Trading 212 Invest Spot Mechanics**: All securities purchased on Trading 212 Invest (standard stocks, 3x Long ETPs, and Inverse Short ETPs) are spot assets held long ($Q > 0$). An inverse ETP's price inherently rises when the underlying falls and drops when the underlying rises. Manually inverting the P&L formula for `direction == "SHORT"` is doubly erroneous: for genuine inverse ETPs it turns gains into losses, and for long ETPs mislabeled as short it turns losses into gains.
2. **Orchestrator Fallback on Short Signals**: On Trading 212 Invest, direct stock short selling is impossible without an inverse ETP. When `get_execution_ticker(underlying, "SHORT")` returned `None`, the orchestrator fell back to `get_instrument_by_symbol(underlying)`, which picked the 3x Long ETP and executed a buy under the guise of a short trade.
3. **Missing Universe Metadata**: `CURRENCY_ALTERNATIVES` for `IONQ` lacked `IONQl1_EQ` (the USD-quoted listing), preventing bidirectional ETP-to-pair mapping and ticker recognition.

## Resolution
1. **Authoritative Direction Resolution**:
   - Added `get_etp_direction(etp_ticker: str) -> str` to `LeveragedUniverse` and as a module-level helper. It inspects curated 3x/5x pairs and `CURRENCY_ALTERNATIVES` to determine if a ticker is an inverse `SHORT` or a `LONG` instrument.
   - Added `IONQl1_EQ` to `CURRENCY_ALTERNATIVES["IONQ"]["USD"]` and `USD_TICKERS`.
2. **Orchestrator Short Safety Gates**:
   - In `orchestrator.py` (`deep_analyse`): If `direction in ("SHORT", "SELL", "BEARISH")` and no inverse short ETP is returned, reject the trade immediately with a warning instead of falling back to buying the long instrument.
   - Added pre-order execution gate: If `card.direction` is `SHORT` but `get_etp_direction(card.trading212_ticker)` is `LONG`, immediately abort execution and log a critical safety error.
3. **Unified Spot P&L Formula**:
   - Corrected `load_active_signals` and `build_dashboard_data` in `server.py` to use standard spot P&L for all held positions:
     $$\text{pnl\_amount} = (\text{current\_price} - \text{entry}) \times \text{remaining\_quantity}$$
     $$\text{pnl\_pct} = \frac{\text{current\_price} - \text{entry}}{\text{entry}} \times 100.0$$
4. **Auto-Healing on Startup & Reconciliation**:
   - In `PositionMonitor._load_state()` and `reconcile_with_broker()`, any position whose stored direction contradicts `get_etp_direction(ticker)` is auto-healed to the authoritative direction, stop-loss and take-profit targets are calibrated to the correct side of entry, and the state file is persisted.
