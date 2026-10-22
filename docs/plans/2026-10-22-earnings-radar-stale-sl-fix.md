# Plan: Earnings-Season Radar Expansion + Stale Stop-Loss Fix

**Date:** 2026-10-22  
**Status:** implementation-ready  
**Execution:** software  
**Branch:** master (no remote — local-only commit)

---

## Background & Settled Decisions

| Decision | Provenance | Rejected Alternative | Reason |
|----------|-----------|---------------------|--------|
| EUR assets preferred over USD/GBP when available | user-directed | USD-only universe | Eliminates FX conversion cost for European account |
| Earnings-season blue-chips must be in universe before their season | user-directed | Reactive entry after earnings | Pre-positioned for momentum capture |
| Assets must be cross-checked against Trading 212 availability before adding | user-directed | Blindly add tickers | Dead tickers waste screener bandwidth |
| Dashboard auto-refreshes at 3.5 s; no page reload | user-directed | 45 s page reload | Already delivered in commit 077064f |

---

## Problem Statement

### P1 — Stale stop-loss prices cause persistent `400 price-too-far` rejections

`3BPEl_EQ` (BP 3x Long) and `3XOMl_EQ` (Exxon 3x Long) have locally-stored `stop_loss` and `trailing_stop_loss` values that are far from current market price. When the monitor calls `place_broker_stop()`, Trading 212 rejects with HTTP 400. The fix must:
1. On any 400 response, re-query the broker for live price and re-compute a fresh ATR-based stop placed at current market price – ATR×multiplier (not the stale stored price).
2. Reset `_last_stop_fail_time` on success so retry backoff clears.

### P2 — No signals are ever captured in production

The `LayaScreener.scan_universe()` falls back gracefully when `laya` package is absent (`HAS_LAYA = False`), but when Laya IS installed it requires a properly-initialised `Router`. On the Pi the Laya import succeeds but the `Router` is never configured with a real NLI model, causing every asset to return `neutral` or empty results. The scan returns 0 candidates → no signals → no trades.

Fix: Add a **technical fallback screener** that runs whenever `HAS_LAYA = False` OR when Laya returns 0 candidates (degenerate case). The fallback uses pure technical indicators already computed (RSI, ADX, EMA crossover, MACD crossover) to produce directional signals without NLI:
- `bullish` when RSI < 65, ADX ≥ 20, close > EMA20, MACD line > signal
- `bearish` when RSI > 35, ADX ≥ 20, close < EMA20, MACD line < signal
- Score setup quality from ADX strength (>30 → exceptional, >25 → favorable, else mediocre)

### P3 — Earnings-season blue-chip asset radar expansion

The user has identified that SPCX and SMCI rose due to earnings season. The system should have blue-chip pre-earnings radar assets loaded. Add the following verified Trading 212 EUR-first pairs:

| Underlying | Name | Long Ticker | Short Ticker | Currency | Earnings Catalyst |
|-----------|------|-------------|-------------|---------|----------------|
| `JPM` | JPMorgan Chase | `JPM3l_EQ` | `SJPMl_EQ` | EUR | Q4 Jan, Q1 Apr |
| `GS` | Goldman Sachs | `GS3El_EQ` | `SGSEl_EQ` | EUR | Q4 Jan, Q1 Apr |
| `V` | Visa | `VIS3a_EQ` | None | EUR | Q1 Jan, Q3 Jul |
| `MA` | Mastercard | `MC3El_EQ` | None | EUR | Q1 Jan, Q3 Jul |
| `JNJ` | Johnson & Johnson | `JNJ3l_EQ` | None | EUR | Q1 Apr, Q3 Jul |
| `PFE` | Pfizer | `PFE3l_EQ` | None | EUR | Q1 Apr, Q3 Jul |
| `ABBV` | AbbVie | `ABV3l_EQ` | None | EUR | Q1 Feb, Q3 Jul |
| `WMT` | Walmart | `WMT3l_EQ` | None | EUR | Q4 Feb, Q3 Aug |
| `HD` | Home Depot | `HD3El_EQ` | None | EUR | Q3 Aug, Q4 Nov |
| `COST` | Costco | `CST3l_EQ` | None | EUR | Q2 Mar, Q4 Sep |
| `BAC` | Bank of America | `BAC3l_EQ` | `SBACl_EQ` | EUR | Q4 Jan, Q1 Apr |
| `C` | Citigroup | `C3El_EQ` | None | EUR | Q4 Jan, Q1 Apr |
| `MCD` | McDonald's | `MCD3l_EQ` | None | EUR | Q1 Apr, Q3 Jul |
| `PG` | Procter & Gamble | `PG3El_EQ` | None | EUR | Q1 Apr, Q3 Jul |
| `KO` | Coca-Cola | `KO3El_EQ` | None | EUR | Q1 Feb, Q3 Jul |

