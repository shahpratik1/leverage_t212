"""Fail-closed authentication and HTTP security utilities for the dashboard server.

Provides:
- ``DashboardAuthManager``: fail-closed token resolution
  (``DASHBOARD_AUTH_TOKEN`` > ``PANIC_FLATTEN_TOKEN`` > atomic ``0o600``
  persistent token file), constant-time header verification, and bounded
  per-IP sliding-window brute-force throttling.
- Reusable HTTP security validators and sanitizers usable by ``server.py``
  without importing ``server.py``: ``is_valid_host_header``,
  ``is_valid_origin_header``, ``json_safe_for_inline_script``,
  ``scrub_error_message``, and ``is_sensitive_key``.
"""

from __future__ import annotations

import argparse
import atexit
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import shutil
import socket
import stat
import sys
import tempfile
import threading
import time
import hmac
from collections import OrderedDict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Iterable, Mapping, Optional, Sequence, TextIO, Tuple, Union
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

MIN_TOKEN_LENGTH = 16
DEFAULT_TOKEN_FILE = Path("data") / ".dashboard_token"
DEFAULT_MAX_FAILURES = 5
DEFAULT_FAILURE_WINDOW_SECONDS = 60.0
DEFAULT_MAX_TRACKED_IPS = 1024

_LOOPBACK_IPV4_NET = ipaddress.IPv4Network("127.0.0.0/8")
_RFC1918_IPV4_NETS = (
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
)
_DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_BRACKETED_HOST_RE = re.compile(r"^\[([^\]]+)\](?::([0-9]+))?$")

_ENV_SECRET_NAMES = (
    "DASHBOARD_AUTH_TOKEN",
    "PANIC_FLATTEN_TOKEN",
    "TRADING212_API_KEY",
    "TRADING212_API_SECRET",
    "OPENAI_API_KEY",
    "POLYGON_API_KEY",
    "FINNHUB_API_KEY",
    "OLOSTEP_API_KEY",
    "NIXTLA_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "DISCORD_WEBHOOK_URL",
)

SENSITIVE_CONFIG_KEYS = frozenset({
    "TRADING212_API_KEY",
    "TRADING212_API_SECRET",
    "TRADING212_BASE_URL",
    "TRADING212_LIVE_URL",
    "USE_LIVE",
    "OPENAI_API_KEY",
    "POLYGON_API_KEY",
    "FINNHUB_API_KEY",
    "OLOSTEP_API_KEY",
    "NIXTLA_API_KEY",
    "PANIC_FLATTEN_TOKEN",
    "DASHBOARD_AUTH_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "DISCORD_WEBHOOK_URL",
    "NOTIFICATIONS_LOG_PATH",
    "ACTIVE_TRADES_PATH",
    "CANDLE_DB_PATH",
    "TRADES_LOG_PATH",
    "DASHBOARD_PORT",
    "ENABLE_EMBEDDED_DASHBOARD",
})

_SENSITIVE_TOKEN_PARTS = frozenset({
    "SECRET",
    "SECRETS",
    "TOKEN",
    "TOKENS",
    "PASSWORD",
    "PASSWD",
    "PWD",
    "CREDENTIAL",
    "CREDENTIALS",
    "WEBHOOK",
    "PRIVATE",
    "APIKEY",
    "BEARER",
    "COOKIE",
    "PASSPHRASE",
    "PATH",
    "PATHS",
    "FILEPATH",
    "DIR",
    "DIRECTORY",
})

_SENSITIVE_KEY_SUFFIXES = (
    "_API_KEY",
    "_SECRET_KEY",
    "_PRIVATE_KEY",
    "_SIGNING_KEY",
    "_ENCRYPTION_KEY",
    "_ACCESS_KEY",
    "_AUTH_KEY",
    "_FILE",
    "_FILES",
    "_AUTH",
)

