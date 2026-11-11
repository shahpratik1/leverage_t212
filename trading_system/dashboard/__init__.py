"""Embedded local trading signals dashboard and control gateway package."""

from __future__ import annotations

from typing import Any

_SERVER_EXPORTS = {
    "build_dashboard_data",
    "fetch_live_broker_notifications",
    "generate_html_dashboard",
    "load_active_signals",
    "load_recent_history",
    "load_recent_notifications",
    "make_dashboard_handler",
    "SSEBroadcaster",
    "start_local_server",
    "start_local_server_thread",
}

_AUTH_EXPORTS = {
    "DashboardAuthManager",
}

__all__ = sorted(_SERVER_EXPORTS | _AUTH_EXPORTS)


def __getattr__(name: str) -> Any:
    """Lazily resolve dashboard package exports without triggering eager imports."""
    if name in _SERVER_EXPORTS:
        from trading_system.dashboard import server

        return getattr(server, name)
    if name in _AUTH_EXPORTS:
        from trading_system.dashboard import auth

        return getattr(auth, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
