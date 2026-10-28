"""Unit tests for embedded local signals dashboard server in Orchestrator."""

import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from trading_system.config import settings
from trading_system.dashboard.server import generate_html_dashboard, start_local_server_thread
from trading_system.notifications.notifier import Notifier
from trading_system.orchestrator import Orchestrator


class TestEmbeddedDashboard(unittest.TestCase):
    """Test suite for embedded dashboard web server functionality."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.notifier = Notifier(enable_desktop=False)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_start_local_server_thread_lifecycle(self):
        """Verify start_local_server_thread creates daemon thread, binds port, and sets URL."""
        test_html = Path(self.temp_dir.name) / "test_dashboard.html"
        test_html.write_text("<html><body><h1>Live Dashboard Test</h1></body></html>", encoding="utf-8")

        thread, httpd, url = start_local_server_thread(
            html_file=str(test_html),
            port=8980,
            open_browser=False,
        )
        self.assertIsNotNone(thread)
        self.assertTrue(thread.is_alive())
        self.assertTrue(thread.daemon)
        self.assertIsNotNone(httpd)
        self.assertEqual(url, "http://localhost:8980/test_dashboard.html")

        # Socket must be active and bound
        self.assertGreaterEqual(httpd.fileno(), 0)

        # Clean shutdown
        httpd.shutdown()
        httpd.server_close()
        self.assertEqual(httpd.fileno(), -1)

    def test_port_fallback_when_port_busy(self):
        """Verify server attempts next port if primary port is already bound."""
        # Occupy port 8985 with a raw socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", 8985))
        sock.listen(1)

        test_html = Path(self.temp_dir.name) / "signals_dashboard.html"
        test_html.write_text("<html><body>Fallback Test</body></html>", encoding="utf-8")

        try:
            thread, httpd, url = start_local_server_thread(
                html_file=str(test_html),
                port=8985,
                open_browser=False,
            )
            self.assertIsNotNone(httpd)
            # Port should have automatically bumped to 8986
            self.assertEqual(url, "http://localhost:8986/signals_dashboard.html")
            self.assertGreaterEqual(httpd.fileno(), 0)

            httpd.shutdown()
            httpd.server_close()
        finally:
            sock.close()

    def test_orchestrator_embedded_dashboard_lifecycle(self):
        """Verify Orchestrator starts and stops dashboard server cleanly."""
        orch = Orchestrator(
            dry_run=True,
            notifier=self.notifier,
            enable_dashboard=True,
            dashboard_port=8988,
            open_browser=False,
        )
        self.assertTrue(orch.enable_dashboard)
        self.assertIsNotNone(orch.dashboard_server)
        self.assertEqual(orch.dashboard_url, "http://localhost:8988/signals_dashboard.html")
        self.assertGreaterEqual(orch.dashboard_server.fileno(), 0)

        # Verify update method
        orch._update_dashboard()

        # Clean shutdown releases socket
        orch.shutdown()
        self.assertEqual(orch.dashboard_server.fileno(), -1)

    def test_orchestrator_dashboard_disabled(self):
        """Verify Orchestrator respects enable_dashboard=False."""
        orch = Orchestrator(
            dry_run=True,
            notifier=self.notifier,
            enable_dashboard=False,
        )
        self.assertFalse(orch.enable_dashboard)
        self.assertIsNone(orch.dashboard_server)
        self.assertEqual(orch.dashboard_url, "")
        orch.shutdown()

    def test_custom_handler_path_rewrite(self):
        """Verify CustomHandler translates root path '/' to dashboard html file."""
        from trading_system.dashboard.server import generate_html_dashboard
        import http.server

        test_html = Path(self.temp_dir.name) / "my_signals.html"
        test_html.write_text("<html><body>My Signals</body></html>", encoding="utf-8")

        web_dir = test_html.resolve().parent
        html_name = test_html.name

        class DummyHandler(http.server.SimpleHTTPRequestHandler):
            def __init__(self):
                self.directory = str(web_dir)
                self.path = "/"

            def do_GET(self):
                if self.path in ("/", ""):
                    self.path = f"/{html_name}"

        h = DummyHandler()
        h.do_GET()
        self.assertEqual(h.path, "/my_signals.html")

    def test_dashboard_handler_api_data_endpoint(self):
        """Verify CustomDashboardHandler returns live JSON data on /api/data."""
        from trading_system.dashboard.server import make_dashboard_handler, generate_html_dashboard
        from io import BytesIO
        import json

        test_html = Path(self.temp_dir.name) / "signals_dashboard.html"
        generate_html_dashboard(str(test_html))

        web_dir = test_html.resolve().parent
        html_name = test_html.name
        HandlerClass = make_dashboard_handler(web_dir, html_name)

        # Mock request to /api/data
        class MockSocket:
            def __init__(self):
                self.data = BytesIO()
            def makefile(self, *args, **kwargs):
                return BytesIO(b"GET /api/data HTTP/1.1\r\nHost: localhost\r\n\r\n")
            def sendall(self, data):
                self.data.write(data)

        sock = MockSocket()
        handler = HandlerClass(sock, ("127.0.0.1", 12345), None)
        response_bytes = sock.data.getvalue()
        response_text = response_bytes.decode("utf-8")

        self.assertIn("HTTP/1.0 200 OK", response_text)
        self.assertIn("Content-Type: application/json", response_text)
        self.assertIn("Cache-Control: no-cache, no-store, must-revalidate", response_text)
        
        # Verify JSON body parses and has required keys
        body_part = response_text.split("\r\n\r\n", 1)[1]
        payload = json.loads(body_part)
        self.assertIn("active_signals", payload)
        self.assertIn("broker_status", payload)
        self.assertIn("pairs_watchlist", payload)
        self.assertIn("equity_history", payload)

    def test_fetch_live_broker_notifications(self):
        """Verify historical orders from Trading 212 format into live notification stream."""
        from trading_system.dashboard.server import fetch_live_broker_notifications
        from trading_system.data.leveraged_universe import LeveragedUniverse

        mock_client = MagicMock()
        mock_client.get_historical_orders.return_value = {
            "items": [
                {
                    "order": {
                        "id": 57100172148,
                        "type": "MARKET",
                        "ticker": "IONQl1_EQ",
                        "quantity": 1.0,
                        "filledQuantity": 1.0,
                        "status": "FILLED",
                        "currency": "EUR",
                        "side": "BUY",
                        "createdAt": "2026-10-26T10:50:22.000Z",
                    },
                    "fill": {
                        "id": 57100172150,
                        "quantity": 1.0,
                        "price": 1.678,
                        "type": "TRADE",
                        "filledAt": "2026-10-26T10:50:23.000Z",
                        "walletImpact": {"currency": "EUR", "realisedProfitLoss": None},
                    },
                },
                {
                    "order": {
                        "id": 57100160996,
                        "type": "STOP",
                        "ticker": "3LFEl_EQ",
                        "quantity": -1.0,
                        "filledQuantity": -1.0,
                        "stopPrice": 25.72,
                        "status": "FILLED",
                        "currency": "EUR",
                        "side": "SELL",
                        "createdAt": "2026-10-26T08:05:49.000Z",
                    },
                    "fill": {
                        "id": 57100172056,
                        "quantity": -1.0,
                        "price": 25.58,
                        "type": "TRADE",
                        "filledAt": "2026-10-26T10:42:36.000Z",
                        "walletImpact": {"currency": "EUR", "realisedProfitLoss": -2.0},
                    },
                },
            ]
        }
        universe = LeveragedUniverse()
        events = fetch_live_broker_notifications(mock_client, universe, limit=10)
        self.assertEqual(len(events), 2)

        # Check Buy event
        buy_ev = events[0]
        self.assertEqual(buy_ev["event_type"], "BUY")
        self.assertEqual(buy_ev["ticker"], "IONQl1_EQ")
        self.assertEqual(buy_ev["price"], 1.678)
        self.assertEqual(buy_ev["quantity"], 1.0)
        self.assertIsNone(buy_ev["pnl"])
        self.assertIsNone(buy_ev["pnl_pct"])
        self.assertIn("Live T212", buy_ev["rationale"])

        # Check Stop Sell event with realized loss
        sl_ev = events[1]
        self.assertEqual(sl_ev["event_type"], "SL")
        self.assertEqual(sl_ev["ticker"], "3LFEl_EQ")
        self.assertEqual(sl_ev["price"], 25.58)
        self.assertEqual(sl_ev["quantity"], 1.0)
        self.assertEqual(sl_ev["pnl"], -2.0)
        self.assertEqual(sl_ev["pnl_pct"], -7.25)

    def test_load_active_signals_ionq_direction_and_spot_pnl(self):
        """Verify load_active_signals corrects IONQl1_EQ direction to LONG and calculates spot P&L accurately."""
        import json
        from trading_system.dashboard.server import load_active_signals

        signals_file = Path(self.temp_dir.name) / "active_trades.json"
        # Simulate an erroneous state where IONQl1_EQ was recorded as SHORT
        state_data = {
            "IONQl1_EQ": {
                "ticker": "IONQl1_EQ",
                "underlying": "IONQ",
                "direction": "SHORT",  # Erroneous label in file
                "entry_price": 1.68,
                "current_price": 1.66,  # Price dropped from 1.68 to 1.66 (-1.19%)
                "original_quantity": 10.0,
                "remaining_quantity": 10.0,
                "stop_loss": 1.39,
                "tp1": 1.97,
            }
        }
        signals_file.write_text(json.dumps(state_data), encoding="utf-8")

        signals = load_active_signals(str(signals_file), live_prices={"IONQl1_EQ": 1.66})
        self.assertEqual(len(signals), 1)
        sig = signals[0]
        # Must authoritatively correct direction to LONG
        self.assertEqual(sig["direction"], "LONG")
        # Must reflect true spot loss: (1.66 - 1.68) * 10 = -0.20 (-1.19%)
        self.assertEqual(sig["pnl_amount"], -0.20)
        self.assertAlmostEqual(sig["pnl_pct"], -1.19, places=2)
        # Currency must be USD
        self.assertEqual(sig["currency_code"], "USD")
        self.assertEqual(sig["currency_prefix"], "$")

    def test_orchestrator_rejects_short_without_inverse_etp(self):
        """Verify Orchestrator rejects SHORT trade for IONQ since no inverse ETP exists."""
        from trading_system.screener.laya_screener import ScreenerResult

        orch = Orchestrator(dry_run=True, notifier=self.notifier, enable_dashboard=False)
        candidate = ScreenerResult(
            ticker="IONQ",
            bias="bearish",
            bias_confidence=0.85,
            setup_score="favorable",
            setup_confidence=0.80,
            catalyst_risk=0.10,
            overall_confidence=0.82,
            metrics={"close": 1.68, "atr": 0.05, "adx": 25.0},
        )
        card = orch.deep_analyse(candidate)
        # Must reject trade and return None
        self.assertIsNone(card)
        orch.shutdown()

    def test_equity_curve_and_sparkline_rendered_in_dashboard(self):
        """Verify generated dashboard HTML contains the Equity Curve sparkline card and renderEquityChart logic."""
        test_html = Path(self.temp_dir.name) / "test_equity_dashboard.html"
        generated_path = generate_html_dashboard(str(test_html))
        self.assertTrue(Path(generated_path).exists())

        html_text = Path(generated_path).read_text(encoding="utf-8")
        self.assertIn("equity-chart-svg", html_text)
        self.assertIn("equity-val-current", html_text)
        self.assertIn("renderEquityChart", html_text)
        self.assertIn("equity_history", html_text)


if __name__ == "__main__":
    unittest.main()
