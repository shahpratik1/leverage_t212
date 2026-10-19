"""Dashboard package for 3x Leveraged Trading Engine."""

def __getattr__(name: str):
    if name in ("build_dashboard_data", "generate_html_dashboard", "load_active_signals"):
        from .server import build_dashboard_data, generate_html_dashboard, load_active_signals
        globals()["build_dashboard_data"] = build_dashboard_data
        globals()["generate_html_dashboard"] = generate_html_dashboard
        globals()["load_active_signals"] = load_active_signals
        return globals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = ["generate_html_dashboard", "build_dashboard_data", "load_active_signals"]
