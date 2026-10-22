"""Tests for Active-Trade Driven Architecture and Signal-Based Execution."""

import unittest
from unittest.mock import MagicMock, patch
from trading_system.execution.position_monitor import PositionMonitor, TrackedPosition
from trading_system.data.trading212_client import OrderResult, Position
from trading_system.orchestrator import Orchestrator


class TestActiveTradesDriven(unittest.TestCase):
    def setUp(self):
        self.mock_t212 = MagicMock()
        self.mock_notifier = MagicMock()
        self.mock_rm = MagicMock()
        self.mock_rm.calculate_escape_threshold.return_value = 106.0
        self.mock_rm.calculate_trailing_stop.return_value = 105.0

        self.monitor = PositionMonitor(
            t212_client=self.mock_t212,
            notifier=self.mock_notifier,
            risk_manager=self.mock_rm,
            dry_run=False,
            state_file=":memory:",
        )
        self.monitor._save_state = MagicMock()

    # ── 1. Dynamic SL, TP1, TP2, TP3 Calculation ──────────────────
    def test_calculate_exit_levels_long_default(self):
        """Test exit level calculations for Long with no existing stop."""
        levels = self.monitor.calculate_exit_levels(
            entry_price=100.0,
            direction="LONG",
            atr=2.5,
        )
        # raw_dist = 2.5 * 2.2 = 5.5 (between 3.0 and 8.0)
        self.assertEqual(levels["stop_loss"], 94.5)
        self.assertEqual(levels["trailing_stop_loss"], 94.5)
        self.assertEqual(levels["tp1"], 105.5)
        self.assertEqual(levels["tp2"], 111.0)
        self.assertEqual(levels["tp3"], 116.5)
        self.assertEqual(levels["atr"], 2.5)

    def test_calculate_exit_levels_with_existing_broker_stop(self):
        """Test exit levels when aligning with an existing broker stop order."""
        levels = self.monitor.calculate_exit_levels(
            entry_price=100.0,
            direction="LONG",
            existing_stop_price=95.0,
        )
        # sl_dist = 5.0
        self.assertEqual(levels["stop_loss"], 95.0)
        self.assertEqual(levels["tp1"], 105.0)
        self.assertEqual(levels["tp2"], 110.0)
        self.assertEqual(levels["tp3"], 115.0)

    def test_calculate_exit_levels_short(self):
        """Test exit level calculations for Short."""
        levels = self.monitor.calculate_exit_levels(
            entry_price=100.0,
            direction="SHORT",
            atr=2.0,
        )
        # raw_dist = 2.0 * 2.2 = 4.4
        self.assertEqual(levels["stop_loss"], 104.4)
        self.assertEqual(levels["tp1"], 95.6)
        self.assertEqual(levels["tp2"], 91.2)
        self.assertEqual(levels["tp3"], 86.8)

    # ── 2. Native Broker Stop & Limit Orders ("Set the Price") ─────
    def test_sync_broker_orders_places_missing_stop_and_limit(self):
        """Verify sync_broker_orders checks and submits missing stop and limit orders."""
        self.mock_t212.place_stop_order.return_value = OrderResult(
            id="ORDER_STOP_101",
            ticker="QQQ3l_EQ",
            type="STOP",
            status="NEW",
            quantity=-10.0,
            filled_quantity=0.0,
            fill_price=0.0,
        )
        self.mock_t212.place_limit_order.return_value = OrderResult(
            id="ORDER_LIMIT_202",
            ticker="QQQ3l_EQ",
            type="LIMIT",
            status="NEW",
            quantity=-5.0,
            filled_quantity=0.0,
            fill_price=0.0,
        )

        pos = TrackedPosition(
            ticker="QQQ3l_EQ",
            underlying="QQQ",
            direction="LONG",
            entry_price=100.0,
            original_quantity=10.0,
            remaining_quantity=10.0,
            stop_loss=95.0,
            trailing_stop_loss=95.0,
            highest_price=100.0,
            lowest_price=100.0,
            tp1=105.0,
            tp2=110.0,
            tp3=115.0,
            atr=2.5,
        )
        self.monitor.positions["QQQ3l_EQ"] = pos

        res = self.monitor.sync_broker_orders()
        self.assertIn("QQQ3l_EQ", res["stops"])
        self.assertIn("QQQ3l_EQ", res["tps"])
        self.assertEqual(pos.broker_stop_order_id, "ORDER_STOP_101")
        self.assertEqual(pos.broker_tp_order_id, "ORDER_LIMIT_202")

    # ── 3. Untracked Broker Adoption with Native Order Placement ───
    def test_reconciliation_adopts_and_sets_broker_orders(self):
        """When untracked broker position is adopted, it computes levels and sets native orders."""
        pos_broker = Position(
            ticker="3XOMl_EQ",
            quantity=5.0,
            average_price=30.0,
            current_price=31.0,
            ppl=5.0,
            max_buy=10.0,
            max_sell=10.0,
        )
        self.mock_t212.get_portfolio.return_value = [pos_broker]
        self.mock_t212.get_orders.return_value = []
        self.mock_t212.place_stop_order.return_value = OrderResult(
            id="STOP_XOM_001",
            ticker="3XOMl_EQ",
            type="STOP",
            status="NEW",
            quantity=-5.0,
            filled_quantity=0.0,
            fill_price=0.0,
        )
        self.mock_t212.place_limit_order.return_value = OrderResult(
            id="LIMIT_XOM_001",
            ticker="3XOMl_EQ",
            type="LIMIT",
            status="NEW",
            quantity=-2.5,
            filled_quantity=0.0,
            fill_price=0.0,
        )

        summary = self.monitor.reconcile_with_broker(adopt_untracked=True)
        self.assertIn("3XOMl_EQ", summary["adopted_from_broker"])
        self.assertIn("3XOMl_EQ", self.monitor.positions)

        adopted = self.monitor.positions["3XOMl_EQ"]
        self.assertEqual(adopted.underlying, "XOM")
        self.assertEqual(adopted.entry_price, 30.0)
        self.assertGreater(adopted.tp1, 30.0)
        self.assertLess(adopted.stop_loss, 30.0)
        self.assertEqual(adopted.broker_stop_order_id, "STOP_XOM_001")
        self.assertEqual(adopted.broker_tp_order_id, "LIMIT_XOM_001")

    # ── 4. Signal-Driven Exits on Active Trades ─────────────────────
    def test_evaluate_active_trade_signals_rsi3_overbought_long(self):
        """Connors 3-RSI overbought (>= 75.0) on Long in profit triggers TP1 trim."""
        pos = TrackedPosition(
            ticker="QQQ3l_EQ",
            underlying="QQQ",
            direction="LONG",
            entry_price=100.0,
            current_price=103.5,
            original_quantity=10.0,
            remaining_quantity=10.0,
            stop_loss=95.0,
            trailing_stop_loss=95.0,
            highest_price=103.5,
            lowest_price=100.0,
            tp1=105.0,
            tp2=110.0,
            tp3=115.0,
            atr=2.5,
        )
        self.monitor.positions["QQQ3l_EQ"] = pos
        self.monitor._execute_sell = MagicMock(return_value=True)
        self.monitor.place_broker_stop = MagicMock(return_value="NEW_BE_STOP")
        self.monitor.place_broker_tp_limit = MagicMock(return_value="NEW_TP2_LIMIT")

        # Active metrics with RSI3 overbought at 78.4
        active_metrics = {
            "QQQ": {
                "rsi3": 78.4,
                "rsi7": 62.0,
                "rsi14": 56.0,
                "sma5": 102.0,
                "close": 103.5,
            }
        }

        events = self.monitor.evaluate_active_trade_signals(active_metrics)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "TP1")
        self.assertEqual(events[0]["signal_reason"], "3RSI_OVERBOUGHT_78.4")
        self.assertTrue(pos.tp1_hit)
        self.assertEqual(pos.remaining_quantity, 5.0)
        self.assertEqual(pos.stop_loss, 100.0)  # Breakeven stop

    def test_evaluate_active_trade_signals_rsi3_extreme_overbought_tp2(self):
        """Connors 3-RSI extreme overbought (>= 82.0) after TP1 triggers TP2 trim."""
        pos = TrackedPosition(
            ticker="QQQ3l_EQ",
            underlying="QQQ",
            direction="LONG",
            entry_price=100.0,
            current_price=106.0,
            original_quantity=10.0,
            remaining_quantity=5.0,
            stop_loss=100.0,
            trailing_stop_loss=100.0,
            highest_price=106.0,
            lowest_price=100.0,
            tp1=105.0,
            tp2=110.0,
            tp3=115.0,
            atr=2.5,
            tp1_hit=True,
        )
        self.monitor.positions["QQQ3l_EQ"] = pos
        self.monitor._execute_sell = MagicMock(return_value=True)
        self.monitor.place_broker_stop = MagicMock(return_value="NEW_TP1_STOP")
        self.monitor.place_broker_tp_limit = MagicMock(return_value="NEW_TP3_LIMIT")

        active_metrics = {
            "QQQ": {
                "rsi3": 85.0,
                "rsi7": 72.0,
                "rsi14": 65.0,
                "sma5": 104.0,
                "close": 106.0,
            }
        }

        events = self.monitor.evaluate_active_trade_signals(active_metrics)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "TP2")
        self.assertEqual(events[0]["signal_reason"], "3RSI_EXTREME_OVERBOUGHT_85.0")
        self.assertTrue(pos.tp2_hit)
        self.assertEqual(pos.remaining_quantity, 2.5)

    def test_evaluate_active_trade_signals_sma5_reversion_exit(self):
        """Price crossing above 5-day SMA with RSI3 >= 68 in profit triggers TP1 trim."""
        pos = TrackedPosition(
            ticker="QQQ3l_EQ",
            underlying="QQQ",
            direction="LONG",
            entry_price=100.0,
            current_price=102.5,
            original_quantity=10.0,
            remaining_quantity=10.0,
            stop_loss=95.0,
            trailing_stop_loss=95.0,
            highest_price=102.5,
            lowest_price=100.0,
            tp1=105.0,
            tp2=110.0,
            tp3=115.0,
            atr=2.5,
        )
        self.monitor.positions["QQQ3l_EQ"] = pos
        self.monitor._execute_sell = MagicMock(return_value=True)
        self.monitor.place_broker_stop = MagicMock(return_value="NEW_BE_STOP")
        self.monitor.place_broker_tp_limit = MagicMock(return_value="NEW_TP2_LIMIT")

        active_metrics = {
            "QQQ": {
                "rsi3": 69.5,
                "rsi7": 55.0,
                "rsi14": 52.0,
                "sma5": 101.5,  # price (102.5) > sma5 (101.5)
                "close": 102.5,
            }
        }

        events = self.monitor.evaluate_active_trade_signals(active_metrics)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "TP1")
        self.assertEqual(events[0]["signal_reason"], "SMA5_REVERSION_EXIT")
        self.assertTrue(pos.tp1_hit)

    def test_evaluate_active_trade_signals_short_oversold_cover(self):
        """Connors 3-RSI oversold (<= 25.0) on Short in profit triggers TP1 cover."""
        pos = TrackedPosition(
            ticker="QQQ3S_EQ",
            underlying="QQQ",
            direction="SHORT",
            entry_price=100.0,
            current_price=96.0,  # +4% gain for Short
            original_quantity=10.0,
            remaining_quantity=10.0,
            stop_loss=105.0,
            trailing_stop_loss=105.0,
            highest_price=100.0,
            lowest_price=96.0,
            tp1=95.0,
            tp2=90.0,
            tp3=85.0,
            atr=2.5,
        )
        self.monitor.positions["QQQ3S_EQ"] = pos
        self.monitor._execute_sell = MagicMock(return_value=True)
        self.monitor.place_broker_stop = MagicMock(return_value="NEW_BE_STOP")
        self.monitor.place_broker_tp_limit = MagicMock(return_value="NEW_TP2_LIMIT")

        active_metrics = {
            "QQQ": {
                "rsi3": 21.0,
                "rsi7": 35.0,
                "rsi14": 42.0,
                "sma5": 98.0,
                "close": 96.0,
            }
        }

        events = self.monitor.evaluate_active_trade_signals(active_metrics)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "TP1")
        self.assertEqual(events[0]["signal_reason"], "3RSI_OVERSOLD_COVER_21.0")
        self.assertTrue(pos.tp1_hit)

    # ── 5. Orchestrator Targeted Active Metrics ────────────────────
    @patch("trading_system.orchestrator.MarketDataProvider")
    def test_orchestrator_fetch_active_trades_metrics(self, mock_mdp):
        """fetch_active_trades_metrics queries ONLY active trade underlyings."""
        mock_mdp.get_daily_candles.return_value = MagicMock()
        mock_mdp.get_hourly_candles.return_value = MagicMock()
        mock_mdp.compute_indicators.return_value = {
            "close": 125.0,
            "rsi3": 72.0,
            "sma5": 124.0,
        }

        pos1 = TrackedPosition(
            ticker="3NVDl_EQ",
            underlying="NVDA",
            direction="LONG",
            entry_price=120.0,
            original_quantity=10.0,
            remaining_quantity=10.0,
            stop_loss=114.0,
            trailing_stop_loss=114.0,
            highest_price=120.0,
            lowest_price=120.0,
            tp1=126.0,
            tp2=132.0,
            tp3=138.0,
            atr=3.0,
        )
        self.monitor.positions["3NVDl_EQ"] = pos1

        orch = Orchestrator(
            dry_run=True,
            position_monitor=self.monitor,
            enable_dashboard=False,
        )
        metrics = orch.fetch_active_trades_metrics()
        self.assertIn("NVDA", metrics)
        self.assertEqual(metrics["NVDA"]["close"], 125.0)
        self.assertEqual(metrics["NVDA"]["rsi3"], 72.0)
        orch.shutdown()


if __name__ == "__main__":
    unittest.main()
