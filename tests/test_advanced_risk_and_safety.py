"""Unit tests for Advanced Speed, Security, and Calculated Risk features:
1. Real-time Bid-Ask Spread & Liquidity Gate
2. Sector Concentration & Aggregate Portfolio Beta Heat Limits
3. Daily Portfolio Drawdown Circuit Breaker
4. Local Incremental Candle Cache
5. Broker-State Portfolio Reconciliation Loop
6. Emergency Panic Flatten & Remote Kill-Switch
"""

import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from trading_system.agents.risk_manager import DecisionCard, RiskManager
from trading_system.config import settings
from trading_system.data.leveraged_universe import LeveragedUniverse
from trading_system.data.trading212_client import (
    AccountCash,
    MarketDataProvider,
    Position,
    Trading212Client,
)
from trading_system.execution.position_monitor import PositionMonitor, TrackedPosition
from trading_system.notifications.notifier import Notifier
from trading_system.orchestrator import Orchestrator


class TestBidAskSpreadGate(unittest.TestCase):
    """Test Bid-Ask spread gate blocks trades with excessive slippage/spread."""

    def setUp(self):
        self.rm = RiskManager(
            enforce_spread_gate=True,
            max_bid_ask_spread_pct=0.0035,  # 0.35% max spread
        )
        self.card = DecisionCard(
            ticker="QQQ",
            trading212_ticker="QQQ3l_EQ",
            direction="LONG",
            decision="BUY_ENTRY",
            entry_price=100.0,
            stop_loss=95.0,
            trailing_stop_loss=95.0,
            stop_distance_pct=0.05,
            take_profit_1=105.0,
            take_profit_2=110.0,
            take_profit_3=115.0,
            position_size_shares=10.0,
            position_notional=1000.0,
            risk_amount=50.0,
            risk_pct_of_equity=0.005,
            conviction_score=0.85,
            rationale="High conviction Connors 3-RSI",
        )

    def test_spread_within_threshold_passes(self):
        # 0.20% spread <= 0.35% threshold
        valid, reason = self.rm.validate_trade(
            card=self.card,
            account_cash=5000.0,
            existing_positions=[],
            spread_pct=0.0020,
        )
        self.assertTrue(valid)
        self.assertEqual(reason, "All checks passed")

    def test_spread_exceeding_threshold_rejected(self):
        # 0.50% spread > 0.35% threshold
        valid, reason = self.rm.validate_trade(
            card=self.card,
            account_cash=5000.0,
            existing_positions=[],
            spread_pct=0.0050,
        )
        self.assertFalse(valid)
        self.assertIn("Bid-ask spread too wide", reason)
        self.assertIn("0.50%", reason)

    def test_spread_gate_disabled(self):
        rm_disabled = RiskManager(enforce_spread_gate=False)
        valid, reason = rm_disabled.validate_trade(
            card=self.card,
            account_cash=5000.0,
            existing_positions=[],
            spread_pct=0.0150,  # 1.5% spread
        )
        self.assertTrue(valid)


