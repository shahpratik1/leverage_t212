"""Unit tests for fail-closed dashboard authentication and HTTP security utilities."""

from __future__ import annotations

import hmac
import http.client
import io
import json
import logging
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from trading_system.dashboard.auth import (
    DEFAULT_TOKEN_FILE,
    MIN_TOKEN_LENGTH,
    AuthResult,
    DashboardAuthManager,
    extract_token_from_headers,
    is_sensitive_key,
    is_valid_host_header,
    is_valid_origin_header,
    json_safe_for_inline_script,
    main as auth_cli_main,
    print_dashboard_token,
    scrub_error_message,
)


class _FakeClock:
    def __init__(self, initial: float = 1000.0) -> None:
        self.now = initial

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestDashboardAuthTokenResolution(unittest.TestCase):
    """Tests for token precedence, fail-closed length checks, and atomic token files."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.token_path = Path(self.temp_dir.name) / "nested" / ".dashboard_token"
        self._saved_env = {
            "DASHBOARD_AUTH_TOKEN": os.environ.pop("DASHBOARD_AUTH_TOKEN", None),
            "PANIC_FLATTEN_TOKEN": os.environ.pop("PANIC_FLATTEN_TOKEN", None),
        }

    def tearDown(self) -> None:
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.temp_dir.cleanup()

    def test_precedence_dashboard_env_over_panic_and_file(self) -> None:
        dash_tok = "dash_secret_token_123456"
        panic_tok = "panic_secret_token_123456"
        with patch.dict(
            os.environ,
            {"DASHBOARD_AUTH_TOKEN": dash_tok, "PANIC_FLATTEN_TOKEN": panic_tok},
        ):
            mgr = DashboardAuthManager(token_file=self.token_path)
            self.assertEqual(mgr.token_source, "DASHBOARD_AUTH_TOKEN")
            self.assertEqual(mgr.get_token(), dash_tok)
            self.assertFalse(self.token_path.exists())

    def test_precedence_panic_env_when_dashboard_unset_or_whitespace(self) -> None:
        panic_tok = "panic_fallback_token_9876"
        with patch.dict(
            os.environ,
            {"DASHBOARD_AUTH_TOKEN": "   \t\n  ", "PANIC_FLATTEN_TOKEN": panic_tok},
        ):
            mgr = DashboardAuthManager(token_file=self.token_path)
            self.assertEqual(mgr.token_source, "PANIC_FLATTEN_TOKEN")
            self.assertEqual(mgr.get_token(), panic_tok)
            self.assertFalse(self.token_path.exists())

    def test_whitespace_only_envs_fallback_to_atomic_file(self) -> None:
        with patch.dict(
            os.environ,
            {"DASHBOARD_AUTH_TOKEN": "   ", "PANIC_FLATTEN_TOKEN": "\t\n"},
        ):
            mgr = DashboardAuthManager(token_file=self.token_path)
            self.assertEqual(mgr.token_source, "token_file")
            self.assertTrue(mgr.created_token_file)
            self.assertTrue(self.token_path.exists())
            self.assertGreaterEqual(len(mgr.get_token()), 32)
            self.assertEqual(stat.S_IMODE(self.token_path.stat().st_mode), 0o600)

    def test_rejects_short_configured_env_tokens(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DASHBOARD_AUTH_TOKEN": "short_15_chars!",
                "PANIC_FLATTEN_TOKEN": "valid_panic_token_long_enough",
            },
        ):
            with self.assertRaises(ValueError):
                DashboardAuthManager(token_file=self.token_path)

        with patch.dict(os.environ, {"PANIC_FLATTEN_TOKEN": "too_short"}):
            with self.assertRaises(ValueError):
                DashboardAuthManager(token_file=self.token_path)

        exact_16 = "a" * MIN_TOKEN_LENGTH
        with patch.dict(os.environ, {"DASHBOARD_AUTH_TOKEN": exact_16}):
            mgr = DashboardAuthManager(token_file=self.token_path)
            self.assertEqual(mgr.get_token(), exact_16)

    def test_atomic_o_creat_o_excl_0600_creation_and_reread(self) -> None:
        seen_calls = []
        real_open = os.open

        def spy_open(path, flags, mode=0o777, *args, **kwargs):
            seen_calls.append((str(path), flags, mode))
            return real_open(path, flags, mode, *args, **kwargs)

        with patch("trading_system.dashboard.auth.os.open", side_effect=spy_open):
            mgr1 = DashboardAuthManager(token_file=self.token_path)

        create_calls = [
            call for call in seen_calls
            if call[0] == str(self.token_path) and (call[1] & os.O_CREAT)
        ]
        self.assertEqual(len(create_calls), 1)
        _, flags, mode = create_calls[0]
        self.assertTrue(flags & os.O_CREAT)
        self.assertTrue(flags & os.O_EXCL)
        self.assertTrue(flags & os.O_WRONLY)
        self.assertEqual(mode, 0o600)

        # Loosen permissions to verify best-effort repair on FileExistsError re-read
        os.chmod(self.token_path, 0o644)
        mgr2 = DashboardAuthManager(token_file=self.token_path)
        self.assertEqual(mgr2.get_token(), mgr1.get_token())
        self.assertFalse(mgr2.created_token_file)
        self.assertEqual(stat.S_IMODE(self.token_path.stat().st_mode), 0o600)

    def test_concurrent_token_file_bootstrap_converges_cleanly(self) -> None:
        tokens = []
        errors = []
        barrier = threading.Barrier(6)

        def worker() -> None:
            try:
                barrier.wait(timeout=2.0)
                mgr = DashboardAuthManager(token_file=self.token_path)
                tokens.append(mgr.get_token())
            except Exception as exc:  # pragma: no cover - assertion verifies empty
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        self.assertEqual(errors, [])
        self.assertEqual(len(tokens), 6)
        self.assertEqual(len(set(tokens)), 1)

    def test_unittest_default_token_file_does_not_touch_repo_cwd(self) -> None:
        preexisting = DEFAULT_TOKEN_FILE.exists()
        mgr = DashboardAuthManager()
        self.assertNotEqual(mgr.token_file.resolve(), DEFAULT_TOKEN_FILE.resolve())
        self.assertTrue(mgr.token_file.exists())
        if not preexisting:
            self.assertFalse(DEFAULT_TOKEN_FILE.exists())

    def test_non_test_mode_uses_default_data_dashboard_token_path(self) -> None:
        orig_cwd = os.getcwd()
        try:
            os.chdir(self.temp_dir.name)
            with patch("trading_system.dashboard.auth._is_test_runner", return_value=False):
                mgr = DashboardAuthManager()
            expected = Path(self.temp_dir.name) / "data" / ".dashboard_token"
            self.assertEqual(mgr.token_file.resolve(), expected.resolve())
            self.assertTrue(expected.exists())
            self.assertEqual(stat.S_IMODE(expected.stat().st_mode), 0o600)
        finally:
            os.chdir(orig_cwd)

    def test_rejects_symlink_or_short_existing_token_file(self) -> None:
        short_file = Path(self.temp_dir.name) / "short_token"
        short_file.write_text("too_short\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            DashboardAuthManager(token_file=short_file)

        target = Path(self.temp_dir.name) / "real_token"
        target.write_text("valid_long_token_12345678\n", encoding="utf-8")
        link = Path(self.temp_dir.name) / "link_token"
        link.symlink_to(target)
        with self.assertRaises(ValueError):
            DashboardAuthManager(token_file=link)

    def test_setup_hint_and_startup_logs_never_leak_raw_token_and_cli_prints(self) -> None:
        secret_tok = "super_secret_dashboard_token_xyz"
        with patch.dict(os.environ, {"DASHBOARD_AUTH_TOKEN": secret_tok}):
            with self.assertLogs("trading_system.dashboard.auth", level="INFO") as logs:
                mgr = DashboardAuthManager(token_file=self.token_path)

            joined_logs = "\n".join(logs.output)
            self.assertNotIn(secret_tok, joined_logs)
            hint = mgr.setup_hint()
            self.assertIn(str(self.token_path), hint)
            self.assertIn("--print-token", hint)
            self.assertNotIn(secret_tok, hint)

            buf = io.StringIO()
            returned = print_dashboard_token(token_file=self.token_path, stream=buf)
            self.assertEqual(returned, secret_tok)
            self.assertEqual(buf.getvalue().strip(), secret_tok)

            stdout_buf = io.StringIO()
            with patch("sys.stdout", stdout_buf):
                rc = auth_cli_main(["--print-token", "--token-file", str(self.token_path)])
            self.assertEqual(rc, 0)
            self.assertEqual(stdout_buf.getvalue().strip(), secret_tok)


class TestDashboardAuthVerificationAndThrottling(unittest.TestCase):
    """Tests for header parsing, constant-time compare, and per-IP sliding-window limits."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.token_path = Path(self.temp_dir.name) / ".dashboard_token"
        self.token = "valid_control_plane_token_123456"
        self.clock = _FakeClock(100.0)
        self._env_patch = patch.dict(os.environ, {"DASHBOARD_AUTH_TOKEN": self.token})
        self._env_patch.start()
        self.mgr = DashboardAuthManager(
            token_file=self.token_path,
            max_failures=5,
            window_seconds=60.0,
            max_tracked_ips=4,
            clock=self.clock,
        )

    def tearDown(self) -> None:
        self._env_patch.stop()
        self.temp_dir.cleanup()

    def test_extract_token_from_headers_variants(self) -> None:
        self.assertEqual(
            extract_token_from_headers({"Authorization": f"Bearer {self.token}"}),
            self.token,
        )
        self.assertEqual(
            extract_token_from_headers({"authorization": f"bearer   {self.token}  "}),
            self.token,
        )
        self.assertEqual(
            extract_token_from_headers({"X-Dashboard-Token": f" {self.token} "}),
            self.token,
        )
        self.assertEqual(
            extract_token_from_headers({"x-dashboard-token": self.token}),
            self.token,
        )

        msg = http.client.HTTPMessage()
        msg["Authorization"] = f"Bearer {self.token}"
        self.assertEqual(self.mgr.extract_token_from_headers(msg), self.token)

        self.assertIsNone(extract_token_from_headers(None))
        self.assertIsNone(extract_token_from_headers({}))
        self.assertIsNone(extract_token_from_headers({"Authorization": "Basic dXNlcjpwYXNz"}))
        self.assertIsNone(extract_token_from_headers({"Authorization": "Bearer   "}))

    def test_verify_uses_constant_time_compare_digest_and_handles_non_ascii(self) -> None:
        with patch(
            "trading_system.dashboard.auth.hmac.compare_digest",
            wraps=hmac.compare_digest,
        ) as spy_compare:
            ok_res = self.mgr.verify_request_auth(
                {"Authorization": f"Bearer {self.token}"},
                client_ip="127.0.0.1",
            )
            self.assertEqual(ok_res, AuthResult(ok=True, status_code=200, error="", retry_after=None))
            self.assertTrue(spy_compare.called)

            non_ascii_res = self.mgr.verify_request_auth(
                {"X-Dashboard-Token": "café_token_invalid"},
                client_ip="127.0.0.1",
            )
            self.assertFalse(non_ascii_res.ok)
            self.assertEqual(non_ascii_res.status_code, 401)

    def test_per_ip_throttling_and_window_recovery_and_no_token_leak_in_logs(self) -> None:
        bad_token = "wrong_candidate_secret_999"
        with self.assertLogs("trading_system.dashboard.auth", level="WARNING") as logs:
            for idx in range(5):
                res = self.mgr.verify_request_auth(
                    {"X-Dashboard-Token": bad_token},
                    client_ip="192.168.1.50",
                )
                self.assertFalse(res.ok, f"Attempt {idx + 1} should fail")
                self.assertEqual(res.status_code, 401)
                self.assertIsNone(res.retry_after)
                self.clock.advance(1.0)

            # 6th attempt within 60s window is throttled even with valid token
            throttled = self.mgr.verify_request_auth(
                {"Authorization": f"Bearer {self.token}"},
                client_ip="192.168.1.50",
            )
            self.assertFalse(throttled.ok)
            self.assertEqual(throttled.status_code, 429)
            self.assertIsNotNone(throttled.retry_after)
            self.assertGreaterEqual(throttled.retry_after, 1)
            self.assertLessEqual(throttled.retry_after, 60)

        joined_logs = "\n".join(logs.output)
        self.assertNotIn(self.token, joined_logs)
        self.assertNotIn(bad_token, joined_logs)

        # Unrelated client IP remains unaffected
        other_ip = self.mgr.verify_request_auth(
            {"Authorization": f"Bearer {self.token}"},
            client_ip="192.168.1.99",
        )
        self.assertTrue(other_ip.ok)
        self.assertEqual(other_ip.status_code, 200)

        # Advance past first failure expiration -> window drops below 5 -> valid auth succeeds
        self.clock.advance(56.0)
        recovered = self.mgr.verify_request_auth(
            {"Authorization": f"Bearer {self.token}"},
            client_ip="192.168.1.50",
        )
        self.assertTrue(recovered.ok)
        self.assertEqual(recovered.status_code, 200)

    def test_valid_auth_clears_prior_failure_counter(self) -> None:
        for _ in range(4):
            res = self.mgr.verify_request_auth({"X-Dashboard-Token": "wrong"}, client_ip="10.0.0.2")
            self.assertEqual(res.status_code, 401)

        ok = self.mgr.verify_request_auth({"X-Dashboard-Token": self.token}, client_ip="10.0.0.2")
        self.assertTrue(ok.ok)

        # Counter must have cleared so another 4 failures remain 401 instead of tripping 429
        for _ in range(4):
            res = self.mgr.verify_request_auth({"X-Dashboard-Token": "wrong"}, client_ip="10.0.0.2")
            self.assertEqual(res.status_code, 401)

    def test_tracked_ips_map_is_bounded(self) -> None:
        for i in range(25):
            self.mgr.verify_request_auth({}, client_ip=f"10.0.0.{i + 1}")
        self.assertLessEqual(len(self.mgr._failures), 4)


