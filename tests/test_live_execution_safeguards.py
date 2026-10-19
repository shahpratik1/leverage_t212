"""Unit tests for live execution safeguards, market data multi-source cascade, and price calibration."""

import os
import tempfile
import unittest
from unittest.mock import MagicMock
from trading_system.agents.risk_manager import DecisionCard, RiskManager
from trading_system.data.trading212_client import MarketDataProvider, OrderResult, Trading212Client
from trading_system.execution.position_monitor import PositionMonitor, TrackedPosition
from trading_system.notifications.notifier import Notifier
from trading_system.orchestrator import Orchestrator


def _create_test_card(
    ticker: str = "NVDA",
    trading212_ticker: str = "3NVDl_EQ",
    direction: str = "LONG",
    decision: str = "BUY_ENTRY",
    entry_price: float = 130.0,
    stop_loss: float = 123.50,
    take_profit_1: float = 136.50,
    take_profit_2: float = 143.00,
    take_profit_3: float = 149.50,
    shares: float = 10.0,
) -> DecisionCard:
    return DecisionCard(
        ticker=ticker,
        trading212_ticker=trading212_ticker,
        direction=direction,
        decision=decision,
        entry_price=entry_price,
        stop_loss=stop_loss,
        trailing_stop_loss=stop_loss,
        stop_distance_pct=abs(entry_price - stop_loss) / entry_price if entry_price > 0 else 0.05,
        take_profit_1=take_profit_1,
        take_profit_2=take_profit_2,
        take_profit_3=take_profit_3,
        position_size_shares=shares,
        position_notional=entry_price * shares,
        risk_amount=abs(entry_price - stop_loss) * shares,
        risk_pct_of_equity=0.005,
        conviction_score=0.85,
        rationale="Unit test card",
    )


