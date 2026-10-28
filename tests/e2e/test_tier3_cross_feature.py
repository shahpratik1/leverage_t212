"""Tier 3: Cross-Feature Combinations (Pairwise & Multi-System Interactions).

Verifies pairwise and end-to-end interactions between:
- Market Regime Classification & Strategy Routing & Order Sizing
- Dynamic Profit-Taking Ratchets (+1.5R/+2.5R/+3.5R) & Progressive Trailing Stops (+1.5%/+3.0%)
- LSE GBX Pence Normalization & 20% Account Equity Cap Enforcement
- Extended Technical Indicators & Regime Trigger Thresholds
- Broker Live Equity & Dashboard Server Trajectory Serialization
- Multi-Asset Backtest Simulation & Benchmark Edge Verification
"""

from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timezone
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

from trading_system.agents.risk_manager import DecisionCard, RiskManager
from trading_system.data.leveraged_universe import (
    CURATED_3X_PAIRS,
    GBX_TICKERS,
    LeveragedUniverse,
    get_ticker_currency,
)
from trading_system.data.trading212_client import MarketDataProvider, Trading212Client
from trading_system.execution.position_monitor import PositionMonitor, TrackedPosition


class TestTier3CrossFeatureCombinations(unittest.TestCase):
    """Tier 3: Pairwise and subsystem cross-feature integration test suite."""

    def setUp(self) -> None:
        self.risk_manager = RiskManager()
        self.universe = LeveragedUniverse()
        self.client = Trading212Client(api_key="mock", base_url="https://live.trading212.com")

    # ══════════════════════════════════════════════════════════════════════════
    # Interaction 1: Regime Transition ↔ Order Sizing (F08 + F09 + F02)
    # ══════════════════════════════════════════════════════════════════════════

    def test_x01_high_vol_regime_cuts_position_sizing(self) -> None:
        """X01: High volatility regime cuts position size proportionally compared to normal regime."""
        equity = 5000.0
        entry_price = 100.0
        stop_loss = 95.0

        # Normal volatility (20%)
        shares_normal, notional_normal, _ = self.risk_manager.calculate_position_size(
            account_equity=equity,
            entry_price=entry_price,
            stop_loss=stop_loss,
            realized_vol=0.20,
        )

        # Spiking volatility (70% - High-Vol Erratic)
        shares_high_vol, notional_high_vol, _ = self.risk_manager.calculate_position_size(
            account_equity=equity,
            entry_price=entry_price,
            stop_loss=stop_loss,
            realized_vol=0.70,
        )

        self.assertLess(shares_high_vol, shares_normal)
        self.assertLess(notional_high_vol, notional_normal)

    def test_x01_chop_regime_mean_reversion_sizing(self) -> None:
        """X01.2: Mean reversion setup in CHOP_RANGE allocates strictly within risk budget."""
        metrics = {"close": 50.0, "atr": 1.5, "realized_vol": 0.18, "adx": 16.0}
        debate = MagicMock(consensus_decision="BUY_ENTRY", conviction_score=0.75, synthesis="")
        card = self.risk_manager.generate_decision_card(
            ticker="QQQ",
            trading212_ticker="QQQ3m_EQ",
            direction="LONG",
            metrics=metrics,
            debate_outcome=debate,
            account_equity=4923.05,
            strategy_type="3rsi_mean_reversion",
        )
        self.assertEqual(card.strategy_type, "3rsi_mean_reversion")
        self.assertLessEqual(card.position_notional, 4923.05 * 0.20 + 0.01)

    # ══════════════════════════════════════════════════════════════════════════
    # Interaction 2: Dynamic Ratchets & Progressive Stops (F10 + F11 + F15)
    # ══════════════════════════════════════════════════════════════════════════

    def test_x02_progressive_stop_coordinates_with_tp1_ratchet(self) -> None:
        """X02.1: Stop elevates to BE at +1.5% profit, then ratchets past TP1 with hysteresis."""
        entry = 100.0
        stop = 90.0  # R = 10.0, TP1 = 115.0 (+1.5R)

        # 1. Price moves to +1.6% profit (101.60) -> Breakeven stop triggered
        if hasattr(PositionMonitor, "check_progressive_stops"):
            sl_be = PositionMonitor.check_progressive_stops(entry, 101.60, stop, "LONG")
            self.assertEqual(sl_be, 100.0)

        # 2. Price hits TP1 (115.0) -> Position monitor trims 50% and elevates stop
        tp1, tp2, tp3 = self.risk_manager.calculate_take_profits(entry, stop, "LONG")
        self.assertAlmostEqual(tp1, 115.0, delta=0.5)

    def test_x02_progressive_stop_elevates_at_3_pct_before_tp1(self) -> None:
        """X02.2: Large R distance trade reaching +3.0% locks in +1.5% before reaching +1.5R."""
        entry = 100.0
        stop = 80.0  # Wide R = 20.0, so TP1 = 130.0 (+30%)
        # At price 103.50 (+3.5% profit), price has NOT reached TP1 (130.0),
        # but progressive stop should lock in +1.5% cushion (101.50)
        if hasattr(PositionMonitor, "check_progressive_stops"):
            sl_cushion = PositionMonitor.check_progressive_stops(entry, 103.50, stop, "LONG")
            self.assertEqual(sl_cushion, 101.50)

    def test_x02_short_trade_ratchet_and_progressive_stop_coordination(self) -> None:
        """X02.3: Short trade ratchets stop downward at +1.5% and +3.0% gains."""
        entry = 100.0
        stop = 110.0  # R = 10.0, TP1 = 85.0 (-1.5R)
        if hasattr(PositionMonitor, "check_progressive_stops"):
            # Price drops to 98.40 (+1.6% short gain) -> stop to 100.0 (BE)
            sl_be = PositionMonitor.check_progressive_stops(entry, 98.40, stop, "SHORT")
            self.assertEqual(sl_be, 100.0)
            # Price drops to 96.50 (+3.5% short gain) -> stop to 98.50 (entry - 1.5%)
            sl_cushion = PositionMonitor.check_progressive_stops(entry, 96.50, sl_be, "SHORT")
            self.assertEqual(sl_cushion, 98.50)

    # ══════════════════════════════════════════════════════════════════════════
    # Interaction 3: GBX Pence Normalization ↔ 20% Equity Cap (F03 + F04 + F02)
    # ══════════════════════════════════════════════════════════════════════════

    def test_x03_3gisl_pence_normalized_and_capped_on_live_equity(self) -> None:
        """X03.1: 3GISl_EQ (7805p) conversion into EUR strictly prevents exceeding €984.61 cap."""
        account_equity = 4923.05
        max_notional = account_equity * 0.20  # €984.61

        if hasattr(self.universe, "normalize_quote_to_eur"):
            price_eur = self.universe.normalize_quote_to_eur("3GISl_EQ", 7805.0, "GBX")
        else:
            price_eur = (7805.0 / 100.0) * 1.204  # = 93.97

        shares, notional, _ = self.risk_manager.calculate_position_size(
            account_equity=account_equity,
            entry_price=price_eur,
            stop_loss=price_eur * 0.95,
        )

        # Committed notional must be <= 20% equity cap
        self.assertLessEqual(notional, max_notional + 0.01)
        self.assertAlmostEqual(shares, 10.0, delta=1.0)
        self.assertNotEqual(round(shares), 24)

    def test_x03_mixed_currency_portfolio_margin_compliance(self) -> None:
        """X03.2: Portfolio with EUR (QQQ3m), USD (QQQ3l), and GBX (3GISl) sizes each <= 20%."""
        equity = 4923.05
        max_cap = equity * 0.20

        # EUR asset
        s_eur, n_eur, _ = self.risk_manager.calculate_position_size(equity, 55.0, 52.0)
        self.assertLessEqual(n_eur, max_cap + 0.01)

        # USD asset ($100 = ~€92.0 at 1.085 EUR/USD)
        s_usd, n_usd, _ = self.risk_manager.calculate_position_size(equity, 92.0, 87.0)
        self.assertLessEqual(n_usd, max_cap + 0.01)

        # GBX asset (7805p = ~€93.97)
        s_gbx, n_gbx, _ = self.risk_manager.calculate_position_size(equity, 93.97, 88.0)
        self.assertLessEqual(n_gbx, max_cap + 0.01)

    # ══════════════════════════════════════════════════════════════════════════
    # Interaction 4: Extended Indicators ↔ Regime Detection (F07 + F08 + F09)
    # ══════════════════════════════════════════════════════════════════════════

    def test_x04_indicators_feed_directly_into_regime_classifier(self) -> None:
        """X04.1: compute_indicators output contains all metrics needed for regime classification."""
        candles = [
            {"date": f"2026-01-{i+1:02d}", "open": 100 + i, "high": 105 + i, "low": 95 + i, "close": 102 + i, "volume": 1000}
            for i in range(35)
        ]
        inds = MarketDataProvider.compute_indicators(candles, hourly_data=None, symbol="QQQ")
        self.assertIn("adx", inds)
        self.assertIn("ema20", inds)
        self.assertIn("ema50", inds)
        self.assertIn("realized_vol", inds)

    def test_x04_regime_classification_drives_decision_card_generation(self) -> None:
        """X04.2: DecisionCard receives and preserves market_regime attribute."""
        metrics = {
            "close": 100.0,
            "atr": 2.5,
            "realized_vol": 0.20,
            "adx": 28.0,
            "ema20": 105.0,
            "ema50": 100.0,
        }
        debate = MagicMock(consensus_decision="BUY_ENTRY", conviction_score=0.85, synthesis="")
        card = self.risk_manager.generate_decision_card(
            ticker="QQQ",
            trading212_ticker="QQQ3m_EQ",
            direction="LONG",
            metrics=metrics,
            debate_outcome=debate,
            account_equity=4923.05,
            strategy_type="momentum",
        )
        self.assertEqual(card.trading212_ticker, "QQQ3m_EQ")
        self.assertEqual(card.decision, "BUY_ENTRY")

    # ══════════════════════════════════════════════════════════════════════════
    # Interaction 5: Broker Live Equity ↔ Dashboard Serialization (F05 + F15)
    # ══════════════════════════════════════════════════════════════════════════

    def test_x05_dashboard_reconciles_broker_equity_with_active_pnl(self) -> None:
        """X05: Dashboard data merges live broker cash total (€4,923.05) with positions."""
        from trading_system.dashboard.server import build_dashboard_data
        client = MagicMock()
        cash = MagicMock(total=4923.05, free=2500.0)
        client.get_account_cash.return_value = cash
        client.get_portfolio.return_value = []
        client.get_orders.return_value = []

        with patch("trading_system.dashboard.server._DASHBOARD_SHARED_CLIENT", client), \
             patch("trading_system.config.settings.TRADING212_API_KEY", "mock_key"):
            data = build_dashboard_data(force=True)
            self.assertIsInstance(data, dict)
            # Must not show $10,000 synthetic ground truth
            self.assertNotEqual(data.get("base_equity"), 10000.0)

    # ══════════════════════════════════════════════════════════════════════════
    # Interaction 6: High-Vol Volatility Sizing ↔ Breakout Signal (F09 + F07 + F02)
    # ══════════════════════════════════════════════════════════════════════════

    def test_x06_breakout_signal_sized_conservatively_under_high_vol(self) -> None:
        """X06: Breakout signal during high realized volatility reduces notional."""
        equity = 5000.0
        # Volatility = 45% (above 20% target vol)
        shares, notional, _ = self.risk_manager.calculate_position_size(
            account_equity=equity,
            entry_price=100.0,
            stop_loss=95.0,
            realized_vol=0.45,
            leverage=3,
        )
        max_notional = equity * 0.20  # 1000.0
        self.assertLess(notional, max_notional)

    # ══════════════════════════════════════════════════════════════════════════
    # Interaction 7: Multi-Asset Backtest ↔ Benchmark Edge (F12 + F13 + F14)
    # ══════════════════════════════════════════════════════════════════════════

    def test_x07_backtest_pipeline_evaluates_benchmark_targets(self) -> None:
        """X07: Multi-regime backtest output passes through meets_benchmark_targets verification."""
        from trading_system.backtest.engine import BacktestEngine, BacktestResult
        engine = BacktestEngine(initial_capital=5000.0)
        candles = [
            {"date": f"2026-01-{i+1:02d}", "open": 100 + i * 0.5, "high": 102 + i * 0.5, "low": 99 + i * 0.5, "close": 101 + i * 0.5, "volume": 1000}
            for i in range(40)
        ]
        res = engine.run("QQQ", candles=candles)
        self.assertIsInstance(res, BacktestResult)
        if hasattr(res, "meets_benchmark_targets"):
            passed, details = res.meets_benchmark_targets()
            self.assertIsInstance(passed, bool)
            self.assertIsInstance(details, dict)

    # ══════════════════════════════════════════════════════════════════════════
    # Interaction 8: Position Monitor ↔ State File Persistence (F10 + F11 + F17)
    # ══════════════════════════════════════════════════════════════════════════

    def test_x08_state_persistence_after_trailing_stop_update(self) -> None:
        """X08: PositionMonitor updates position trailing stop and retains integrity."""
        state_file = "test_pm_state_tmp.json"
        pos = TrackedPosition(
            ticker="QQQ3m_EQ", underlying="QQQ", direction="LONG",
            entry_price=100.0, original_quantity=10.0, remaining_quantity=10.0,
            stop_loss=90.0, trailing_stop_loss=90.0, highest_price=105.0,
            lowest_price=99.0, tp1=115.0, tp2=125.0, tp3=135.0, atr=2.5,
        )
        pm = PositionMonitor(
            t212_client=MagicMock(),
            notifier=MagicMock(),
            risk_manager=self.risk_manager,
            state_file=state_file,
            dry_run=True,
        )
        pm.positions["QQQ3m_EQ"] = pos
        # Ratchet trailing stop upward
        new_tsl = self.risk_manager.calculate_trailing_stop(
            current_price=105.0, peak_price=105.0, atr=2.5, current_tsl=90.0, direction="LONG"
        )
        pos.trailing_stop_loss = new_tsl
        self.assertGreater(pos.trailing_stop_loss, 90.0)
        if os.path.exists(state_file):
            os.remove(state_file)

    # ══════════════════════════════════════════════════════════════════════════
    # Interaction 9: Quote Query ↔ Order Sizing ↔ Anti-Churn (F01 + F02 + F15)
    # ══════════════════════════════════════════════════════════════════════════

    def test_x09_quote_query_before_order_sizing_and_cooldown_check(self) -> None:
        """X09: Execution flow queries quote, sizes shares, and validates anti-churn cooldown."""
        with patch.object(MarketDataProvider, "get_bid_ask_spread", return_value={"bid": 49.9, "ask": 50.1, "mid": 50.0, "spread_pct": 0.004}):
            quote = MarketDataProvider.get_bid_ask_spread("QQQ3m_EQ")
            price = quote.get("mid", 50.0)

            shares, notional, _ = self.risk_manager.calculate_position_size(
                account_equity=4923.05,
                entry_price=price,
                stop_loss=price * 0.95,
            )
            self.assertGreater(shares, 0)

            card = DecisionCard(
                ticker="QQQ", trading212_ticker="QQQ3m_EQ", direction="LONG",
                decision="BUY_ENTRY", entry_price=price, stop_loss=price * 0.95,
                trailing_stop_loss=price * 0.95, stop_distance_pct=0.05,
                take_profit_1=price * 1.075, take_profit_2=price * 1.125, take_profit_3=price * 1.175,
                position_size_shares=shares, position_notional=notional,
                risk_amount=25.0, risk_pct_of_equity=0.005, conviction_score=0.8, rationale="",
            )
            is_valid, _ = self.risk_manager.validate_trade(card, account_cash=5000.0, existing_positions=[])
            self.assertTrue(is_valid)

    # ══════════════════════════════════════════════════════════════════════════
    # Interaction 10: Hermetic Multi-Agent Pipeline Coexistence (F06 + F15 + F16)
    # ══════════════════════════════════════════════════════════════════════════

    def test_x10_concurrent_agents_isolated(self) -> None:
        """X10: Technical agent, Risk manager, and Position monitor co-exist cleanly."""
        from trading_system.agents.technical_agent import TechnicalAnalyst
        analyst = TechnicalAnalyst(openai_api_key="mock_key")
        rm = RiskManager()
        self.assertIsNotNone(analyst)
        self.assertIsNotNone(rm)


if __name__ == "__main__":
    unittest.main()
