"""Unit and concurrency tests for PositionMonitor epoch ledger, GTC stops, Re-Epoch, and panic/resume safeguards."""

import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

from trading_system.agents.risk_manager import DecisionCard, RiskManager
from trading_system.config_bus import ConfigBus
from trading_system.data.trading212_client import OrderResult, Position, Trading212Client, Trading212Error
from trading_system.execution.position_monitor import (
    PositionEpochConflictError,
    PositionMonitor,
    ResumeBlockedError,
    TrackedPosition,
)
from trading_system.notifications.notifier import Notifier


def _make_card(
    ticker: str = "QQQ3l_EQ",
    underlying: str = "QQQ",
    direction: str = "LONG",
    entry_price: float = 100.0,
    stop_loss: float = 94.5,
    tp1: float = 105.5,
    tp2: float = 111.0,
    tp3: float = 116.5,
    shares: float = 10.0,
    config_epoch_id: str = None,
) -> DecisionCard:
    stop_dist_pct = (abs(entry_price - stop_loss) / entry_price * 100.0) if entry_price else 0.0
    card = DecisionCard(
        ticker=underlying,
        trading212_ticker=ticker,
        direction=direction,
        decision="BUY_ENTRY" if direction == "LONG" else "SELL_ENTRY",
        entry_price=entry_price,
        stop_loss=stop_loss,
        trailing_stop_loss=stop_loss,
        stop_distance_pct=stop_dist_pct,
        take_profit_1=tp1,
        take_profit_2=tp2,
        take_profit_3=tp3,
        position_size_shares=shares,
        position_notional=entry_price * shares,
        risk_amount=abs(entry_price - stop_loss) * shares,
        risk_pct_of_equity=0.01,
        conviction_score=85.0,
        rationale="Unit test trade card",
        strategy_type="momentum",
    )
    if config_epoch_id is not None:
        setattr(card, "config_epoch_id", config_epoch_id)
    return card


