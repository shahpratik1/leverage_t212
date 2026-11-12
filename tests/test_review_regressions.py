"""Targeted regression tests for LFG Step 5 code-review fixes."""

from __future__ import annotations

from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from trading_system.agents.risk_manager import DecisionCard, RiskManager
from trading_system.config_bus import ConfigBus, ConfigSnapshot
from trading_system.config_schema import get_builtin_profiles
from trading_system.dashboard.auth import DashboardAuthManager
from trading_system.dashboard.server import SSEBroadcaster, make_dashboard_handler
from trading_system.data.trading212_client import OrderResult, Position, Trading212Error
from trading_system.execution.position_monitor import PositionMonitor
from trading_system.notifications.notifier import Notifier
from trading_system.orchestrator import Orchestrator
from trading_system.strategies.base import read_config_value
from tests.test_control_gateway_api import _FakeOrchestrator, _GatewayTestClient
from tests.test_orchestrator_control_plane import _FakeT212Client
from tests.test_workbench_assets import WORKBENCH_JS_PATH, _NODE_HARNESS_SCRIPT, _find_node


def _sample_card(ticker: str = "QQQ3l_EQ", price: float = 100.0) -> DecisionCard:
    return DecisionCard(
        ticker="QQQ",
        trading212_ticker=ticker,
        direction="LONG",
        decision="BUY_ENTRY",
        entry_price=price,
        stop_loss=round(price * 0.94, 2),
        trailing_stop_loss=round(price * 0.94, 2),
        stop_distance_pct=6.0,
        take_profit_1=round(price * 1.06, 2),
        take_profit_2=round(price * 1.12, 2),
        take_profit_3=round(price * 1.18, 2),
        position_size_shares=10.0,
        position_notional=price * 10.0,
        risk_amount=price * 0.6,
        risk_pct_of_equity=0.01,
        conviction_score=85.0,
        rationale="Review regression test card",
        strategy_type="directional_trend",
        leverage=3,
    )


