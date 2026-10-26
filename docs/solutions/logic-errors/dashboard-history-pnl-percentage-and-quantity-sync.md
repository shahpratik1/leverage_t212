---
title: Dashboard Trade History P&L Percentage and Quantity Synchronization
date: 2026-10-26
category: logic-errors
module: dashboard
problem_type: false_zero_percentage_and_hidden_quantity
component: trading_system/dashboard/server.py, signals_dashboard.html
symptoms:
  - "Recent Trade & Alert History displayed misleading (0.00%) for real exits with realized profit or loss"
  - "Quantity was completely hidden whenever P&L was displayed in the 'Quantity / P&L' column"
  - "BUY entry fills showed only quantity without distinguishing open entry from closed exits"
root_cause: null_pnl_pct_in_broker_fills_and_improper_fallback_in_dashboard_table_renderer
resolution_type: cost_basis_pnl_pct_calculation_and_combined_quantity_pnl_cell_layout
severity: medium
tags:
  - dashboard
  - trading212
  - pnl-calculation
  - table-rendering
---

# Dashboard Trade History P&L Percentage and Quantity Synchronization

## Problem Description
In the dashboard's `🔔 Recent Trade & Alert History` table, the column titled `Quantity / P&L` showed erroneous data:
1. **Misleading 0% Return**: Exits that had actual realized gains or losses (e.g., `-€2.00`, `-€2.78`, `-€3.14`) were displayed with `(0.00%)` (e.g., `-€2.00 (0.00%)`).
2. **Hidden Quantity**: The table cell evaluated `if (pnl !== null) { ... } else if (n.quantity) { ... }`. When P&L was present, the quantity of shares sold or stopped out was completely omitted from the cell, even though the column header explicitly stated `Quantity / P&L`.
3. **No Trade Status on Entries**: For BUY orders, there was no indication that it was an initial entry where P&L has not yet been realized.

## Root Cause
1. **Missing `pnl_pct` in API Mapping**: In `fetch_live_broker_notifications()`, `pnl_val` was extracted from Trading 212's `walletImpact.realisedProfitLoss`, but `"pnl_pct"` was hardcoded to `None`.
2. **Hardcoded Fallback in UI**: In both `signals_dashboard.html` and `server.py`, the JavaScript renderer contained:
   ```javascript
   (${pnlPct !== null ? (pnlPct >= 0 ? '+' : '') + pnlPct.toFixed(2) : '0.00'}%)
   ```
   Whenever `pnlPct` was null, it evaluated to `'0.00%'`, resulting in `-€2.00 (0.00%)`.

## Solution

### 1. Backend Cost-Basis P&L Percentage Calculation (`trading_system/dashboard/server.py`)
In `fetch_live_broker_notifications()`:
- Extracted `net_val = float(wallet_impact.get("netValue") or 0.0)` and `pnl_val = float(realized_pnl)`.
- Calculated original cost basis from net proceeds:
  $$\text{Cost Basis} = \text{Net Value} - \text{Realized P&L}$$
- Computed the exact return percentage:
  $$\text{PnL \%} = \frac{\text{Realized P&L}}{\text{Cost Basis}} \times 100\%$$
- Example: Sale net value €25.58 with -€2.00 loss $\implies$ Cost basis = €27.58 $\implies \text{PnL \%} = -7.25\%$ (instead of 0.00%).

### 2. Unified Two-Line Cell Layout (`signals_dashboard.html` & `server.py`)
Updated `renderAlertsTable()` to display both pieces of information:
- **Line 1**: Quantity (e.g. `1.00 shs` or `2.00 shs`) in clean, high-contrast monospace font.
- **Line 2**:
  - For Exits (SL / SELL / TP): Highlighted P&L amount and verified percentage (e.g. `<span class="text-rose-400 font-bold text-[11px]">-€2.00 (-7.25%)</span>`).
  - For Entries (BUY): `<span class="text-slate-500 text-[10px] font-sans">Open Entry</span>`.
- Added dynamic frontend fallback calculation so even if `pnl_pct` is missing in legacy rows, it is computed on the fly from `price`, `quantity`, and `pnl`, eliminating false 0% values permanently.

## Verification
1. `tests/test_embedded_dashboard.py`: Verified `pnl_pct` assertion evaluates to `-7.25%` on -€2.00 loss and `None` on BUY.
2. Full test suite: 183/183 tests passing.
3. Live Pi runner verification via `curl http://192.168.68.50:8080/api/data`:
   - `SL 3LFEl_EQ`: PnL: -€2.00 $\implies$ **-7.25%**
   - `SELL 3SEMd_EQ`: PnL: -€2.78 $\implies$ **-3.97%**
   - `SL 3LFEl_EQ`: PnL: -€3.14 $\implies$ **-5.51%**
   - `SELL SM3El_EQ`: PnL: -€0.31 $\implies$ **-3.21%**
   - `BUY IONQl1_EQ`: `1.00 shs` $\implies$ **Open Entry**