class TestHTTPSecurityValidators(unittest.TestCase):
    """Tests for Host, Origin, inline JSON XSS escaping, and error/key scrubbing."""

    def test_valid_host_headers(self) -> None:
        valid_hosts = [
            "localhost",
            "localhost:8888",
            "LOCALHOST:8080",
            "127.0.0.1",
            "127.0.0.1:8080",
            "127.0.1.1:9000",
            "[::1]",
            "[::1]:8080",
            "::1",
            "10.0.0.1",
            "10.42.0.15:8080",
            "172.16.0.1",
            "172.31.255.254:8888",
            "192.168.1.1",
            "192.168.178.42:8080",
            "trading212-pi",
            "trading212-pi:8080",
            "trading212-pi.local",
            "trading212-pi.local:8080",
            "trading212-pi.fritz.box",
            "sub.trading212-pi.fritz.box:8888",
        ]
        for host in valid_hosts:
            with self.subTest(host=host):
                self.assertTrue(is_valid_host_header(host))

    def test_invalid_and_dns_rebinding_host_headers(self) -> None:
        invalid_hosts = [
            None,
            "",
            "   ",
            "evil.com",
            "evil.com:8888",
            "localhost.evil.com",
            "127.0.0.1.evil.com",
            "pi.local.evil.com",
            "172.15.255.254",
            "172.32.0.1",
            "192.169.1.1",
            "8.8.8.8",
            "169.254.169.254",
            "0.0.0.0:8888",
            ".local",
            "fritz.box",
            "-bad.local",
            "localhost:0",
            "localhost:080",
            "localhost:65536",
            "localhost:abc",
            "localhost:",
            "localhost@evil.com",
            "localhost/admin",
            "localhost, evil.com",
            "localhost\r\nX-Injected: 1",
            "[fe80::1]:8080",
        ]
        for host in invalid_hosts:
            with self.subTest(host=host):
                self.assertFalse(is_valid_host_header(host))

    def test_hostname_and_env_allowed_hosts(self) -> None:
        with patch("trading_system.dashboard.auth.socket.gethostname", return_value="pi-desk-box"):
            self.assertTrue(is_valid_host_header("pi-desk-box"))
            self.assertTrue(is_valid_host_header("pi-desk-box:8080"))
            self.assertFalse(is_valid_host_header("evil.com"))

        with patch.dict(
            os.environ,
            {"DASHBOARD_ALLOWED_HOSTS": "custom-pi.lan, 100.64.0.10, *, 0.0.0.0"},
        ):
            self.assertTrue(is_valid_host_header("custom-pi.lan"))
            self.assertTrue(is_valid_host_header("custom-pi.lan:8080"))
            self.assertTrue(is_valid_host_header("100.64.0.10:8080"))
            self.assertFalse(is_valid_host_header("evil.com"))
            self.assertFalse(is_valid_host_header("custom-pi.lan.evil.com"))
            self.assertFalse(is_valid_host_header("0.0.0.0:8080"))

        self.assertTrue(is_valid_host_header("abc-123.trycloudflare.com"))
        self.assertTrue(is_valid_host_header("abc-123.trycloudflare.com:443"))
        self.assertFalse(is_valid_host_header("trycloudflare.com"))

        self.assertTrue(
            is_valid_origin_header("http://trading212-pi:8080", "trading212-pi:8080")
        )
        self.assertFalse(
            is_valid_origin_header("http://evil.com:8080", "trading212-pi:8080")
        )

    def test_origin_validation(self) -> None:
        # Absent Origin allowed for CLI/curl calls
        self.assertTrue(is_valid_origin_header(None, "localhost:8888"))

        # Same-origin matches
        self.assertTrue(is_valid_origin_header("http://localhost:8888", "localhost:8888"))
        self.assertTrue(is_valid_origin_header("https://localhost:8888", "localhost:8888"))
        self.assertTrue(is_valid_origin_header("http://LOCALHOST:8888", "localhost:8888"))
        self.assertTrue(is_valid_origin_header("http://127.0.0.1:8888", "127.0.0.1:8888"))
        self.assertTrue(is_valid_origin_header("http://[::1]:8888", "[::1]:8888"))
        self.assertTrue(is_valid_origin_header("http://[::1]", "::1"))
        self.assertTrue(is_valid_origin_header("http://trading212-pi.local:8080", "trading212-pi.local:8080"))
        self.assertTrue(is_valid_origin_header("http://trading212-pi.fritz.box:8080", "trading212-pi.fritz.box:8080"))
        self.assertTrue(is_valid_origin_header("http://localhost", "localhost"))
        self.assertTrue(is_valid_origin_header("http://localhost:80", "localhost"))

        # Reject "null", external, port mismatch, host mismatch, or malformed Origins
        rejected = [
            ("null", "localhost:8888"),
            ("NULL", "localhost:8888"),
            ("", "localhost:8888"),
            ("http://evil.com", "localhost:8888"),
            ("http://evil.com:8888", "localhost:8888"),
            ("http://evil.com", "evil.com"),
            ("http://localhost:9999", "localhost:8888"),
            ("http://localhost", "localhost:8888"),
            ("http://localhost:8888", "localhost"),
            ("http://127.0.0.1:8888", "localhost:8888"),
            ("http://localhost:8888/path", "localhost:8888"),
            ("http://user:pass@localhost:8888", "localhost:8888"),
            ("file://localhost", "localhost"),
            ("http://localhost:8888", None),
        ]
        for origin, host in rejected:
            with self.subTest(origin=origin, host=host):
                self.assertFalse(is_valid_origin_header(origin, host))

    def test_json_safe_for_inline_script_escapes_breakout_sequences(self) -> None:
        payload = {
            "xss": "</script><script>alert('xss' & \"1\")</script>",
            "unicode_sep": "line1\u2028line2\u2029line3",
        }
        encoded = json_safe_for_inline_script(payload)
        self.assertNotIn("<", encoded)
        self.assertNotIn(">", encoded)
        self.assertNotIn("&", encoded)
        self.assertNotIn("\u2028", encoded)
        self.assertNotIn("\u2029", encoded)
        self.assertIn("\\u003c/script\\u003e", encoded)
        self.assertIn("\\u0026", encoded)
        self.assertIn("\\u2028", encoded)
        self.assertIn("\\u2029", encoded)
        self.assertEqual(json.loads(encoded), payload)

    def test_scrub_error_message_and_sensitive_key_detection(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DASHBOARD_AUTH_TOKEN": "raw_env_secret_token_alpha",
                "OPENAI_API_KEY": "sk-proj-testsecretkey123456",
            },
        ):
            raw_msg = (
                "Traceback (most recent call last):\n"
                '  File "/Users/pi/modest-bose/trading_system/dashboard/server.py", line 42, in do_POST\n'
                "ValueError: Authorization: Bearer header_tok_12345 failed with "
                "raw_env_secret_token_alpha and sk-proj-testsecretkey123456 reading "
                "/home/pi/bot/data/.dashboard_token or data/.dashboard_token "
                "and https://user:pw123@api.example.com/v1?api_key=abc999"
            )
            scrubbed = scrub_error_message(raw_msg)

        for secret in (
            "raw_env_secret_token_alpha",
            "sk-proj-testsecretkey123456",
            "header_tok_12345",
            "pw123",
            "abc999",
            "/Users/pi",
            "/home/pi",
            ".dashboard_token",
            "server.py",
        ):
            self.assertNotIn(secret, scrubbed)

        # Benign domain errors and API route names must remain readable
        benign = scrub_error_message("Invalid MAX_RISK_PER_TRADE on /api/config: 401/403")
        self.assertEqual(benign, "Invalid MAX_RISK_PER_TRADE on /api/config: 401/403")
        self.assertEqual(scrub_error_message("Invalid token value"), "Invalid token value")

        # Instance scrubbing also redacts generated file token + Windows/home paths
        with tempfile.TemporaryDirectory() as tmp_dir, patch.dict(
            os.environ, {"DASHBOARD_AUTH_TOKEN": "", "PANIC_FLATTEN_TOKEN": ""}
        ):
            tmp_path = Path(tmp_dir) / "sub" / ".dashboard_token"
            mgr = DashboardAuthManager(token_file=tmp_path)
            leaked = RuntimeError(
                f"Bad {mgr.get_token()} at ~/bot/.env and C:\\Users\\op\\secret.json"
            )
            mgr_scrubbed = mgr.scrub_error_message(leaked)
            self.assertNotIn(mgr.get_token(), mgr_scrubbed)
            self.assertNotIn("~/bot/.env", mgr_scrubbed)
            self.assertNotIn("C:\\Users\\op", mgr_scrubbed)

        for key in (
            "TRADING212_API_KEY",
            "TRADING212_API_SECRET",
            "OPENAI_API_KEY",
            "DASHBOARD_AUTH_TOKEN",
            "PANIC_FLATTEN_TOKEN",
            "DISCORD_WEBHOOK_URL",
            "TELEGRAM_BOT_TOKEN",
            "CANDLE_DB_PATH",
            "ACTIVE_TRADES_PATH",
            "TRADES_LOG_PATH",
            "USE_LIVE",
            "custom_api_key",
            "api_key",
            "API_KEY",
            "secret_key",
            "my_secret",
            "token_file",
        ):
            with self.subTest(sensitive_key=key):
                self.assertTrue(is_sensitive_key(key))

        for key in (
            "key",
            "MIN_ADX_TREND",
            "MAX_RISK_PER_TRADE",
            "ATR_SL_MULTIPLIER",
            "PURE_TSL_MODE",
            "ETP_MIN_SL_PCT",
            "TSL_DECAY_DAY1_MULT",
            "MAX_BID_ASK_SPREAD_PCT",
            "pending_restart_keys",
        ):
            with self.subTest(tunable_key=key):
                self.assertFalse(is_sensitive_key(key))


if __name__ == "__main__":
    unittest.main()