class TestCoreAndMonitorReviewRegressions(unittest.TestCase):
    """Regressions across strategies/base, ConfigBus, RiskManager, PositionMonitor, Orchestrator."""

    def test_read_config_value_reads_dynamic_parameters_and_ignores_mock_get(self) -> None:
        snap = ConfigSnapshot.from_dict({
            "config_epoch_id": "ep-dyn",
            "dynamic_parameters": {"CUSTOM_RSI_GATE": 42.5},
        })
        self.assertEqual(read_config_value(snap, "CUSTOM_RSI_GATE", 0.0), 42.5)
        mock_with_get = MagicMock(spec=["get"])
        self.assertEqual(read_config_value(mock_with_get, "MISSING_KEY", 7.25), 7.25)
        mock_with_get.get.assert_not_called()

    def test_config_bus_quarantine_boot_persists_recovery_epoch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            bus1 = ConfigBus(data_dir=root, ignore_process_env=True)
            bus1.commit_patch({"MIN_ADX_TREND": 21.0}, expected_epoch_id=bus1.active_epoch_id, author="op", reason="seed")
            (root / "config_overrides.json").write_text("{corrupt json!!!", encoding="utf-8")

            bus2 = ConfigBus(data_dir=root, ignore_process_env=True)
            warnings = bus2.get_quarantine_warnings()
            self.assertGreaterEqual(len(warnings), 1)
            lines = [
                json.loads(line)
                for line in (root / "config_epochs.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertTrue(
                any(
                    "epoch-1-quarantine-recovery"
                    in (
                        row.get("epoch_id"),
                        row.get("config_epoch_id"),
                        (row.get("snapshot") or {}).get("config_epoch_id"),
                    )
                    for row in lines
                ),
                lines,
            )

    def test_risk_manager_uses_025_fallback_floor_when_snapshot_present(self) -> None:
        rm = RiskManager()
        snap = ConfigSnapshot.from_dict({"config_epoch_id": "ep-min-sl"})
        # With atr=0.1 and peak_price=100, fallback ETP_MIN_SL_PCT floor governs: 2.5% -> 97.5
        tsl = rm.calculate_trailing_stop(
            current_price=100.0,
            peak_price=100.0,
            atr=0.1,
            current_tsl=90.0,
            direction="LONG",
            ticker="QQQ3l_EQ",
            config_snapshot=snap,
        )
        self.assertEqual(tsl, 97.5)

    def test_orchestrator_apply_snapshot_rebuilds_universe_and_syncs_pure_tsl(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            bus = ConfigBus(data_dir=tmpdir, ignore_process_env=True)
            orch = Orchestrator(
                dry_run=True,
                enable_dashboard=False,
                t212_client=_FakeT212Client(),
                config_bus=bus,
                data_dir=tmpdir,
            )
            snap = bus.commit_patch(
                {"PURE_TSL_MODE": True, "ENABLE_5X_LEVERAGE": True, "MAX_LEVERAGE": 5},
                expected_epoch_id=bus.active_epoch_id,
                author="op",
                reason="test",
            )
            with patch.object(orch, "_rebuild_universe", wraps=orch._rebuild_universe) as spy_rebuild:
                orch._apply_snapshot_to_runtime(snap)
                spy_rebuild.assert_called_once_with(snap)
            self.assertTrue(orch.position_monitor.pure_tsl_mode)

    def test_place_broker_stop_strict_gtc_default_and_auto_restore_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            t212 = MagicMock()
            t212.cancel_order.return_value = True
            calls = []

            def _fail_new_restore_old(**kw):
                calls.append((round(kw["stop_price"], 2), kw["time_validity"]))
                if len(calls) == 1:
                    raise Trading212Error(500, "temporary failure on new stop")
                return OrderResult(
                    id="STOP_RESTORED",
                    ticker=kw["ticker"],
                    type="STOP",
                    status="NEW",
                    quantity=kw["quantity"],
                    filled_quantity=0.0,
                    fill_price=0.0,
                )

            t212.place_stop_order.side_effect = _fail_new_restore_old
            mon = PositionMonitor(
                t212_client=t212,
                notifier=Notifier(),
                risk_manager=RiskManager(),
                state_file=os.path.join(tmpdir, "state.json"),
                dry_run=False,
            )
            pos = mon.register_new_trade(_sample_card(), atr=2.0, fill_price=100.0)
            pos.broker_stop_order_id = "STOP_OLD"
            pos._last_broker_stop_price = 94.0

            res_id = mon.place_broker_stop(pos, stop_price=97.0)
            self.assertIsNone(res_id)
            self.assertEqual(calls, [(97.0, "GOOD_TILL_CANCEL"), (94.0, "GOOD_TILL_CANCEL")])
            self.assertEqual(pos.broker_stop_order_id, "STOP_RESTORED")
            self.assertIsNone(pos.unprotected_reason)

    def test_heal_stale_stop_skips_rejected_price_and_respects_halt_and_panic_latency(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            t212 = MagicMock()
            t212.get_portfolio.return_value = [
                Position(ticker="QQQ3l_EQ", quantity=10.0, average_price=100.0, current_price=100.0, ppl=0.0, max_buy=100.0, max_sell=100.0)
            ]
            t212.get_orders.return_value = []
            mon = PositionMonitor(
                t212_client=t212,
                notifier=Notifier(),
                risk_manager=RiskManager(),
                state_file=os.path.join(tmpdir, "state.json"),
                dry_run=False,
            )
            pos = mon.register_new_trade(_sample_card(), atr=2.0, fill_price=100.0)
            pos.stop_loss = 98.0
            pos.trailing_stop_loss = 98.0
            pos._last_atr_pct = 0.02

            mon._heal_stale_stop(pos, rejected_price=98.0)
            self.assertNotAlmostEqual(pos.trailing_stop_loss, 98.0, places=3)
            self.assertLess(pos.trailing_stop_loss, 100.0)

            mon.is_halted = True
            blocked = mon.re_epoch_position("QQQ3l_EQ", expected_position_epoch_id=pos.config_epoch_id)
            self.assertEqual(blocked["status"], "blocked")
            self.assertFalse(blocked["applied"])

            t212.get_portfolio.return_value = [
                Position(ticker="QQQ3l_EQ", quantity=10.0, average_price=100.0, current_price=100.0, ppl=0.0, max_buy=100.0, max_sell=100.0),
                Position(ticker="3NVDl_EQ", quantity=5.0, average_price=50.0, current_price=50.0, ppl=0.0, max_buy=100.0, max_sell=100.0),
            ]
            summary = mon.reconcile_with_broker(adopt_untracked=True)
            self.assertEqual(summary["adopted_from_broker"], [])
            self.assertNotIn("3NVDl_EQ", mon.positions)

            # Non-blocking panic_flatten_all while place_stop_order sleeps
            mon.is_halted = False
            pos.trailing_stop_loss = 94.0
            entered_stop = threading.Event()

            def _slow_stop(**kw):
                entered_stop.set()
                time.sleep(0.22)
                return OrderResult(
                    id="ORPHAN_STOP",
                    ticker=kw["ticker"],
                    type="STOP",
                    status="NEW",
                    quantity=kw["quantity"],
                    filled_quantity=0.0,
                    fill_price=0.0,
                )

            t212.place_stop_order.side_effect = _slow_stop
            t212.place_market_order.return_value = OrderResult(
                id="PANIC_SELL",
                ticker="QQQ3l_EQ",
                type="MARKET",
                status="FILLED",
                quantity=-10.0,
                filled_quantity=10.0,
                fill_price=100.0,
            )
            bg = threading.Thread(target=lambda: mon.place_broker_stop(pos, stop_price=95.0))
            bg.start()
            self.assertTrue(entered_stop.wait(timeout=1.0))

            t0 = time.monotonic()
            mon.panic_flatten_all()
            elapsed = time.monotonic() - t0
            bg.join(timeout=2.0)

            self.assertLess(elapsed, 0.12)
            t212.cancel_order.assert_any_call("ORPHAN_STOP")

    def test_pure_tsl_config_bus_overrides_explicit_mode_and_passes_ticker_to_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            bus = ConfigBus(data_dir=tmpdir, ignore_process_env=True)
            rm = RiskManager()
            t212 = MagicMock()
            mon = PositionMonitor(
                t212_client=t212,
                notifier=Notifier(),
                risk_manager=rm,
                state_file=os.path.join(tmpdir, "state.json"),
                pure_tsl_mode=False,
                config_bus=bus,
                dry_run=True,
            )
            bus.commit_patch({"PURE_TSL_MODE": True}, expected_epoch_id=bus.active_epoch_id, author="op", reason="toggle")
            pos = mon.register_new_trade(_sample_card(), atr=2.0, fill_price=100.0)
            pos.config_epoch_id = bus.active_epoch_id
            pos.entry_config_snapshot = {"config_epoch_id": bus.active_epoch_id, "PURE_TSL_MODE": True}
            self.assertTrue(mon._is_pure_tsl_for_position(pos))

            with patch.object(rm, "calculate_escape_threshold", wraps=rm.calculate_escape_threshold) as spy_esc:
                mon._calculate_escape_threshold_for_pos(pos, None)
                spy_esc.assert_called_once()
                _, kwargs = spy_esc.call_args
                self.assertEqual(kwargs.get("ticker"), "QQQ3l_EQ")


class TestServerReviewRegressions(unittest.TestCase):
    """Regressions in trading_system/dashboard/server.py."""

    def setUp(self) -> None:
        self._orig_cwd = os.getcwd()
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        os.chdir(self.root)
        (self.root / "data").mkdir(parents=True, exist_ok=True)
        self.auth = DashboardAuthManager(token_file=self.root / "data" / ".dashboard_token")
        self.token = self.auth.get_token()
        SSEBroadcaster.get_instance().close_all()
        self._patches = ExitStack()
        self._patches.enter_context(patch("trading_system.dashboard.server._DASHBOARD_SHARED_CLIENT", None))

    def tearDown(self) -> None:
        SSEBroadcaster.get_instance().close_all()
        self._patches.close()
        os.chdir(self._orig_cwd)
        self.tmp_dir.cleanup()

    def test_preview_profile_values_and_sse_origin_rejection(self) -> None:
        orch = _FakeOrchestrator(self.root / "data")
        handler_cls = make_dashboard_handler(
            self.root,
            "signals_dashboard.html",
            orchestrator=orch,
            auth_manager=self.auth,
        )
        gw = _GatewayTestClient(handler_cls)
        profile_id = next(iter(get_builtin_profiles().keys()))
        payload = json.dumps({"profile_id": profile_id}).encode("utf-8")
        status, _, body, _ = gw.request_raw(
            "POST",
            "/api/config/preview",
            headers={
                "Host": "localhost:8888",
                "Origin": "http://localhost:8888",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
                "Authorization": f"Bearer {self.token}",
            },
            body=payload,
        )
        self.assertEqual(status, 200, body)
        parsed = json.loads(body.decode("utf-8"))
        self.assertIn("valid", parsed)

        # Mismatched Origin on SSE stream must fail closed with 403
        bad_status, _, _, _ = gw.request_raw(
            "GET",
            "/api/stream",
            headers={"Host": "localhost:8888", "Origin": "https://evil.example"},
        )
        self.assertEqual(bad_status, 403)


class TestWorkbenchJSReviewRegressions(unittest.TestCase):
    """Node-harness regressions for Step 5 fixes in workbench.js."""

    def test_workbench_js_step5_regressions(self) -> None:
        node = _find_node()
        preamble = _NODE_HARNESS_SCRIPT.split("async function runAll()", 1)[0]
        script = preamble + r"""
function collectTextAndAttrs(root) {
  let out = "";
  const stack = [root];
  while (stack.length) {
    const n = stack.pop();
    if (!n) continue;
    if (typeof n.textContent === "string") out += " " + n.textContent;
    if (n.attributes) out += " " + Object.values(n.attributes).join(" ");
    if (typeof n.href === "string") out += " " + n.href;
    if (Array.isArray(n.children)) {
      for (const c of n.children) stack.push(c);
    }
  }
  return out;
}

function findNode(root, pred) {
  const stack = [root];
  while (stack.length) {
    const n = stack.pop();
    if (!n) continue;
    if (pred(n)) return n;
    if (Array.isArray(n.children)) {
      for (const c of n.children) stack.push(c);
    }
  }
  return null;
}

async function runStep5Regressions() {
  // 1. Deep-link key= parsing + parameter_recommendations
  {
    const env = createMockEnv({
      hash: "#/config?key=MIN_ADX_TREND",
      initialData: makeSampleInitialData({
        last_audit: {
          timestamp: "2026-11-11T10:00:00Z",
          regime: "CHOP",
          parameter_recommendations: [{ key: "MIN_ADX_TREND", suggested_value: 20 }],
          action_taken: "FLAGGED",
        },
      }),
    });
    const parsed = env.api.parseHashRoute();
    assert.strictEqual(parsed.tab, "config");
    assert.strictEqual(parsed.param, "MIN_ADX_TREND");
    env.api.navigateTo("audit");
    assert.ok(collectTextAndAttrs(env.doc.body).includes("MIN_ADX_TREND"));
    env.api.destroyWorkbench();
  }

  // 2. Retry Liquidation targets /retry-liquidation + commit double-submit guard + reason preservation + 15k chart + config_epoch_changed + 409 SSE rebase
  {
    const postUrls = [];
    let resolveCommit = null;
    const env = createMockEnv({
      hash: "#monitor",
      initialData: makeSampleInitialData({
        is_halted: true,
        system_halted: true,
        active_signals: [
          {
            ticker: "3LNV",
            underlying: "NVDA",
            direction: "LONG",
            entry_price: 40,
            current_price: 42,
            stop_price: 38,
            config_epoch_id: "epoch-1",
            status: "PANIC_FAILED",
            panic_failed: true,
          },
        ],
      }),
      fetchHandler: async (url, opts) => {
        if (opts && opts.method === "POST") postUrls.push(url);
        if (url === "/api/config") {
          return await new Promise((r) => {
            resolveCommit = () => r({ status: 200, body: { ok: true, config_epoch_id: "epoch-2", config: { config_epoch_id: "epoch-2", values: { MIN_ADX_TREND: 24 } } } });
          });
        }
        return { status: 200, body: { ok: true } };
      },
    });
    env.api.openAuthModal();
    env.doc.getElementById("auth-token-input").value = "tok-ok";
    env.doc.getElementById("auth-submit-btn").click();
    await env.flushMicrotasks();

    const retryBtn = findNode(env.doc.body, (n) => n.tagName === "BUTTON" && typeof n.textContent === "string" && n.textContent.includes("Retry Liquidation"));
    assert.ok(retryBtn, "Expected Retry Liquidation button on monitor");
    retryBtn.click();
    const confirmBtn = env.doc.getElementById("retry-liquidation-confirm-btn");
    assert.ok(confirmBtn);
    confirmBtn.click();
    await env.flushMicrotasks();
    assert.ok(postUrls.includes("/api/positions/3LNV/retry-liquidation"), "Expected /retry-liquidation call, got " + JSON.stringify(postUrls));
    assert.ok(!postUrls.includes("/api/panic"), "Must NOT call /api/panic");

    // 15,000 point equity chart must not throw RangeError
    const bigHist = Array.from({ length: 15000 }, (_, i) => ({ time: String(i), equity: 10000 + (i % 50) }));
    env.api.renderEquityChart(bigHist, 10025);

    // Double-submit guard & reason preservation
    env.api.navigateTo("config");
    env.api.stageParameterValue("MIN_ADX_TREND", 24);
    const reasonEl = env.doc.getElementById("commit-reason-input");
    if (reasonEl) {
      reasonEl.focus();
      reasonEl.value = "keep my reason";
      reasonEl.selectionStart = 4;
      reasonEl.selectionEnd = 10;
    }
    env.api.stageParameterValue("RISK_PER_TRADE_PCT", 1.2);
    const reasonAfter = env.doc.getElementById("commit-reason-input");
    if (reasonAfter) {
      assert.strictEqual(reasonAfter.value, "keep my reason");
    }

    const p1 = env.api.submitConfigCommit("reason");
    const p2 = env.api.submitConfigCommit("reason");
    await env.flushMicrotasks();
    assert.strictEqual(postUrls.filter((u) => u === "/api/config").length, 1, "Double submit must send only 1 POST /api/config");
    if (resolveCommit) resolveCommit();
    await Promise.all([p1, p2]);

    // config_epoch_changed SSE event applies parsed.dashboard
    env.sseInstances[0].emit("config_epoch_changed", {
      dashboard: makeSampleInitialData({ config_epoch_id: "epoch-9", config: { config_epoch_id: "epoch-9", values: { MIN_ADX_TREND: 24 } } }),
    });
    assert.strictEqual(env.api.getState().data.config_epoch_id, "epoch-9");

    // 409 rebase preserves newer SSE epoch
    env.api.getState().conflictState = { expected_epoch_id: "epoch-1", active_epoch_id: "epoch-2", server_values: { MIN_ADX_TREND: 22 } };
    env.api.applySnapshotData(makeSampleInitialData({ config_epoch_id: "epoch-3", config: { config_epoch_id: "epoch-3", values: { MIN_ADX_TREND: 25 } } }));
    assert.strictEqual(env.api.getState().data.config_epoch_id, "epoch-3");

    env.api.destroyWorkbench();
  }
}

runStep5Regressions()
  .then(() => console.log("STEP5_JS_REGRESSIONS_PASSED"))
  .catch((err) => {
    console.error(err);
    process.exit(1);
  });
"""
        env = dict(os.environ)
        env["WORKBENCH_JS_PATH"] = str(WORKBENCH_JS_PATH)
        res = subprocess.run([node, "-e", script], env=env, capture_output=True, text=True, check=False)
        self.assertEqual(res.returncode, 0, f"Node regression failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}")
        self.assertIn("STEP5_JS_REGRESSIONS_PASSED", res.stdout)


if __name__ == "__main__":
    unittest.main()