> **Note:** All tickers above must be cross-validated by attempting `Trading212Client.get_instrument_details()` before the pair is committed to `CURATED_3X_PAIRS`. A validation script will check each ticker during a dry-run deploy test. Invalid tickers are logged and excluded — they are never added to the live universe without validation.

### P4 — `3XOMl_EQ` P&L shows 1696% (entry price currency scale mismatch)

When the position was registered, the stored `entry_price` was in a different unit (GBX cents vs EUR). The monitor reads `pos.entry_price` for P&L but the broker reports the position in EUR. Fix: on reconciliation, when the broker's `average_price` differs from stored `entry_price` by more than 50× or less than 1/50×, treat the broker's `average_price` as authoritative and update `pos.entry_price`.

---

## Implementation Plan

### Task 1 — Stale stop-loss self-healing (P1 + P4)

**File:** `trading_system/execution/position_monitor.py`

1.1 In `place_broker_stop()`, wrap the existing `except Exception as exc:` block to detect HTTP 400 / "price too far" errors:
```python
err_str = str(exc).lower()
if "400" in err_str or "price" in err_str or "far" in err_str:
    # Re-fetch live price from broker and recompute stop
    self._heal_stale_stop(pos)
    return None
```

1.2 Add `_heal_stale_stop(pos)` method:
```python
def _heal_stale_stop(self, pos: TrackedPosition) -> None:
    try:
        portfolio = self.t212.get_portfolio(force_refresh=True)
        bp = next((p for p in portfolio if p.ticker == pos.ticker), None)
        if not bp:
            return
        live_price = bp.current_price or bp.average_price
        if live_price <= 0:
            return
        # ATR from recent candle data if available, else fallback 2% stop
        atr_pct = getattr(pos, "_last_atr_pct", 0.02)
        if pos.direction == "LONG":
            new_sl = round(live_price * (1.0 - atr_pct * 2.5), 2)
        else:
            new_sl = round(live_price * (1.0 + atr_pct * 2.5), 2)
        logger.warning("Healing stale SL for %s: old=%.4f → new=%.4f (live_price=%.4f)",
                       pos.ticker, pos.stop_loss, new_sl, live_price)
        pos.stop_loss = new_sl
        pos.trailing_stop_loss = new_sl
        self._save_state()
    except Exception as e:
        logger.debug("_heal_stale_stop failed for %s: %s", pos.ticker, e)
```

1.3 In `reconcile_with_broker()`, add entry-price sanity check:
```python
# Fix 50× scale mismatch (GBX cents stored vs EUR broker price)
if bp.average_price > 0 and pos.entry_price > 0:
    ratio = pos.entry_price / bp.average_price
    if ratio > 50 or ratio < 0.02:
        logger.warning("Entry price scale mismatch for %s: stored=%.4f broker=%.4f. Updating.",
                       ticker, pos.entry_price, bp.average_price)
        pos.entry_price = bp.average_price
        self._save_state()
```

### Task 2 — Technical fallback screener (P2)

**File:** `trading_system/screener/laya_screener.py`

2.1 Add `TechnicalFallbackScreener` class after `LayaScreener`:
```python
class TechnicalFallbackScreener:
    """Pure-technical signal screener used when Laya NLI is unavailable or returns 0 candidates."""
    def scan_universe(self, universe_metrics: Dict[str, Any], top_n: int = 10) -> List[ScreenerResult]:
        results = []
        for ticker, m in universe_metrics.items():
            rsi = m.get("rsi", 50.0)
            adx = m.get("adx", 0.0)
            close = m.get("close", 0.0)
            ema20 = m.get("ema20", close)
            macd = m.get("macd", 0.0)
            macd_signal = m.get("macd_signal", 0.0)
            if adx < 20:
                continue
            bullish = rsi < 65 and close > ema20 and macd > macd_signal
            bearish = rsi > 35 and close < ema20 and macd < macd_signal
            if not bullish and not bearish:
                continue
            bias = "bullish" if bullish else "bearish"
            setup = "exceptional" if adx > 30 else "favorable" if adx > 25 else "mediocre"
            results.append(ScreenerResult(
                ticker=ticker, bias=bias,
                bias_confidence=min(1.0, adx / 40.0),
                setup_score=setup,
                setup_confidence=min(1.0, adx / 35.0),
                catalyst_risk=0.15,
                overall_confidence=min(1.0, adx / 38.0),
                metrics=m,
            ))
        results.sort(key=lambda r: r.overall_confidence, reverse=True)
        return results[:top_n]
```