class TestSectorAndBetaHeatLimits(unittest.TestCase):
    """Test Sector Concentration and Aggregate Portfolio Beta Heat ceilings."""

    def setUp(self):
        self.rm = RiskManager(
            enforce_correlation_limits=True,
            max_sector_positions=2,
            max_portfolio_beta_heat=3.0,
        )
        self.card_nvda = DecisionCard(
            ticker="NVDA",
            trading212_ticker="3LNVl_EQ",
            direction="LONG",
            decision="BUY_ENTRY",
            entry_price=100.0,
            stop_loss=95.0,
            trailing_stop_loss=95.0,
            stop_distance_pct=0.05,
            take_profit_1=105.0,
            take_profit_2=110.0,
            take_profit_3=115.0,
            position_size_shares=50.0,
            position_notional=5000.0,  # $5,000 notional * 3x = $15,000 beta notional
            risk_amount=250.0,
            risk_pct_of_equity=0.005,
            conviction_score=0.88,
            rationale="Tech breakout",
            leverage=3,
        )

    def test_sector_concentration_blocks_excessive_sector_positions(self):
        # Existing positions already contain 2 Tech Equity positions (AAPL and MSFT)
        pos_aapl = Position(
            ticker="AAP3l_EQ",
            quantity=10.0,
            average_price=100.0,
            current_price=102.0,
            ppl=20.0,
            max_buy=100.0,
            max_sell=100.0,
        )
        pos_msft = Position(
            ticker="MSF3l_EQ",
            quantity=10.0,
            average_price=100.0,
            current_price=101.0,
            ppl=10.0,
            max_buy=100.0,
            max_sell=100.0,
        )

        valid, reason = self.rm.validate_trade(
            card=self.card_nvda,
            account_cash=20000.0,
            existing_positions=[pos_aapl, pos_msft],
            account_equity=20000.0,
        )
        self.assertFalse(valid)
        self.assertIn("Sector concentration limit reached", reason)

    def test_different_sector_passes(self):
        # Existing positions are Commodity (GLD) and Fixed Income (TLT)
        pos_gld = Position(
            ticker="3GLEl_EQ",
            quantity=10.0,
            average_price=100.0,
            current_price=100.0,
            ppl=0.0,
            max_buy=100.0,
            max_sell=100.0,
        )
        pos_tlt = Position(
            ticker="3TYLl_EQ",
            quantity=10.0,
            average_price=100.0,
            current_price=100.0,
            ppl=0.0,
            max_buy=100.0,
            max_sell=100.0,
        )

        valid, reason = self.rm.validate_trade(
            card=self.card_nvda,
            account_cash=20000.0,
            existing_positions=[pos_gld, pos_tlt],
            account_equity=20000.0,
        )
        self.assertTrue(valid)

    def test_aggregate_beta_heat_ceiling_blocks_overleveraged_portfolio(self):
        # Account equity = $10,000. Max beta heat = 3.0x -> max beta notional = $30,000.
        # Suppose existing positions already have $20,000 beta notional.
        # Candidate has $5,000 * 3x = $15,000 beta notional.
        # Total = $35,000 / $10,000 = 3.50x > 3.0x limit!
        pos_existing = Position(
            ticker="QQQ3l_EQ",
            quantity=66.67,
            average_price=100.0,
            current_price=100.0,
            ppl=0.0,
            max_buy=100.0,
            max_sell=100.0,
        )  # $6,667 * 3x = ~$20,000 beta notional

        valid, reason = self.rm.validate_trade(
            card=self.card_nvda,
            account_cash=10000.0,
            existing_positions=[pos_existing],
            account_equity=10000.0,
        )
        self.assertFalse(valid)
        self.assertIn("Aggregate portfolio beta heat ceiling exceeded", reason)


class TestDailyDrawdownCircuitBreaker(unittest.TestCase):
    """Test Daily Drawdown Circuit Breaker trips and halts trading on -2.0% loss."""

    def setUp(self):
        self.rm = RiskManager(
            enforce_daily_circuit_breaker=True,
            daily_max_drawdown_pct=0.02,  # 2.0% max loss
        )
        self.card = DecisionCard(
            ticker="QQQ",
            trading212_ticker="QQQ3l_EQ",
            direction="LONG",
            decision="BUY_ENTRY",
            entry_price=100.0,
            stop_loss=95.0,
            trailing_stop_loss=95.0,
            stop_distance_pct=0.05,
            take_profit_1=105.0,
            take_profit_2=110.0,
            take_profit_3=115.0,
            position_size_shares=10.0,
            position_notional=1000.0,
            risk_amount=50.0,
            risk_pct_of_equity=0.005,
            conviction_score=0.85,
            rationale="Setup",
        )

    def test_circuit_breaker_trips_on_steep_drawdown(self):
        start_equity = 10000.0
        # Normal check at start
        tripped, _ = self.rm.check_circuit_breaker(current_equity=10000.0, session_start_equity=start_equity)
        self.assertFalse(tripped)

        # Drawdown to $9,750 (-2.5% loss > 2.0% threshold)
        tripped, msg = self.rm.check_circuit_breaker(current_equity=9750.0)
        self.assertTrue(tripped)
        self.assertIn("CIRCUIT BREAKER TRIPPED", msg)
        self.assertTrue(self.rm.is_circuit_breaker_tripped)

        # Validate trade should now be rejected
        valid, reason = self.rm.validate_trade(
            card=self.card,
            account_cash=5000.0,
            existing_positions=[],
        )
        self.assertFalse(valid)
        self.assertIn("Daily circuit breaker tripped", reason)

    def test_circuit_breaker_reset(self):
        self.rm.check_circuit_breaker(current_equity=9700.0, session_start_equity=10000.0)
        self.assertTrue(self.rm.is_circuit_breaker_tripped)

        # Reset for next session day
        self.rm.reset_circuit_breaker(new_session_equity=9700.0)
        self.assertFalse(self.rm.is_circuit_breaker_tripped)

        valid, _ = self.rm.validate_trade(
            card=self.card,
            account_cash=5000.0,
            existing_positions=[],
        )
        self.assertTrue(valid)