# Error-scrubbing compiled patterns
_AUTH_HEADER_RE = re.compile(
    r"(?i)\b(authorization\s*[:=]\s*)(?:bearer|basic|token)\s+\S+"
)
_BEARER_TOKEN_RE = re.compile(
    r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"
)
_X_DASHBOARD_TOKEN_RE = re.compile(
    r"(?i)(x-dashboard-token\s*[:=]\s*)([^\s,;\"']+)"
)
_URL_USERINFO_RE = re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@")
_DISCORD_WEBHOOK_RE = re.compile(r"https?://discord(?:app)?\.com/api/webhooks/\S+")
_TELEGRAM_BOT_URL_RE = re.compile(r"https?://api\.telegram\.org/bot\S+")
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:token|auth|api_?key|secret|password|passwd|access_token|webhook)=)[^&\s'\"]+"
)
_KV_SECRET_RE = re.compile(
    r"""(?i)(["']?(?:TRADING212_API_KEY|TRADING212_API_SECRET|OPENAI_API_KEY|POLYGON_API_KEY|"""
    r"""FINNHUB_API_KEY|OLOSTEP_API_KEY|NIXTLA_API_KEY|PANIC_FLATTEN_TOKEN|DASHBOARD_AUTH_TOKEN|"""
    r"""TELEGRAM_BOT_TOKEN|DISCORD_WEBHOOK_URL|api[_-]?key|api[_-]?secret|auth[_-]?token|"""
    r"""access[_-]?token|secret|token|password|passwd)["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;}\]]+)"""
)
_OPENAI_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")
_TELEGRAM_TOKEN_RE = re.compile(r"\b[0-9]{6,}:[A-Za-z0-9_-]{15,}\b")
_TRACEBACK_LINE_RE = re.compile(r'File\s+"[^"]+",\s+line\s+\d+[^\n]*')
_UNIX_ABS_PATH_RE = re.compile(
    r"(?:(?<=^)|(?<=[\s'\"(=,:]))/(?!api(?:/|\b)|stream\b|assets/)"
    r"(?:(?:[A-Za-z0-9._@-]+/)+[A-Za-z0-9._@-]+|"
    r"(?:etc|tmp|var|home|Users|root|opt|private|usr|srv|mnt|app|data|\.env|\.dashboard_token)\b)"
)
_HOME_REL_PATH_RE = re.compile(
    r"(?:(?<=^)|(?<=[\s'\"(=,:]))(?:~|\.\.?|data|trading_system)(?:/[A-Za-z0-9._@-]+)+"
)
_WIN_PATH_RE = re.compile(r"\b[A-Za-z]:\\(?:[^\\\s'\"]+(?:\\[^\\\s'\"]+)*)")
_SENSITIVE_FILENAME_RE = re.compile(
    r"(?:(?<=^)|(?<=[\s'\"(=,:]))(?:\.env(?:\.[A-Za-z0-9._-]+)?|\.dashboard_token)\b"
)

_ACTIVE_RUNTIME_SECRETS_LOCK = threading.Lock()
_ACTIVE_RUNTIME_SECRETS: Deque[str] = deque(maxlen=32)


def _register_runtime_secret(secret: str) -> None:
    """Remember active generated/resolved secrets so error scrubbing can redact them."""
    if not secret or len(secret) < 12:
        return
    with _ACTIVE_RUNTIME_SECRETS_LOCK:
        if secret not in _ACTIVE_RUNTIME_SECRETS:
            _ACTIVE_RUNTIME_SECRETS.append(secret)


def _is_test_runner() -> bool:
    """Detect whether code is executing inside a unit/integration test runner."""
    return "unittest" in sys.modules or "pytest" in sys.modules


def _allocate_test_token_file() -> Path:
    """Create an isolated temporary directory path for unit-test token storage."""
    tmp_dir = tempfile.mkdtemp(prefix="dashboard_auth_")
    atexit.register(shutil.rmtree, tmp_dir, True)
    return Path(tmp_dir) / ".dashboard_token"