class TestPositionMonitorEpochs(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmpdir.name) / "config_data"
        self.state_file = str(Path(self.tmpdir.name) / "active_trades.json")
        self.t212 = MagicMock(spec=Trading212Client)
        self.t212.rate_limit_delay = 0.0
        self.t212.is_market_open.return_value = True
        self.t212.cancel_order.return_value = True
        self.t212.get_orders.return_value = []
        self.t212.place_stop_order.side_effect = lambda **kw: OrderResult(
            id=f"STOP_{kw['ticker']}_{int(kw['stop_price'] * 100)}",
            ticker=kw["ticker"],
            type="STOP",
            status="NEW",
            quantity=kw["quantity"],
            filled_quantity=0.0,
            fill_price=0.0,
        )
        self.t212.place_limit_order.side_effect = lambda **kw: OrderResult(
            id=f"LIM_{kw['ticker']}_{int(kw['limit_price'] * 100)}",
            ticker=kw["ticker"],
            type="LIMIT",
            status="NEW",
            quantity=kw["quantity"],
            filled_quantity=0.0,
            fill_price=0.0,
        )
        self.notifier = MagicMock(spec=Notifier)
        self.rm = RiskManager()
        self.bus = ConfigBus(data_dir=self.data_dir)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_legacy_active_trades_json_loads_and_epoch_roundtrips(self):
        legacy_payload = {
            "QQQ3l_EQ": {
                "ticker": "QQQ3l_EQ",
                "underlying": "QQQ",
                "direction": "LONG",
                "entry_price": 100.0,
                "original_quantity": 10.0,
                "remaining_quantity": 10.0,
                "stop_loss": 94.5,
                "trailing_stop_loss": 94.5,
                "highest_price": 102.0,
                "lowest_price": 99.0,
                "tp1": 105.5,
                "tp2": 111.0,
                "tp3": 116.5,
                "atr": 2.5,
                "currency": "USD",
            }
        }
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(legacy_payload, f)

        mon = PositionMonitor(
            t212_client=self.t212,
            notifier=self.notifier,
            risk_manager=self.rm,
            state_file=self.state_file,
            config_bus=self.bus,
        )
        pos = mon.positions["QQQ3l_EQ"]
        self.assertIsNone(pos.config_epoch_id)
        self.assertIsNone(pos.entry_config_snapshot)
        self.assertFalse(pos.panic_failed)
        self.assertIsNone(pos.unprotected_reason)

        # Register a new trade under active epoch and verify persistence round-trip
        snap_e1 = self.bus.commit_patch(
            {"ATR_SL_MULTIPLIER": 1.6},
            expected_epoch_id=self.bus.active_epoch_id,
            author="test",
            reason="set ATR mult",
        )
        card = _make_card(ticker="3NVDe_EQ", underlying="NVDA", config_epoch_id=snap_e1.config_epoch_id)
        pos2 = mon.register_new_trade(card, atr=2.5, fill_price=100.0)
        self.assertEqual(pos2.config_epoch_id, snap_e1.config_epoch_id)
        self.assertIsInstance(pos2.entry_config_snapshot, dict)
        self.assertEqual(pos2.entry_config_snapshot["ATR_SL_MULTIPLIER"], 1.6)

        reloaded = PositionMonitor(
            t212_client=self.t212,
            notifier=self.notifier,
            risk_manager=self.rm,
            state_file=self.state_file,
            config_bus=self.bus,
        )
        self.assertEqual(reloaded.positions["3NVDe_EQ"].config_epoch_id, snap_e1.config_epoch_id)
        self.assertEqual(reloaded.positions["3NVDe_EQ"].entry_config_snapshot["ATR_SL_MULTIPLIER"], 1.6)

    def test_epoch_parameterized_exits_and_per_position_isolation(self):
        mon = PositionMonitor(
            t212_client=self.t212,
            notifier=self.notifier,
            risk_manager=self.rm,
            state_file=self.state_file,
            pure_tsl_mode=False,
            config_bus=self.bus,
        )
        # Preserve default baseline test expectation
        default_levels = mon.calculate_exit_levels(entry_price=100.0, direction="LONG", atr=2.5)
        self.assertEqual(default_levels["stop_loss"], 94.5)
        self.assertEqual(default_levels["tp1"], 105.5)
        self.assertEqual(default_levels["tp2"], 111.0)
        self.assertEqual(default_levels["tp3"], 116.5)

        # Custom snapshot exit math
        snap_tight = {
            "ATR_SL_MULTIPLIER": 1.2,
            "ETP_MIN_SL_PCT": 0.015,
            "ETP_MAX_SL_PCT": 0.05,
            "TP1_R_MULTIPLE": 1.5,
            "TP2_R_MULTIPLE": 2.5,
            "TP3_R_MULTIPLE": 4.0,
        }
        tight_levels = mon.calculate_exit_levels(
            entry_price=100.0,
            direction="LONG",
            atr=2.5,
            config_snapshot=snap_tight,
        )
        self.assertEqual(tight_levels["stop_loss"], 97.0)  # 100 - 1.2 * 2.5
        self.assertEqual(tight_levels["tp1"], 104.5)       # 100 + 1.5 * 3.0

        # @classmethod check_progressive_stops remains directly callable without instance
        self.assertEqual(
            PositionMonitor.check_progressive_stops(100.0, 102.0, 94.5, "LONG", config_snapshot=snap_tight),
            100.0,
        )

        # Mixed-epoch isolation in monitor_tick & apply_daily_rollover_stops
        snap1 = self.bus.commit_patch(
            {"PURE_TSL_MODE": False, "TSL_DECAY_DAY1_MULT": 2.4},
            expected_epoch_id=self.bus.active_epoch_id,
        )
        card_a = _make_card(ticker="QQQ3l_EQ", underlying="QQQ", entry_price=100.0, stop_loss=94.5, tp1=105.5, config_epoch_id=snap1.config_epoch_id)
        pos_a = mon.register_new_trade(card_a, atr=2.5, fill_price=100.0, config_snapshot=snap1)

        snap2 = self.bus.commit_patch(
            {"PURE_TSL_MODE": True, "TSL_DECAY_DAY1_MULT": 1.4},
            expected_epoch_id=self.bus.active_epoch_id,
        )
        card_b = _make_card(ticker="3NVDl_EQ", underlying="NVDA", entry_price=100.0, stop_loss=94.5, tp1=105.5, config_epoch_id=snap2.config_epoch_id)
        pos_b = mon.register_new_trade(card_b, atr=2.5, fill_price=100.0, config_snapshot=snap2)

        # Verify both hit 106.0: pos_a executes TP1 trim (PURE_TSL_MODE=False), pos_b stays 100% riding TSL (PURE_TSL_MODE=True)
        events = mon.monitor_tick({"QQQ3l_EQ": 106.0, "3NVDl_EQ": 106.0})
        ev_map = {(e["ticker"], e["event"]) for e in events}
        self.assertIn(("QQQ3l_EQ", "TP1"), ev_map)
        self.assertNotIn(("3NVDl_EQ", "TP1"), ev_map)
        self.assertAlmostEqual(pos_a.remaining_quantity, 5.0, places=3)
        self.assertAlmostEqual(pos_b.remaining_quantity, 10.0, places=3)

        # Daily rollover uses each position's own decay multiplier (2.4 vs 1.4) when not a runner
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        pos_a.last_session_date = today_str
        pos_a.session_count = 1
        pos_a.is_runner = False
        pos_a.highest_price = 101.5
        pos_a.current_price = 101.0
        pos_a.trailing_stop_loss = 94.5
        pos_b.last_session_date = today_str
        pos_b.session_count = 1
        pos_b.is_runner = False
        pos_b.highest_price = 101.5
        pos_b.current_price = 101.0
        pos_b.trailing_stop_loss = 94.5
        rollover = mon.apply_daily_rollover_stops()
        by_ticker = {r["ticker"]: r for r in rollover}
        self.assertEqual(by_ticker["QQQ3l_EQ"]["decay_multiplier"], 2.4)
        self.assertEqual(by_ticker["3NVDl_EQ"]["decay_multiplier"], 1.4)

    def test_mixed_epoch_sync_broker_orders_preserves_non_pure_tsl_limit(self):
        snap_pure = self.bus.commit_patch(
            {"PURE_TSL_MODE": True},
            expected_epoch_id=self.bus.active_epoch_id,
        )
        mon = PositionMonitor(
            t212_client=self.t212,
            notifier=self.notifier,
            risk_manager=self.rm,
            state_file=self.state_file,
            config_bus=self.bus,
        )
        pos_pure = mon.register_new_trade(
            _make_card(ticker="QQQ3l_EQ", config_epoch_id=snap_pure.config_epoch_id),
            atr=2.5,
            fill_price=100.0,
            config_snapshot=snap_pure,
        )
        pos_pure.broker_stop_order_id = "STOP_A"

        snap_legacy = self.bus.commit_patch(
            {"PURE_TSL_MODE": False},
            expected_epoch_id=self.bus.active_epoch_id,
        )
        pos_legacy = mon.register_new_trade(
            _make_card(ticker="3NVDl_EQ", underlying="NVDA", config_epoch_id=snap_legacy.config_epoch_id),
            atr=2.5,
            fill_price=100.0,
            config_snapshot=snap_legacy,
        )
        pos_legacy.broker_stop_order_id = "STOP_B"
        pos_legacy.broker_tp_order_id = "LIM_B"

        self.t212.get_orders.return_value = [
            {"id": "LIM_A", "ticker": "QQQ3l_EQ", "type": "LIMIT"},
            {"id": "LIM_B", "ticker": "3NVDl_EQ", "type": "LIMIT"},
        ]

        mon.sync_broker_orders()
        cancelled_ids = [call.args[0] for call in self.t212.cancel_order.call_args_list]
        self.assertIn("LIM_A", cancelled_ids)
        self.assertNotIn("LIM_B", cancelled_ids)

    def test_heal_stale_stop_never_loosens_ratcheted_stop(self):
        mon = PositionMonitor(
            t212_client=self.t212,
            notifier=self.notifier,
            risk_manager=self.rm,
            state_file=self.state_file,
        )
        pos = mon.register_new_trade(_make_card(ticker="QQQ3l_EQ", entry_price=100.0, stop_loss=94.5), atr=2.5, fill_price=100.0)
        pos.stop_loss = 108.0
        pos.trailing_stop_loss = 112.0
        setattr(pos, "_last_atr_pct", 0.02)

        # live_price=114.0 -> raw healed_sl = 114 * (1 - 0.05) = 108.30 (< ratcheted 112.0)
        self.t212.get_portfolio.return_value = [
            Position(
                ticker="QQQ3l_EQ",
                quantity=10.0,
                average_price=100.0,
                current_price=114.0,
                ppl=140.0,
                max_buy=100.0,
                max_sell=10.0,
            )
        ]
        mon._heal_stale_stop(pos)
        self.assertEqual(pos.stop_loss, 112.0)
        self.assertEqual(pos.trailing_stop_loss, 112.0)

    def test_place_broker_stop_allow_day_fallback_false(self):
        mon = PositionMonitor(
            t212_client=self.t212,
            notifier=self.notifier,
            risk_manager=self.rm,
            state_file=self.state_file,
        )
        pos = mon.register_new_trade(_make_card(ticker="QQQ3l_EQ", entry_price=100.0, stop_loss=94.5), atr=2.5, fill_price=100.0)
        self.t212.place_stop_order.side_effect = Trading212Error(400, "GTC rejected")

        res = mon.place_broker_stop(pos, stop_price=95.0, allow_day_fallback=False, _allow_heal=False)
        self.assertIsNone(res)
        self.assertEqual(self.t212.place_stop_order.call_count, 1)
        self.assertEqual(self.t212.place_stop_order.call_args.kwargs["time_validity"], "GOOD_TILL_CANCEL")

        self.t212.place_stop_order.reset_mock()
        res_day = mon.place_broker_stop(pos, stop_price=95.0, allow_day_fallback=True, _allow_heal=False)
        self.assertIsNone(res_day)
        validities = [c.kwargs["time_validity"] for c in self.t212.place_stop_order.call_args_list]
        self.assertEqual(validities, ["GOOD_TILL_CANCEL", "DAY"])

    def test_re_epoch_cas_no_loosen_clamp_and_closed_market(self):
        # Epoch 1: standard 2.2x ATR SL -> 94.5
        e1 = self.bus.commit_patch(
            {"ATR_SL_MULTIPLIER": 2.2, "PURE_TSL_MODE": True},
            expected_epoch_id=self.bus.active_epoch_id,
        )
        mon = PositionMonitor(
            t212_client=self.t212,
            notifier=self.notifier,
            risk_manager=self.rm,
            state_file=self.state_file,
            config_bus=self.bus,
        )
        pos = mon.register_new_trade(
            _make_card(ticker="QQQ3l_EQ", entry_price=100.0, stop_loss=94.5, config_epoch_id=e1.config_epoch_id),
            atr=2.5,
            fill_price=100.0,
            config_snapshot=e1,
        )
        pos.broker_stop_order_id = "STOP_INITIAL"

        # Epoch 2: looser ATR multiplier (3.0x -> 92.5 < 94.5). Commit Epoch 3 right after so Epoch 2 becomes stale.
        e2 = self.bus.commit_patch(
            {"ATR_SL_MULTIPLIER": 3.0, "ETP_MAX_SL_PCT": 0.12},
            expected_epoch_id=e1.config_epoch_id,
        )
        # CAS conflict: wrong expected_position_epoch_id
        with self.assertRaises(PositionEpochConflictError):
            mon.re_epoch_position("QQQ3l_EQ", expected_position_epoch_id="epoch-999", target_epoch_id=e2.config_epoch_id)

        # Re-epoch to looser e2 -> keeps 94.5 untouched with ZERO broker cancel/place calls even when market closed
        self.t212.is_market_open.return_value = False
        self.t212.cancel_order.reset_mock()
        self.t212.place_stop_order.reset_mock()
        preview_loose = mon.preview_re_epoch("QQQ3l_EQ", target_epoch_id=e2.config_epoch_id)
        self.assertEqual(preview_loose["action"], "keep_existing_stop")
        res_loose = mon.re_epoch_position("QQQ3l_EQ", expected_position_epoch_id=e1.config_epoch_id, target_epoch_id=e2.config_epoch_id)
        self.assertTrue(res_loose["applied"])
        self.assertFalse(res_loose["broker_modified"])
        self.assertEqual(pos.stop_loss, 94.5)
        self.assertEqual(pos.config_epoch_id, e2.config_epoch_id)
        self.assertEqual(self.t212.cancel_order.call_count, 0)
        self.assertEqual(self.t212.place_stop_order.call_count, 0)

        # Epoch 3: tight SL (1.0x ATR -> 97.5) while live price is 96.0 -> clamps below cur_px - tick_buffer
        e3 = self.bus.commit_patch(
            {"ATR_SL_MULTIPLIER": 1.0, "ETP_MIN_SL_PCT": 0.01},
            expected_epoch_id=e2.config_epoch_id,
        )
        # Stale target epoch e2 fails CAS now that e3 is active
        with self.assertRaises(PositionEpochConflictError):
            mon.re_epoch_position("QQQ3l_EQ", expected_position_epoch_id=e2.config_epoch_id, target_epoch_id=e2.config_epoch_id)

        self.t212.get_quote.return_value = {"bid": 96.0, "price": 96.0}
        # Market closed refuses broker tightening
        closed_res = mon.re_epoch_position("QQQ3l_EQ", expected_position_epoch_id=e2.config_epoch_id, target_epoch_id=e3.config_epoch_id)
        self.assertFalse(closed_res["applied"])
        self.assertEqual(closed_res["status"], "market_closed")
        self.assertEqual(pos.config_epoch_id, e2.config_epoch_id)
        self.assertEqual(pos.stop_loss, 94.5)

        # Open market allows clamped tightening (< 96.0 - tick_buf, > 94.5) via GTC only
        self.t212.is_market_open.return_value = True
        tight_res = mon.re_epoch_position("QQQ3l_EQ", expected_position_epoch_id=e2.config_epoch_id, target_epoch_id=e3.config_epoch_id)
        self.assertTrue(tight_res["applied"])
        self.assertTrue(tight_res["broker_modified"])
        self.assertTrue(tight_res["was_clamped_to_market"])
        self.assertGreater(pos.stop_loss, 94.5)
        self.assertLess(pos.stop_loss, 96.0)
        self.assertEqual(pos.config_epoch_id, e3.config_epoch_id)
        self.assertEqual(self.t212.place_stop_order.call_args.kwargs["time_validity"], "GOOD_TILL_CANCEL")

    def test_re_epoch_pure_tsl_confirmation_and_failure_restores_old_sl(self):
        e1 = self.bus.commit_patch(
            {"PURE_TSL_MODE": True, "ATR_SL_MULTIPLIER": 2.2},
            expected_epoch_id=self.bus.active_epoch_id,
        )
        mon = PositionMonitor(
            t212_client=self.t212,
            notifier=self.notifier,
            risk_manager=self.rm,
            state_file=self.state_file,
            config_bus=self.bus,
        )
        pos = mon.register_new_trade(
            _make_card(ticker="QQQ3l_EQ", entry_price=100.0, stop_loss=94.5, config_epoch_id=e1.config_epoch_id),
            atr=2.5,
            fill_price=100.0,
            config_snapshot=e1,
        )
        pos.broker_stop_order_id = "STOP_OLD"
        self.t212.get_quote.return_value = {"bid": 103.0, "price": 103.0}

        # Epoch 2 flips PURE_TSL_MODE=False and tightens SL -> requires confirmation
        e2 = self.bus.commit_patch(
            {"PURE_TSL_MODE": False, "ATR_SL_MULTIPLIER": 1.2, "ETP_MIN_SL_PCT": 0.01},
            expected_epoch_id=e1.config_epoch_id,
        )
        blocked_preview = mon.preview_re_epoch("QQQ3l_EQ", target_epoch_id=e2.config_epoch_id)
        self.assertTrue(blocked_preview["pure_tsl_mode_changed"])
        self.assertTrue(blocked_preview["requires_confirmation"])
        self.assertEqual(blocked_preview["action"], "blocked")

        blocked_res = mon.re_epoch_position("QQQ3l_EQ", expected_position_epoch_id=e1.config_epoch_id, target_epoch_id=e2.config_epoch_id)
        self.assertFalse(blocked_res["applied"])
        self.assertEqual(blocked_res["status"], "blocked")
        self.assertEqual(pos.config_epoch_id, e1.config_epoch_id)

        # Simulate GTC replacement failing after cancel -> must immediately restore old_sl (94.5) without DAY fallback
        submitted_orders = []

        def _flaky_stop_order(**kw):
            submitted_orders.append((round(kw["stop_price"], 2), kw["time_validity"]))
            if len(submitted_orders) == 1:
                raise Trading212Error(400, "Transient 400 on new stop")
            return OrderResult(id="STOP_RESTORED", ticker=kw["ticker"], type="STOP", status="NEW", quantity=kw["quantity"], filled_quantity=0.0, fill_price=0.0)

        self.t212.place_stop_order.side_effect = _flaky_stop_order
        fail_res = mon.re_epoch_position(
            "QQQ3l_EQ",
            expected_position_epoch_id=e1.config_epoch_id,
            target_epoch_id=e2.config_epoch_id,
            confirm_pure_tsl_mode_change=True,
        )
        self.assertFalse(fail_res["applied"])
        self.assertEqual(fail_res["status"], "replacement_failed")
        self.assertTrue(fail_res["restored_old_stop"])
        self.assertEqual(pos.stop_loss, 94.5)
        self.assertEqual(pos.trailing_stop_loss, 94.5)
        self.assertEqual(pos.config_epoch_id, e1.config_epoch_id)
        self.assertEqual(pos.broker_stop_order_id, "STOP_RESTORED")
        self.assertEqual(submitted_orders, [(97.0, "GOOD_TILL_CANCEL"), (94.5, "GOOD_TILL_CANCEL")])

    def test_re_epoch_all_preempted_by_panic_halt(self):
        e1 = self.bus.commit_patch({"ATR_SL_MULTIPLIER": 2.2}, expected_epoch_id=self.bus.active_epoch_id)
        mon = PositionMonitor(
            t212_client=self.t212,
            notifier=self.notifier,
            risk_manager=self.rm,
            state_file=self.state_file,
            config_bus=self.bus,
        )
        p1 = mon.register_new_trade(_make_card(ticker="QQQ3l_EQ", config_epoch_id=e1.config_epoch_id), atr=2.5, fill_price=100.0, config_snapshot=e1)
        p2 = mon.register_new_trade(_make_card(ticker="3NVDl_EQ", underlying="NVDA", config_epoch_id=e1.config_epoch_id), atr=2.5, fill_price=100.0, config_snapshot=e1)
        p1.broker_stop_order_id = "S1"
        p2.broker_stop_order_id = "S2"

        e2 = self.bus.commit_patch({"ATR_SL_MULTIPLIER": 1.2, "ETP_MIN_SL_PCT": 0.01}, expected_epoch_id=e1.config_epoch_id)
        self.t212.get_quote.return_value = {"bid": 103.0, "price": 103.0}

        original_place = self.t212.place_stop_order.side_effect

        def _halt_on_first(**kw):
            mon.is_halted = True
            return original_place(**kw)

        self.t212.place_stop_order.side_effect = _halt_on_first
        batch = mon.re_epoch_all_positions(target_epoch_id=e2.config_epoch_id)
        self.assertTrue(batch["preempted_by_halt"])
        self.assertEqual(p1.config_epoch_id, e2.config_epoch_id)
        self.assertEqual(p2.config_epoch_id, e1.config_epoch_id)

    def test_panic_flatten_failed_liquidation_preserved_and_resume_fail_closed(self):
        mon = PositionMonitor(
            t212_client=self.t212,
            notifier=self.notifier,
            risk_manager=self.rm,
            state_file=self.state_file,
        )
        pos_fail = mon.register_new_trade(_make_card(ticker="QQQ3l_EQ", shares=10.0), atr=2.5, fill_price=100.0)
        pos_ok = mon.register_new_trade(_make_card(ticker="3NVDl_EQ", underlying="NVDA", shares=5.0), atr=2.5, fill_price=100.0)
        pos_fail.broker_stop_order_id = "STOP_FAIL"
        pos_ok.broker_stop_order_id = "STOP_OK"

        def _sell_outcome(ticker, qty):
            if ticker == "QQQ3l_EQ":
                raise Trading212Error(400, "Order rejected")
            return OrderResult(id="SELL_OK", ticker=ticker, type="MARKET", status="FILLED", quantity=qty, filled_quantity=abs(qty), fill_price=100.0)

        self.t212.place_market_order.side_effect = _sell_outcome

        res = mon.panic_flatten_all()
        self.assertTrue(mon.is_system_halted())
        self.assertTrue(os.path.exists(mon.halt_marker_file))
        self.assertEqual(res["failed_liquidations"], [("QQQ3l_EQ", 10.0)])
        self.assertEqual(res["liquidated_positions"], [("3NVDl_EQ", 5.0)])

        # Failed position MUST remain open in memory & active_trades.json marked panic_failed
        self.assertFalse(pos_fail.is_closed)
        self.assertEqual(pos_fail.remaining_quantity, 10.0)
        self.assertTrue(pos_fail.panic_failed)
        self.assertEqual(pos_fail.unprotected_reason, "Panic liquidation failed")
        with open(self.state_file, "r", encoding="utf-8") as f:
            disk_state = json.load(f)
        self.assertIn("QQQ3l_EQ", disk_state)
        self.assertNotIn("3NVDl_EQ", disk_state)
        self.assertTrue(disk_state["QQQ3l_EQ"]["panic_failed"])
        self.assertEqual(disk_state["QQQ3l_EQ"]["unprotected_reason"], "Panic liquidation failed")

        # Fail closed when stop re-arming fails in resume_trading()
        self.t212.place_stop_order.side_effect = Trading212Error(503, "Exchange still halted")
        with self.assertRaises(ResumeBlockedError) as ctx:
            mon.resume_trading()
        self.assertEqual(len(ctx.exception.unprotected_positions), 1)
        self.assertTrue(mon.is_system_halted())
        self.assertTrue(os.path.exists(mon.halt_marker_file))

        # Re-arm succeeds -> panic_failed clears and trading resumes
        self.t212.place_stop_order.side_effect = lambda **kw: OrderResult(
            id="STOP_REARMED", ticker=kw["ticker"], type="STOP", status="NEW", quantity=kw["quantity"], filled_quantity=0.0, fill_price=0.0
        )
        resume_ok = mon.resume_trading()
        self.assertTrue(resume_ok["resumed"])
        self.assertEqual(resume_ok["rearmed_stops"], ["QQQ3l_EQ"])
        self.assertFalse(pos_fail.panic_failed)
        self.assertIsNone(pos_fail.unprotected_reason)
        self.assertEqual(pos_fail.broker_stop_order_id, "STOP_REARMED")
        self.assertFalse(mon.is_system_halted())
        self.assertFalse(os.path.exists(mon.halt_marker_file))

    def test_concurrent_panic_save_state_and_monitor_tick_thread_safety(self):
        gate_lock = threading.RLock()
        mon = PositionMonitor(
            t212_client=self.t212,
            notifier=self.notifier,
            risk_manager=self.rm,
            state_file=self.state_file,
            pure_tsl_mode=True,
            execution_gate_lock=gate_lock,
        )
        pos1 = mon.register_new_trade(_make_card(ticker="QQQ3l_EQ", entry_price=100.0, stop_loss=94.5), atr=2.5, fill_price=100.0)
        pos2 = mon.register_new_trade(_make_card(ticker="3NVDl_EQ", underlying="NVDA", entry_price=100.0, stop_loss=94.5), atr=2.5, fill_price=100.0)
        pos1.broker_stop_order_id = "S1"
        pos2.broker_stop_order_id = "S2"

        # Simulate slow broker stop update inside monitor_tick while panic + atomic state writers run concurrently
        broker_entered = threading.Event()
        panic_finished_before_broker_returned = []

        def _slow_place_stop(**kw):
            broker_entered.set()
            time.sleep(0.20)
            return OrderResult(id=f"S_{kw['ticker']}", ticker=kw["ticker"], type="STOP", status="NEW", quantity=kw["quantity"], filled_quantity=0.0, fill_price=0.0)

        self.t212.place_stop_order.side_effect = _slow_place_stop
        self.t212.place_market_order.return_value = OrderResult(id="M_OK", ticker="QQQ3l_EQ", type="MARKET", status="FILLED", quantity=-10.0, filled_quantity=10.0, fill_price=103.0)

        errors = []
        stop_readers = threading.Event()

        def _tick_worker():
            try:
                mon.monitor_tick({"QQQ3l_EQ": 103.5, "3NVDl_EQ": 103.5})
            except Exception as exc:
                errors.append(exc)

        def _writer_worker():
            try:
                for _ in range(25):
                    mon._save_state()
                    time.sleep(0.002)
            except Exception as exc:
                errors.append(exc)

        def _reader_worker():
            try:
                while not stop_readers.is_set():
                    if os.path.exists(self.state_file):
                        with open(self.state_file, "r", encoding="utf-8") as f:
                            content = f.read()
                        if content:
                            json.loads(content)
                    time.sleep(0.001)
            except Exception as exc:
                errors.append(exc)

        t_tick = threading.Thread(target=_tick_worker)
        t_write = threading.Thread(target=_writer_worker)
        t_read = threading.Thread(target=_reader_worker)

        t_tick.start()
        t_write.start()
        t_read.start()

        self.assertTrue(broker_entered.wait(timeout=2.0))
        t0 = time.monotonic()
        panic_res = mon.panic_flatten_all()
        elapsed_panic = time.monotonic() - t0
        panic_finished_before_broker_returned.append(elapsed_panic)

        t_tick.join(timeout=3.0)
        t_write.join(timeout=3.0)
        stop_readers.set()
        t_read.join(timeout=3.0)

        self.assertFalse(t_tick.is_alive())
        self.assertFalse(t_write.is_alive())
        self.assertFalse(t_read.is_alive())
        self.assertEqual(errors, [])
        self.assertLess(panic_finished_before_broker_returned[0], 0.12)
        self.assertTrue(mon.is_system_halted())
        self.assertEqual(panic_res["failed_liquidations"], [])

    def test_session_cutoff_isolation_and_halt_marker_unification(self):
        from datetime import datetime, timezone

        e1 = self.bus.commit_patch(
            {"SESSION_CLOSE_CUTOFF_UTC": "15:00", "ENFORCE_LONG_BREAKEVEN_OVERNIGHT": True, "PURE_TSL_MODE": True},
            expected_epoch_id=self.bus.active_epoch_id,
        )
        mon = PositionMonitor(
            t212_client=self.t212,
            notifier=self.notifier,
            risk_manager=self.rm,
            state_file=self.state_file,
            config_bus=self.bus,
        )
        mon.register_new_trade(
            _make_card(ticker="QQQ3l_EQ", entry_price=100.0, stop_loss=94.5, config_epoch_id=e1.config_epoch_id),
            atr=2.5,
            fill_price=100.0,
            config_snapshot=e1,
        )

        e2 = self.bus.commit_patch(
            {"SESSION_CLOSE_CUTOFF_UTC": "16:20", "ENFORCE_LONG_BREAKEVEN_OVERNIGHT": True, "PURE_TSL_MODE": False},
            expected_epoch_id=e1.config_epoch_id,
        )
        mon.register_new_trade(
            _make_card(ticker="3NVDl_EQ", underlying="NVDA", entry_price=100.0, stop_loss=94.5, tp1=105.5, config_epoch_id=e2.config_epoch_id),
            atr=2.5,
            fill_price=100.0,
            config_snapshot=e2,
        )

        # At 15:10 UTC, only e1 (cutoff 15:00) evacuates; e2 (cutoff 16:20) stays open!
        now_1510 = datetime(2026, 3, 18, 15, 10, 0, tzinfo=timezone.utc)
        ev = mon.evaluate_asymmetric_session(current_time=now_1510, current_prices={"QQQ3l_EQ": 100.5, "3NVDl_EQ": 100.5})
        ev_tickers = [e["ticker"] for e in ev]
        self.assertEqual(ev_tickers, ["QQQ3l_EQ"])
        self.assertIn("3NVDl_EQ", mon.positions)

        # Marker file created externally halts BOTH monitor_tick and evaluate_active_trade_signals
        with open(mon.halt_marker_file, "w", encoding="utf-8") as f:
            json.dump({"halted": True}, f)
        mon.is_halted = False
        self.assertTrue(mon.is_system_halted())
        self.assertEqual(mon.monitor_tick({"3NVDl_EQ": 106.0}), [])
        self.assertEqual(mon.evaluate_active_trade_signals({"NVDA": {"rsi3": 85.0}}), [])

    def test_preview_re_epoch_all_leases_and_zero_arg_clean_resume(self):
        e1 = self.bus.commit_patch(
            {"ATR_SL_MULTIPLIER": 2.2, "PURE_TSL_MODE": True},
            expected_epoch_id=self.bus.active_epoch_id,
        )
        mon = PositionMonitor(
            t212_client=self.t212,
            notifier=self.notifier,
            risk_manager=self.rm,
            state_file=self.state_file,
            config_bus=self.bus,
        )
        # Lease acquire/release under _state_lock
        self.assertTrue(mon.acquire_position_lease("QQQ3l_EQ", ttl_seconds=10.0))
        self.assertFalse(mon.acquire_position_lease("QQQ3l_EQ", ttl_seconds=10.0))
        mon.release_position_lease("QQQ3l_EQ")
        self.assertTrue(mon.acquire_position_lease("QQQ3l_EQ", ttl_seconds=10.0))
        mon.release_position_lease("QQQ3l_EQ")

        # Zero-arg resume_trading works cleanly when no positions are unprotected
        mon.is_halted = True
        with open(mon.halt_marker_file, "w", encoding="utf-8") as f:
            json.dump({"halted": True}, f)
        res_clean = mon.resume_trading()
        self.assertTrue(res_clean["resumed"])
        self.assertFalse(mon.is_system_halted())

        mon.register_new_trade(
            _make_card(ticker="QQQ3l_EQ", entry_price=100.0, stop_loss=94.5, config_epoch_id=e1.config_epoch_id),
            atr=2.5,
            fill_price=100.0,
            config_snapshot=e1,
        )
        e2 = self.bus.commit_patch(
            {"ATR_SL_MULTIPLIER": 1.2, "ETP_MIN_SL_PCT": 0.01, "PURE_TSL_MODE": False},
            expected_epoch_id=e1.config_epoch_id,
        )
        self.t212.get_quote.return_value = {"bid": 103.0, "price": 103.0}
        summary = mon.preview_re_epoch_all(target_epoch_id=e2.config_epoch_id)
        self.assertTrue(summary["requires_confirmation"])
        self.assertEqual(summary["blocked_count"], 1)


if __name__ == "__main__":
    unittest.main()
