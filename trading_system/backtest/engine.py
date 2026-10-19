"""Historical Backtesting Engine and Benchmark Simulator.

Replays historical daily OHLCV market data through Connors 3-RSI Mean Reversion
and ADX Momentum Breakout strategies, faithfully modeling 3x leveraged ETP execution,
volatility-targeted position sizing, and 3-tier profit-taking lifecycle exits.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class SimulatedTrade:
    """Represents a closed or open simulated trade during backtesting."""
    trade_id: int
    symbol: str
    direction: str  # 'LONG' or 'SHORT'
    strategy: str   # '3rsi_mean_reversion' or 'momentum'
    entry_date: str
    entry_price: float
    exit_date: Optional[str] = None
    exit_price: Optional[str] = None
    shares: float = 0.0
    notional: float = 0.0
    initial_sl: float = 0.0
    tp1: float = 0.0
    tp2: float = 0.0
    tp3: float = 0.0
    realized_pnl: float = 0.0
    return_pct: float = 0.0
    hold_days: int = 0
    exit_reason: str = ""  # 'SL', 'TP1_SL_BE', 'TP2_TSL', 'TP3', 'MAX_HOLD', 'END_OF_DATA'
    tp1_hit: bool = False
    tp2_hit: bool = False
    tp3_hit: bool = False


@dataclass
class BacktestResult:
    """Comprehensive performance statistics from a backtest run."""
    symbol: str
    start_date: str
    end_date: str
    initial_capital: float
    ending_capital: float
    total_return_pct: float
    benchmark_return_pct: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate_pct: float
    profit_factor: float
    max_drawdown_pct: float
    sharpe_ratio: float
    avg_trade_pnl: float
    avg_hold_days: float
    trades: List[SimulatedTrade] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["trades"] = [asdict(t) for t in self.trades]
        return d


class BacktestEngine:
    """Event-driven simulator for 3x leveraged trading strategies."""

    def __init__(
        self,
        initial_capital: float = 10000.0,
        leverage: int = 3,
        max_risk_per_trade: float = 0.005,
        max_position_pct: float = 0.05,
        target_vol: float = 0.20,
        max_hold_days: int = 4,
    ):
        self.initial_capital = initial_capital
        self.leverage = leverage
        self.max_risk_per_trade = max_risk_per_trade
        self.max_position_pct = max_position_pct
        self.target_vol = target_vol
        self.max_hold_days = max_hold_days

    @staticmethod
    def _calculate_sma(values: List[float], period: int) -> List[Optional[float]]:
        res: List[Optional[float]] = []
        for i in range(len(values)):
            if i + 1 < period:
                res.append(sum(values[: i + 1]) / (i + 1))
            else:
                res.append(sum(values[i + 1 - period : i + 1]) / period)
        return res

    @staticmethod
    def _calculate_rsi(closes: List[float], period: int) -> List[Optional[float]]:
        if len(closes) < period + 1:
            return [None] * len(closes)

        gains = []
        losses = []
        for i in range(1, len(closes)):
            diff = closes[i] - closes[i - 1]
            gains.append(max(0.0, diff))
            losses.append(max(0.0, -diff))

        rsi: List[Optional[float]] = [None] * (period)
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period

        if avg_loss == 0:
            rsi.append(100.0)
        else:
            rs = avg_gain / avg_loss
            rsi.append(100.0 - (100.0 / (1.0 + rs)))

        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
            if avg_loss == 0:
                rsi.append(100.0)
            else:
                rs = avg_gain / avg_loss
                rsi.append(100.0 - (100.0 / (1.0 + rs)))

        return rsi

    @staticmethod
    def _calculate_atr(
        highs: List[float], lows: List[float], closes: List[float], period: int = 14
    ) -> List[Optional[float]]:
        if len(closes) < 2:
            return [None] * len(closes)

        tr = [highs[0] - lows[0]]
        for i in range(1, len(closes)):
            tr.append(max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            ))

        atr: List[Optional[float]] = [None] * (period - 1)
        if len(tr) < period:
            return [None] * len(closes)

        curr_atr = sum(tr[:period]) / period
        atr.append(curr_atr)

        for i in range(period, len(tr)):
            curr_atr = (curr_atr * (period - 1) + tr[i]) / period
            atr.append(curr_atr)

        return atr

    @staticmethod
    def _calculate_realized_vol(closes: List[float], window: int = 20) -> List[Optional[float]]:
        vol: List[Optional[float]] = [None] * len(closes)
        for i in range(window, len(closes)):
            sub = closes[i - window : i + 1]
            rets = [(sub[j] - sub[j - 1]) / sub[j - 1] for j in range(1, len(sub))]
            mean_r = sum(rets) / len(rets)
            var_r = sum((r - mean_r) ** 2 for r in rets) / max(1, len(rets) - 1)
            ann_vol = math.sqrt(var_r) * math.sqrt(252)
            vol[i] = ann_vol
        return vol

    def run(
        self,
        symbol: str,
        candles: List[Dict[str, Any]],
        strategy_filter: str = "both",
    ) -> BacktestResult:
        """Run the simulation over a chronological list of daily OHLCV candles.

        Each candle must have:
        'date': str (YYYY-MM-DD), 'open': float, 'high': float, 'low': float, 'close': float, 'volume': float
        """
        if len(candles) < 30:
            raise ValueError(f"Insufficient historical candles ({len(candles)}) to run backtest.")

        dates = [c["date"] for c in candles]
        opens = [float(c["open"]) for c in candles]
        highs = [float(c["high"]) for c in candles]
        lows = [float(c["low"]) for c in candles]
        closes = [float(c["close"]) for c in candles]

        # Indicator Series
        sma200 = self._calculate_sma(closes, 200)
        rsi3 = self._calculate_rsi(closes, 3)
        rsi7 = self._calculate_rsi(closes, 7)
        atr14 = self._calculate_atr(highs, lows, closes, 14)
        vol20 = self._calculate_realized_vol(closes, 20)

        capital = self.initial_capital
        equity_curve: List[float] = [capital]
        trades: List[SimulatedTrade] = []
        open_trades: List[Dict[str, Any]] = []
        trade_counter = 0

        # Daily simulation loop
        for i in range(25, len(candles)):
            curr_date = dates[i]
            o = opens[i]
            h = highs[i]
            l = lows[i]
            c = closes[i]

            # ── 1. Process Open Trades (Exits) ──
            still_open: List[Dict[str, Any]] = []
            for pos in open_trades:
                pos["hold_days"] += 1
                entry_p = pos["entry_price"]
                direction = pos["direction"]
                stop_loss = pos["current_sl"]
                rem_shares = pos["remaining_shares"]
                is_long = direction == "LONG"

                # Check SL hit
                sl_hit = (l <= stop_loss) if is_long else (h >= stop_loss)
                if sl_hit:
                    exit_p = stop_loss
                    pnl_per_share = (exit_p - entry_p) if is_long else (entry_p - exit_p)
                    # Scale PnL by 3x leverage
                    trade_pnl = rem_shares * pnl_per_share * self.leverage
                    pos["realized_pnl"] += trade_pnl
                    capital += (rem_shares * entry_p) + trade_pnl

                    reason = "SL" if not pos["tp1_hit"] else "TP1_SL_BE"
                    t = SimulatedTrade(
                        trade_id=pos["id"],
                        symbol=symbol,
                        direction=direction,
                        strategy=pos["strategy"],
                        entry_date=pos["entry_date"],
                        entry_price=entry_p,
                        exit_date=curr_date,
                        exit_price=exit_p,
                        shares=pos["total_shares"],
                        notional=pos["notional"],
                        initial_sl=pos["initial_sl"],
                        tp1=pos["tp1"],
                        tp2=pos["tp2"],
                        tp3=pos["tp3"],
                        realized_pnl=round(pos["realized_pnl"], 2),
                        return_pct=round((pos["realized_pnl"] / pos["notional"]) * 100, 2) if pos["notional"] > 0 else 0.0,
                        hold_days=pos["hold_days"],
                        exit_reason=reason,
                        tp1_hit=pos["tp1_hit"],
                        tp2_hit=pos["tp2_hit"],
                        tp3_hit=pos["tp3_hit"],
                    )
                    trades.append(t)
                    continue

                # Check TP1 hit (+1.0R): Banks 50%, moves SL to Break-Even
                if not pos["tp1_hit"]:
                    tp1_hit = (h >= pos["tp1"]) if is_long else (l <= pos["tp1"])
                    if tp1_hit:
                        pos["tp1_hit"] = True
                        trim_shares = pos["total_shares"] * 0.50
                        pos["remaining_shares"] -= trim_shares
                        exit_p = pos["tp1"]
                        pnl = trim_shares * ((exit_p - entry_p) if is_long else (entry_p - exit_p)) * self.leverage
                        pos["realized_pnl"] += pnl
                        capital += (trim_shares * entry_p) + pnl
                        # Elevate stop to break-even
                        pos["current_sl"] = entry_p

                # Check TP2 hit (+2.0R): Banks 25%, ratchets stop to TP1
                if pos["tp1_hit"] and not pos["tp2_hit"]:
                    tp2_hit = (h >= pos["tp2"]) if is_long else (l <= pos["tp2"])
                    if tp2_hit:
                        pos["tp2_hit"] = True
                        trim_shares = pos["total_shares"] * 0.25
                        pos["remaining_shares"] -= trim_shares
                        exit_p = pos["tp2"]
                        pnl = trim_shares * ((exit_p - entry_p) if is_long else (entry_p - exit_p)) * self.leverage
                        pos["realized_pnl"] += pnl
                        capital += (trim_shares * entry_p) + pnl
                        # Ratchet stop to TP1
                        pos["current_sl"] = pos["tp1"]

                # Check TP3 hit (+3.0R): Banks remaining 25%
                if pos["tp2_hit"] and not pos["tp3_hit"]:
                    tp3_hit = (h >= pos["tp3"]) if is_long else (l <= pos["tp3"])
                    if tp3_hit:
                        pos["tp3_hit"] = True
                        rem = pos["remaining_shares"]
                        pos["remaining_shares"] = 0.0
                        exit_p = pos["tp3"]
                        pnl = rem * ((exit_p - entry_p) if is_long else (entry_p - exit_p)) * self.leverage
                        pos["realized_pnl"] += pnl
                        capital += (rem * entry_p) + pnl

                        t = SimulatedTrade(
                            trade_id=pos["id"],
                            symbol=symbol,
                            direction=direction,
                            strategy=pos["strategy"],
                            entry_date=pos["entry_date"],
                            entry_price=entry_p,
                            exit_date=curr_date,
                            exit_price=exit_p,
                            shares=pos["total_shares"],
                            notional=pos["notional"],
                            initial_sl=pos["initial_sl"],
                            tp1=pos["tp1"],
                            tp2=pos["tp2"],
                            tp3=pos["tp3"],
                            realized_pnl=round(pos["realized_pnl"], 2),
                            return_pct=round((pos["realized_pnl"] / pos["notional"]) * 100, 2) if pos["notional"] > 0 else 0.0,
                            hold_days=pos["hold_days"],
                            exit_reason="TP3",
                            tp1_hit=True,
                            tp2_hit=True,
                            tp3_hit=True,
                        )
                        trades.append(t)
                        continue

                # Check Max Hold Days cap (Connors <= 4 days to kill beta decay)
                if pos["hold_days"] >= self.max_hold_days and pos["remaining_shares"] > 0:
                    exit_p = c
                    rem = pos["remaining_shares"]
                    pos["remaining_shares"] = 0.0
                    pnl = rem * ((exit_p - entry_p) if is_long else (entry_p - exit_p)) * self.leverage
                    pos["realized_pnl"] += pnl
                    capital += (rem * entry_p) + pnl

                    t = SimulatedTrade(
                        trade_id=pos["id"],
                        symbol=symbol,
                        direction=direction,
                        strategy=pos["strategy"],
                        entry_date=pos["entry_date"],
                        entry_price=entry_p,
                        exit_date=curr_date,
                        exit_price=exit_p,
                        shares=pos["total_shares"],
                        notional=pos["notional"],
                        initial_sl=pos["initial_sl"],
                        tp1=pos["tp1"],
                        tp2=pos["tp2"],
                        tp3=pos["tp3"],
                        realized_pnl=round(pos["realized_pnl"], 2),
                        return_pct=round((pos["realized_pnl"] / pos["notional"]) * 100, 2) if pos["notional"] > 0 else 0.0,
                        hold_days=pos["hold_days"],
                        exit_reason="MAX_HOLD",
                        tp1_hit=pos["tp1_hit"],
                        tp2_hit=pos["tp2_hit"],
                        tp3_hit=pos["tp3_hit"],
                    )
                    trades.append(t)
                    continue

                still_open.append(pos)

            open_trades = still_open

            # ── 2. Signal Generation (Entries) ──
            # Only open if no existing open position for this asset
            if not open_trades and i < len(candles) - 1:
                curr_c = closes[i]
                curr_atr = atr14[i] or (curr_c * 0.02)
                curr_r3 = rsi3[i]
                curr_r7 = rsi7[i]
                curr_sma = sma200[i]
                curr_vol = vol20[i] or 0.25

                signal_dir: Optional[str] = None
                strategy_name: str = ""

                # 3-RSI Mean Reversion
                if strategy_filter in ("both", "3rsi"):
                    if curr_r3 is not None and curr_r7 is not None and curr_sma is not None:
                        # Long Dip: In uptrend (close >= sma) and extreme oversold
                        if curr_c >= curr_sma and curr_r3 <= 22.0 and curr_r7 <= 42.0:
                            signal_dir = "LONG"
                            strategy_name = "3rsi_mean_reversion"
                        # Short Rip: In downtrend (close < sma) and extreme overbought
                        elif curr_c < curr_sma and curr_r3 >= 78.0 and curr_r7 >= 58.0:
                            signal_dir = "SHORT"
                            strategy_name = "3rsi_mean_reversion"

                # Momentum breakout (fallback if no mean reversion signal)
                if not signal_dir and strategy_filter in ("both", "momentum"):
                    # 20-day high breakout
                    high_20d = max(highs[max(0, i - 20) : i])
                    low_20d = min(lows[max(0, i - 20) : i])
                    if curr_c > high_20d and curr_sma and curr_c > curr_sma:
                        signal_dir = "LONG"
                        strategy_name = "momentum"
                    elif curr_c < low_20d and curr_sma and curr_c < curr_sma:
                        signal_dir = "SHORT"
                        strategy_name = "momentum"

                if signal_dir:
                    trade_counter += 1
                    # Enter at next candle open (or today close)
                    entry_p = curr_c
                    sl_dist = max(2.2 * curr_atr, entry_p * 0.005)
                    initial_sl = (entry_p - sl_dist) if signal_dir == "LONG" else (entry_p + sl_dist)
                    stop_dist_pct = abs(entry_p - initial_sl) / entry_p
                    risk_amount = capital * self.max_risk_per_trade

                    notional_from_risk = risk_amount / stop_dist_pct if stop_dist_pct > 0 else 0.0
                    max_notional = capital * self.max_position_pct
                    target_notional = min(notional_from_risk, max_notional)

                    # Dynamic Volatility Sizing:
                    if curr_vol > 0.05:
                        effective_vol = max(0.10, self.leverage * curr_vol)
                        vol_budget = max_notional * (self.target_vol / effective_vol)
                        target_notional = min(target_notional, vol_budget)

                    shares = max(1.0, round(target_notional / entry_p, 2))
                    actual_notional = round(shares * entry_p, 2)

                    # R targets
                    r_dist = abs(entry_p - initial_sl)
                    if signal_dir == "LONG":
                        tp1 = entry_p + (1.0 * r_dist)
                        tp2 = entry_p + (2.0 * r_dist)
                        tp3 = entry_p + (3.0 * r_dist)
                    else:
                        tp1 = max(0.01, entry_p - (1.0 * r_dist))
                        tp2 = max(0.01, entry_p - (2.0 * r_dist))
                        tp3 = max(0.01, entry_p - (3.0 * r_dist))

                    capital -= actual_notional
                    open_trades.append({
                        "id": trade_counter,
                        "entry_date": curr_date,
                        "entry_price": entry_p,
                        "direction": signal_dir,
                        "strategy": strategy_name,
                        "total_shares": shares,
                        "remaining_shares": shares,
                        "notional": actual_notional,
                        "initial_sl": initial_sl,
                        "current_sl": initial_sl,
                        "tp1": tp1,
                        "tp2": tp2,
                        "tp3": tp3,
                        "tp1_hit": False,
                        "tp2_hit": False,
                        "tp3_hit": False,
                        "realized_pnl": 0.0,
                        "hold_days": 0,
                    })

            # Track end-of-day equity
            open_pnl = 0.0
            for pos in open_trades:
                is_long = pos["direction"] == "LONG"
                cur_pnl = pos["remaining_shares"] * ((c - pos["entry_price"]) if is_long else (pos["entry_price"] - c)) * self.leverage
                open_pnl += pos["realized_pnl"] + cur_pnl + (pos["remaining_shares"] * pos["entry_price"])
            equity_curve.append(capital + open_pnl)

        # Close any lingering positions at end of dataset
        if open_trades:
            last_date = dates[-1]
            last_close = closes[-1]
            for pos in open_trades:
                rem = pos["remaining_shares"]
                if rem > 0:
                    is_long = pos["direction"] == "LONG"
                    pnl = rem * ((last_close - pos["entry_price"]) if is_long else (pos["entry_price"] - last_close)) * self.leverage
                    pos["realized_pnl"] += pnl
                    capital += (rem * pos["entry_price"]) + pnl
                    trades.append(SimulatedTrade(
                        trade_id=pos["id"],
                        symbol=symbol,
                        direction=pos["direction"],
                        strategy=pos["strategy"],
                        entry_date=pos["entry_date"],
                        entry_price=pos["entry_price"],
                        exit_date=last_date,
                        exit_price=last_close,
                        shares=pos["total_shares"],
                        notional=pos["notional"],
                        initial_sl=pos["initial_sl"],
                        tp1=pos["tp1"],
                        tp2=pos["tp2"],
                        tp3=pos["tp3"],
                        realized_pnl=round(pos["realized_pnl"], 2),
                        return_pct=round((pos["realized_pnl"] / pos["notional"]) * 100, 2) if pos["notional"] > 0 else 0.0,
                        hold_days=pos["hold_days"],
                        exit_reason="END_OF_DATA",
                        tp1_hit=pos["tp1_hit"],
                        tp2_hit=pos["tp2_hit"],
                        tp3_hit=pos["tp3_hit"],
                    ))

        # ── 3. Performance Metric Computations ──
        ending_capital = capital
        total_ret_pct = ((ending_capital - self.initial_capital) / self.initial_capital) * 100.0
        # Benchmark (1x Buy & Hold)
        bench_ret_pct = ((closes[-1] - closes[25]) / closes[25]) * 100.0

        win_trades = [t for t in trades if t.realized_pnl > 0]
        loss_trades = [t for t in trades if t.realized_pnl <= 0]
        win_count = len(win_trades)
        loss_count = len(loss_trades)
        total_count = len(trades)
        win_rate = (win_count / total_count * 100.0) if total_count > 0 else 0.0

        gross_profit = sum(t.realized_pnl for t in win_trades)
        gross_loss = abs(sum(t.realized_pnl for t in loss_trades))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (99.0 if gross_profit > 0 else 1.0)

        # Max Drawdown
        peak = equity_curve[0]
        max_dd = 0.0
        for eq in equity_curve:
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak if peak > 0 else 0.0
            if dd > max_dd:
                max_dd = dd
        max_dd_pct = max_dd * 100.0

        # Sharpe Ratio (daily equity changes)
        daily_returns = [
            (equity_curve[j] - equity_curve[j - 1]) / equity_curve[j - 1]
            for j in range(1, len(equity_curve))
            if equity_curve[j - 1] > 0
        ]
        if len(daily_returns) > 5:
            mean_ret = sum(daily_returns) / len(daily_returns)
            var_ret = sum((r - mean_ret) ** 2 for r in daily_returns) / (len(daily_returns) - 1)
            std_ret = math.sqrt(var_ret) if var_ret > 0 else 0.0
            sharpe = (mean_ret / std_ret * math.sqrt(252)) if std_ret > 0 else 0.0
        else:
            sharpe = 0.0

        avg_pnl = sum(t.realized_pnl for t in trades) / total_count if total_count > 0 else 0.0
        avg_hold = sum(t.hold_days for t in trades) / total_count if total_count > 0 else 0.0

        return BacktestResult(
            symbol=symbol,
            start_date=dates[25],
            end_date=dates[-1],
            initial_capital=self.initial_capital,
            ending_capital=round(ending_capital, 2),
            total_return_pct=round(total_ret_pct, 2),
            benchmark_return_pct=round(bench_ret_pct, 2),
            total_trades=total_count,
            winning_trades=win_count,
            losing_trades=loss_count,
            win_rate_pct=round(win_rate, 2),
            profit_factor=round(profit_factor, 2),
            max_drawdown_pct=round(max_dd_pct, 2),
            sharpe_ratio=round(sharpe, 2),
            avg_trade_pnl=round(avg_pnl, 2),
            avg_hold_days=round(avg_hold, 1),
            trades=trades,
        )


def generate_synthetic_candles(symbol: str, count: int = 150) -> List[Dict[str, Any]]:
    """Generate realistic daily OHLCV candles for backtest simulation verification."""
    import random
    base_price = 150.0 if "QQQ" in symbol else (100.0 if "NVDA" in symbol else 80.0)
    current_price = base_price
    candles: List[Dict[str, Any]] = []

    # Deterministic seed for reproducible tests
    rng = random.Random(42)

    for i in range(count):
        # Drift + volatility with occasional mean-reverting pullbacks
        drift = 0.0008
        shock = rng.gauss(0, 0.02)
        if i % 15 == 0:  # Occasional oversold dip
            shock = -0.04
        daily_ret = drift + shock
        open_p = current_price
        close_p = open_p * (1.0 + daily_ret)
        high_p = max(open_p, close_p) * (1.0 + abs(rng.gauss(0, 0.008)))
        low_p = min(open_p, close_p) * (1.0 - abs(rng.gauss(0, 0.008)))
        vol = rng.randint(2000000, 10000000)

        dt = f"2025-01-{(i % 28) + 1:02d}"
        candles.append({
            "date": dt,
            "open": round(open_p, 2),
            "high": round(high_p, 2),
            "low": round(low_p, 2),
            "close": round(close_p, 2),
            "volume": vol,
        })
        current_price = close_p

    return candles


def print_backtest_report(res: BacktestResult) -> None:
    """Print an institutional-grade ASCII performance report."""
    sep = "═" * 68
    sub_sep = "─" * 68
    print(sep)
    print(f"  📊 HISTORICAL BACKTEST BENCHMARK REPORT: {res.symbol}")
    print(sep)
    print(f"  Period:            {res.start_date} to {res.end_date}")
    print(f"  Initial Capital:   ${res.initial_capital:,.2f}")
    print(f"  Ending Capital:    ${res.ending_capital:,.2f}")
    print(f"  Strategy Return:   {res.total_return_pct:+.2f}%")
    print(f"  Benchmark (1x B&H): {res.benchmark_return_pct:+.2f}%")
    print(sub_sep)
    print(f"  Total Trades:      {res.total_trades}")
    print(f"  Win Rate:          {res.win_rate_pct:.1f}% ({res.winning_trades} wins, {res.losing_trades} losses)")
    print(f"  Profit Factor:     {res.profit_factor:.2f}")
    print(f"  Max Drawdown:      {res.max_drawdown_pct:.2f}%")
    print(f"  Sharpe Ratio:      {res.sharpe_ratio:.2f}")
    print(f"  Avg Trade PnL:     ${res.avg_trade_pnl:+.2f}")
    print(f"  Avg Holding Period: {res.avg_hold_days:.1f} days")
    print(sub_sep)
    if res.trades:
        print("  Sample Trades:")
        for t in res.trades[:5]:
            outcome = "WIN" if t.realized_pnl > 0 else "LOSS"
            print(f"   [{outcome}] #{t.trade_id} {t.direction} {t.entry_date}->{t.exit_date} "
                  f"Entry: ${t.entry_price:.2f} Exit: ${float(t.exit_price or 0):.2f} "
                  f"PnL: ${t.realized_pnl:+.2f} ({t.return_pct:+.1f}%) via {t.exit_reason}")
    print(sep)


def main() -> None:
    """Command-line entry point for backtesting."""
    parser = argparse.ArgumentParser(description="3x Leveraged Strategy Backtest & Benchmark Engine")
    parser.add_argument("--symbol", type=str, default="QQQ", help="Underlying asset symbol (e.g. QQQ, NVDA, SPY)")
    parser.add_argument("--period", type=str, default="1y", help="Historical data window (e.g. 6m, 1y, 2y)")
    parser.add_argument("--capital", type=float, default=10000.0, help="Starting account equity")
    parser.add_argument("--strategy", type=str, choices=["3rsi", "momentum", "both"], default="both", help="Strategy to test")
    parser.add_argument("--leverage", type=int, default=3, help="Leverage multiplier (default 3)")
    parser.add_argument("--json", action="store_true", help="Output raw JSON results")
    parser.add_argument("--dry-run", action="store_true", help="Run quick verification with synthetic candles")

    args = parser.parse_args()

    candles = None
    if not args.dry_run:
        try:
            import yfinance as yf
            ticker = yf.Ticker(args.symbol)
            hist = ticker.history(period=args.period)
            if not hist.empty:
                candles = []
                for dt, row in hist.iterrows():
                    candles.append({
                        "date": dt.strftime("%Y-%m-%d"),
                        "open": float(row["Open"]),
                        "high": float(row["High"]),
                        "low": float(row["Low"]),
                        "close": float(row["Close"]),
                        "volume": float(row["Volume"]),
                    })
        except Exception as e:
            logger.warning("Could not fetch yfinance data for %s (%s). Using synthetic candles.", args.symbol, e)

    if not candles:
        candles = generate_synthetic_candles(args.symbol, count=160)

    engine = BacktestEngine(
        initial_capital=args.capital,
        leverage=args.leverage,
        max_risk_per_trade=0.005,
        max_position_pct=0.05,
        target_vol=0.20,
        max_hold_days=4,
    )

    result = engine.run(symbol=args.symbol, candles=candles, strategy_filter=args.strategy)

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print_backtest_report(result)


if __name__ == "__main__":
    main()