@dataclass(frozen=True)
class AuthResult:
    """Outcome of authenticating an HTTP dashboard control request."""

    ok: bool
    status_code: int
    error: str = ""
    retry_after: Optional[int] = None


def extract_token_from_headers(headers: Optional[Any]) -> Optional[str]:
    """Extract a bearer or ``X-Dashboard-Token`` credential from HTTP headers.

    Performs case-insensitive lookup across both ``http.client.HTTPMessage`` and
    plain dictionaries. Conflicting duplicate auth headers fail closed (``None``).
    """
    if headers is None:
        return None

    auth_values: list[str] = []
    dash_values: list[str] = []

    if hasattr(headers, "items"):
        for key, value in headers.items():
            if not isinstance(key, str) or value is None:
                continue
            key_lower = key.strip().lower()
            cleaned = str(value).strip()
            if not cleaned:
                continue
            if key_lower == "authorization":
                auth_values.append(cleaned)
            elif key_lower == "x-dashboard-token":
                dash_values.append(cleaned)
    elif hasattr(headers, "get"):
        for candidate_key in ("Authorization", "authorization"):
            val = headers.get(candidate_key)
            if val is not None and str(val).strip():
                auth_values.append(str(val).strip())
                break
        for candidate_key in ("X-Dashboard-Token", "x-dashboard-token"):
            val = headers.get(candidate_key)
            if val is not None and str(val).strip():
                dash_values.append(str(val).strip())
                break

    if len(set(auth_values)) > 1 or len(set(dash_values)) > 1:
        return None

    if auth_values:
        parts = auth_values[0].split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            bearer_token = parts[1].strip()
            if bearer_token:
                return bearer_token

    if dash_values:
        return dash_values[0]

    return None


def _parse_port(port_str: str) -> Optional[int]:
    """Parse an explicit TCP port string in 1..65535 without leading zeroes."""
    if not port_str or not (port_str.isascii() and port_str.isdigit()) or port_str.startswith("0"):
        return None
    port = int(port_str)
    return port if 1 <= port <= 65535 else None


def _extra_allowed_hosts() -> frozenset[str]:
    """Parse optional comma-separated extra allowed hosts from ``DASHBOARD_ALLOWED_HOSTS``."""
    raw = os.environ.get("DASHBOARD_ALLOWED_HOSTS", "")
    if not raw:
        return frozenset()
    allowed: set[str] = set()
    for item in raw.split(","):
        candidate = item.strip().lower().rstrip(".")
        if not candidate or candidate in ("*", "0.0.0.0", "::"):
            continue
        allowed.add(candidate)
    return frozenset(allowed)


def _local_machine_hostname() -> str:
    """Return the normalized local OS hostname when available."""
    try:
        raw = socket.gethostname() or ""
    except OSError:
        return ""
    norm = raw.strip().lower().rstrip(".")
    if not norm or norm in ("*", "0.0.0.0", "::"):
        return ""
    return norm


