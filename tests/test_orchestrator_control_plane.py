"""Comprehensive tests for Unit U4 (Single-Orchestrator Control Integration, Funnel Publisher,
Mid-Scan Epoch Revalidation, LeveragedUniverse Dynamic 5x) and Orchestrator U5 wiring.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

from trading_system.agents.risk_manager import DecisionCard, RiskManager


@dataclass
class DebatePosition:
    side: str = "BULL"
    thesis: str = ""
    confidence: float = 0.8


@dataclass
class DebateOutcome:
    ticker: str = "QQQ"
    winning_side: str = "BULL"
    consensus_decision: str = "BUY_ENTRY"
    bull_position: Any = None
    bear_position: Any = None
    synthesis: str = ""
    conviction_score: float = 0.85
    debate_rounds: int = 3
    config_epoch_id: Optional[str] = None
from trading_system.config import settings
from trading_system.config_bus import ConfigBus, ConfigConflictError, ConfigSnapshot
from trading_system.data.leveraged_universe import LeveragedUniverse
from trading_system.data.trading212_client import (
    AccountCash,
    MarketDataProvider,
    OrderResult,
    Position,
    Trading212Client,
    Trading212Error,
)
from trading_system.execution.position_monitor import (
    PositionEpochConflictError,
    PositionMonitor,
    ResumeBlockedError,
)
from trading_system.notifications.notifier import Notifier, TradeNotification
from trading_system.orchestrator import Orchestrator
from trading_system.screener.laya_screener import ScreenerResult


@dataclass
class _DummyAccountCash:
    free: float = 8000.0
    total: float = 10000.0
    invested: float = 2000.0
    ppl: float = 0.0
    result: float = 0.0


class _FakeT212Client:
    """In-memory fake Trading212Client tracking HTTP vs cached calls and order execution."""

    def __init__(self, initial_equity: float = 10000.0, spread: float = 0.002) -> None:
        self.initial_equity = float(initial_equity)
        self.spread = float(spread)
        self.cash_calls: List[Dict[str, Any]] = []
        self.portfolio_calls: List[Dict[str, Any]] = []
        self.orders_calls: List[Dict[str, Any]] = []
        self.hist_calls: List[Dict[str, Any]] = []
        self.placed_orders: List[Dict[str, Any]] = []
        self.cancelled_orders: List[str] = []
        self.stop_orders: List[Dict[str, Any]] = []
        self.limit_orders: List[Dict[str, Any]] = []
        self.portfolio_positions: List[Position] = []
        self.open_orders: List[Dict[str, Any]] = []
        self.fail_sell_tickers: set[str] = set()
        self.fail_stop_tickers: set[str] = set()
        self.on_place_market_order: Optional[Any] = None
        self.cached_broker_snapshot: Dict[str, Any] = {
            "cash": {"total": initial_equity, "free": initial_equity * 0.8, "invested": 0.0, "ppl": 0.0},
            "portfolio": [],
            "orders": [],
            "historical_orders": {"items": []},
            "active_server_stops_count": 0,
        }
        self._order_seq = 0

    def get_account_cash(self, fallback_equity: float = 10000.0, **kwargs: Any) -> AccountCash:
        self.cash_calls.append(dict(kwargs))
        return AccountCash(
            free=self.initial_equity * 0.8,
            total=self.initial_equity,
            invested=0.0,
            ppl=0.0,
            result=0.0,
        )

    def get_portfolio(self, **kwargs: Any) -> List[Position]:
        self.portfolio_calls.append(dict(kwargs))
        return list(self.portfolio_positions)

    def get_orders(self, **kwargs: Any) -> List[Dict[str, Any]]:
        self.orders_calls.append(dict(kwargs))
        return list(self.open_orders)

    def get_historical_orders(self, limit: int = 50, **kwargs: Any) -> Dict[str, Any]:
        self.hist_calls.append({"limit": limit, **kwargs})
        return {"items": []}

    def get_cached_broker_snapshot(self) -> Dict[str, Any]:
        return json.loads(json.dumps(self.cached_broker_snapshot))

    def get_rate_limit_telemetry(self) -> Dict[str, Any]:
        return {"throttle_count": 0, "buckets": {"portfolio": {"remaining": 9}}}

    def get_bid_ask_spread(self, ticker: str, current_price: float = 0.0) -> Optional[float]:
        return self.spread

    def get_quote(self, ticker: str) -> Dict[str, Any]:
        ask = 100.0
        bid = round(100.0 * (1.0 - self.spread), 6)
        return {"bid": bid, "ask": ask, "mid": (bid + ask) / 2.0, "spread_pct": self.spread}

    def is_market_open(self, ticker: str, **kwargs: Any) -> bool:
        return True

    def is_any_market_open(self, tickers: List[str], **kwargs: Any) -> bool:
        return True

    def place_market_order(self, ticker: str, quantity: float) -> OrderResult:
        self._order_seq += 1
        oid = f"MKT-{self._order_seq}"
        if self.on_place_market_order is not None:
            self.on_place_market_order(ticker, quantity, oid)
        if quantity < 0 and ticker in self.fail_sell_tickers:
            raise Trading212Error(400, "Order rejected")
        self.placed_orders.append({"order_id": oid, "ticker": ticker, "quantity": quantity})
        if quantity > 0:
            self.portfolio_positions.append(
                Position(
                    ticker=ticker,
                    quantity=abs(quantity),
                    average_price=100.0,
                    current_price=100.0,
                    ppl=0.0,
                    max_buy=1000.0,
                    max_sell=abs(quantity),
                )
            )
        else:
            self.portfolio_positions = [p for p in self.portfolio_positions if p.ticker != ticker]
        return OrderResult(
            id=oid,
            ticker=ticker,
            type="MARKET",
            status="FILLED",
            quantity=quantity,
            filled_quantity=abs(quantity),
            fill_price=100.0,
        )

    def place_stop_order(self, ticker: str, quantity: float, stop_price: float, **kwargs: Any) -> OrderResult:
        if ticker in self.fail_stop_tickers:
            raise Trading212Error(503, "Exchange halted")
        self._order_seq += 1
        oid = f"STOP-{self._order_seq}"
        self.stop_orders.append({"order_id": oid, "ticker": ticker, "quantity": quantity, "stop_price": stop_price})
        return OrderResult(
            id=oid,
            ticker=ticker,
            type="STOP",
            status="NEW",
            quantity=quantity,
            filled_quantity=0.0,
            fill_price=0.0,
        )

    def place_limit_order(self, ticker: str, quantity: float, limit_price: float, **kwargs: Any) -> OrderResult:
        self._order_seq += 1
        oid = f"LMT-{self._order_seq}"
        self.limit_orders.append({"order_id": oid, "ticker": ticker, "quantity": quantity, "limit_price": limit_price})
        return OrderResult(
            id=oid,
            ticker=ticker,
            type="LIMIT",
            status="NEW",
            quantity=quantity,
            filled_quantity=0.0,
            fill_price=0.0,
        )

    def cancel_order(self, order_id: str) -> bool:
        self.cancelled_orders.append(str(order_id))
        self.open_orders = [o for o in self.open_orders if str(o.get("id")) != str(order_id)]
        return True

    def get_instrument_by_symbol(self, symbol: str) -> Optional[Any]:
        return None

    def close(self) -> None:
        return None


def _make_metrics(adx: float = 32.0, atr: float = 2.0, close: float = 100.0) -> Dict[str, Any]:
    return {
        "close": close,
        "etp_price": close,
        "atr": atr,
        "adx": adx,
        "adx_14": adx,
        "plus_di": 30.0,
        "minus_di": 12.0,
        "rsi": 58.0,
        "rsi_14": 58.0,
        "sma_20": close * 0.96,
        "sma_50": close * 0.92,
        "ema_12": close * 0.99,
        "ema_26": close * 0.97,
        "macd_hist": 0.6,
        "volume_ratio": 1.6,
        "realized_vol": 0.25,
        "momentum_5d": 0.04,
        "momentum_20d": 0.08,
        "catalyst_risk": 0.0,
        "earnings_in_days": 30.0,
    }


def _make_screener_candidate(
    ticker: str = "QQQ",
    bias: str = "bullish",
    confidence: float = 0.88,
    metrics: Optional[Dict[str, Any]] = None,
) -> ScreenerResult:
    m = dict(metrics or _make_metrics())
    return ScreenerResult(
        ticker=ticker,
        bias=bias,
        setup_score="favorable",
        overall_confidence=confidence,
        catalyst_risk=0.0,
        strategy_type="directional_trend",
        state_document="Strong trend setup",
        metrics=m,
    )


def _make_debate_outcome(ticker: str = "QQQ", decision: str = "BUY_ENTRY", conviction: float = 0.85) -> DebateOutcome:
    return DebateOutcome(
        ticker=ticker,
        winning_side="BULL" if decision == "BUY_ENTRY" else "BEAR",
        consensus_decision=decision,
        bull_position=DebatePosition(side="BULL", thesis="Strong uptrend", confidence=conviction),
        bear_position=DebatePosition(side="BEAR", thesis="Mild overextension", confidence=0.35),
        synthesis="Bullish trend consensus",
        conviction_score=conviction,
        debate_rounds=3,
    )


class TestLeveragedUniverseDynamic5x(unittest.TestCase):
    """Tests for U4 LeveragedUniverse dynamic 5x override & deep_analyse bugfix."""

    def test_dynamic_allow_5x_and_config_snapshot_override(self) -> None:
        uni = LeveragedUniverse()
        with patch.object(settings, "ENABLE_5X_LEVERAGE", False):
            # Default preserves global settings fallback (False -> 3x)
            self.assertEqual(uni.get_execution_ticker("QQQ", "LONG", leverage=5), "QQQ3m_EQ")
            self.assertEqual(uni.get_execution_ticker("QQQ", "SHORT", leverage=5), "QQQSm_EQ")

            # Explicit allow_5x=True overrides settings=False
            self.assertEqual(uni.get_execution_ticker("QQQ", "LONG", leverage=5, allow_5x=True), "5QQEl_EQ")
            self.assertEqual(uni.get_execution_ticker("QQQ", "SHORT", leverage=5, allow_5x=True), "SQQEl_EQ")

            # ConfigSnapshot override overrides settings=False
            snap_on = ConfigSnapshot.from_dict({"config_epoch_id": "ep_5x_on", "ENABLE_5X_LEVERAGE": True})
            self.assertEqual(
                uni.get_execution_ticker("QQQ", "LONG", leverage=5, config_snapshot=snap_on),
                "5QQEl_EQ",
            )
            all_on = uni.get_all_execution_tickers(config_snapshot=snap_on)
            self.assertIn("5QQEl_EQ", all_on)
            self.assertIn("SQQEl_EQ", all_on)

        # Explicit False / snapshot False overrides settings=True
        with patch.object(settings, "ENABLE_5X_LEVERAGE", True):
            snap_off = ConfigSnapshot.from_dict({"config_epoch_id": "ep_5x_off", "ENABLE_5X_LEVERAGE": False})
            self.assertEqual(uni.get_execution_ticker("QQQ", "LONG", leverage=5, allow_5x=False), "QQQ3m_EQ")
            self.assertEqual(
                uni.get_execution_ticker("QQQ", "LONG", leverage=5, config_snapshot=snap_off),
                "QQQ3m_EQ",
            )
            all_off = uni.get_all_execution_tickers(config_snapshot=snap_off)
            self.assertNotIn("5QQEl_EQ", all_off)
            self.assertIn("QQQ3m_EQ", all_off)

    def test_orchestrator_deep_analyse_5x_false_default_and_snapshot_toggle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            bus = ConfigBus(data_dir=tmpdir)
            client = _FakeT212Client()
            orch = Orchestrator(dry_run=True, enable_dashboard=False, t212_client=client, config_bus=bus, data_dir=tmpdir)
            try:
                cand = _make_screener_candidate("QQQ", "bullish", confidence=0.92, metrics=_make_metrics(close=100.0, atr=2.0))
                orch.technical_analyst.analyze = MagicMock(return_value="tech")
                orch.technical_analyst.format_report = MagicMock(return_value="tech report")
                orch.fundamental_analyst.analyze = MagicMock(return_value="fund")
                orch.fundamental_analyst.format_report = MagicMock(return_value="fund report")
                orch.debate_engine.run_debate = MagicMock(return_value=_make_debate_outcome("QQQ", "BUY_ENTRY", 0.9))

                with patch.object(settings, "ENABLE_5X_LEVERAGE", True):
                    # Explicitly using a snapshot with ENABLE_5X_LEVERAGE=False must NOT leak settings=True or old True fallback
                    snap_no_5x = ConfigSnapshot.from_dict({
                        "config_epoch_id": "ep_off",
                        "ENABLE_5X_LEVERAGE": False,
                        "MAX_LEVERAGE": 3,
                        "ENABLE_FINNHUB_FUNDAMENTALS": False,
                        "ENABLE_FINNHUB_NEWS": False,
                        "ENABLE_OLOSTEP_SENTIMENT": False,
                        "ENABLE_NIXTLA_FORECAST": False,
                    })
                    card_3x = orch.deep_analyse(cand, account_equity=10000.0, config_snapshot=snap_no_5x)
                    self.assertIsNotNone(card_3x)
                    self.assertEqual(card_3x.trading212_ticker, "QQQ3m_EQ")
                    self.assertEqual(card_3x.leverage, 3)

                # Snapshot enabling 5x resolves 5QQEl_EQ even when global settings is False
                with patch.object(settings, "ENABLE_5X_LEVERAGE", False):
                    snap_5x = ConfigSnapshot.from_dict({
                        "config_epoch_id": "ep_on",
                        "ENABLE_5X_LEVERAGE": True,
                        "PREFER_5X_LEVERAGE": True,
                        "MAX_LEVERAGE": 5,
                        "ENABLE_FINNHUB_FUNDAMENTALS": False,
                        "ENABLE_FINNHUB_NEWS": False,
                        "ENABLE_OLOSTEP_SENTIMENT": False,
                        "ENABLE_NIXTLA_FORECAST": False,
                    })
                    card_5x = orch.deep_analyse(cand, account_equity=10000.0, config_snapshot=snap_5x)
                    self.assertIsNotNone(card_5x)
                    self.assertEqual(card_5x.trading212_ticker, "5QQEl_EQ")
                    self.assertEqual(card_5x.leverage, 5)
            finally:
                orch.shutdown()


class TestOrchestratorControlPlane(unittest.TestCase):
    """Integration tests for mid-scan epoch checks, execution lock, spread gate, funnel, and control plane."""

    def setUp(self) -> None:
        super().setUp()
        for target, kwargs in (
            ("trading_system.data.trading212_client.MarketDataProvider.is_ticker_synthetic", {"return_value": False}),
            ("trading_system.data.trading212_client.MarketDataProvider.get_bid_ask_spread", {"return_value": {"spread_pct": 0.002}}),
            ("trading_system.data.trading212_client.MarketDataProvider.get_quote", {"return_value": {"price": 100.0}}),
            (
                "trading_system.data.trading212_client.Trading212Client.get_market_session_info",
                {"return_value": {"exchange": "CRYPTO", "open_time_utc": "00:00", "close_time_utc": "23:59", "current_time_utc": "12:00"}},
            ),
        ):
            p = patch(target, **kwargs)
            p.start()
            self.addCleanup(p.stop)

    def _make_orch(self, tmpdir: str, *, dry_run: bool = False, spread: float = 0.002) -> tuple[Orchestrator, _FakeT212Client, ConfigBus]:
        bus = ConfigBus(data_dir=tmpdir)
        client = _FakeT212Client(initial_equity=10000.0, spread=spread)
        notifier = Notifier(log_file=os.path.join(tmpdir, "notifications.jsonl"), enable_desktop=False)
        orch = Orchestrator(
            dry_run=dry_run,
            notifier=notifier,
            enable_dashboard=False,
            t212_client=client,
            config_bus=bus,
            data_dir=tmpdir,
        )
        return orch, client, bus

    def test_mid_scan_abort_stale_scan_prunes_llm_and_records_funnel(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            orch, client, bus = self._make_orch(tmpdir, dry_run=True)
            try:
                metrics_map = {"QQQ": _make_metrics(adx=34.0)}
                cand = _make_screener_candidate("QQQ", "bullish", metrics=metrics_map["QQQ"])

                def _ingest_and_mutate(config_snapshot: Optional[ConfigSnapshot] = None) -> Dict[str, Dict[str, Any]]:
                    # Operator commits a screener parameter mutation mid-scan -> abort_stale_scan!
                    bus.commit_patch({"MIN_ADX_TREND": 28.0}, expected_epoch_id=bus.active_epoch_id, author="test")
                    return metrics_map

                orch.ingest_universe_data = _ingest_and_mutate  # type: ignore[method-assign]
                orch.screen_candidates_with_funnel = MagicMock(return_value=([cand], [cand]))  # type: ignore[method-assign]
                orch.technical_analyst.analyze = MagicMock()
                orch.fundamental_analyst.analyze = MagicMock()
                orch.debate_engine.run_debate = MagicMock()

                sse_events: List[tuple[str, Any]] = []
                with patch.object(orch, "_broadcast_sse", side_effect=lambda et, d: sse_events.append((et, d))):
                    cards = orch._scan_and_deliberate()

                self.assertEqual(cards, [])
                # Zero expensive LLM calls executed
                orch.technical_analyst.analyze.assert_not_called()
                orch.fundamental_analyst.analyze.assert_not_called()
                orch.debate_engine.run_debate.assert_not_called()

                # Funnel persisted atomically & snapshot updated with stale_epoch_superseded
                funnel_path = Path(tmpdir) / "funnel_state.json"
                self.assertTrue(funnel_path.exists())
                persisted = json.loads(funnel_path.read_text(encoding="utf-8"))
                snap = orch.get_dashboard_snapshot()
                self.assertEqual(snap["funnel"]["status"], "aborted_stale_epoch")
                gate_ids = {g["gate_id"] for g in persisted.get("gate_breakdown", [])}
                self.assertIn("stale_epoch_superseded", gate_ids)
                self.assertTrue(any(et == "dashboard_update" for et, _ in sse_events))
            finally:
                orch.shutdown()

    def test_mid_scan_pre_execution_abort_and_deterministic_resize_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            orch, client, bus = self._make_orch(tmpdir, dry_run=False, spread=0.002)
            try:
                with patch(
                    "trading_system.orchestrator.MarketDataProvider.get_bid_ask_spread",
                    return_value={"spread_pct": 0.002},
                ):
                    cand = _make_screener_candidate("QQQ", "bullish", confidence=0.88, metrics=_make_metrics(close=100.0, atr=1.0))
                    snap1 = bus.commit_patch(
                        {"MAX_RISK_PER_TRADE": 0.01, "MAX_POSITION_PCT": 0.50, "ATR_SL_MULTIPLIER": 1.5},
                        expected_epoch_id=bus.active_epoch_id,
                        author="test",
                    )
                    epoch1 = snap1.config_epoch_id
                    card = orch.risk_manager.generate_decision_card(
                        ticker="QQQ",
                        trading212_ticker="QQQ3m_EQ",
                        direction="LONG",
                        metrics=dict(cand.metrics),
                        debate_outcome=_make_debate_outcome("QQQ", "BUY_ENTRY", 0.88),
                        account_equity=10000.0,
                        strategy_type="directional_trend",
                        leverage=3,
                        config_snapshot=snap1,
                    )
                    self.assertIsNotNone(card)
                    card.config_epoch_id = epoch1

                    # Case 1: Only resize_entry keys change -> deterministically resizes & stamps new epoch without aborting
                    snap2 = bus.commit_patch(
                        {"MAX_RISK_PER_TRADE": 0.02, "MAX_POSITION_PCT": 0.50, "ATR_SL_MULTIPLIER": 2.0},
                        expected_epoch_id=epoch1,
                        author="test",
                    )
                    expected_resized = orch.risk_manager.generate_decision_card(
                        ticker="QQQ",
                        trading212_ticker="QQQ3m_EQ",
                        direction="LONG",
                        metrics=dict(cand.metrics),
                        debate_outcome=_make_debate_outcome("QQQ", "BUY_ENTRY", 0.88),
                        account_equity=10000.0,
                        strategy_type="directional_trend",
                        leverage=3,
                        config_snapshot=snap2,
                    )
                    executed = orch.execute_trades_batch([(cand, card)])
                    self.assertEqual(len(executed), 1)
                    self.assertEqual(len(client.placed_orders), 1)
                    res_card = executed[0]
                    self.assertEqual(res_card.config_epoch_id, snap2.config_epoch_id)
                    self.assertAlmostEqual(res_card.stop_loss, expected_resized.stop_loss, places=4)
                    self.assertAlmostEqual(res_card.position_size_shares, expected_resized.position_size_shares, places=4)
                    self.assertLess(res_card.stop_loss, card.stop_loss)
                    pos = orch.position_monitor.positions["QQQ3m_EQ"]
                    self.assertEqual(pos.config_epoch_id, snap2.config_epoch_id)

                    # Clean out position so Case 2 tests abort_stale_scan pre-execution
                    orch.position_monitor.positions.clear()
                    client.placed_orders.clear()
                    epoch2 = bus.active_epoch_id
                    card2 = DecisionCard(
                        ticker="NVDA",
                        trading212_ticker="3LVEl_EQ",
                        direction="LONG",
                        decision="BUY_ENTRY",
                        entry_price=100.0,
                        stop_loss=96.0,
                        trailing_stop_loss=96.0,
                        stop_distance_pct=0.04,
                        take_profit_1=104.0,
                        take_profit_2=108.0,
                        take_profit_3=112.0,
                        position_size_shares=20.0,
                        position_notional=2000.0,
                        risk_amount=80.0,
                        risk_pct_of_equity=0.008,
                        conviction_score=0.85,
                        rationale="Card 2",
                        strategy_type="directional_trend",
                        leverage=3,
                        config_epoch_id=epoch2,
                    )
                    cand2 = _make_screener_candidate("NVDA", "bullish")
                    bus.commit_patch({"MIN_ADX_TREND": 30.0}, expected_epoch_id=epoch2, author="test")
                    executed2 = orch.execute_trades_batch([(cand2, card2)])
                    self.assertEqual(executed2, [])
                    self.assertEqual(len(client.placed_orders), 0)
                    snap_after = orch.get_dashboard_snapshot()
                    gate_ids = {g["gate_id"] for g in snap_after["funnel"].get("gate_breakdown", [])}
                    self.assertIn("stale_epoch_superseded", gate_ids)
            finally:
                orch.shutdown()

    def test_concurrent_execute_trade_vs_panic_flatten_execution_gate_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            orch, client, bus = self._make_orch(tmpdir, dry_run=False, spread=0.002)
            try:
                snap = bus.active_snapshot()
                card = DecisionCard(
                    ticker="QQQ",
                    trading212_ticker="QQQ3m_EQ",
                    direction="LONG",
                    decision="BUY_ENTRY",
                    entry_price=100.0,
                    stop_loss=97.0,
                    trailing_stop_loss=97.0,
                    stop_distance_pct=0.03,
                    take_profit_1=103.0,
                    take_profit_2=106.0,
                    take_profit_3=109.0,
                    position_size_shares=10.0,
                    position_notional=1000.0,
                    risk_amount=30.0,
                    risk_pct_of_equity=0.003,
                    conviction_score=0.85,
                    rationale="Gate lock race test",
                    config_epoch_id=snap.config_epoch_id,
                )

                entered_order = threading.Event()
                allow_order_finish = threading.Event()
                panic_done = threading.Event()
                panic_result: Dict[str, Any] = {}

                def _slow_place(ticker: str, quantity: float, oid: str) -> None:
                    if quantity > 0:
                        entered_order.set()
                        allow_order_finish.wait(timeout=5.0)

                client.on_place_market_order = _slow_place

                with patch(
                    "trading_system.orchestrator.MarketDataProvider.get_bid_ask_spread",
                    return_value={"spread_pct": 0.002},
                ):
                    t_exec = threading.Thread(
                        target=lambda: orch.execute_trade(card, atr=2.0, available_cash=8000.0, positions=[], config_snapshot=snap),
                        daemon=True,
                    )
                    t_exec.start()
                    self.assertTrue(entered_order.wait(timeout=5.0))

                    def _run_panic() -> None:
                        panic_result["res"] = orch.trigger_panic_flatten(reason="Concurrent panic race test")
                        panic_done.set()

                    t_panic = threading.Thread(target=_run_panic, daemon=True)
                    t_panic.start()
                    # Panic MUST wait for _execution_gate_lock while execute_trade finishes placing & registering
                    time.sleep(0.08)
                    self.assertFalse(panic_done.is_set(), "panic_flatten_all bypassed _execution_gate_lock!")

                    allow_order_finish.set()
                    t_exec.join(timeout=5.0)
                    t_panic.join(timeout=5.0)
                    self.assertTrue(panic_done.is_set())

                    # Because execute_trade registered before releasing _execution_gate_lock, panic liquidated it cleanly
                    self.assertTrue(orch.position_monitor.is_system_halted())
                    tracked = orch.position_monitor.positions["QQQ3m_EQ"]
                    self.assertTrue(tracked.is_closed)
                    self.assertEqual(tracked.remaining_quantity, 0.0)
                    self.assertEqual(len(orch.get_dashboard_snapshot()["active_signals"]), 0)
                    sell_orders = [o for o in client.placed_orders if o["quantity"] < 0 and o["ticker"] == "QQQ3m_EQ"]
                    self.assertEqual(len(sell_orders), 1)

                    # Subsequent execute_trade while halted MUST re-check inside _execution_gate_lock and refuse to place order
                    before_count = len(client.placed_orders)
                    ok_after = orch.execute_trade(card, atr=2.0, available_cash=8000.0, positions=[], config_snapshot=snap)
                    self.assertFalse(ok_after)
                    self.assertEqual(len(client.placed_orders), before_count)
            finally:
                orch.shutdown()

    def test_spread_gate_reads_snapshot_max_bid_ask_spread_pct(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            # 1.2% spread exceeds old hardcoded 0.010, but passes default MAX_BID_ASK_SPREAD_PCT=0.015
            orch, client, bus = self._make_orch(tmpdir, dry_run=False, spread=0.012)
            try:
                with patch(
                    "trading_system.orchestrator.MarketDataProvider.get_bid_ask_spread",
                    return_value={"spread_pct": 0.002},
                ):
                    snap_default = bus.active_snapshot()
                    card = DecisionCard(
                        ticker="QQQ",
                        trading212_ticker="QQQ3m_EQ",
                        direction="LONG",
                        decision="BUY_ENTRY",
                        entry_price=100.0,
                        stop_loss=97.0,
                        trailing_stop_loss=97.0,
                        stop_distance_pct=0.03,
                        take_profit_1=103.0,
                        take_profit_2=106.0,
                        take_profit_3=109.0,
                        position_size_shares=10.0,
                        position_notional=1000.0,
                        risk_amount=30.0,
                        risk_pct_of_equity=0.003,
                        conviction_score=0.85,
                        rationale="Spread test",
                        config_epoch_id=snap_default.config_epoch_id,
                    )
                    ok_default = orch.execute_trade(card, atr=2.0, available_cash=8000.0, positions=[], config_snapshot=snap_default)
                    self.assertTrue(ok_default)
                    self.assertEqual(len(client.placed_orders), 1)

                    # Tightening MAX_BID_ASK_SPREAD_PCT to 0.008 rejects the exact same 1.2% spread on NVDA
                    snap_tight = bus.commit_patch({"MAX_BID_ASK_SPREAD_PCT": 0.008}, expected_epoch_id=bus.active_epoch_id, author="test")
                    card_nvda = DecisionCard(
                        ticker="NVDA",
                        trading212_ticker="3LVEl_EQ",
                        direction="LONG",
                        decision="BUY_ENTRY",
                        entry_price=100.0,
                        stop_loss=97.0,
                        trailing_stop_loss=97.0,
                        stop_distance_pct=0.03,
                        take_profit_1=103.0,
                        take_profit_2=106.0,
                        take_profit_3=109.0,
                        position_size_shares=10.0,
                        position_notional=1000.0,
                        risk_amount=30.0,
                        risk_pct_of_equity=0.003,
                        conviction_score=0.85,
                        rationale="Tight spread test",
                        config_epoch_id=snap_tight.config_epoch_id,
                    )
                    ok_tight = orch.execute_trade(card_nvda, atr=2.0, available_cash=8000.0, positions=[], config_snapshot=snap_tight)
                    self.assertFalse(ok_tight)
                    self.assertEqual(len(client.placed_orders), 1)

                    # Disabling ENFORCE_SPREAD_GATE allows it through even with max spread 0.008
                    snap_bypass = bus.commit_patch({"ENFORCE_SPREAD_GATE": False}, expected_epoch_id=bus.active_epoch_id, author="test")
                    ok_bypass = orch.execute_trade(card_nvda, atr=2.0, available_cash=8000.0, positions=[], config_snapshot=snap_bypass)
                    self.assertTrue(ok_bypass)
                    self.assertEqual(len(client.placed_orders), 2)
            finally:
                orch.shutdown()

    def test_boot_quarantine_notifier_zero_candidate_funnel_and_zero_http_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            # Write corrupt persisted override before boot -> triggers ConfigBus quarantine warning + boot Notifier alert
            (Path(tmpdir) / "config_overrides.json").write_text("{corrupt_json", encoding="utf-8")
            client = _FakeT212Client(initial_equity=12500.0)
            notifier = Notifier(log_file=os.path.join(tmpdir, "notifications.jsonl"), enable_desktop=False)
            dispatched: List[TradeNotification] = []
            notifier.dispatch = MagicMock(side_effect=lambda note: dispatched.append(note))  # type: ignore[method-assign]

            orch = Orchestrator(
                dry_run=True,
                notifier=notifier,
                enable_dashboard=False,
                t212_client=client,
                data_dir=tmpdir,
            )
            try:
                snap_boot = orch.get_dashboard_snapshot()
                self.assertTrue(snap_boot["config_quarantine_warnings"])
                self.assertTrue(
                    any("quarantine" in f"{n.event_type} {n.rationale}".lower() for n in dispatched),
                    f"Expected quarantine SYSTEM_ALERT dispatch, got {dispatched}",
                )

                # get_dashboard_snapshot() must NEVER call Trading 212 HTTP getters
                client.cash_calls.clear()
                client.portfolio_calls.clear()
                client.orders_calls.clear()
                client.hist_calls.clear()
                dash = orch.get_dashboard_snapshot()
                self.assertEqual(len(client.cash_calls), 0)
                self.assertEqual(len(client.portfolio_calls), 0)
                self.assertEqual(len(client.orders_calls), 0)
                self.assertEqual(len(client.hist_calls), 0)

                # Dynamic broker_status check (no legacy hardcoded 84% / 4923.05)
                self.assertTrue(dash["controller_attached"])
                self.assertIn("eur_ratio", dash["broker_status"])
                self.assertNotIn("4923.05", json.dumps(dash))
                self.assertEqual(dash["broker_status"]["universe_count"], len(orch.universe))

                # 0-candidate _scan_and_deliberate MUST still persist funnel_state.json + update read-model + emit SSE
                raw_metrics = {"QQQ": _make_metrics(adx=12.0), "NVDA": _make_metrics(adx=11.0)}
                orch.ingest_universe_data = MagicMock(return_value=raw_metrics)  # type: ignore[method-assign]
                orch.screen_candidates_with_funnel = MagicMock(  # type: ignore[method-assign]
                    return_value=(
                        [],
                        [
                            ScreenerResult(
                                ticker="QQQ",
                                bias="neutral",
                                setup_score="unfavorable",
                                overall_confidence=0.1,
                                catalyst_risk=0.0,
                                state_document="ADX low",
                                metrics=raw_metrics["QQQ"],
                                gate_rejections=[{"gate_id": "min_adx_trend", "param_keys": ["MIN_ADX_TREND"]}],
                            )
                        ],
                    )
                )
                sse_events: List[tuple[str, Any]] = []
                with patch.object(orch, "_broadcast_sse", side_effect=lambda et, d: sse_events.append((et, d))):
                    cards = orch._scan_and_deliberate()
                self.assertEqual(cards, [])
                funnel_disk = json.loads((Path(tmpdir) / "funnel_state.json").read_text(encoding="utf-8"))
                self.assertEqual(funnel_disk["stage_counts"]["universe"], 2)
                self.assertEqual(funnel_disk["stage_counts"]["screened"], 0)
                self.assertEqual(funnel_disk["stage_counts"]["executed"], 0)
                self.assertTrue(any(et == "dashboard_update" for et, _ in sse_events))

                # Off-hours reconciliation step uses cached getters and broadcasts dashboard_update
                client.cash_calls.clear()
                sse_events.clear()
                with patch.object(orch, "_broadcast_sse", side_effect=lambda et, d: sse_events.append((et, d))):
                    orch._run_off_hours_reconciliation_step(now=time.time())
                self.assertEqual(len(client.cash_calls), 0)
                self.assertTrue(any(et == "dashboard_update" for et, _ in sse_events))

                # Priority monitor tick publishes cached update only when position state / events change
                orch.position_monitor.register_new_trade(
                    DecisionCard(
                        ticker="QQQ",
                        trading212_ticker="QQQ3l_EQ",
                        direction="LONG",
                        decision="BUY_ENTRY",
                        entry_price=100.0,
                        stop_loss=94.0,
                        trailing_stop_loss=94.0,
                        stop_distance_pct=0.06,
                        take_profit_1=106.0,
                        take_profit_2=112.0,
                        take_profit_3=118.0,
                        position_size_shares=5.0,
                        position_notional=500.0,
                        risk_amount=30.0,
                        risk_pct_of_equity=0.003,
                        conviction_score=0.8,
                        rationale="Monitor tick test",
                        config_epoch_id=orch.config_bus.active_epoch_id,
                    ),
                    atr=2.0,
                    fill_price=100.0,
                )
                client.portfolio_positions = [
                    Position(
                        ticker="QQQ3l_EQ",
                        quantity=5.0,
                        average_price=100.0,
                        current_price=101.0,
                        ppl=5.0,
                        max_buy=100.0,
                        max_sell=5.0,
                    )
                ]
                client.portfolio_calls.clear()
                sse_events.clear()
                orch.fetch_active_trades_metrics = MagicMock(return_value={"QQQ": _make_metrics(close=101.0)})  # type: ignore[method-assign]
                with patch.object(orch, "_broadcast_sse", side_effect=lambda et, d: sse_events.append((et, d))):
                    orch._run_priority_monitor_step(now=time.time())
                self.assertTrue(all(c.get("cached_only") is True for c in client.portfolio_calls))
                self.assertTrue(any(et == "dashboard_update" for et, _ in sse_events))
            finally:
                orch.shutdown()

    def test_control_gateway_mutations_re_epoch_panic_resume_and_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            orch, client, bus = self._make_orch(tmpdir, dry_run=False, spread=0.002)
            try:
                # Seed funnel compact indicators so preview_config_patch includes screener gate impact
                raw_metrics = {"QQQ": _make_metrics(adx=26.0), "NVDA": _make_metrics(adx=19.0)}
                orch.ingest_universe_data = MagicMock(return_value=raw_metrics)  # type: ignore[method-assign]
                orch.screen_candidates_with_funnel = MagicMock(return_value=([], []))  # type: ignore[method-assign]
                orch._scan_and_deliberate()

                # Register a position at ep0 and advance config bus to ep1 so preview sees 1 position behind epoch
                ep0 = bus.active_epoch_id
                orch.position_monitor.register_new_trade(
                    DecisionCard(
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
                        rationale="Control test",
                        config_epoch_id=ep0,
                    ),
                    atr=2.0,
                    fill_price=100.0,
                    config_snapshot=bus.active_snapshot(),
                )
                ep1 = orch.commit_config_patch({"MIN_ADX_TREND": 24.0}, expected_epoch_id=ep0)["active_epoch_id"]

                # 1. preview_config_patch includes screener impact + behind epoch positions
                prev = orch.preview_config_patch({"MIN_ADX_TREND": 28.0}, expected_epoch_id=ep1)
                self.assertTrue(prev["valid"])
                self.assertIn("gate_impact", prev)
                self.assertTrue(prev["gate_impact"]["available"])
                self.assertEqual(prev["positions_behind_epoch_count"], 1)

                # 2. commit_config_patch CAS check + SSE config_epoch_changed
                sse_events: List[tuple[str, Any]] = []
                with patch.object(orch, "_broadcast_sse", side_effect=lambda et, d: sse_events.append((et, d))):
                    res_commit = orch.commit_config_patch(
                        {"MIN_ADX_TREND": 28.0, "ATR_SL_MULTIPLIER": 2.0},
                        expected_epoch_id=ep1,
                        author="operator",
                        reason="Raise ADX floor",
                    )
                ep2 = res_commit["active_epoch_id"]
                self.assertNotEqual(ep1, ep2)
                self.assertTrue(any(et == "config_epoch_changed" for et, _ in sse_events))
                with self.assertRaises(ConfigConflictError):
                    orch.commit_config_patch({"MIN_ADX_TREND": 25.0}, expected_epoch_id=ep1)

                # 3. add/delete custom parameter
                added = orch.add_custom_parameter(
                    {
                        "key": "CUSTOM_ADX_MIN",
                        "label": "Custom ADX Min",
                        "type": "float",
                        "default": 20.0,
                        "min": 0.0,
                        "max": 80.0,
                        "screener_metric_gate": {"metric": "adx", "op": ">=", "threshold": 20.0, "on_missing": "reject"},
                    },
                    expected_epoch_id=ep2,
                )
                ep3 = added["active_epoch_id"]
                self.assertIn("CUSTOM_ADX_MIN", {p["key"] for p in added["schema"]["parameters"]})
                self.assertEqual(added["snapshot"]["dynamic_parameter_specs"][0]["key"], "CUSTOM_ADX_MIN")
                self.assertEqual(added["dashboard"]["config"]["dynamic_parameter_specs"][0]["key"], "CUSTOM_ADX_MIN")
                self.assertNotIn("TRADING212_API_KEY", added["dashboard"]["config"])
                deleted = orch.delete_custom_parameter("CUSTOM_ADX_MIN", expected_epoch_id=ep3)
                ep4 = deleted["active_epoch_id"]
                self.assertNotIn("CUSTOM_ADX_MIN", {p["key"] for p in deleted["schema"]["parameters"]})

                # 4. apply_config_profile
                prof_res = orch.apply_config_profile("conservative", expected_epoch_id=ep4, author="operator")
                ep5 = prof_res["active_epoch_id"]
                self.assertEqual(ep5, orch.get_dashboard_snapshot()["active_epoch_id"])

                # 5. Re-epoch position, batch re-epoch & behind-epoch tracking
                snap_behind = orch.get_dashboard_snapshot()
                self.assertEqual(len(snap_behind["positions_behind_epoch"]), 1)
                pos_prev = orch.preview_position_re_epoch("QQQ3l_EQ")
                self.assertIn("target_epoch_id", pos_prev)
                all_prev = orch.preview_re_epoch_all()
                self.assertIn("positions", all_prev)
                with self.assertRaises(PositionEpochConflictError):
                    orch.re_epoch_position("QQQ3l_EQ", expected_position_epoch_id="wrong_ep")
                re_ep_res = orch.re_epoch_position("QQQ3l_EQ", expected_position_epoch_id=ep0)
                self.assertEqual(len(re_ep_res["dashboard"]["positions_behind_epoch"]), 0)

                # Advance one more epoch and exercise batch re_epoch_all_positions CAS + migration
                ep6 = orch.commit_config_patch({"TP1_R_MULTIPLE": 1.25}, expected_epoch_id=ep5)["active_epoch_id"]
                self.assertEqual(len(orch.get_dashboard_snapshot()["positions_behind_epoch"]), 1)
                with self.assertRaises(PositionEpochConflictError):
                    orch.re_epoch_all_positions(expected_epoch_id=ep5)
                batch_re = orch.re_epoch_all_positions(expected_epoch_id=ep6)
                self.assertEqual(batch_re["succeeded_count"], 1)
                self.assertEqual(len(batch_re["dashboard"]["positions_behind_epoch"]), 0)

                # 6. Panic with failing liquidation -> retry_panic_liquidation -> ResumeBlockedError -> resume
                client.fail_sell_tickers.add("QQQ3l_EQ")
                client.fail_stop_tickers.add("QQQ3l_EQ")
                sse_events.clear()
                with patch.object(orch, "_broadcast_sse", side_effect=lambda et, d: sse_events.append((et, d))):
                    panic_out = orch.trigger_panic_flatten(reason="Failed sell test")
                self.assertTrue(panic_out["is_halted"])
                self.assertTrue(any(et == "panic_triggered" for et, _ in sse_events))
                self.assertEqual(len(orch.get_dashboard_snapshot()["unprotected_positions"]), 1)

                # Fail-closed resume raises ResumeBlockedError while panic_failed and stop re-arm fails
                with self.assertRaises(ResumeBlockedError):
                    orch.resume_trading(confirm_unprotected=False)

                # Now let broker accept liquidation and retry_panic_liquidation cleanly
                client.fail_sell_tickers.clear()
                client.fail_stop_tickers.clear()
                retry_out = orch.retry_panic_liquidation("QQQ3l_EQ")
                self.assertTrue(retry_out["ok"])
                self.assertTrue(orch.position_monitor.positions["QQQ3l_EQ"].is_closed)
                self.assertEqual(len(retry_out["dashboard"]["unprotected_positions"]), 0)
                self.assertEqual(len(retry_out["dashboard"]["active_signals"]), 0)

                # Now clean resume succeeds with confirm_unprotected=False
                sse_events.clear()
                with patch.object(orch, "_broadcast_sse", side_effect=lambda et, d: sse_events.append((et, d))):
                    resumed = orch.resume_trading(confirm_unprotected=False)
                self.assertFalse(resumed["dashboard"]["is_halted"])
                self.assertTrue(any(et == "trading_resumed" for et, _ in sse_events))
            finally:
                orch.shutdown()

    def test_u5_start_local_server_thread_passes_orchestrator_and_no_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            bus = ConfigBus(data_dir=tmpdir)
            client = _FakeT212Client()
            called: Dict[str, Any] = {}

            def _fake_start_server(*, port: int = 8888, open_browser: bool = False, orchestrator: Any = None, allow_port_fallback: bool = True) -> tuple[Any, Any, str]:
                called["port"] = port
                called["open_browser"] = open_browser
                called["orchestrator"] = orchestrator
                called["allow_port_fallback"] = allow_port_fallback
                return None, None, f"http://localhost:{port}"

            with patch("trading_system.dashboard.server.start_local_server_thread", side_effect=_fake_start_server):
                orch = Orchestrator(
                    dry_run=True,
                    enable_dashboard=True,
                    dashboard_port=8999,
                    open_browser=False,
                    t212_client=client,
                    config_bus=bus,
                    data_dir=tmpdir,
                )
                try:
                    self.assertIs(called["orchestrator"], orch)
                    self.assertEqual(called["allow_port_fallback"], False)
                    self.assertEqual(called["port"], 8999)
                finally:
                    orch.shutdown()

    def test_magicmock_safe_active_position_and_snapshot_inspection(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            bus = ConfigBus(data_dir=tmpdir)
            client = _FakeT212Client()
            orch = Orchestrator(
                dry_run=True,
                enable_dashboard=False,
                t212_client=client,
                config_bus=bus,
                data_dir=tmpdir,
            )
            try:
                orch.position_monitor.positions = {
                    "MOCK_UNCONFIGURED": MagicMock(),
                    "MOCK_CLOSED": MagicMock(is_closed=True, remaining_quantity=5.0),
                    "MOCK_OPEN": MagicMock(
                        is_closed=False,
                        remaining_quantity=4.0,
                        ticker="MOCK_OPEN",
                        underlying="QQQ",
                        direction="LONG",
                        entry_price=100.0,
                    ),
                    "DICT_OPEN": {
                        "ticker": "DICT_OPEN",
                        "underlying": "NVDA",
                        "direction": "LONG",
                        "is_closed": False,
                        "remaining_quantity": 2.0,
                        "entry_price": 50.0,
                    },
                }

                self.assertTrue(orch._has_active_position("MOCK_UNCONFIGURED"))
                self.assertFalse(orch._has_active_position("MOCK_CLOSED"))
                self.assertTrue(orch._has_active_position("MOCK_OPEN"))
                self.assertTrue(orch._has_active_position("DICT_OPEN"))

                halted, rows = orch._snapshot_positions_under_lock()
                self.assertFalse(halted)
                row_tickers = {t for t, _ in rows}
                self.assertEqual(row_tickers, {"MOCK_OPEN", "DICT_OPEN"})
                refreshed = orch._refresh_read_model_snapshot(persist_funnel=False, broadcast=False)
                self.assertEqual(len(refreshed["active_signals"]), 2)
                snap = orch.get_dashboard_snapshot()
                self.assertEqual(len(snap["active_signals"]), 2)
            finally:
                orch.shutdown()


if __name__ == "__main__":
    unittest.main()