class TestIncrementalCandleCache(unittest.TestCase):
    """Test in-memory candle caching and speedup."""

    def setUp(self):
        MarketDataProvider.clear_candle_cache()

    def test_candle_caching_returns_cached_data_quickly(self):
        # First call fetches or generates
        t0 = time.time()
        candles_1 = MarketDataProvider.get_daily_candles("QQQ", period="1y")
        elapsed_1 = time.time() - t0

        # Second call hits memory cache
        t1 = time.time()
        candles_2 = MarketDataProvider.get_daily_candles("QQQ", period="1y")
        elapsed_2 = time.time() - t1

        self.assertIsNotNone(candles_1)
        self.assertIsNotNone(candles_2)
        # Cache hit should be fast (< 10ms)
        self.assertLess(elapsed_2, 0.05)

    def test_clear_cache_removes_entries(self):
        MarketDataProvider.get_daily_candles("SPY", period="1y")
        self.assertTrue(len(MarketDataProvider._CANDLE_CACHE) > 0)

        MarketDataProvider.clear_candle_cache()
        self.assertEqual(len(MarketDataProvider._CANDLE_CACHE), 0)


class TestBrokerReconciliationLoop(unittest.TestCase):
    """Test broker-state reconciliation detects stopped-out and external positions."""

    def setUp(self):
        self.mock_t212 = MagicMock(spec=Trading212Client)
        self.mock_notifier = MagicMock(spec=Notifier)
        self.mock_rm = MagicMock(spec=RiskManager)
        self.test_state = "test_reconcile_active.json"
        if os.path.exists(self.test_state):
            os.remove(self.test_state)

        self.monitor = PositionMonitor(
            t212_client=self.mock_t212,
            notifier=self.mock_notifier,
            risk_manager=self.mock_rm,
            state_file=self.test_state,
            dry_run=False,
        )

    def tearDown(self):
        if os.path.exists(self.test_state):
            os.remove(self.test_state)

    def test_reconciliation_detects_externally_closed_position(self):
        card = DecisionCard(
            ticker="QQQ",
            trading212_ticker="QQQ3l_EQ",
            direction="LONG",
            decision="BUY_ENTRY",
            entry_price=100.0,
            stop_loss=95.0,
            trailing_stop_loss=95.0,
            stop_distance_pct=0.05,
            take_profit_1=105.0,
            take_profit_2=110.0,
            take_profit_3=115.0,
            position_size_shares=10.0,
            position_notional=1000.0,
            risk_amount=50.0,
            risk_pct_of_equity=0.005,
            conviction_score=0.85,
            rationale="Test",
        )
        pos = self.monitor.register_new_trade(card)
        pos.broker_stop_order_id = "STOP_123"

        # Mock broker portfolio returning EMPTY (e.g. stopped out natively on T212)
        self.mock_t212.get_portfolio.return_value = []
        self.mock_t212.cancel_order.return_value = True

        summary = self.monitor.reconcile_with_broker()

        self.assertIn("QQQ3l_EQ", summary["closed_externally"])
        self.assertTrue(self.monitor.positions["QQQ3l_EQ"].is_closed)
        self.assertEqual(self.monitor.positions["QQQ3l_EQ"].remaining_quantity, 0.0)

    def test_reconciliation_synchronizes_partial_quantity(self):
        card = DecisionCard(
            ticker="NVDA",
            trading212_ticker="3LNVl_EQ",
            direction="LONG",
            decision="BUY_ENTRY",
            entry_price=100.0,
            stop_loss=95.0,
            trailing_stop_loss=95.0,
            stop_distance_pct=0.05,
            take_profit_1=105.0,
            take_profit_2=110.0,
            take_profit_3=115.0,
            position_size_shares=20.0,
            position_notional=2000.0,
            risk_amount=100.0,
            risk_pct_of_equity=0.005,
            conviction_score=0.85,
            rationale="Test",
        )
        self.monitor.register_new_trade(card)

        # Broker reports only 10 shares remaining (e.g. partial manual sell)
        broker_pos = Position(
            ticker="3LNVl_EQ",
            quantity=10.0,
            average_price=100.0,
            current_price=105.0,
            ppl=50.0,
            max_buy=100.0,
            max_sell=100.0,
        )
        self.mock_t212.get_portfolio.return_value = [broker_pos]

        summary = self.monitor.reconcile_with_broker()

        self.assertEqual(self.monitor.positions["3LNVl_EQ"].remaining_quantity, 10.0)
        self.assertFalse(self.monitor.positions["3LNVl_EQ"].is_closed)
        self.assertEqual(len(summary["quantity_synced"]), 1)

    def test_reconciliation_adopts_untracked_broker_positions(self):
        # Broker has an untracked position in 3TSMl_EQ (TSM) and 3SEMl_EQ (SOXX)
        pos_tsm = Position(
            ticker="3TSMl_EQ",
            quantity=1.21,
            average_price=1138.0,
            current_price=1140.0,
            ppl=2.42,
            max_buy=10.0,
            max_sell=10.0,
        )
        pos_sem = Position(
            ticker="3SEMl_EQ",
            quantity=1.01,
            average_price=94.02,
            current_price=95.0,
            ppl=0.98,
            max_buy=10.0,
            max_sell=10.0,
        )
        self.mock_t212.get_portfolio.return_value = [pos_tsm, pos_sem]

        summary = self.monitor.reconcile_with_broker(adopt_untracked=True)

        self.assertIn("3TSMl_EQ", summary["adopted_from_broker"])
        self.assertIn("3SEMl_EQ", summary["adopted_from_broker"])
        self.assertIn("3TSMl_EQ", self.monitor.positions)
        self.assertIn("3SEMl_EQ", self.monitor.positions)

        adopted = self.monitor.positions["3TSMl_EQ"]
        self.assertEqual(adopted.underlying, "TSM")
        self.assertEqual(adopted.direction, "LONG")
        self.assertEqual(adopted.entry_price, 1138.0)
        self.assertEqual(adopted.remaining_quantity, 1.21)
        self.assertGreater(adopted.tp1, adopted.entry_price)
        self.assertLess(adopted.stop_loss, adopted.entry_price)
        self.assertEqual(adopted.strategy_type, "broker_placed")

    def test_reconciliation_links_existing_broker_stop_orders(self):
        # Position with existing pending stop order on broker
        pos_lzp = Position(
            ticker="3LZPl_EQ",
            quantity=2.0,
            average_price=3820.0,
            current_price=4200.0,
            ppl=760.0,
            max_buy=10.0,
            max_sell=10.0,
        )
        self.mock_t212.get_portfolio.return_value = [pos_lzp]
        self.mock_t212.get_orders.return_value = [
            {
                "id": "56750363542",
                "ticker": "3LZPl_EQ",
                "type": "STOP",
                "stopPrice": 3593.25,
                "quantity": -2.0,
            }
        ]

        summary = self.monitor.reconcile_with_broker(adopt_untracked=True)
        self.assertIn("3LZPl_EQ", summary["adopted_from_broker"])
        lzp_pos = self.monitor.positions["3LZPl_EQ"]
        self.assertEqual(lzp_pos.broker_stop_order_id, "56750363542")
        self.assertEqual(lzp_pos.stop_loss, 3593.25)
        # Stop distance is 3820 - 3593.25 = 226.75, so TP1 = 3820 + 226.75 = 4046.75
        self.assertEqual(lzp_pos.tp1, 4046.75)



