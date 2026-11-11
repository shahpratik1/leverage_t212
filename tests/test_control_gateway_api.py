"""Integration and security tests for Unit U5 Control Gateway API and Unit U6 HTML bundling."""

from __future__ import annotations

from contextlib import ExitStack
import http.client
import io
import json
import os
from pathlib import Path
import queue
import re
import socket
import sys
import tempfile
import threading
import time
from typing import Any, Dict, List, Mapping, Optional, Tuple
import unittest
from unittest.mock import patch

from trading_system.config_bus import (
    ConfigBus,
    ConfigConflictError,
    ConfigValidationError,
)
from trading_system.dashboard.auth import DashboardAuthManager
from trading_system.dashboard.server import (
    MAX_REQUEST_BODY_BYTES,
    MAX_SSE_SUBSCRIBERS,
    SSEBroadcaster,
    _resolve_dashboard_bind_host,
    build_dashboard_data,
    generate_html_dashboard,
    main as server_main,
    make_dashboard_handler,
    start_local_server_thread,
)
from trading_system.execution.position_monitor import (
    PositionEpochConflictError,
    ResumeBlockedError,
)


class _SpySocket:
    """Wrap a real socket to track whether request body bytes were read from rfile."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self.recv_chunks: List[bytes] = []

    def makefile(self, mode: str = "r", buffering: int = -1, **kwargs: Any) -> Any:
        raw = self._sock.makefile(mode, buffering, **kwargs)
        if "r" not in mode:
            return raw
        outer = self

        class _SpyReader:
            def __init__(self, inner: Any) -> None:
                self._inner = inner
                self.headers_done = False
                self.body_bytes_read = 0

            def readline(self, limit: int = -1) -> bytes:
                line = self._inner.readline(limit) if limit >= 0 else self._inner.readline()
                if self.headers_done:
                    self.body_bytes_read += len(line)
                elif line in (b"\r\n", b"\n", b""):
                    self.headers_done = True
                return line

            def read(self, size: int = -1) -> bytes:
                data = self._inner.read(size)
                if self.headers_done:
                    self.body_bytes_read += len(data)
                return data

            def __getattr__(self, name: str) -> Any:
                return getattr(self._inner, name)

        spy_reader = _SpyReader(raw)
        outer._spy_reader = spy_reader
        return spy_reader

    def __getattr__(self, name: str) -> Any:
        return getattr(self._sock, name)


class _FakeOrchestrator:
    """Stateful Orchestrator controller stub matching the U5 contract."""

    def __init__(self, data_dir: Path) -> None:
        self.config_bus = ConfigBus(data_dir=data_dir, ignore_process_env=True)
        self.halted = False
        self.halt_reason = ""
        self.raise_on_next_call: Optional[Exception] = None
        self.positions: Dict[str, Dict[str, Any]] = {
            "QQQ3m_EQ": {
                "ticker": "QQQ3m_EQ",
                "underlying": "QQQ",
                "name": "Nasdaq 100",
                "direction": "LONG",
                "entry_price": 100.0,
                "current_price": 102.0,
                "stop_loss": 95.0,
                "trailing_stop_loss": 97.0,
                "broker_stop_order_id": "STOP_1",
                "config_epoch_id": "epoch-0",
                "panic_failed": False,
                "unprotected_reason": "",
            }
        }

    def _maybe_raise(self) -> None:
        if self.raise_on_next_call is not None:
            exc = self.raise_on_next_call
            self.raise_on_next_call = None
            raise exc

    def get_dashboard_snapshot(self) -> Dict[str, Any]:
        snap = self.config_bus.active_snapshot()
        return {
            "controller_attached": True,
            "config_epoch_id": self.config_bus.active_epoch_id,
            "config": snap.to_dict(include_specs=True),
            "schema": self.config_bus.get_schema(),
            "epoch_history": list(reversed(self.config_bus.epoch_history(limit=50))),
            "pending_restart_keys": list(snap.pending_restart_keys),
            "config_quarantine_warnings": list(self.config_bus.get_quarantine_warnings()),
            "active_signals": list(self.positions.values()),
            "active_signals_count": len(self.positions),
            "is_halted": self.halted,
            "system_halted": self.halted,
            "halt_reason": self.halt_reason,
            "base_equity": 12000.0,
            "current_equity": 12150.0,
        }

    def preview_config_patch(
        self, patch: Mapping[str, Any], expected_epoch_id: Optional[str] = None
    ) -> Dict[str, Any]:
        self._maybe_raise()
        res = self.config_bus.preview_patch(patch, expected_epoch_id=expected_epoch_id)
        if res.get("conflict"):
            raise ConfigConflictError(
                expected_epoch_id,
                self.config_bus.active_epoch_id,
                self.config_bus.active_snapshot(),
            )
        return res

    def commit_config_patch(
        self,
        patch: Mapping[str, Any],
        expected_epoch_id: str,
        author: str = "operator",
        reason: str = "",
    ) -> Dict[str, Any]:
        self._maybe_raise()
        snap = self.config_bus.commit_patch(
            patch, expected_epoch_id=expected_epoch_id, author=author, reason=reason
        )
        return {
            "config_epoch_id": snap.config_epoch_id,
            "config": snap.to_dict(include_specs=True),
        }

    def apply_config_profile(
        self,
        profile_id: str,
        expected_epoch_id: str,
        author: str = "operator",
        reason: str = "",
    ) -> Dict[str, Any]:
        self._maybe_raise()
        snap = self.config_bus.apply_profile(
            profile_id, expected_epoch_id=expected_epoch_id, author=author, reason=reason
        )
        return {
            "config_epoch_id": snap.config_epoch_id,
            "config": snap.to_dict(include_specs=True),
        }

    def add_custom_parameter(
        self, spec_dict: Mapping[str, Any], expected_epoch_id: Optional[str] = None
    ) -> Dict[str, Any]:
        self._maybe_raise()
        snap = self.config_bus.add_dynamic_parameter(
            spec_dict, expected_epoch_id=expected_epoch_id
        )
        return {
            "config_epoch_id": snap.config_epoch_id,
            "config": snap.to_dict(include_specs=True),
        }

    def delete_custom_parameter(
        self, key: str, expected_epoch_id: Optional[str] = None
    ) -> Dict[str, Any]:
        self._maybe_raise()
        snap = self.config_bus.delete_dynamic_parameter(
            key, expected_epoch_id=expected_epoch_id
        )
        return {
            "config_epoch_id": snap.config_epoch_id,
            "config": snap.to_dict(include_specs=True),
        }

    def preview_position_re_epoch(
        self,
        ticker: str,
        target_epoch_id: Optional[str] = None,
        confirm_pure_tsl_mode_change: bool = False,
    ) -> Dict[str, Any]:
        self._maybe_raise()
        if ticker not in self.positions:
            raise KeyError(ticker)
        pos = self.positions[ticker]
        return {
            "ticker": ticker,
            "from_epoch_id": pos["config_epoch_id"],
            "target_epoch_id": target_epoch_id or self.config_bus.active_epoch_id,
            "action": "PREVIEW",
            "pure_tsl_mode_changed": not confirm_pure_tsl_mode_change,
            "old_stop_loss": pos["stop_loss"],
            "new_stop_loss": 96.2,
        }

    def re_epoch_position(
        self,
        ticker: str,
        expected_position_epoch_id: Optional[str],
        target_epoch_id: Optional[str] = None,
        confirm_pure_tsl_mode_change: bool = False,
    ) -> Dict[str, Any]:
        self._maybe_raise()
        if ticker not in self.positions:
            raise KeyError(ticker)
        pos = self.positions[ticker]
        if expected_position_epoch_id and pos["config_epoch_id"] != expected_position_epoch_id:
            raise PositionEpochConflictError(
                "Position epoch mismatch",
                ticker=ticker,
                expected_position_epoch_id=expected_position_epoch_id,
                actual_position_epoch_id=pos["config_epoch_id"],
            )
        new_epoch = target_epoch_id or self.config_bus.active_epoch_id
        pos["config_epoch_id"] = new_epoch
        pos["stop_loss"] = 96.2
        return {"ticker": ticker, "config_epoch_id": new_epoch}

    def preview_re_epoch_all(
        self,
        target_epoch_id: Optional[str] = None,
        confirm_pure_tsl_mode_change: bool = False,
    ) -> Dict[str, Any]:
        self._maybe_raise()
        target = target_epoch_id or self.config_bus.active_epoch_id
        items = [
            {"ticker": tk, "from_epoch_id": p["config_epoch_id"], "target_epoch_id": target}
            for tk, p in self.positions.items()
            if p["config_epoch_id"] != target
        ]
        return {"target_epoch_id": target, "items": items}

    def re_epoch_all_positions(
        self,
        expected_epoch_id: Optional[str] = None,
        target_epoch_id: Optional[str] = None,
        confirm_pure_tsl_mode_change: bool = False,
    ) -> Dict[str, Any]:
        self._maybe_raise()
        if expected_epoch_id and expected_epoch_id != self.config_bus.active_epoch_id:
            raise ConfigConflictError(
                expected_epoch_id,
                self.config_bus.active_epoch_id,
                self.config_bus.active_snapshot(),
            )
        target = target_epoch_id or self.config_bus.active_epoch_id
        for pos in self.positions.values():
            pos["config_epoch_id"] = target
        return {"updated": list(self.positions.keys()), "target_epoch_id": target}

    def trigger_panic_flatten(self, reason: str = "") -> Dict[str, Any]:
        self._maybe_raise()
        self.halted = True
        self.halt_reason = reason
        self.positions["QQQ3m_EQ"]["panic_failed"] = True
        self.positions["QQQ3m_EQ"]["unprotected_reason"] = "Broker order pending retry"
        return {"halted": True, "reason": reason, "failed_tickers": ["QQQ3m_EQ"]}

    def resume_trading(self, confirm_unprotected: bool = False) -> Dict[str, Any]:
        self._maybe_raise()
        unprotected = [
            {"ticker": tk, "reason": p["unprotected_reason"]}
            for tk, p in self.positions.items()
            if p.get("panic_failed")
        ]
        if unprotected and not confirm_unprotected:
            raise ResumeBlockedError(
                "Resume blocked while unprotected panic positions exist",
                unprotected_positions=unprotected,
            )
        self.halted = False
        self.halt_reason = ""
        for p in self.positions.values():
            p["panic_failed"] = False
            p["unprotected_reason"] = ""
        return {"halted": False, "resumed": True}

    def retry_panic_liquidation(self, ticker: str) -> Dict[str, Any]:
        self._maybe_raise()
        if ticker not in self.positions:
            raise KeyError(ticker)
        self.positions[ticker]["panic_failed"] = False
        self.positions[ticker]["unprotected_reason"] = ""
        return {"ticker": ticker, "retried": True}


class _GatewayTestClient:
    """Drive ControlGatewayHandler directly over Unix socketpair inside sandbox cleanly."""

    def __init__(self, handler_cls: Any) -> None:
        self.handler_cls = handler_cls

    def request_raw(
        self,
        method: str,
        path: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        body: bytes = b"",
        client_ip: str = "127.0.0.1",
        delay_body: bool = False,
        spy_socket: bool = False,
    ) -> Tuple[int, http.client.HTTPMessage, bytes, Optional[_SpySocket]]:
        client_sock, server_sock = socket.socketpair()
        client_sock.settimeout(2.0)
        wrapped = _SpySocket(server_sock) if spy_socket else server_sock

        hdr_lines = [f"{method} {path} HTTP/1.1"]
        merged = dict(headers or {})
        if not any(k.lower() == "host" for k in merged):
            merged["Host"] = "localhost:8888"
        for k, v in merged.items():
            hdr_lines.append(f"{k}: {v}")
        raw_head = ("\r\n".join(hdr_lines) + "\r\n\r\n").encode("utf-8")

        def _serve() -> None:
            try:
                self.handler_cls(wrapped, (client_ip, 54321), None)
            except Exception:
                pass
            finally:
                try:
                    server_sock.close()
                except OSError:
                    pass

        t = threading.Thread(target=_serve, daemon=True)
        t.start()

        try:
            client_sock.sendall(raw_head)
            if body and not delay_body:
                client_sock.sendall(body)
            resp = http.client.HTTPResponse(client_sock, method=method)
            resp.begin()
            resp_body = b"" if method == "HEAD" else resp.read()
            return resp.status, resp.headers, resp_body, wrapped if spy_socket else None
        finally:
            try:
                client_sock.close()
            except OSError:
                pass
            t.join(timeout=2.0)

    def open_stream(
        self,
        path: str = "/stream",
        *,
        headers: Optional[Mapping[str, str]] = None,
        client_ip: str = "127.0.0.1",
    ) -> Tuple[int, http.client.HTTPMessage, socket.socket, io.BufferedReader, threading.Thread]:
        client_sock, server_sock = socket.socketpair()
        client_sock.settimeout(2.0)
        hdrs = {"Host": "localhost:8888"}
        if headers:
            hdrs.update(headers)
        raw_req = (
            f"GET {path} HTTP/1.1\r\n"
            + "\r\n".join(f"{k}: {v}" for k, v in hdrs.items())
            + "\r\n\r\n"
        ).encode("utf-8")

        def _serve() -> None:
            try:
                self.handler_cls(server_sock, (client_ip, 54322), None)
            except Exception:
                pass
            finally:
                try:
                    server_sock.close()
                except OSError:
                    pass

        t = threading.Thread(target=_serve, daemon=True)
        t.start()
        client_sock.sendall(raw_req)

        fp = client_sock.makefile("rb")
        status_line = fp.readline().decode("utf-8", errors="replace")
        parts = status_line.strip().split(" ", 2)
        status_code = int(parts[1]) if len(parts) >= 2 else 0
        msg = http.client.parse_headers(fp)
        return status_code, msg, client_sock, fp, t


def _read_sse_event(fp: io.BufferedReader, timeout_s: float = 2.0) -> Tuple[str, Dict[str, Any]]:
    """Read one complete SSE event frame (`event:` + `data:` + blank line)."""
    deadline = time.monotonic() + timeout_s
    event_name = ""
    data_lines: List[str] = []
    while time.monotonic() < deadline:
        line_bytes = fp.readline()
        if not line_bytes:
            break
        line = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
        if line == "":
            if event_name or data_lines:
                raw_json = "\n".join(data_lines)
                return event_name or "message", json.loads(raw_json) if raw_json else {}
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].strip())
    raise TimeoutError(f"Timed out waiting for complete SSE event (last event={event_name!r})")


class TestControlGatewayAPI(unittest.TestCase):
    """Comprehensive U5 gateway security, SSE heartbeat/capping, controller, and U6 bundling tests."""

    def setUp(self) -> None:
        self._orig_cwd = os.getcwd()
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        os.chdir(self.root)
        (self.root / "data").mkdir(parents=True, exist_ok=True)
        self.token_file = self.root / "data" / ".dashboard_token"
        self.auth = DashboardAuthManager(
            token_file=self.token_file, max_failures=5, window_seconds=60.0
        )
        self.token = self.auth.get_token()
        self.html_path = self.root / "signals_dashboard.html"

        # Reset SSEBroadcaster singleton stream slots cleanly between tests
        SSEBroadcaster.get_instance().close_all()

        self._patches = ExitStack()
        self._patches.enter_context(
            patch("trading_system.dashboard.server._DASHBOARD_SHARED_CLIENT", None)
        )
        self._patches.enter_context(
            patch("trading_system.config.settings.ACTIVE_TRADES_PATH", str(self.root / "active_trades.json"))
        )
        self._patches.enter_context(
            patch("trading_system.config.settings.TRADES_LOG_PATH", str(self.root / "trades.jsonl"))
        )
        self._patches.enter_context(
            patch("trading_system.config.settings.NOTIFICATIONS_LOG_PATH", str(self.root / "notifications.jsonl"))
        )

    def tearDown(self) -> None:
        SSEBroadcaster.get_instance().close_all()
        self._patches.close()
        os.chdir(self._orig_cwd)
        self.tmp_dir.cleanup()

    def _make_gateway(
        self,
        orchestrator: Optional[Any] = None,
        *,
        heartbeat_s: float = 10.0,
        max_sse: int = MAX_SSE_SUBSCRIBERS,
    ) -> _GatewayTestClient:
        handler_cls = make_dashboard_handler(
            self.root,
            "signals_dashboard.html",
            orchestrator=orchestrator,
            auth_manager=self.auth,
            stream_heartbeat_seconds=heartbeat_s,
            max_sse_subscribers=max_sse,
        )
        return _GatewayTestClient(handler_cls)

    def _auth_headers(self, token: Optional[str] = None, extra: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
        hdrs = {
            "Host": "localhost:8888",
            "Origin": "http://localhost:8888",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token if token is not None else self.token}",
        }
        if extra:
            hdrs.update(extra)
        return hdrs

    # ── 1. Fail-Closed Auth, Pre-Auth Body Refusal, Throttling & Body Limits ─────────

    def test_pre_auth_body_refusal_and_ip_throttling(self) -> None:
        orch = _FakeOrchestrator(self.root / "data")
        gw = self._make_gateway(orch)

        # Missing token with delayed Content-Length: 512 MUST reject 401 immediately without waiting/reading body
        status, hdrs, raw_body, spy = gw.request_raw(
            "POST",
            "/api/config",
            headers={
                "Host": "localhost:8888",
                "Origin": "http://localhost:8888",
                "Content-Type": "application/json",
                "Content-Length": "512",
            },
            body=b'{"should_never_be_read":true}',
            client_ip="10.99.0.1",
            delay_body=True,
            spy_socket=True,
        )
        self.assertEqual(status, 401)
        self.assertIsNotNone(spy)
        self.assertEqual(spy._spy_reader.body_bytes_read, 0)
        self.assertNotIn("Access-Control-Allow-Origin", hdrs)

        # Trigger per-IP rate limiting (5 failures from same IP -> 6th returns 429 + Retry-After)
        for _ in range(4):
            s, _, _, spy_i = gw.request_raw(
                "POST",
                "/api/config",
                headers={
                    "Host": "localhost:8888",
                    "Content-Type": "application/json",
                    "Authorization": "Bearer wrong-token",
                    "Content-Length": "20",
                },
                body=b'{"patch":{"A":1}}',
                client_ip="10.99.0.1",
                spy_socket=True,
            )
            self.assertEqual(s, 401)
            self.assertEqual(spy_i._spy_reader.body_bytes_read, 0)

        s_429, h_429, _, spy_429 = gw.request_raw(
            "POST",
            "/api/config",
            headers={
                "Host": "localhost:8888",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
                "Content-Length": "20",
            },
            body=b'{"patch":{"A":1}}',
            client_ip="10.99.0.1",
            spy_socket=True,
        )
        self.assertEqual(s_429, 429)
        self.assertIn("Retry-After", h_429)
        self.assertEqual(spy_429._spy_reader.body_bytes_read, 0)

    def test_oversized_non_json_and_malformed_requests_rejected(self) -> None:
        orch = _FakeOrchestrator(self.root / "data")
        gw = self._make_gateway(orch)

        # > 131072 bytes returns 413 without reading body
        s_413, _, _, spy = gw.request_raw(
            "POST",
            "/api/config",
            headers=self._auth_headers(extra={"Content-Length": str(MAX_REQUEST_BODY_BYTES + 1)}),
            delay_body=True,
            spy_socket=True,
        )
        self.assertEqual(s_413, 413)
        self.assertEqual(spy._spy_reader.body_bytes_read, 0)

        # Non-JSON Content-Type -> 415
        s_415, _, _, spy_415 = gw.request_raw(
            "POST",
            "/api/config",
            headers=self._auth_headers(extra={"Content-Type": "text/plain", "Content-Length": "4"}),
            body=b"test",
            spy_socket=True,
        )
        self.assertEqual(s_415, 415)
        self.assertEqual(spy_415._spy_reader.body_bytes_read, 0)

        # Chunked Transfer-Encoding -> 411
        s_chunked, _, _, _ = gw.request_raw(
            "POST",
            "/api/config",
            headers=self._auth_headers(extra={"Transfer-Encoding": "chunked"}),
        )
        self.assertEqual(s_chunked, 411)

        # Malformed JSON / NaN / array -> 400
        for bad in (b"{broken", b'{"a": NaN}', b"[1, 2]"):
            s_400, _, _, _ = gw.request_raw(
                "POST",
                "/api/config",
                headers=self._auth_headers(extra={"Content-Length": str(len(bad))}),
                body=bad,
            )
            self.assertEqual(s_400, 400)

        # Empty-body /api/auth/verify with valid token succeeds cleanly
        s_ok, _, body_ok, _ = gw.request_raw(
            "POST",
            "/api/auth/verify",
            headers={
                "Host": "localhost:8888",
                "X-Dashboard-Token": self.token,
                "Content-Length": "0",
            },
        )
        self.assertEqual(s_ok, 200)
        self.assertTrue(json.loads(body_ok.decode("utf-8"))["controller_attached"])

    # ── 2. Host, Origin, Zero Wildcard CORS, CSP & Static Allowlist ─────────────────

    def test_host_origin_cors_and_security_headers(self) -> None:
        orch = _FakeOrchestrator(self.root / "data")
        gw = self._make_gateway(orch)

        # Bad Host -> 403
        s, h, _, _ = gw.request_raw("GET", "/api/data", headers={"Host": "evil.example:8888"})
        self.assertEqual(s, 403)
        self.assertNotIn("Access-Control-Allow-Origin", h)

        # Mismatched Origin on POST -> 403
        s, h, _, _ = gw.request_raw(
            "POST",
            "/api/auth/verify",
            headers={
                "Host": "localhost:8888",
                "Origin": "https://evil.example",
                "Authorization": f"Bearer {self.token}",
                "Content-Length": "0",
            },
        )
        self.assertEqual(s, 403)
        self.assertNotIn("Access-Control-Allow-Origin", h)

        # Mismatched Origin on OPTIONS -> 403
        s_opt_bad, h_opt_bad, _, _ = gw.request_raw(
            "OPTIONS",
            "/api/config",
            headers={"Host": "localhost:8888", "Origin": "http://localhost:9999"},
        )
        self.assertEqual(s_opt_bad, 403)
        self.assertNotIn("Access-Control-Allow-Origin", h_opt_bad)

        # Matching Origin on OPTIONS -> 204 with ZERO Access-Control-Allow-Origin
        s_opt, h_opt, _, _ = gw.request_raw(
            "OPTIONS",
            "/api/config",
            headers={"Host": "localhost:8888", "Origin": "http://localhost:8888"},
        )
        self.assertEqual(s_opt, 204)
        self.assertNotIn("Access-Control-Allow-Origin", h_opt)
        self.assertEqual(h_opt.get("X-Content-Type-Options"), "nosniff")
        self.assertIn("object-src 'none'", h_opt.get("Content-Security-Policy", ""))

        # GET /api/data headers: strict CSP, nosniff, no-cache, NO CORS
        s_get, h_get, _, _ = gw.request_raw("GET", "/api/data")
        self.assertEqual(s_get, 200)
        self.assertNotIn("Access-Control-Allow-Origin", h_get)
        self.assertEqual(h_get.get("X-Content-Type-Options"), "nosniff")
        self.assertIn("no-store", h_get.get("Cache-Control", ""))
        self.assertEqual(
            h_get.get("Content-Security-Policy"),
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; object-src 'none'; base-uri 'none'",
        )

    def test_static_allowlist_and_sensitive_path_blocking(self) -> None:
        (self.root / ".env").write_text("TRADING212_API_KEY=leak\n", encoding="utf-8")
        (self.root / "active_trades.json").write_text("{}", encoding="utf-8")
        (self.root / "data" / "secret.json").write_text("{}", encoding="utf-8")
        generate_html_dashboard(str(self.html_path))

        # Use an empty separate temp dir as web_dir to verify packaged /assets/* serving works anywhere
        with tempfile.TemporaryDirectory() as isolated_web:
            iso_path = Path(isolated_web)
            generate_html_dashboard(str(iso_path / "signals_dashboard.html"))
            handler_cls = make_dashboard_handler(
                iso_path, "signals_dashboard.html", auth_manager=self.auth
            )
            gw = _GatewayTestClient(handler_cls)

            for blocked in (
                "/.env",
                "/.env.local",
                "/active_trades.json",
                "/data/.dashboard_token",
                "/data/config_overrides.json",
                "/../.env",
                "/%2e%2e/.env",
                "/trades.jsonl",
            ):
                s, _, _, _ = gw.request_raw("GET", blocked)
                self.assertIn(s, (403, 404), f"Expected 403/404 for {blocked}, got {s}")
                self.assertEqual(s, 403, f"Expected 403 sensitive block for {blocked}, got {s}")

            s_unknown, _, _, _ = gw.request_raw("GET", "/nonexistent.js")
            self.assertEqual(s_unknown, 404)

            s_css, h_css, body_css, _ = gw.request_raw("GET", "/assets/styles.css")
            self.assertEqual(s_css, 200)
            self.assertIn("text/css", h_css.get("Content-Type", ""))
            self.assertIn(".wb-", body_css.decode("utf-8"))

            s_js, h_js, body_js, _ = gw.request_raw("GET", "/assets/workbench.js")
            self.assertEqual(s_js, 200)
            self.assertIn("javascript", h_js.get("Content-Type", ""))
            self.assertIn("renderEquityChart", body_js.decode("utf-8"))

    # ── 3. Standalone Read-Only Mode & Zero Broker API Quota ────────────────────────

    def test_standalone_read_only_and_zero_trading212_calls(self) -> None:
        active_trades_file = self.root / "active_trades.json"
        trades_payload = {
            "QQQ3m_EQ": {
                "ticker": "QQQ3m_EQ",
                "underlying": "QQQ",
                "direction": "LONG",
                "entry_price": 100.0,
                "original_quantity": 5.0,
                "remaining_quantity": 5.0,
                "stop_loss": 95.0,
                "trailing_stop_loss": 96.0,
                "highest_price": 102.0,
                "lowest_price": 99.0,
                "tp1": 105.0,
                "tp2": 110.0,
                "tp3": 115.0,
                "atr": 2.0,
                "config_epoch_id": "epoch-1",
                "panic_failed": False,
                "is_closed": False,
            }
        }
        active_trades_file.write_text(json.dumps(trades_payload), encoding="utf-8")
        orig_bytes = active_trades_file.read_bytes()

        (self.root / "data" / "funnel_state.json").write_text(
            json.dumps({"stage_counts": {"universe": 120, "screened": 12, "executed": 1}}),
            encoding="utf-8",
        )
        (self.root / "data" / "audit_history.jsonl").write_text(
            json.dumps({"cycle": 7, "win_rate": 0.62, "summary": "Stable"}) + "\n",
            encoding="utf-8",
        )

        gw = self._make_gateway(orchestrator=None)
        with patch("trading_system.config.settings.TRADING212_API_KEY", "fake_t212_key"), patch(
            "trading_system.dashboard.server.Trading212Client.__init__",
            side_effect=AssertionError("Trading212Client must NEVER be auto-instantiated"),
        ), patch(
            "urllib.request.urlopen",
            side_effect=AssertionError("No network calls allowed in GET read path"),
        ):
            for _ in range(20):
                s, _, body, _ = gw.request_raw("GET", "/api/data")
                self.assertEqual(s, 200)
                payload = json.loads(body.decode("utf-8"))
                self.assertFalse(payload["controller_attached"])
                self.assertTrue(payload["read_only_reason"])
                self.assertEqual(payload["funnel"]["stage_counts"]["screened"], 12)
                self.assertEqual(payload["audit"]["latest"]["cycle"], 7)
                self.assertIn("rate_limit_telemetry", payload)
                self.assertEqual(payload["active_signals"][0]["config_epoch_id"], "epoch-1")
                raw_text = body.decode("utf-8")
                for junk in ("Trading 212 Demo", "84% EUR", "4923.05", "Connors 3-RSI"):
                    self.assertNotIn(junk, raw_text)

            # Verify /api/schema/config & /api/config succeed read-only and never leak secrets
            s_schema, _, b_schema, _ = gw.request_raw("GET", "/api/schema/config")
            self.assertEqual(s_schema, 200)
            self.assertIn("parameters", json.loads(b_schema.decode("utf-8"))["schema"])

            s_cfg, _, b_cfg, _ = gw.request_raw("GET", "/api/config")
            self.assertEqual(s_cfg, 200)
            cfg_json = b_cfg.decode("utf-8")
            self.assertNotIn("fake_t212_key", cfg_json)

            # Mutating control endpoints fail closed with 503 when standalone (orchestrator=None)
            verify_s, _, verify_b, _ = gw.request_raw(
                "POST", "/api/auth/verify", headers=self._auth_headers(), body=b"{}"
            )
            self.assertEqual(verify_s, 200)
            self.assertFalse(json.loads(verify_b.decode("utf-8"))["controller_attached"])

            for endpoint, req_body in (
                ("/api/config", json.dumps({"patch": {"SCREENER_TOP_N": 10}, "expected_epoch_id": "epoch-0"}).encode()),
                ("/api/panic", b"{}"),
                ("/api/resume", b'{"confirm_unprotected": false}'),
            ):
                s_mut, _, b_mut, _ = gw.request_raw(
                    "POST",
                    endpoint,
                    headers=self._auth_headers(extra={"Content-Length": str(len(req_body))}),
                    body=req_body,
                )
                self.assertEqual(s_mut, 503)
                self.assertFalse(json.loads(b_mut.decode("utf-8"))["controller_attached"])

        self.assertEqual(active_trades_file.read_bytes(), orig_bytes)

    # ── 4. SSE Streaming, Heartbeat Events & 32-Subscriber Cap ──────────────────────

    def test_sse_snapshot_heartbeat_broadcast_and_subscriber_cap(self) -> None:
        orch = _FakeOrchestrator(self.root / "data")
        gw = self._make_gateway(orch, heartbeat_s=0.08, max_sse=MAX_SSE_SUBSCRIBERS)

        # Open 32 concurrent streams
        streams: List[Tuple[socket.socket, io.BufferedReader, threading.Thread]] = []
        try:
            for i in range(MAX_SSE_SUBSCRIBERS):
                status, hdrs, sock, fp, thread = gw.open_stream("/stream")
                self.assertEqual(status, 200, f"Stream #{i} failed with {status}")
                self.assertIn("text/event-stream", hdrs.get("Content-Type", ""))
                self.assertNotIn("Access-Control-Allow-Origin", hdrs)
                ev, snap = _read_sse_event(fp)
                self.assertEqual(ev, "snapshot")
                self.assertEqual(snap["config_epoch_id"], "epoch-0")
                streams.append((sock, fp, thread))

            self.assertEqual(SSEBroadcaster.get_instance().active_stream_count(), MAX_SSE_SUBSCRIBERS)

            # 33rd subscriber MUST get 503
            s_33, _, b_33, _ = gw.request_raw("GET", "/stream")
            self.assertEqual(s_33, 503)
            self.assertIn("32", json.loads(b_33.decode("utf-8"))["error"])

            # Verify named heartbeat arrives on idle stream #0
            hb_ev, hb_data = _read_sse_event(streams[0][1], timeout_s=1.5)
            self.assertEqual(hb_ev, "heartbeat")
            self.assertEqual(hb_data["epoch"], "epoch-0")
            self.assertTrue(hb_data["ts"])

            # Commit a config mutation and verify SSE subscriber receives config_epoch_changed
            commit_body = json.dumps(
                {"patch": {"SCREENER_TOP_N": 11}, "expected_epoch_id": "epoch-0"}
            ).encode()
            s_mut, _, _, _ = gw.request_raw(
                "POST",
                "/api/config",
                headers=self._auth_headers(extra={"Content-Length": str(len(commit_body))}),
                body=commit_body,
            )
            self.assertEqual(s_mut, 200)
            ev_name, ev_payload = _read_sse_event(streams[0][1], timeout_s=1.5)
            drained = 0
            while ev_name == "heartbeat" and drained < 100:
                drained += 1
                ev_name, ev_payload = _read_sse_event(streams[0][1], timeout_s=1.5)
            self.assertEqual(ev_name, "config_epoch_changed")
            self.assertEqual(ev_payload["config_epoch_id"], "epoch-1")
        finally:
            for sock, fp, thread in streams:
                try:
                    fp.close()
                except OSError:
                    pass
                try:
                    sock.close()
                except OSError:
                    pass
            SSEBroadcaster.get_instance().close_all()
            for _, _, thread in streams:
                thread.join(timeout=1.0)

        self.assertEqual(SSEBroadcaster.get_instance().active_stream_count(), 0)

    # ── 5. Authenticated Control Routes, 409 Conflicts & Scrubbing ──────────────────

    def test_authenticated_config_params_re_epoch_panic_resume_and_409_conflicts(self) -> None:
        orch = _FakeOrchestrator(self.root / "data")
        gw = self._make_gateway(orch, heartbeat_s=2.0)

        # Preview valid patch
        prev_body = json.dumps({"patch": {"SCREENER_TOP_N": 10}, "expected_epoch_id": "epoch-0"}).encode()
        s_prev, _, b_prev, _ = gw.request_raw(
            "POST",
            "/api/config/preview",
            headers=self._auth_headers(extra={"Content-Length": str(len(prev_body))}),
            body=prev_body,
        )
        self.assertEqual(s_prev, 200)
        self.assertTrue(json.loads(b_prev.decode("utf-8"))["valid"])

        # Commit patch -> epoch-1
        commit_body = json.dumps(
            {"patch": {"SCREENER_TOP_N": 10}, "expected_epoch_id": "epoch-0", "reason": "Tighten top N"}
        ).encode()
        s_com, _, b_com, _ = gw.request_raw(
            "POST",
            "/api/config",
            headers=self._auth_headers(extra={"Content-Length": str(len(commit_body))}),
            body=commit_body,
        )
        self.assertEqual(s_com, 200)
        com_json = json.loads(b_com.decode("utf-8"))
        self.assertEqual(com_json["config_epoch_id"], "epoch-1")
        self.assertEqual(com_json["dashboard"]["config"]["SCREENER_TOP_N"], 10)

        # Stale epoch commit -> 409 with actual_epoch_id & latest config
        s_409, _, b_409, _ = gw.request_raw(
            "POST",
            "/api/config",
            headers=self._auth_headers(extra={"Content-Length": str(len(commit_body))}),
            body=commit_body,
        )
        self.assertEqual(s_409, 409)
        conflict_json = json.loads(b_409.decode("utf-8"))
        self.assertEqual(conflict_json["actual_epoch_id"], "epoch-1")
        self.assertEqual(conflict_json["config"]["config_epoch_id"], "epoch-1")

        # Validation error -> 422
        bad_val_body = json.dumps(
            {"patch": {"MAX_RISK_PER_TRADE": 0.99}, "expected_epoch_id": "epoch-1"}
        ).encode()
        s_422, _, b_422, _ = gw.request_raw(
            "POST",
            "/api/config",
            headers=self._auth_headers(extra={"Content-Length": str(len(bad_val_body))}),
            body=bad_val_body,
        )
        self.assertEqual(s_422, 422)
        self.assertTrue(json.loads(b_422.decode("utf-8"))["validation_errors"])

        # Apply built-in profile -> epoch-2
        prof_body = json.dumps(
            {"profile_id": "conservative", "expected_epoch_id": "epoch-1"}
        ).encode()
        s_prof, _, b_prof, _ = gw.request_raw(
            "POST",
            "/api/config",
            headers=self._auth_headers(extra={"Content-Length": str(len(prof_body))}),
            body=prof_body,
        )
        self.assertEqual(s_prof, 200)
        self.assertEqual(json.loads(b_prof.decode("utf-8"))["config_epoch_id"], "epoch-2")

        # Add + delete dynamic custom parameter
        add_param = json.dumps(
            {
                "spec": {
                    "key": "CUSTOM_ADX_MIN",
                    "label": "Custom ADX Minimum",
                    "type": "float",
                    "default": 22.0,
                    "min": 10.0,
                    "max": 60.0,
                    "step": 1.0,
                    "group": "screener",
                    "impact_hint": "Custom ADX floor",
                    "screener_metric_gate": {
                        "metric": "adx",
                        "op": ">=",
                        "threshold": 22.0,
                        "on_missing": "reject",
                    },
                },
                "expected_epoch_id": "epoch-2",
            }
        ).encode()
        s_add, _, b_add, _ = gw.request_raw(
            "POST",
            "/api/params",
            headers=self._auth_headers(extra={"Content-Length": str(len(add_param))}),
            body=add_param,
        )
        self.assertEqual(s_add, 200)
        self.assertEqual(json.loads(b_add.decode("utf-8"))["config_epoch_id"], "epoch-3")

        del_param = json.dumps({"expected_epoch_id": "epoch-3"}).encode()
        s_del, _, b_del, _ = gw.request_raw(
            "POST",
            "/api/params/CUSTOM_ADX_MIN/delete",
            headers=self._auth_headers(extra={"Content-Length": str(len(del_param))}),
            body=del_param,
        )
        self.assertEqual(s_del, 200)
        self.assertEqual(json.loads(b_del.decode("utf-8"))["config_epoch_id"], "epoch-4")

        # Position re-epoch preview + commit + conflict
        pos_prev_body = json.dumps({"target_epoch_id": "epoch-4"}).encode()
        s_pprev, _, b_pprev, _ = gw.request_raw(
            "POST",
            "/api/positions/QQQ3m_EQ/re-epoch/preview",
            headers=self._auth_headers(extra={"Content-Length": str(len(pos_prev_body))}),
            body=pos_prev_body,
        )
        self.assertEqual(s_pprev, 200)
        self.assertEqual(json.loads(b_pprev.decode("utf-8"))["action"], "PREVIEW")

        pos_stale = json.dumps(
            {"expected_position_epoch_id": "epoch-999", "target_epoch_id": "epoch-4"}
        ).encode()
        s_pos_409, _, _, _ = gw.request_raw(
            "POST",
            "/api/positions/QQQ3m_EQ/re-epoch",
            headers=self._auth_headers(extra={"Content-Length": str(len(pos_stale))}),
            body=pos_stale,
        )
        self.assertEqual(s_pos_409, 409)

        pos_ok = json.dumps(
            {"expected_position_epoch_id": "epoch-0", "target_epoch_id": "epoch-4"}
        ).encode()
        s_pos_200, _, b_pos_200, _ = gw.request_raw(
            "POST",
            "/api/positions/QQQ3m_EQ/re-epoch",
            headers=self._auth_headers(extra={"Content-Length": str(len(pos_ok))}),
            body=pos_ok,
        )
        self.assertEqual(s_pos_200, 200)
        self.assertEqual(json.loads(b_pos_200.decode("utf-8"))["config_epoch_id"], "epoch-4")

        # Re-epoch-all preview & execute
        all_prev = json.dumps({"target_epoch_id": "epoch-4"}).encode()
        s_ap, _, _, _ = gw.request_raw(
            "POST",
            "/api/positions/re-epoch-all/preview",
            headers=self._auth_headers(extra={"Content-Length": str(len(all_prev))}),
            body=all_prev,
        )
        self.assertEqual(s_ap, 200)
        all_exec = json.dumps({"expected_epoch_id": "epoch-4", "target_epoch_id": "epoch-4"}).encode()
        s_ae, _, _, _ = gw.request_raw(
            "POST",
            "/api/positions/re-epoch-all",
            headers=self._auth_headers(extra={"Content-Length": str(len(all_exec))}),
            body=all_exec,
        )
        self.assertEqual(s_ae, 200)

        # Panic -> blocked resume (409) -> retry liquidation -> force/clean resume (200)
        s_panic, _, b_panic, _ = gw.request_raw(
            "POST",
            "/api/panic",
            headers={"Host": "localhost:8888", "X-Dashboard-Token": self.token, "Content-Length": "0"},
        )
        self.assertEqual(s_panic, 200)
        self.assertTrue(json.loads(b_panic.decode("utf-8"))["dashboard"]["is_halted"])

        res_no_confirm = json.dumps({"confirm_unprotected": False}).encode()
        s_res_409, _, b_res_409, _ = gw.request_raw(
            "POST",
            "/api/resume",
            headers=self._auth_headers(extra={"Content-Length": str(len(res_no_confirm))}),
            body=res_no_confirm,
        )
        self.assertEqual(s_res_409, 409)
        self.assertTrue(json.loads(b_res_409.decode("utf-8"))["unprotected_positions"])

        s_retry, _, _, _ = gw.request_raw(
            "POST",
            "/api/positions/QQQ3m_EQ/retry-liquidation",
            headers=self._auth_headers(extra={"Content-Length": "2"}),
            body=b"{}",
        )
        self.assertEqual(s_retry, 200)

        s_res_200, _, b_res_200, _ = gw.request_raw(
            "POST",
            "/api/resume",
            headers=self._auth_headers(extra={"Content-Length": str(len(res_no_confirm))}),
            body=res_no_confirm,
        )
        self.assertEqual(s_res_200, 200)
        self.assertFalse(json.loads(b_res_200.decode("utf-8"))["dashboard"]["is_halted"])

        # Sensitive exception message scrubbed cleanly on 500
        orch.raise_on_next_call = RuntimeError(
            f"Leak check /Users/alice/.env Authorization: Bearer {self.token} TRADING212_API_KEY=secret999"
        )
        s_500, _, b_500, _ = gw.request_raw(
            "POST",
            "/panic",
            headers=self._auth_headers(extra={"Content-Length": "2"}),
            body=b"{}",
        )
        self.assertEqual(s_500, 500)
        err_text = json.loads(b_500.decode("utf-8"))["error"]
        self.assertNotIn(self.token, err_text)
        self.assertNotIn("secret999", err_text)
        self.assertNotIn("/Users/alice/.env", err_text)

    # ── 6. Loopback Port Fallback Policy, Atomic HTML Bundling & CLI ────────────────

    def test_allow_port_fallback_false_fails_fast_and_binds_loopback(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy_sock:
            busy_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            busy_sock.bind(("127.0.0.1", 0))
            busy_sock.listen(1)
            busy_port = busy_sock.getsockname()[1]

            with patch("time.sleep", return_value=None):
                with self.assertRaises(OSError):
                    start_local_server_thread(
                        html_file=str(self.html_path),
                        port=busy_port,
                        open_browser=False,
                        auth_manager=self.auth,
                        allow_port_fallback=False,
                    )

        # Verify normal start binds strictly 127.0.0.1 (never 0.0.0.0)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            free_port = probe.getsockname()[1]

        t, httpd, url = start_local_server_thread(
            html_file=str(self.html_path),
            port=free_port,
            open_browser=False,
            auth_manager=self.auth,
            allow_port_fallback=False,
        )
        try:
            self.assertIsNotNone(httpd)
            self.assertEqual(httpd.server_address[0], "127.0.0.1")
            self.assertIn(f"http://localhost:{free_port}/", url)
        finally:
            if httpd is not None:
                httpd.shutdown()
                httpd.server_close()

    def test_atomic_html_bundle_no_cdn_and_xss_escaped(self) -> None:
        orch = _FakeOrchestrator(self.root / "data")
        orch.halted = True
        orch.halt_reason = '</script><script>alert("xss")</script>&test'

        out_path = generate_html_dashboard(str(self.html_path), orchestrator=orch)
        self.assertTrue(Path(out_path).is_file())
        tmp_leftovers = [p for p in self.root.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(tmp_leftovers, [])

        html = self.html_path.read_text(encoding="utf-8")
        # Inline INITIAL_DATA escapes </script> cleanly
        self.assertIn("let INITIAL_DATA =", html)
        self.assertIn(r"\u003c/script\u003e", html)
        self.assertNotIn('</script><script>alert("xss")</script>', html)

        # Compatibility IDs and self-hosted CSS/JS present with zero external CDN URLs
        for token in (
            "equity-chart-svg",
            "equity-val-current",
            "renderEquityChart",
            "equity_history",
            "€",
        ):
            self.assertIn(token, html)
        for junk in ("Trading 212 Demo", "84% EUR", "4923.05", "Connors 3-RSI"):
            self.assertNotIn(junk, html)

        external_urls = [
            m.group(0)
            for m in re.finditer(r"https?://[^\s'\"<>]+", html)
            if not m.group(0).startswith("http://www.w3.org/2000/svg")
        ]
        self.assertEqual(external_urls, [])

    def test_cli_print_token_exits_without_starting_server(self) -> None:
        buf = io.StringIO()
        with patch.object(
            sys,
            "argv",
            ["server.py", "--print-token", "--token-file", str(self.token_file)],
        ), patch("sys.stdout", buf), patch(
            "trading_system.dashboard.server.start_local_server",
            side_effect=AssertionError("Server must not start on --print-token"),
        ):
            server_main()
        self.assertEqual(buf.getvalue().strip(), self.token)

    # ── 7. Regressions: Key Sanitization, Pi LAN Host, Fallback & Panic SSE ────────

    def test_schema_dynamic_specs_and_preview_preserve_param_key_while_stripping_secrets(
        self,
    ) -> None:
        orch = _FakeOrchestrator(self.root / "data")
        gw = self._make_gateway(orch)

        # Add a custom dynamic parameter first
        add_body = json.dumps(
            {
                "spec": {
                    "key": "CUSTOM_ADX_FLOOR",
                    "label": "Custom ADX Floor",
                    "type": "float",
                    "default": 20.0,
                    "min": 5.0,
                    "max": 60.0,
                    "step": 1.0,
                    "group": "screener",
                    "impact_hint": "ADX floor filter",
                    "screener_metric_gate": {
                        "metric": "adx",
                        "op": ">=",
                        "threshold": 20.0,
                    },
                },
                "expected_epoch_id": "epoch-0",
            }
        ).encode()
        s_add, _, b_add, _ = gw.request_raw(
            "POST",
            "/api/params",
            headers=self._auth_headers(extra={"Content-Length": str(len(add_body))}),
            body=add_body,
        )
        self.assertEqual(s_add, 200)

        # Verify build_dashboard_data, GET /api/data, GET /api/schema/config, and GET /api/config preserve "key"
        with patch("trading_system.config.settings.TRADING212_API_KEY", "secret_key_val"):
            snap = build_dashboard_data(force=True, orchestrator=orch)
            self.assertTrue(snap["schema"]["parameters"])
            self.assertTrue(all(p.get("key") for p in snap["schema"]["parameters"]))
            self.assertEqual(snap["config"]["dynamic_parameter_specs"][0]["key"], "CUSTOM_ADX_FLOOR")
            self.assertNotIn("TRADING212_API_KEY", snap["config"])

            s_data, _, b_data, _ = gw.request_raw("GET", "/api/data")
            self.assertEqual(s_data, 200)
            data_json = json.loads(b_data.decode("utf-8"))
            self.assertTrue(all(p.get("key") for p in data_json["schema"]["parameters"]))
            self.assertEqual(
                data_json["config"]["dynamic_parameter_specs"][0]["key"],
                "CUSTOM_ADX_FLOOR",
            )

            s_schema, _, b_schema, _ = gw.request_raw("GET", "/api/schema/config")
            self.assertEqual(s_schema, 200)
            schema_params = json.loads(b_schema.decode("utf-8"))["schema"]["parameters"]
            self.assertTrue(all(p.get("key") for p in schema_params))

            s_cfg, _, b_cfg, _ = gw.request_raw("GET", "/api/config")
            self.assertEqual(s_cfg, 200)
            cfg_payload = json.loads(b_cfg.decode("utf-8"))["config"]
            self.assertEqual(cfg_payload["dynamic_parameter_specs"][0]["key"], "CUSTOM_ADX_FLOOR")
            self.assertNotIn("TRADING212_API_KEY", cfg_payload)

            # Verify preview diff entries retain "key"
            prev_body = json.dumps(
                {"patch": {"MIN_ADX_TREND": 22.0}, "expected_epoch_id": "epoch-1"}
            ).encode()
            s_prev, _, b_prev, _ = gw.request_raw(
                "POST",
                "/api/config/preview",
                headers=self._auth_headers(extra={"Content-Length": str(len(prev_body))}),
                body=prev_body,
            )
            self.assertEqual(s_prev, 200)
            diff_items = json.loads(b_prev.decode("utf-8"))["diff"]
            self.assertTrue(diff_items)
            self.assertEqual(diff_items[0]["key"], "MIN_ADX_TREND")

    def test_read_only_config_bundle_fallback_handles_config_bus_failure(self) -> None:
        with patch(
            "trading_system.dashboard.server.ConfigBus",
            side_effect=RuntimeError("corrupt config store"),
        ):
            payload = build_dashboard_data(force=True, orchestrator=None)

        self.assertFalse(payload["controller_attached"])
        self.assertEqual(payload["config_epoch_id"], "epoch-0")
        self.assertEqual(payload["config"]["config_epoch_id"], "epoch-0")
        self.assertTrue(payload["schema"]["parameters"])
        self.assertTrue(all(p.get("key") for p in payload["schema"]["parameters"]))

    def test_bind_host_resolution_supports_lan_env_and_cli_override(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DASHBOARD_HOST", None)
            self.assertEqual(_resolve_dashboard_bind_host(None), "127.0.0.1")
            self.assertEqual(_resolve_dashboard_bind_host("0.0.0.0"), "0.0.0.0")

            # Outside unittest (production Pi service), default bind host is 0.0.0.0 for LAN access
            fake_mods = dict(sys.modules)
            fake_mods.pop("unittest", None)
            with patch.dict(sys.modules, fake_mods, clear=True):
                self.assertEqual(_resolve_dashboard_bind_host(None), "0.0.0.0")
                with patch.dict(os.environ, {"DASHBOARD_HOST": "192.168.1.50"}):
                    self.assertEqual(_resolve_dashboard_bind_host(None), "192.168.1.50")
                    self.assertEqual(_resolve_dashboard_bind_host("127.0.0.1"), "127.0.0.1")

        # Verify explicit host="0.0.0.0" binds wildcard and accepts Host: trading212-pi:<port>
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("0.0.0.0", 0))
            lan_port = probe.getsockname()[1]

        t, httpd, url = start_local_server_thread(
            html_file=str(self.html_path),
            port=lan_port,
            open_browser=False,
            auth_manager=self.auth,
            allow_port_fallback=False,
            host="0.0.0.0",
        )
        try:
            self.assertIsNotNone(httpd)
            self.assertEqual(httpd.server_address[0], "0.0.0.0")
            self.assertEqual(url, f"http://localhost:{lan_port}/{self.html_path.name}")
            gw = self._make_gateway(orchestrator=None)
            s_pi, _, _, _ = gw.request_raw(
                "GET",
                "/api/data",
                headers={"Host": f"trading212-pi:{lan_port}"},
            )
            self.assertEqual(s_pi, 200)
        finally:
            if httpd is not None:
                httpd.shutdown()
                httpd.server_close()

        # Verify CLI --host forwards cleanly to start_local_server
        with patch.object(
            sys,
            "argv",
            [
                "server.py",
                "--host",
                "127.0.0.1",
                "--port",
                "8999",
                "--output",
                str(self.html_path),
                "--token-file",
                str(self.token_file),
                "--no-browser",
            ],
        ), patch("trading_system.dashboard.server.start_local_server") as mock_serve:
            server_main()
            mock_serve.assert_called_once()
            self.assertEqual(mock_serve.call_args.kwargs.get("host"), "127.0.0.1")

    def test_panic_emits_dashboard_update_sse_event(self) -> None:
        orch = _FakeOrchestrator(self.root / "data")
        gw = self._make_gateway(orch, heartbeat_s=0.5)

        status, _, sock, fp, thread = gw.open_stream("/stream")
        self.assertEqual(status, 200)
        try:
            init_ev, init_data = _read_sse_event(fp)
            self.assertEqual(init_ev, "snapshot")
            self.assertFalse(init_data["is_halted"])

            s_panic, _, b_panic, _ = gw.request_raw(
                "POST",
                "/api/panic",
                headers=self._auth_headers(extra={"Content-Length": "2"}),
                body=b"{}",
            )
            self.assertEqual(s_panic, 200)
            self.assertTrue(json.loads(b_panic.decode("utf-8"))["dashboard"]["is_halted"])

            seen_events: Dict[str, Dict[str, Any]] = {}
            for _ in range(20):
                ev, payload = _read_sse_event(fp, timeout_s=1.5)
                if ev and payload is not None:
                    seen_events[ev] = payload
                if "dashboard_update" in seen_events:
                    break

            self.assertIn("dashboard_update", seen_events)
            self.assertTrue(seen_events["dashboard_update"]["is_halted"])
        finally:
            try:
                fp.close()
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
            SSEBroadcaster.get_instance().close_all()
            thread.join(timeout=1.0)


if __name__ == "__main__":
    unittest.main()