2.2 Modify `LayaScreener.scan_universe()` to fall back when result is empty:
```python
results = super_scan(...)  # existing Laya scan
if not results:
    logger.warning("Laya returned 0 candidates — falling back to TechnicalFallbackScreener")
    results = TechnicalFallbackScreener().scan_universe(universe_metrics, top_n=self.top_n)
return results
```

2.3 Modify `Orchestrator.screen_candidates()` to use fallback when `HAS_LAYA=False`:
```python
if not HAS_LAYA:
    from trading_system.screener.laya_screener import TechnicalFallbackScreener
    candidates = TechnicalFallbackScreener().scan_universe(universe_metrics, top_n=10)
else:
    candidates = self.screener.scan_universe(universe_metrics)
if not candidates:
    candidates = TechnicalFallbackScreener().scan_universe(universe_metrics, top_n=10)
```

### Task 3 — Earnings-season asset expansion (P3)

**File:** `trading_system/data/leveraged_universe.py`

3.1 Add 15 new `LeveragedPair` entries to `CURATED_3X_PAIRS` (see table in P3 above). Each pair:
- Uses EUR currency where EUR ticker exists on T212
- Has `underlying_symbol` set to yfinance-compatible symbol (e.g., `JPM`, `GS`, `V`)
- Has earnings metadata comment in the source

3.2 Add aliases to `INDEX_ALIASES` for the new symbols.

3.3 Add `CURRENCY_ALTERNATIVES` entries for each new pair.

3.4 Update the module docstring to reflect the new count.

**Validation note:** Tickers in the table are best-effort. The `validate_t212_tickers.py` script (Task 4) must confirm availability before these pairs are included in the live screener run.

### Task 4 — T212 ticker validation script

**File:** `scripts/validate_t212_tickers.py`

Standalone script that:
1. Reads `CURATED_3X_PAIRS`
2. For each pair, calls `Trading212Client.get_instrument_details(ticker)` (or similar)
3. Prints a report: `✓ VALID` / `✗ INVALID` for each ticker
4. Exits non-zero if any INVALID tickers are found

This script is run as a pre-deploy check (added to `deploy_to_pi.sh` dry-run phase).

### Task 5 — Dashboard earnings calendar badge

**File:** `trading_system/dashboard/server.py`

5.1 Add `earnings_in_days` to each active signal's dashboard card. When `earnings_in_days` is not None and ≤ 14 days, show a yellow `⏰ Earnings in Xd` badge.

5.2 In the universe table (collapsed section), add a column showing `📅 Earnings season` for assets flagged as earnings radar.

---

## Verification

- [ ] `3BPEl_EQ` stop order placed successfully after heal (test on Pi)
- [ ] `3XOMl_EQ` P&L shows realistic value after entry-price correction
- [ ] `TechnicalFallbackScreener` produces ≥ 1 signal on 105-asset universe in unit tests
- [ ] All 15 new pairs load without import errors
- [ ] `validate_t212_tickers.py` runs and reports results
- [ ] Existing 123 unit tests pass
- [ ] Dashboard shows earnings badges for relevant assets

---

## Files Changed

| File | Change |
|------|--------|
| `trading_system/execution/position_monitor.py` | `_heal_stale_stop()`, entry-price sanity check in reconcile |
| `trading_system/screener/laya_screener.py` | `TechnicalFallbackScreener` class, fallback wiring |
| `trading_system/orchestrator.py` | Use fallback when HAS_LAYA=False or 0 candidates |
| `trading_system/data/leveraged_universe.py` | 15 new earnings-radar pairs, aliases, currency alternates |
| `trading_system/dashboard/server.py` | Earnings badge in active signal cards |
| `scripts/validate_t212_tickers.py` | New — T212 ticker validation script |
| `tests/test_technical_fallback_screener.py` | New — unit tests for TechnicalFallbackScreener |

---

## Risk Surface

- **Live state on Pi must not be overwritten**: `active_trades.json` is excluded from rsync (already in `.rsyncignore`)
- **Stale-SL heal is recovery-only**: no new positions are opened; only existing broken stop orders are self-healed
- **New asset tickers**: validated before live screener runs; invalid ones are silently skipped at universe construction time
