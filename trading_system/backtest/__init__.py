"""Historical backtesting engine and strategy benchmarking harness."""

def __getattr__(name: str):
    if name in ("BacktestEngine", "BacktestResult", "SimulatedTrade"):
        from trading_system.backtest.engine import BacktestEngine, BacktestResult, SimulatedTrade
        globals()["BacktestEngine"] = BacktestEngine
        globals()["BacktestResult"] = BacktestResult
        globals()["SimulatedTrade"] = SimulatedTrade
        return globals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = ["BacktestEngine", "BacktestResult", "SimulatedTrade"]