def _parse_and_validate_host(host_header: Optional[str]) -> Optional[Tuple[str, Optional[int]]]:
    """Parse and validate a ``Host`` header against loopback/RFC1918/LAN allowlists.

    Returns ``(normalized_hostname, explicit_port_or_none)`` when valid, or ``None``.
    """
    if not isinstance(host_header, str):
        return None

    raw = host_header.strip()
    if not raw or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        return None
    if any(ch in raw for ch in ("@", "/", "\\", "?", "#", ",")):
        return None

    host_part: str
    port: Optional[int] = None

    if raw.startswith("["):
        match = _BRACKETED_HOST_RE.match(raw)
        if not match:
            return None
        host_part = match.group(1)
        port_str = match.group(2)
        if port_str is not None:
            port = _parse_port(port_str)
            if port is None:
                return None
        try:
            ipv6_addr = ipaddress.IPv6Address(host_part)
        except ValueError:
            return None
        if ipv6_addr != ipaddress.IPv6Address("::1"):
            return None
        return (str(ipv6_addr), port)

    colon_count = raw.count(":")
    if colon_count > 1:
        # Unbracketed IPv6 literal without port (e.g. "::1")
        try:
            ipv6_addr = ipaddress.IPv6Address(raw)
        except ValueError:
            return None
        if ipv6_addr != ipaddress.IPv6Address("::1"):
            return None
        return (str(ipv6_addr), None)

    if colon_count == 1:
        host_part, port_str = raw.split(":", 1)
        port = _parse_port(port_str)
        if port is None:
            return None
    else:
        host_part = raw

    host_norm = host_part.lower()
    if not host_norm:
        return None

    # Check explicit IPv4 literal first
    try:
        ip_obj = ipaddress.ip_address(host_norm)
    except ValueError:
        ip_obj = None

    if ip_obj is not None:
        if not isinstance(ip_obj, ipaddress.IPv4Address):
            return None
        if not ip_obj.is_unspecified and (
            ip_obj in _LOOPBACK_IPV4_NET
            or any(ip_obj in net for net in _RFC1918_IPV4_NETS)
            or str(ip_obj) in _extra_allowed_hosts()
        ):
            return (str(ip_obj), port)
        return None

    # Hostname validation (strip at most one trailing FQDN dot)
    if host_norm.endswith("."):
        host_norm = host_norm[:-1]
    if not host_norm or len(host_norm) > 253 or ".." in host_norm:
        return None

    labels = host_norm.split(".")
    if not all(_DNS_LABEL_RE.match(label) for label in labels):
        return None

    if host_norm in ("localhost", "trading212-pi"):
        return (host_norm, port)
    if host_norm.endswith(".local") and len(labels) >= 2:
        return (host_norm, port)
    if host_norm.endswith(".trycloudflare.com") and len(labels) >= 3:
        return (host_norm, port)
    if host_norm.endswith(".fritz.box") and len(labels) >= 3:
        return (host_norm, port)
    local_hostname = _local_machine_hostname()
    if local_hostname and host_norm == local_hostname:
        return (host_norm, port)
    if host_norm in _extra_allowed_hosts():
        return (host_norm, port)

    return None


def is_valid_host_header(host_header: Optional[str]) -> bool:
    """Return ``True`` if ``Host`` targets loopback, RFC1918 IPv4, LAN names, or ``DASHBOARD_ALLOWED_HOSTS``."""
    return _parse_and_validate_host(host_header) is not None


def is_valid_origin_header(origin_header: Optional[str], host_header: Optional[str]) -> bool:
    """Validate ``Origin`` against the validated ``Host`` header.

    Absent ``Origin`` (``None``) returns ``True`` for non-browser CLI/curl callers
    authenticating with a token. Explicit ``"null"``, malformed URLs, or mismatched
    origins return ``False``.
    """
    if origin_header is None:
        return True
    if not isinstance(origin_header, str):
        return False

    origin_clean = origin_header.strip()
    if (
        not origin_clean
        or origin_clean.lower() == "null"
        or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in origin_clean)
    ):
        return False

    parsed_host = _parse_and_validate_host(host_header)
    if parsed_host is None:
        return False
    expected_hostname, expected_port = parsed_host

    try:
        parsed_origin = urlsplit(origin_clean)
        origin_port = parsed_origin.port
    except ValueError:
        return False

    scheme = parsed_origin.scheme.lower()
    if scheme not in ("http", "https"):
        return False
    if parsed_origin.username is not None or parsed_origin.password is not None:
        return False
    if parsed_origin.path or parsed_origin.query or parsed_origin.fragment:
        return False

    if origin_port is not None and not (1 <= origin_port <= 65535):
        return False

    origin_hostname = (parsed_origin.hostname or "").lower().rstrip(".")
    if not origin_hostname:
        return False

    # Normalize IPv6 canonical representation if applicable
    try:
        origin_hostname = str(ipaddress.ip_address(origin_hostname))
    except ValueError:
        pass

    if origin_hostname != expected_hostname:
        return False

    default_port = 443 if scheme == "https" else 80
    effective_origin_port = origin_port if origin_port is not None else default_port
    effective_host_port = expected_port if expected_port is not None else default_port
    return effective_origin_port == effective_host_port