class TestEmergencyPanicFlatten(unittest.TestCase):
    """Test emergency panic flatten liquidates all positions and halts trading."""

    def setUp(self):
        self.mock_t212 = MagicMock(spec=Trading212Client)
        self.mock_notifier = MagicMock(spec=Notifier)
        self.mock_rm = MagicMock(spec=RiskManager)
        self.test_state = "test_panic_active.json"
        if os.path.exists(self.test_state):
            os.remove(self.test_state)
        if os.path.exists(f"{self.test_state}.halted"):
            os.remove(f"{self.test_state}.halted")

        self.monitor = PositionMonitor(
            t212_client=self.mock_t212,
            notifier=self.mock_notifier,
            risk_manager=self.mock_rm,
            state_file=self.test_state,
            dry_run=False,
        )

    def tearDown(self):
        if os.path.exists(self.test_state):
            os.remove(self.test_state)
        if os.path.exists(f"{self.test_state}.halted"):
            os.remove(f"{self.test_state}.halted")

    def test_panic_flatten_cancels_stops_and_sells_positions(self):
        card = DecisionCard(
            ticker="QQQ",
            trading212_ticker="QQQ3l_EQ",
            direction="LONG",
            decision="BUY_ENTRY",
            entry_price=100.0,
            stop_loss=95.0,
            trailing_stop_loss=95.0,
            stop_distance_pct=0.05,
            take_profit_1=105.0,
            take_profit_2=110.0,
            take_profit_3=115.0,
            position_size_shares=15.0,
            position_notional=1500.0,
            risk_amount=75.0,
            risk_pct_of_equity=0.005,
            conviction_score=0.85,
            rationale="Test",
        )
        pos = self.monitor.register_new_trade(card)
        pos.broker_stop_order_id = "STOP_999"

        self.mock_t212.place_market_order.return_value = MagicMock(id="SELL_999", fill_price=100.0)
        self.mock_t212.cancel_order.return_value = True

        results = self.monitor.panic_flatten_all()

        self.assertTrue(self.monitor.is_halted)
        self.assertEqual(results["cancelled_stops"], 1)
        self.assertEqual(len(results["liquidated_positions"]), 1)
        self.assertTrue(pos.is_closed)
        self.assertEqual(pos.remaining_quantity, 0.0)

        # Monitor tick should be blocked when halted
        tick_events = self.monitor.monitor_tick({"QQQ3l_EQ": 105.0})
        self.assertEqual(tick_events, [])


