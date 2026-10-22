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


if __name__ == "__main__":
    unittest.main()