class TestLiveExecutionSafeguards(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.temp_dir.name, "active_trades.json")
        self.log_file = os.path.join(self.temp_dir.name, "notifications.jsonl")
        self.trades_file = os.path.join(self.temp_dir.name, "trades.jsonl")

        self.mock_t212 = MagicMock(spec=Trading212Client)
        self.mock_t212.is_market_open.return_value = True
        self.mock_t212.cancel_order.return_value = True
        self.notifier = Notifier(log_file=self.log_file, enable_desktop=False)
        self.risk_manager = RiskManager()

        self.monitor = PositionMonitor(
            t212_client=self.mock_t212,
            notifier=self.notifier,
            risk_manager=self.risk_manager,
            state_file=self.state_file,
            dry_run=True,
        )
        self.orchestrator_live = Orchestrator(
            dry_run=False,
            notifier=self.notifier,
            position_monitor=self.monitor,
            trades_log=self.trades_file,
        )
        self.orchestrator_live.t212 = self.mock_t212
        self.orchestrator_live.risk_manager = self.risk_manager

    def tearDown(self):
        MarketDataProvider.LAST_FETCH_WAS_SYNTHETIC = False
        self.temp_dir.cleanup()

    def test_synthetic_data_blocks_live_order(self):
        """CRITICAL: Live execution must be strictly refused if market data is synthetic."""
        MarketDataProvider.LAST_FETCH_WAS_SYNTHETIC = True
        card = _create_test_card()
        success = self.orchestrator_live.execute_trade(card, available_cash=5000.0, positions=[])
        self.assertFalse(success)
        self.mock_t212.place_market_order.assert_not_called()

    def test_synthetic_data_allowed_in_dry_run(self):
        """Dry-run simulation is permitted to run with synthetic candles."""
        MarketDataProvider.LAST_FETCH_WAS_SYNTHETIC = True
        orch_dry = Orchestrator(
            dry_run=True,
            notifier=self.notifier,
            position_monitor=self.monitor,
            trades_log=self.trades_file,
        )
        orch_dry.t212 = self.mock_t212
        orch_dry.risk_manager = self.risk_manager
        card = _create_test_card()
        success = orch_dry.execute_trade(card, available_cash=5000.0, positions=[])
        self.assertTrue(success)

    def test_fill_price_calibrates_position_levels(self):
        """When an actual ETP fill price is provided, entry, SL, and TP are rescaled."""
        card = _create_test_card(
            entry_price=130.0,
            stop_loss=123.50,     # -5%
            take_profit_1=136.50, # +5%
            take_profit_2=143.00, # +10%
            take_profit_3=149.50, # +15%
        )
        pos = self.monitor.register_new_trade(card, atr=3.0, fill_price=34.50)
        self.assertEqual(pos.entry_price, 34.50)
        self.assertEqual(pos.stop_loss, 32.77)  # 34.50 * 0.95 = 32.775 -> 32.77
        self.assertEqual(pos.tp1, 36.23)        # 34.50 * 1.05 = 36.225 -> 36.22
        self.assertEqual(pos.tp2, 37.95)        # 34.50 * 1.10 = 37.95
        self.assertEqual(pos.tp3, 39.67)        # 34.50 * 1.15 = 39.675 -> 39.67

    def test_scale_calibration_prevents_false_stop_out(self):
        """Initial tick with >30% scale mismatch recalibrates instead of liquidating."""
        card = _create_test_card(
            entry_price=130.0,
            stop_loss=123.50,
            take_profit_1=136.50,
            take_profit_2=143.00,
            take_profit_3=149.50,
        )
        pos = self.monitor.register_new_trade(card, atr=3.0)
        self.assertEqual(pos.entry_price, 130.0)

        # First tick arrives with live ETP price ($34.50). Under old logic, 34.50 <= 123.50 stopped out immediately.
        events = self.monitor.monitor_tick(current_prices={"3NVDl_EQ": 34.50})

        # Must NOT be stopped out
        self.assertFalse(pos.is_closed)
        self.assertEqual(len(events), 0)
        # Position must be recalibrated to 34.50 scale
        self.assertEqual(pos.entry_price, 34.50)
        self.assertEqual(pos.stop_loss, 32.77)
        self.assertEqual(pos.tp1, 36.23)

    def test_gbx_pence_normalization(self):
        """LSE ETPs quoting in GBX pence (e.g. 9173.0) are normalized to base currency."""
        card = _create_test_card(
            ticker="TY",
            trading212_ticker="3TYSl_EQ",
            entry_price=91.73,
            stop_loss=87.14,
            take_profit_1=96.32,
            take_profit_2=100.90,
            take_profit_3=105.49,
            shares=5.0,
        )
        pos = self.monitor.register_new_trade(card, atr=1.5)
        self.assertEqual(pos.entry_price, 91.73)

        # First tick arrives in GBX pence (9173.0 pence = £91.73)
        events = self.monitor.monitor_tick(current_prices={"3TYSl_EQ": 9173.0})

        self.assertFalse(pos.is_closed)
        self.assertEqual(len(events), 0)

    def test_market_session_info_lse_hours(self):
        """LSE ETPs trade 08:00-16:30 UTC weekdays only."""
        from datetime import datetime, timezone
        wed_open = datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc)
        wed_closed = datetime(2026, 10, 14, 18, 0, tzinfo=timezone.utc)
        sat_closed = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)

        info_open = Trading212Client.get_market_session_info("QQQ3l_EQ", now_utc=wed_open)
        info_closed = Trading212Client.get_market_session_info("QQQ3l_EQ", now_utc=wed_closed)
        info_weekend = Trading212Client.get_market_session_info("QQQ3l_EQ", now_utc=sat_closed)

        self.assertTrue(info_open["is_open"])
        self.assertFalse(info_closed["is_open"])
        self.assertFalse(info_weekend["is_open"])
        self.assertTrue(info_weekend["is_weekend"])

    def test_market_session_info_us_hours(self):
        """US equities trade 14:30-21:00 UTC weekdays only."""
        from datetime import datetime, timezone
        wed_morning = datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc)  # Closed
        wed_afternoon = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)  # Open

        info_morning = Trading212Client.get_market_session_info("NVDA_US_EQ", now_utc=wed_morning)
        info_afternoon = Trading212Client.get_market_session_info("NVDA_US_EQ", now_utc=wed_afternoon)

        self.assertFalse(info_morning["is_open"])
        self.assertTrue(info_afternoon["is_open"])

    def test_closed_market_blocks_live_order(self):
        """Live trade execution is aborted if the instrument's market is closed."""
        self.mock_t212.is_market_open.return_value = False
        card = _create_test_card()
        success = self.orchestrator_live.execute_trade(card, available_cash=5000.0, positions=[])
        self.assertFalse(success)
        self.mock_t212.place_market_order.assert_not_called()

    def test_short_etp_fill_price_calibrates_correctly(self):
        """Inverse Short ETPs (direction=SHORT, traded on T212) must have stop_loss < entry < tp1."""
        card = _create_test_card(
            ticker="NVDA",
            trading212_ticker="3SNVl_EQ",
            direction="SHORT",
            entry_price=130.0,
            stop_loss=136.50,     # +5% on underlying
            take_profit_1=123.50, # -5% on underlying
            take_profit_2=117.00, # -10% on underlying
            take_profit_3=110.50, # -15% on underlying
        )
        pos = self.monitor.register_new_trade(card, atr=3.0, fill_price=15.00)
        self.assertEqual(pos.entry_price, 15.00)
        # For the inverse ETP, an increasing price is profit!
        self.assertTrue(pos.stop_loss < pos.entry_price)
        self.assertTrue(pos.tp1 > pos.entry_price)
        self.assertEqual(pos.stop_loss, 14.25)  # 15.00 * (1 - 0.05) = 14.25
        self.assertEqual(pos.tp1, 15.75)        # 15.00 * (1 + 0.05) = 15.75
        self.assertEqual(pos.tp2, 16.50)        # 15.00 * (1 + 0.10) = 16.50
        self.assertEqual(pos.tp3, 17.25)        # 15.00 * (1 + 0.15) = 17.25

    def test_subsequent_tick_crash_is_not_recalibrated(self):
        """A >30% drop after initial calibration must trigger Stop Loss, NOT reset entry levels."""
        card = _create_test_card(
            entry_price=100.0,
            stop_loss=95.0,
            take_profit_1=105.0,
        )
        pos = self.monitor.register_new_trade(card, atr=2.0, fill_price=100.0)
        self.assertTrue(pos.scale_calibrated)

        # Flash crash occurs (price drops from 100 to 65, >30% drop)
        events = self.monitor.monitor_tick(current_prices={"3NVDl_EQ": 65.0})

        # Must trigger STOP_EXIT, NOT recalibrate entry to 65.0
        stop_events = [e for e in events if e.get("event") == "STOP_EXIT"]
        self.assertEqual(len(stop_events), 1)
        self.assertTrue(pos.is_closed)
        self.assertEqual(pos.entry_price, 100.0)  # Entry remains 100.0, not reset to 65.0

    def test_gbx_case_b_normalization(self):
        """When registered entry is in pence (>500) and live price is pounds (<200), levels are normalized."""
        card = _create_test_card(
            ticker="TY",
            trading212_ticker="3TYSl_EQ",
            entry_price=9173.0,  # registered in pence
            stop_loss=8714.0,
            take_profit_1=9632.0,
            take_profit_2=10090.0,
            take_profit_3=10549.0,
        )
        pos = self.monitor.register_new_trade(card, atr=150.0)
        pos.tp1_escape_threshold = 9700.0

        # Tick arrives in base currency (£91.73)
        events = self.monitor.monitor_tick(current_prices={"3TYSl_EQ": 91.73})

        self.assertFalse(pos.is_closed)
        self.assertAlmostEqual(pos.entry_price, 91.73, places=2)
        self.assertAlmostEqual(pos.stop_loss, 87.14, places=2)
        self.assertAlmostEqual(pos.tp1, 96.32, places=2)
        self.assertAlmostEqual(pos.tp1_escape_threshold, 97.00, places=2)


if __name__ == "__main__":
    unittest.main()