class TestPositionMonitorPermanentSync(unittest.TestCase):
    """Test state persistence and dashboard sync strictly exclude closed and phantom positions."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.temp_dir.name, "test_active_trades.json")
        self.mock_t212 = MagicMock(spec=Trading212Client)
        self.mock_notifier = MagicMock(spec=Notifier)
        self.mock_rm = MagicMock(spec=RiskManager)
        self.monitor = PositionMonitor(
            t212_client=self.mock_t212,
            notifier=self.mock_notifier,
            risk_manager=self.mock_rm,
            state_file=self.state_file,
            dry_run=False,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_closed_positions_excluded_from_state_save(self):
        """_save_state never writes closed or zero-quantity positions to active_trades.json."""
        pos = TrackedPosition(
            ticker="QQQ3l_EQ",
            underlying="QQQ",
            direction="LONG",
            entry_price=100.0,
            original_quantity=10.0,
            remaining_quantity=0.0,
            stop_loss=95.0,
            trailing_stop_loss=95.0,
            highest_price=105.0,
            lowest_price=100.0,
            tp1=105.0,
            tp2=110.0,
            tp3=115.0,
            atr=2.5,
            is_closed=True,
        )
        self.monitor.positions["QQQ3l_EQ"] = pos
        self.monitor._save_state()

        with open(self.state_file, "r", encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved, {}, "Closed position must not be persisted in active_trades.json")

    def test_load_state_auto_heals_disk_when_closed_positions_exist(self):
        """_load_state auto-heals state file by stripping closed records from disk."""
        legacy_data = {
            "CLOSED_TICKER": {
                "ticker": "CLOSED_TICKER",
                "underlying": "SPY",
                "direction": "LONG",
                "entry_price": 100.0,
                "original_quantity": 5.0,
                "remaining_quantity": 0.0,
                "stop_loss": 95.0,
                "trailing_stop_loss": 95.0,
                "highest_price": 100.0,
                "lowest_price": 100.0,
                "tp1": 105.0,
                "tp2": 110.0,
                "tp3": 115.0,
                "atr=2.0": 2.0,
                "is_closed": True,
            },
            "ACTIVE_TICKER": {
                "ticker": "ACTIVE_TICKER",
                "underlying": "QQQ",
                "direction": "LONG",
                "entry_price": 200.0,
                "original_quantity": 5.0,
                "remaining_quantity": 5.0,
                "stop_loss": 190.0,
                "trailing_stop_loss": 190.0,
                "highest_price": 200.0,
                "lowest_price": 200.0,
                "tp1": 210.0,
                "tp2": 220.0,
                "tp3": 230.0,
                "atr": 3.0,
                "is_closed": False,
            },
        }
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(legacy_data, f)

        loaded = self.monitor._load_state()
        self.assertIn("ACTIVE_TICKER", loaded)
        self.assertNotIn("CLOSED_TICKER", loaded)

        # Verify auto-heal wrote cleaned dictionary back to disk
        with open(self.state_file, "r", encoding="utf-8") as f:
            healed = json.load(f)
        self.assertIn("ACTIVE_TICKER", healed)
        self.assertNotIn("CLOSED_TICKER", healed)

    def test_load_active_signals_filters_closed_and_zero_quantity(self):
        """load_active_signals strictly returns only live open positions."""
        from trading_system.dashboard.server import load_active_signals

        dirty_data = {
            "DEAD_1": {"entry_price": 100.0, "is_closed": True, "remaining_quantity": 0.0},
            "DEAD_2": {"entry_price": 100.0, "is_closed": False, "remaining_quantity": 0.0},
            "LIVE_1": {"entry_price": 100.0, "is_closed": False, "remaining_quantity": 5.0, "direction": "LONG"},
        }
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(dirty_data, f)

        signals = load_active_signals(self.state_file)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["ticker"], "LIVE_1")


if __name__ == "__main__":
    unittest.main()
