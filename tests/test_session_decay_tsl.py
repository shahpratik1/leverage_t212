"""Unit tests for Dynamic Volatility-Drag Time-Decay Trailing Stop Envelope.

Verifies:
1. State Schema & Dataclass defaults:
   - TrackedPosition initializes session_count=1, is_runner=False, and last_session_date.
   - Legacy state files without decay attributes load safely with backward compatibility.
2. Dynamic Session Decay Multipliers in RiskManager:
   - Non-runner ATR multiplier compresses: Day 1 (2.20x), Day 2 (1.85x), Day 3 (1.50x), Day 4+ (1.15x floor).
   - Runner exemption freezes decay at 2.00x ATR regardless of session age.
3. Daily Rollover Envelope in PositionMonitor:
   - Increments session_count on business trading days.
   - Skips session increments on weekends and offline exchange holidays.
   - Clamps and ratchets broker stops strictly in the favorable direction (never loosens).
   - Day 4+ Stagnancy Gate: elevates recommended stop directly to Entry Price (Break-Even).
4. Asymmetric Session Close 16:20 UTC Integration:
   - Day 4+ stagnant positions (session_count >= 4, not runner, gain < 1.5%) are liquidated with ZERO_ALPHA_EVACUATION.
   - Tenured runners (is_runner = True) and profitable positions are retained overnight.
5. Dashboard Telemetry:
   - load_active_signals exposes session_count, is_runner, decay_multiplier, and decay_label.
"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from trading_system.agents.risk_manager import DecisionCard, RiskManager
from trading_system.dashboard.server import load_active_signals
from trading_system.data.trading212_client import OrderResult, Trading212Client
from trading_system.execution.position_monitor import (
    OFFLINE_EXCHANGE_HOLIDAYS,
    PositionMonitor,
    TrackedPosition,
    is_exchange_trading_day,
)
from trading_system.notifications.notifier import Notifier


class TestSessionDecayTSL(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.temp_dir.name, "active_trades.json")
        self.log_file = os.path.join(self.temp_dir.name, "notifications.jsonl")

        self.mock_t212 = MagicMock(spec=Trading212Client)
        self.mock_t212.place_stop_order.return_value = OrderResult(
            id="stop_gtc_decay_01",
            ticker="QQQ3l_EQ",
            type="STOP",
            status="NEW",
            quantity=-10.0,
            filled_quantity=0.0,
            fill_price=0.0,
        )
        self.mock_t212.place_market_order.return_value = OrderResult(
            id="market_sell_001",
            ticker="QQQ3l_EQ",
            type="MARKET",
            status="FILLED",
            quantity=-10.0,
            filled_quantity=10.0,
            fill_price=100.0,
        )
        self.mock_t212.cancel_order.return_value = True

        self.notifier = Notifier(log_file=self.log_file, enable_desktop=False)
        self.risk_manager = RiskManager()
        self.monitor = PositionMonitor(
            t212_client=self.mock_t212,
            notifier=self.notifier,
            risk_manager=self.risk_manager,
            state_file=self.state_file,
            dry_run=False,
            pure_tsl_mode=True,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    # ─────────────────────────────────────────────────────────────
    # 1. State Schema & Dataclass Accounting
    # ─────────────────────────────────────────────────────────────
    def test_tracked_position_decay_defaults(self):
        """TrackedPosition initializes with session_count=1, is_runner=False, and today's date."""
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
            atr=2.0,
        )
        self.assertEqual(pos.session_count, 1)
        self.assertFalse(pos.is_runner)
        self.assertTrue(len(pos.last_session_date) >= 10)

    def test_legacy_json_backward_compatibility(self):
        """Older active_trades.json missing decay fields loads cleanly with default values."""
        legacy_data = {
            "QQQ3l_EQ": {
                "ticker": "QQQ3l_EQ",
                "underlying": "QQQ",
                "direction": "LONG",
                "entry_price": 100.0,
                "original_quantity": 10.0,
                "remaining_quantity": 10.0,
                "stop_loss": 95.0,
                "trailing_stop_loss": 95.0,
                "highest_price": 100.0,
                "lowest_price": 100.0,
                "tp1": 105.0,
                "tp2": 110.0,
                "tp3": 115.0,
                "atr": 2.0,
                "is_closed": False,
            }
        }
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(legacy_data, f)

        loaded_monitor = PositionMonitor(
            t212_client=self.mock_t212,
            notifier=self.notifier,
            risk_manager=self.risk_manager,
            state_file=self.state_file,
            dry_run=False,
        )
        pos = loaded_monitor.positions["QQQ3l_EQ"]
        self.assertEqual(pos.session_count, 1)
        self.assertFalse(pos.is_runner)
        self.assertIsInstance(pos.last_session_date, str)

    def test_save_and_load_state_persists_decay_attributes(self):
        """Positions modified with higher session_count and runner flag persist across reloads."""
        pos = TrackedPosition(
            ticker="QQQ3l_EQ",
            underlying="QQQ",
            direction="LONG",
            entry_price=100.0,
            original_quantity=10.0,
            remaining_quantity=10.0,
            stop_loss=95.0,
            trailing_stop_loss=95.0,
            highest_price=108.0,
            lowest_price=99.0,
            tp1=105.0,
            tp2=110.0,
            tp3=115.0,
            atr=2.0,
            session_count=3,
            is_runner=True,
            last_session_date="2026-10-28",
        )
        self.monitor.positions["QQQ3l_EQ"] = pos
        self.monitor._save_state()

        new_monitor = PositionMonitor(
            t212_client=self.mock_t212,
            notifier=self.notifier,
            risk_manager=self.risk_manager,
            state_file=self.state_file,
            dry_run=False,
        )
        loaded = new_monitor.positions["QQQ3l_EQ"]
        self.assertEqual(loaded.session_count, 3)
        self.assertTrue(loaded.is_runner)
        self.assertEqual(loaded.last_session_date, "2026-10-28")

    # ─────────────────────────────────────────────────────────────
    # 2. Dynamic Session Decay Multipliers in RiskManager
    # ─────────────────────────────────────────────────────────────
    def test_decay_atr_multipliers(self):
        """Verifies session compression schedule: 2.20x -> 1.85x -> 1.50x -> 1.15x (floor)."""
        rm = self.risk_manager
        self.assertAlmostEqual(rm.get_decay_atr_multiplier(session_count=1, is_runner=False), 2.20)
        self.assertAlmostEqual(rm.get_decay_atr_multiplier(session_count=2, is_runner=False), 1.85)
        self.assertAlmostEqual(rm.get_decay_atr_multiplier(session_count=3, is_runner=False), 1.50)
        self.assertAlmostEqual(rm.get_decay_atr_multiplier(session_count=4, is_runner=False), 1.15)
        self.assertAlmostEqual(rm.get_decay_atr_multiplier(session_count=5, is_runner=False), 1.15)
        self.assertAlmostEqual(rm.get_decay_atr_multiplier(session_count=10, is_runner=False), 1.15)

    def test_runner_immunity_freezes_decay(self):
        """Runner exemption freezes decay at 2.00x ATR regardless of session age."""
        rm = self.risk_manager
        self.assertAlmostEqual(rm.get_decay_atr_multiplier(session_count=1, is_runner=True), 2.00)
        self.assertAlmostEqual(rm.get_decay_atr_multiplier(session_count=4, is_runner=True), 2.00)
        self.assertAlmostEqual(rm.get_decay_atr_multiplier(session_count=10, is_runner=True), 2.00)

    def test_calculate_trailing_stop_uses_decay_multiplier(self):
        """calculate_trailing_stop respects session_count and is_runner."""
        rm = self.risk_manager
        # Long position: entry 100, peak 110, ATR 2.0
        # Day 1: trail distance = 2.20 * 2.0 = 4.40 -> TSL = 110 - 4.40 = 105.60
        tsl_d1 = rm.calculate_trailing_stop(
            current_price=109.0,
            peak_price=110.0,
            atr=2.0,
            current_tsl=95.0,
            direction="LONG",
            session_count=1,
            is_runner=False,
        )
        self.assertAlmostEqual(tsl_d1, 105.60)

        # Day 3: trail distance = 1.50 * 2.0 = 3.00 -> TSL = 110 - 3.00 = 107.00
        tsl_d3 = rm.calculate_trailing_stop(
            current_price=109.0,
            peak_price=110.0,
            atr=2.0,
            current_tsl=95.0,
            direction="LONG",
            session_count=3,
            is_runner=False,
        )
        self.assertAlmostEqual(tsl_d3, 107.00)

        # Runner on Day 4: trail distance = 2.00 * 2.0 = 4.00 -> TSL = 110 - 4.00 = 106.00
        tsl_runner = rm.calculate_trailing_stop(
            current_price=109.0,
            peak_price=110.0,
            atr=2.0,
            current_tsl=95.0,
            direction="LONG",
            session_count=4,
            is_runner=True,
        )
        self.assertAlmostEqual(tsl_runner, 106.00)

    # ─────────────────────────────────────────────────────────────
    # 3. Calendar & Holiday Validation
    # ─────────────────────────────────────────────────────────────
    def test_exchange_trading_day_calendar(self):
        """Weekdays are trading days; weekends and offline holidays are not."""
        wednesday = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
        self.assertTrue(is_exchange_trading_day(wednesday))

        saturday = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
        sunday = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
        self.assertFalse(is_exchange_trading_day(saturday))
        self.assertFalse(is_exchange_trading_day(sunday))

        christmas = datetime(2026, 12, 25, 12, 0, tzinfo=timezone.utc)
        self.assertFalse(is_exchange_trading_day(christmas))

    # ─────────────────────────────────────────────────────────────
    # 4. Daily Rollover ATR Stop Compression in PositionMonitor
    # ─────────────────────────────────────────────────────────────
    def test_daily_rollover_increments_session_and_compresses_stop(self):
        """Rollover on trading day increments session_count and ratchets stop loss tighter."""
        pos = TrackedPosition(
            ticker="QQQ3l_EQ",
            underlying="QQQ",
            direction="LONG",
            entry_price=100.0,
            original_quantity=10.0,
            remaining_quantity=10.0,
            stop_loss=95.0,
            trailing_stop_loss=95.0,
            highest_price=103.0,
            lowest_price=99.0,
            tp1=105.0,
            tp2=110.0,
            tp3=115.0,
            atr=2.0,
            session_count=1,
            last_session_date="2026-10-27",
            current_price=102.5,
        )
        self.monitor.positions["QQQ3l_EQ"] = pos

        # Mock today as Wednesday 2026-10-28
        mock_now = datetime(2026, 10, 28, 7, 30, tzinfo=timezone.utc)
        with patch("trading_system.execution.position_monitor.datetime") as mock_dt:
            mock_dt.now.return_value = mock_now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)
            results = self.monitor.apply_daily_rollover_stops()

        self.assertEqual(len(results), 1)
        self.assertEqual(pos.session_count, 2)
        self.assertEqual(pos.last_session_date, "2026-10-28")
        # On Day 2, decay multiplier is 1.85
        # Gain is 103 - 100 = 3.0. Half gain = 101.5. Chandelier = 103 - (2.0 * 1.85) = 99.30
        # recommended_sl = max(101.5, 99.30) = 101.50
        self.assertAlmostEqual(pos.trailing_stop_loss, 101.50)
        self.mock_t212.place_stop_order.assert_called_once()

    def test_daily_rollover_weekend_skips_increment(self):
        """Rollover executed on Saturday/Sunday does not increment session_count."""
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
            atr=2.0,
            session_count=1,
            last_session_date="2026-10-30",
            current_price=100.0,
        )
        self.monitor.positions["QQQ3l_EQ"] = pos

        saturday_now = datetime(2026, 10, 31, 7, 30, tzinfo=timezone.utc)
        with patch("trading_system.execution.position_monitor.datetime") as mock_dt:
            mock_dt.now.return_value = saturday_now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)
            self.monitor.apply_daily_rollover_stops()

        self.assertEqual(pos.session_count, 1)

    def test_daily_rollover_stagnant_day4_forces_breakeven(self):
        """Position reaching Day 4 with <1.5% gain elevates stop directly to Entry Price."""
        pos = TrackedPosition(
            ticker="QQQ3l_EQ",
            underlying="QQQ",
            direction="LONG",
            entry_price=100.0,
            original_quantity=10.0,
            remaining_quantity=10.0,
            stop_loss=95.0,
            trailing_stop_loss=95.0,
            highest_price=100.5,
            lowest_price=99.2,
            tp1=105.0,
            tp2=110.0,
            tp3=115.0,
            atr=2.0,
            session_count=3,
            last_session_date="2026-10-27",
            current_price=100.2,
        )
        self.monitor.positions["QQQ3l_EQ"] = pos

        # Thursday 2026-10-29 -> advances session_count from 3 to 4
        mock_now = datetime(2026, 10, 29, 7, 30, tzinfo=timezone.utc)
        with patch("trading_system.execution.position_monitor.datetime") as mock_dt:
            mock_dt.now.return_value = mock_now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)
            results = self.monitor.apply_daily_rollover_stops()

        self.assertEqual(pos.session_count, 4)
        # Gain is +0.2%, session_count >= 4 -> Elevated directly to entry_price (100.00)
        self.assertAlmostEqual(pos.trailing_stop_loss, 100.0)

    def test_stop_invariant_never_loosens_stop(self):
        """If calculated decayed stop is lower than active stop, active stop is preserved."""
        pos = TrackedPosition(
            ticker="QQQ3l_EQ",
            underlying="QQQ",
            direction="LONG",
            entry_price=100.0,
            original_quantity=10.0,
            remaining_quantity=10.0,
            stop_loss=102.0,
            trailing_stop_loss=102.0,  # Already elevated stop
            highest_price=102.5,
            lowest_price=99.0,
            tp1=105.0,
            tp2=110.0,
            tp3=115.0,
            atr=2.0,
            session_count=1,
            last_session_date="2026-10-27",
            current_price=102.2,
        )
        self.monitor.positions["QQQ3l_EQ"] = pos

        mock_now = datetime(2026, 10, 28, 7, 30, tzinfo=timezone.utc)
        with patch("trading_system.execution.position_monitor.datetime") as mock_dt:
            mock_dt.now.return_value = mock_now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)
            results = self.monitor.apply_daily_rollover_stops()

        # Stop should remain at 102.00, not widen down
        self.assertEqual(pos.trailing_stop_loss, 102.0)

    # ─────────────────────────────────────────────────────────────
    # 5. Day 4+ Zero-Alpha Stagnancy Evacuation (16:20 UTC)
    # ─────────────────────────────────────────────────────────────
    def test_1620_stagnant_day4_evacuation(self):
        """Position with session_count >= 4, gain < 1.5%, and not runner is liquidated at 16:20 UTC."""
        pos = TrackedPosition(
            ticker="QQQ3l_EQ",
            underlying="QQQ",
            direction="LONG",
            entry_price=100.0,
            original_quantity=10.0,
            remaining_quantity=10.0,
            stop_loss=100.0,
            trailing_stop_loss=100.0,
            highest_price=100.8,
            lowest_price=99.5,
            tp1=105.0,
            tp2=110.0,
            tp3=115.0,
            atr=2.0,
            session_count=4,
            is_runner=False,
            current_price=100.4,
        )
        self.monitor.positions["QQQ3l_EQ"] = pos

        cutoff_time = datetime(2026, 10, 29, 16, 20, tzinfo=timezone.utc)
        events = self.monitor.evaluate_asymmetric_session(
            current_time=cutoff_time,
            current_prices={"QQQ3l_EQ": 100.4},
            force_evaluation=True,
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "ZERO_ALPHA_EVACUATION")
        self.assertEqual(events[0]["ticker"], "QQQ3l_EQ")
        self.assertTrue(pos.is_closed)
        self.assertNotIn("QQQ3l_EQ", self.monitor.positions)

    def test_1620_runner_retained_overnight(self):
        """Runner position (is_runner = True) is retained overnight at 16:20 UTC."""
        pos = TrackedPosition(
            ticker="QQQ3l_EQ",
            underlying="QQQ",
            direction="LONG",
            entry_price=100.0,
            original_quantity=10.0,
            remaining_quantity=10.0,
            stop_loss=101.5,
            trailing_stop_loss=101.5,
            highest_price=104.5,
            lowest_price=99.5,
            tp1=105.0,
            tp2=110.0,
            tp3=115.0,
            atr=2.0,
            session_count=4,
            is_runner=True,
            current_price=103.8,
        )
        self.monitor.positions["QQQ3l_EQ"] = pos

        cutoff_time = datetime(2026, 10, 29, 16, 20, tzinfo=timezone.utc)
        events = self.monitor.evaluate_asymmetric_session(
            current_time=cutoff_time,
            current_prices={"QQQ3l_EQ": 103.8},
            force_evaluation=True,
        )

        self.assertEqual(len(events), 0)
        self.assertTrue(pos.is_overnight_eligible)
        self.assertIn("QQQ3l_EQ", self.monitor.positions)

    def test_1620_day2_position_not_evacuated(self):
        """Day 2 position with breakeven stop is retained overnight (decay continues)."""
        pos = TrackedPosition(
            ticker="QQQ3l_EQ",
            underlying="QQQ",
            direction="LONG",
            entry_price=100.0,
            original_quantity=10.0,
            remaining_quantity=10.0,
            stop_loss=100.0,
            trailing_stop_loss=100.0,
            highest_price=101.0,
            lowest_price=99.5,
            tp1=105.0,
            tp2=110.0,
            tp3=115.0,
            atr=2.0,
            session_count=2,
            is_runner=False,
            current_price=100.8,
        )
        self.monitor.positions["QQQ3l_EQ"] = pos

        cutoff_time = datetime(2026, 10, 27, 16, 20, tzinfo=timezone.utc)
        events = self.monitor.evaluate_asymmetric_session(
            current_time=cutoff_time,
            current_prices={"QQQ3l_EQ": 100.8},
            force_evaluation=True,
        )

        self.assertEqual(len(events), 0)
        self.assertTrue(pos.is_overnight_eligible)
        self.assertIn("QQQ3l_EQ", self.monitor.positions)

    # ─────────────────────────────────────────────────────────────
    # 6. Intraday Tick Runner Flag Transition
    # ─────────────────────────────────────────────────────────────
    def test_cushion_lock_triggers_runner_flag(self):
        """Reaching +3.0% gain in monitor_tick sets is_runner=True."""
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
            atr=2.0,
            session_count=2,
            is_runner=False,
        )
        self.monitor.positions["QQQ3l_EQ"] = pos

        # Price advances to 103.5 (+3.5% gain)
        self.monitor.monitor_tick(current_prices={"QQQ3l_EQ": 103.5})

        self.assertTrue(pos.is_runner)
        self.assertGreaterEqual(pos.trailing_stop_loss, 101.5)

    # ─────────────────────────────────────────────────────────────
    # 7. Dashboard Telemetry & Signal Exposure
    # ─────────────────────────────────────────────────────────────
    def test_dashboard_load_active_signals_exposes_decay_envelope(self):
        """load_active_signals exposes session_count, is_runner, decay_multiplier, and decay_label."""
        data = {
            "QQQ3l_EQ": {
                "ticker": "QQQ3l_EQ",
                "underlying": "QQQ",
                "direction": "LONG",
                "entry_price": 100.0,
                "original_quantity": 10.0,
                "remaining_quantity": 10.0,
                "stop_loss": 95.0,
                "trailing_stop_loss": 98.0,
                "highest_price": 102.0,
                "lowest_price": 99.0,
                "tp1": 105.0,
                "tp2": 110.0,
                "tp3": 115.0,
                "atr": 2.0,
                "session_count": 3,
                "is_runner": False,
                "is_closed": False,
            }
        }
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(data, f)

        signals = load_active_signals(self.state_file, live_prices={"QQQ3l_EQ": 102.0})
        self.assertEqual(len(signals), 1)
        sig = signals[0]
        self.assertEqual(sig["session_count"], 3)
        self.assertFalse(sig["is_runner"])
        self.assertAlmostEqual(sig["decay_multiplier"], 1.50)
        self.assertIn("Day 3", sig["decay_label"])
        self.assertIn("DECAY ENVELOPE:", sig["copy_text"])


if __name__ == "__main__":
    unittest.main()