def json_safe_for_inline_script(
    obj: Any,
    *,
    default: Optional[Callable[[Any], Any]] = None,
) -> str:
    """Serialize ``obj`` to JSON escaped for safe inline ``<script>`` embedding."""
    serialized = json.dumps(obj, ensure_ascii=True, default=default)
    return (
        serialized.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def is_sensitive_key(key: str) -> bool:
    """Return ``True`` if a config key represents a secret, path, or env-only setting."""
    if not isinstance(key, str):
        return False
    norm = key.strip().upper()
    if not norm:
        return False
    if norm in SENSITIVE_CONFIG_KEYS:
        return True
    if norm in ("API_KEY", "SECRET_KEY", "PRIVATE_KEY", "TOKEN_FILE", "STATE_FILE", "LOG_FILE", "ENV_FILE"):
        return True
    if norm.endswith(_SENSITIVE_KEY_SUFFIXES) or norm.startswith("AUTH_"):
        return True
    tokens = set(re.split(r"[^A-Z0-9]+", norm)) - {""}
    if tokens & _SENSITIVE_TOKEN_PARTS:
        return True
    if "API" in tokens and ("KEY" in tokens or "KEYS" in tokens):
        return True
    return False


def sanitize_mapping(value: Any) -> Any:
    """Recursively strip sensitive config keys from JSON payloads and ConfigSnapshots."""
    if hasattr(value, "to_dict") and not isinstance(value, Mapping):
        try:
            value = value.to_dict(include_specs=True)
        except TypeError:
            value = value.to_dict()
    if isinstance(value, Mapping):
        cleaned: Dict[str, Any] = {}
        for k, v in value.items():
            key_str = str(k)
            if is_sensitive_key(key_str):
                continue
            cleaned[key_str] = sanitize_mapping(v)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [sanitize_mapping(item) for item in value]
    return value


def scrub_error_message(
    exc_or_msg: Any,
    extra_secrets: Optional[Iterable[str]] = None,
) -> str:
    """Strip credentials, tokens, tracebacks, and filesystem paths from an error string."""
    if exc_or_msg is None:
        return "Internal server error"

    if isinstance(exc_or_msg, BaseException):
        raw = str(exc_or_msg).strip() or type(exc_or_msg).__name__
    else:
        raw = str(exc_or_msg)

    if not raw.strip():
        return ""

    # Strip traceback preamble and file/line frames before collapsing whitespace
    lines = [
        line.strip()
        for line in raw.splitlines()
        if line.strip() and line.strip() != "Traceback (most recent call last):"
    ]
    cleaned_lines = [_TRACEBACK_LINE_RE.sub("[TRACE_FRAME]", line) for line in lines]
    text = " ".join(cleaned_lines)

    # Redact any known active secret values first (longest first to avoid partial matches)
    secret_candidates: set[str] = set()
    for env_name in _ENV_SECRET_NAMES:
        val = os.environ.get(env_name, "").strip()
        if len(val) >= 6:
            secret_candidates.add(val)
    with _ACTIVE_RUNTIME_SECRETS_LOCK:
        for secret in _ACTIVE_RUNTIME_SECRETS:
            if len(secret) >= 6:
                secret_candidates.add(secret)
    if extra_secrets is not None:
        for secret in extra_secrets:
            if isinstance(secret, str):
                s = secret.strip()
                if len(s) >= 6:
                    secret_candidates.add(s)

    for secret in sorted(secret_candidates, key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")

    # Redact structured URL credentials, headers, tokens, and paths
    text = _DISCORD_WEBHOOK_RE.sub("https://discord.com/api/webhooks/[REDACTED]", text)
    text = _TELEGRAM_BOT_URL_RE.sub("https://api.telegram.org/bot[REDACTED]", text)
    text = _URL_USERINFO_RE.sub(r"\1[REDACTED]@", text)
    text = _QUERY_SECRET_RE.sub(r"\1[REDACTED]", text)
    text = _AUTH_HEADER_RE.sub(r"\1[REDACTED]", text)
    text = _BEARER_TOKEN_RE.sub("Bearer [REDACTED]", text)
    text = _X_DASHBOARD_TOKEN_RE.sub(r"\1[REDACTED]", text)
    text = _KV_SECRET_RE.sub(r"\1[REDACTED]", text)
    text = _OPENAI_KEY_RE.sub("[REDACTED]", text)
    text = _TELEGRAM_TOKEN_RE.sub("[REDACTED]", text)
    text = _WIN_PATH_RE.sub("[REDACTED_PATH]", text)
    text = _UNIX_ABS_PATH_RE.sub("[REDACTED_PATH]", text)
    text = _HOME_REL_PATH_RE.sub("[REDACTED_PATH]", text)
    text = _SENSITIVE_FILENAME_RE.sub("[REDACTED_PATH]", text)

    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > 240:
        text = text[:237].rstrip() + "..."
    return text


class DashboardAuthManager:
    """Fail-closed token manager and sliding-window request verifier."""

    def __init__(
        self,
        token_file: Optional[Union[str, Path]] = None,
        *,
        max_failures: int = DEFAULT_MAX_FAILURES,
        window_seconds: float = DEFAULT_FAILURE_WINDOW_SECONDS,
        max_tracked_ips: int = DEFAULT_MAX_TRACKED_IPS,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        if max_failures < 1:
            raise ValueError("max_failures must be >= 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        if max_tracked_ips < 1:
            raise ValueError("max_tracked_ips must be >= 1")

        self.max_failures = int(max_failures)
        self.window_seconds = float(window_seconds)
        self.max_tracked_ips = int(max_tracked_ips)
        self._clock = clock

        if token_file is not None:
            if not str(token_file).strip():
                raise ValueError("token_file must not be empty")
            self.token_file = Path(token_file)
        elif _is_test_runner():
            self.token_file = _allocate_test_token_file()
        else:
            self.token_file = DEFAULT_TOKEN_FILE

        self._lock = threading.Lock()
        self._failures: "OrderedDict[str, Deque[float]]" = OrderedDict()
        self.created_token_file = False
        self.token_source, self._token = self._resolve_token()
        _register_runtime_secret(self._token)

        logger.info("Dashboard authentication initialized (%s).", self.setup_hint())

    def _now(self) -> float:
        if self._clock is not None:
            return float(self._clock())
        return float(time.monotonic())

    @staticmethod
    def _enforce_permissions(path: Path) -> None:
        """Best-effort enforcement of ``0o600`` owner-only permissions on token file."""
        try:
            current_mode = stat.S_IMODE(path.stat().st_mode)
            if current_mode != 0o600:
                os.chmod(path, 0o600)
        except OSError as exc:
            logger.warning("Could not enforce 0600 permissions on %s: %s", path, exc)

    @staticmethod
    def _read_existing_token_file(path: Path) -> str:
        """Read and validate an existing token file safely without following symlinks."""
        if path.is_symlink():
            raise ValueError(f"Refusing to read symlinked dashboard token file: {path}")

        no_follow = getattr(os, "O_NOFOLLOW", 0)
        flags = os.O_RDONLY | no_follow

        for attempt in range(5):
            fd = os.open(str(path), flags)
            try:
                content = os.read(fd, 4096).decode("utf-8")
            finally:
                os.close(fd)
            token = content.strip()
            if token:
                if len(token) < MIN_TOKEN_LENGTH:
                    raise ValueError(
                        f"Configured token in {path} is shorter than {MIN_TOKEN_LENGTH} characters."
                    )
                return token
            if attempt < 4:
                time.sleep(0.01)

        raise ValueError(f"Dashboard token file {path} is empty.")

    def _bootstrap_token_file(self) -> str:
        """Atomically create or safely re-read the persistent ``0o600`` dashboard token file."""
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        if self.token_file.is_symlink():
            raise ValueError(f"Refusing to use symlinked dashboard token file: {self.token_file}")

        generated = secrets.token_urlsafe(32)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        try:
            fd = os.open(str(self.token_file), flags, 0o600)
            try:
                os.write(fd, (generated + "\n").encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            self._enforce_permissions(self.token_file)
            self.created_token_file = True
            return generated
        except FileExistsError:
            token = self._read_existing_token_file(self.token_file)
            self._enforce_permissions(self.token_file)
            return token

    def _resolve_token(self) -> Tuple[str, str]:
        """Resolve dashboard auth token according to strict precedence rules."""
        for env_name in ("DASHBOARD_AUTH_TOKEN", "PANIC_FLATTEN_TOKEN"):
            raw = os.environ.get(env_name)
            if raw is None:
                continue
            candidate = raw.strip()
            if not candidate:
                # Whitespace-only env vars are treated as unset
                continue
            if len(candidate) < MIN_TOKEN_LENGTH:
                raise ValueError(
                    f"{env_name} must be at least {MIN_TOKEN_LENGTH} characters long."
                )
            return env_name, candidate

        return "token_file", self._bootstrap_token_file()

    def get_token(self) -> str:
        """Return the resolved dashboard authentication token."""
        return self._token

    def setup_hint(self) -> str:
        """Return an operator setup hint naming only the token file path and ``--print-token``."""
        return (
            f"token_source={self.token_source}; "
            f"token_file={self.token_file}; "
            f"run `python3 -m trading_system.dashboard.server --print-token` to view the token."
        )

    def print_token(self, stream: Optional[TextIO] = None) -> str:
        """Write the resolved token to ``stream`` (defaults to stdout) and return it."""
        out = stream if stream is not None else sys.stdout
        out.write(f"{self._token}\n")
        out.flush()
        return self._token

    @staticmethod
    def extract_token_from_headers(headers: Optional[Any]) -> Optional[str]:
        """Extract ``Authorization: Bearer <token>`` or ``X-Dashboard-Token: <token>``."""
        return extract_token_from_headers(headers)

    def _prune_ip_locked(self, ip: str, now: float) -> Optional[Deque[float]]:
        dq = self._failures.get(ip)
        if dq is None:
            return None
        while dq and (now - dq[0]) >= self.window_seconds:
            dq.popleft()
        if not dq:
            del self._failures[ip]
            return None
        return dq

    def _evict_if_full_locked(self, now: float) -> None:
        if len(self._failures) < self.max_tracked_ips:
            return
        expired_ips = [
            tracked_ip
            for tracked_ip, dq in self._failures.items()
            if not dq or (now - dq[-1]) >= self.window_seconds
        ]
        for tracked_ip in expired_ips:
            del self._failures[tracked_ip]
        while len(self._failures) >= self.max_tracked_ips:
            self._failures.popitem(last=False)

    def clear_failures(self, client_ip: Optional[str] = None) -> None:
        """Clear recorded auth failure state for one IP or all tracked IPs."""
        with self._lock:
            if client_ip is None:
                self._failures.clear()
            else:
                norm_ip = (client_ip or "unknown").strip()[:128] or "unknown"
                self._failures.pop(norm_ip, None)

    def verify_request_auth(
        self,
        headers: Optional[Any],
        client_ip: str = "127.0.0.1",
    ) -> AuthResult:
        """Verify request credentials using constant-time digest comparison and per-IP throttling."""
        now = self._now()
        ip = (client_ip or "unknown").strip()[:128] or "unknown"
        supplied = extract_token_from_headers(headers)

        throttled_retry_after: Optional[int] = None
        failure_count = 0
        reason = ""

        with self._lock:
            dq = self._prune_ip_locked(ip, now)
            if dq is not None and len(dq) >= self.max_failures:
                retry_after = max(1, int(math.ceil(dq[0] + self.window_seconds - now)))
                self._failures.move_to_end(ip)
                throttled_retry_after = retry_after
                failure_count = len(dq)
            else:
                supplied_bytes = (supplied or "").encode("utf-8")
                expected_bytes = self._token.encode("utf-8")
                if supplied is not None and hmac.compare_digest(supplied_bytes, expected_bytes):
                    self._failures.pop(ip, None)
                    return AuthResult(ok=True, status_code=200, error="", retry_after=None)

                if ip not in self._failures:
                    self._evict_if_full_locked(now)
                    self._failures[ip] = deque()
                self._failures[ip].append(now)
                self._failures.move_to_end(ip)
                failure_count = len(self._failures[ip])
                reason = "missing token" if not supplied else "invalid token"

        if throttled_retry_after is not None:
            logger.warning(
                "Dashboard auth throttled for client_ip=%s (failures=%d, retry_after=%ds)",
                ip,
                failure_count,
                throttled_retry_after,
            )
            return AuthResult(
                ok=False,
                status_code=429,
                error="Too many failed authentication attempts; retry later.",
                retry_after=throttled_retry_after,
            )

        logger.warning(
            "Dashboard auth rejected (%s) for client_ip=%s (failures=%d/%d)",
            reason,
            ip,
            failure_count,
            self.max_failures,
        )
        return AuthResult(
            ok=False,
            status_code=401,
            error="Unauthorized: invalid or missing dashboard token.",
            retry_after=None,
        )

    def scrub_error_message(self, exc_or_msg: Any) -> str:
        """Scrub an error message while also redacting this manager's active token."""
        return scrub_error_message(exc_or_msg, extra_secrets=(self._token,))


def print_dashboard_token(
    token_file: Optional[Union[str, Path]] = None,
    stream: Optional[TextIO] = None,
) -> str:
    """Resolve and print the active dashboard control token for CLI usage."""
    manager = DashboardAuthManager(token_file=token_file)
    return manager.print_token(stream=stream)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entrypoint for inspecting or printing the dashboard auth token."""
    parser = argparse.ArgumentParser(description="Dashboard authentication helper")
    parser.add_argument(
        "--print-token",
        action="store_true",
        help="Print the active dashboard authentication token to stdout",
    )
    parser.add_argument(
        "--token-file",
        type=str,
        default=None,
        help="Optional path to the persistent dashboard token file",
    )
    args = parser.parse_args(argv)

    manager = DashboardAuthManager(token_file=args.token_file)
    if args.print_token:
        manager.print_token()
    else:
        print(manager.setup_hint())
    return 0


__all__ = [
    "AuthResult",
    "DEFAULT_FAILURE_WINDOW_SECONDS",
    "DEFAULT_MAX_FAILURES",
    "DEFAULT_MAX_TRACKED_IPS",
    "DEFAULT_TOKEN_FILE",
    "DashboardAuthManager",
    "MIN_TOKEN_LENGTH",
    "SENSITIVE_CONFIG_KEYS",
    "extract_token_from_headers",
    "is_sensitive_key",
    "is_valid_host_header",
    "is_valid_origin_header",
    "json_safe_for_inline_script",
    "main",
    "print_dashboard_token",
    "scrub_error_message",
]


if __name__ == "__main__":
    raise SystemExit(main())
