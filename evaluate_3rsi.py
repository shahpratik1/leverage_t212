"""Quantitative Evaluation & Backtest Harness for 3-RSI Mean Reversion Strategy.

Simulates walk-forward execution across the curated universe, evaluates Sharpe,
Profit Factor, Win Rate, and Drawdown, and outputs standardized JSON to stdout.
"""

from __future__ import annotations

import json
import math
import sys
from typing import Any, Dict, List

from trading_system.config import settings
from trading_system.data.leveraged_universe import LeveragedUniverse
from trading_system.data.trading212_client import MarketDataProvider


def compute_rsi(prices: List[float], period: int) -> List[float]:
    """Pure Python RSI calculation across price series."""
    if len(prices) <= period:
        return [50.0] * len(prices)

    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [abs(min(d, 0.0)) for d in deltas]

    rsi_series = [50.0] * len(prices)
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(prices)):
        gain = gains[i - 1]
        loss = losses[i - 1]
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

        if avg_loss == 0.0:
            rsi_series[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi_series[i] = round(100.0 - (100.0 / (1.0 + rs)), 2)

    return rsi_series


def compute_sma(prices: List[float], period: int) -> List[float]:
    """Simple moving average series."""
    sma = [0.0] * len(prices)
    for i in range(len(prices)):
        if i + 1 >= period:
            sma[i] = sum(prices[i + 1 - period : i + 1]) / period
        else:
            sma[i] = sum(prices[: i + 1]) / (i + 1)
    return sma


def compute_atr(candles: List[Dict[str, float]], period: int = 14) -> List[float]:
    """Average True Range series."""
    atr = [0.0] * len(candles)
    if not candles:
        return atr

    tr_list = []
    for i in range(len(candles)):
        c = candles[i]
        if i == 0:
            tr = c["high"] - c["low"]
        else:
            prev_c = candles[i - 1]["close"]
            tr = max(
                c["high"] - c["low"],
                abs(c["high"] - prev_c),
                abs(c["low"] - prev_c),
            )
        tr_list.append(tr)

    for i in range(len(candles)):
        if i + 1 >= period:
            atr[i] = sum(tr_list[i + 1 - period : i + 1]) / period
        else:
            atr[i] = sum(tr_list[: i + 1]) / (i + 1)
    return atr


def run_backtest() -> Dict[str, Any]:
    """Execute historical walk-forward simulation across universe."""
    universe = LeveragedUniverse()
    underlyings = universe.get_all_underlyings()

    trades = []
    worst_cases = []

    # Strategy Parameters directly from Settings
    rsi3_oversold = getattr(settings, "RSI3_OVERSOLD", 20.0)
    rsi3_overbought = getattr(settings, "RSI3_OVERBOUGHT", 80.0)
    rsi7_oversold = getattr(settings, "RSI7_OVERSOLD", 40.0)
    rsi7_overbought = getattr(settings, "RSI7_OVERBOUGHT", 60.0)
    max_hold_days = getattr(settings, "MR_MAX_HOLD_DAYS", 4)
    atr_sl_mult = getattr(settings, "ATR_SL_MULTIPLIER", 1.5)
    tp1_r_mult = getattr(settings, "TP1_R_MULTIPLE", 1.0)
    tp2_r_mult = getattr(settings, "TP2_R_MULTIPLE", 2.0)
    tp3_r_mult = getattr(settings, "TP3_R_MULTIPLE", 3.0)

    for sym in underlyings:
        candles = MarketDataProvider.get_daily_candles(sym, period="1y")
        if not candles or len(candles) < 60:
            continue

        prices = [c["close"] for c in candles]
        sma200 = compute_sma(prices, 200)
        sma5 = compute_sma(prices, 5)
        rsi3 = compute_rsi(prices, 3)
        rsi7 = compute_rsi(prices, 7)
        atr_series = compute_atr(candles, 14)

        # Walk through candles from index 30 to len(candles) - 5
        in_pos = False
        pos_entry_price = 0.0
        pos_entry_idx = 0
        pos_sl = 0.0
        pos_tp1 = 0.0
        pos_tp2 = 0.0
        pos_tp3 = 0.0
        pos_direction = "LONG"
        pos_tp1_hit = False
        pos_tp2_hit = False

        for i in range(30, len(candles) - 1):
            c = candles[i]
            cur_price = c["close"]
            cur_rsi3 = rsi3[i]
            cur_rsi7 = rsi7[i]
            cur_sma200 = sma200[i]
            cur_sma5 = sma5[i]
            cur_atr = atr_series[i]

            if not in_pos:
                # Check Bullish Dip setup
                if cur_price > cur_sma200 and cur_rsi3 <= rsi3_oversold and cur_rsi7 <= rsi7_oversold:
                    in_pos = True
                    pos_direction = "LONG"
                    pos_entry_price = cur_price
                    pos_entry_idx = i
                    sl_dist = max(atr_sl_mult * cur_atr, cur_price * 0.015)
                    pos_sl = cur_price - sl_dist
                    r = cur_price - pos_sl
                    pos_tp1 = cur_price + (tp1_r_mult * r)
                    pos_tp2 = cur_price + (tp2_r_mult * r)
                    pos_tp3 = cur_price + (tp3_r_mult * r)
                    pos_tp1_hit = False
                    pos_tp2_hit = False
                    continue

                # Check Bearish Overbought setup (Inverse Short)
                if cur_price < cur_sma200 and cur_rsi3 >= rsi3_overbought and cur_rsi7 >= rsi7_overbought:
                    in_pos = True
                    pos_direction = "SHORT"
                    pos_entry_price = cur_price
                    pos_entry_idx = i
                    sl_dist = max(atr_sl_mult * cur_atr, cur_price * 0.015)
                    pos_sl = cur_price + sl_dist
                    r = pos_sl - cur_price
                    pos_tp1 = cur_price - (tp1_r_mult * r)
                    pos_tp2 = cur_price - (tp2_r_mult * r)
                    pos_tp3 = cur_price - (tp3_r_mult * r)
                    pos_tp1_hit = False
                    pos_tp2_hit = False
                    continue

            else:
                hold_days = i - pos_entry_idx
                exit_price = None
                exit_reason = ""

                if pos_direction == "LONG":
                    # Check Stop Loss
                    if c["low"] <= pos_sl:
                        exit_price = pos_sl
                        exit_reason = "SL"
                    # Check TP1 ratchet
                    elif not pos_tp1_hit and c["high"] >= pos_tp1:
                        pos_tp1_hit = True
                        pos_sl = pos_entry_price  # Move to break-even
                    # Check TP2 ratchet
                    elif pos_tp1_hit and not pos_tp2_hit and c["high"] >= pos_tp2:
                        pos_tp2_hit = True
                        pos_sl = pos_tp1
                    # Check TP3 runner
                    elif pos_tp2_hit and c["high"] >= pos_tp3:
                        exit_price = pos_tp3
                        exit_reason = "TP3"
                    # Check SMA5 Mean Reversion Exit
                    elif cur_price >= cur_sma5 and hold_days >= 1:
                        exit_price = cur_price
                        exit_reason = "SMA5_REVERSION"
                    # Check Max Days Exit
                    elif hold_days >= max_hold_days:
                        exit_price = cur_price
                        exit_reason = "TIME_EXIT"

                    if exit_price is not None:
                        pnl_pct = ((exit_price - pos_entry_price) / pos_entry_price) * 100.0
                        trade_record = {
                            "symbol": sym,
                            "direction": "LONG",
                            "entry": pos_entry_price,
                            "exit": exit_price,
                            "hold_days": hold_days,
                            "pnl_pct": round(pnl_pct, 2),
                            "reason": exit_reason,
                        }
                        trades.append(trade_record)
                        if pnl_pct < 0:
                            worst_cases.append(trade_record)
                        in_pos = False

                elif pos_direction == "SHORT":
                    # Check Stop Loss
                    if c["high"] >= pos_sl:
                        exit_price = pos_sl
                        exit_reason = "SL"
                    # Check TP1 ratchet
                    elif not pos_tp1_hit and c["low"] <= pos_tp1:
                        pos_tp1_hit = True
                        pos_sl = pos_entry_price  # Move to break-even
                    # Check TP2 ratchet
                    elif pos_tp1_hit and not pos_tp2_hit and c["low"] <= pos_tp2:
                        pos_tp2_hit = True
                        pos_sl = pos_tp1
                    # Check TP3 runner
                    elif pos_tp2_hit and c["low"] <= pos_tp3:
                        exit_price = pos_tp3
                        exit_reason = "TP3"
                    # Check SMA5 Mean Reversion Exit
                    elif cur_price <= cur_sma5 and hold_days >= 1:
                        exit_price = cur_price
                        exit_reason = "SMA5_REVERSION"
                    # Check Max Days Exit
                    elif hold_days >= max_hold_days:
                        exit_price = cur_price
                        exit_reason = "TIME_EXIT"

                    if exit_price is not None:
                        pnl_pct = ((pos_entry_price - exit_price) / pos_entry_price) * 100.0
                        trade_record = {
                            "symbol": sym,
                            "direction": "SHORT",
                            "entry": pos_entry_price,
                            "exit": exit_price,
                            "hold_days": hold_days,
                            "pnl_pct": round(pnl_pct, 2),
                            "reason": exit_reason,
                        }
                        trades.append(trade_record)
                        if pnl_pct < 0:
                            worst_cases.append(trade_record)
                        in_pos = False

    total_trades = len(trades)
    if total_trades == 0:
        return {
            "sharpe_ratio": 0.0,
            "profit_factor": 0.0,
            "win_rate": 0.0,
            "max_drawdown_pct": 0.0,
            "total_trades": 0,
            "avg_trade_pnl_pct": 0.0,
            "avg_holding_days": 0.0,
            "total_pnl_usd": 0.0,
            "worst_cases": [],
        }

    wins = [t for t in trades if t["pnl_pct"] > 0]
    losses = [t for t in trades if t["pnl_pct"] <= 0]

    win_rate = round(len(wins) / total_trades, 3)
    gross_profit = sum(t["pnl_pct"] for t in wins)
    gross_loss = abs(sum(t["pnl_pct"] for t in losses))

    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else 10.0

    returns = [t["pnl_pct"] for t in trades]
    mean_ret = sum(returns) / total_trades
    variance = sum((r - mean_ret) ** 2 for r in returns) / max(1, total_trades - 1)
    std_ret = math.sqrt(variance) if variance > 0 else 0.001

    avg_hold = sum(t["hold_days"] for t in trades) / total_trades
    periods_per_year = 252.0 / max(1.0, avg_hold)
    sharpe_ratio = round((mean_ret / std_ret) * math.sqrt(periods_per_year), 2)

    # Compute Max Drawdown on equity curve
    eq = 100.0
    peak = 100.0
    max_dd = 0.0
    for r in returns:
        eq *= (1.0 + (r / 100.0))
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak
        if dd > max_dd:
            max_dd = dd

    max_drawdown_pct = round(max_dd, 3)
    worst_cases.sort(key=lambda x: x["pnl_pct"])

    return {
        "sharpe_ratio": sharpe_ratio,
        "profit_factor": profit_factor,
        "win_rate": win_rate,
        "max_drawdown_pct": max_drawdown_pct,
        "total_trades": total_trades,
        "avg_trade_pnl_pct": round(mean_ret, 2),
        "avg_holding_days": round(avg_hold, 1),
        "total_pnl_usd": round(eq - 100.0, 2),
        "worst_cases": worst_cases[:5],
    }


if __name__ == "__main__":
    result = run_backtest()
    print(json.dumps(result))
