"""Orchestrator — 3x Leveraged Long & Short Quantitative Engine.

Main Execution Pipeline:
1. Data Ingestion: Ingests OHLCV for liquid 3x underlying assets (Indices, Tech Giants, Commodities).
2. System 1 Fast Screening: Laya NLI screener detects Bullish (3x Long) and Bearish (3x Short) momentum,
   rejecting choppy consolidation (ADX < 20) to prevent volatility decay.
3. System 2 Deliberation: Multi-agent debate & Risk Manager compute position sizing, ATR Stop Loss,
   multi-stage Take Profits (TP1, TP2, TP3), and initial Trailing Stop Loss.
4. Automated Execution & Notifications: Places market entry orders on Trading 212 and dispatches
   real-time multi-channel alerts (Console, Desktop, Telegram, Discord, JSONL) for:
   BUY, SELL, TP1, TP2, TP3, SL, and TSL.
5. Continuous Position Monitor: Tracks live positions, triggers partial profit trims, ratchets TSL,
   and closes stopped-out trades.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import logging
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from trading_system.config import settings
from trading_system.data.leveraged_universe import LeveragedUniverse
from trading_system.data.trading212_client import (
    AccountCash,
    MarketDataProvider,
    Position,
    Trading212Client,
    Trading212Error,
)
from trading_system.execution.position_monitor import PositionMonitor
from trading_system.notifications.notifier import Notifier
from trading_system.screener.laya_screener import LayaScreener, ScreenerResult
from trading_system.agents.technical_agent import TechnicalAnalyst
from trading_system.agents.fundamental_agent import FundamentalAnalyst
from trading_system.agents.debate_engine import DebateEngine
from trading_system.agents.antigravity_agent import AntigravityTradingAgent
from trading_system.agents.risk_manager import DecisionCard, RiskManager

logger = logging.getLogger(__name__)

# Fallback watchlist of ordinary equities if 3x mode is disabled
DEFAULT_UNIVERSE = [
    "AAPL", "MSFT", "AMZN", "NVDA", "GOOGL", "META", "TSLA", "BRK-B",
    "UNH", "LLY", "JPM", "XOM", "JNJ", "V", "PG", "AVGO", "MA", "HD",
    "MRK", "CVX", "ABBV", "COST", "PEP", "KO", "ADBE", "WMT", "MCD",
    "CRM", "CSCO", "TMO", "ACN", "ABT", "LIN", "BAC", "NFLX", "PFE",
    "AMD", "DHR", "DIS", "ORCL", "CMCSA", "PM", "INTC", "VZ", "WFC",
]


class Orchestrator:
    """Master controller for 3x Long/Short Quantitative Trading Engine."""

    def __init__(
        self,
        dry_run: bool = False,
        notifier: Optional[Notifier] = None,
        position_monitor: Optional[PositionMonitor] = None,
        trades_log: Optional[Path | str] = None,
        enable_dashboard: Optional[bool] = None,
        dashboard_port: Optional[int] = None,
        open_browser: bool = False,
    ) -> None:
        settings.validate()
        self.dry_run = dry_run

        # ── 1. Trading 212 API Client ─────────────────────────────
        self.t212 = Trading212Client(
            api_key=settings.TRADING212_API_KEY,
            api_secret=settings.TRADING212_API_SECRET,
            base_url=settings.api_base_url,
        )

        # ── 2. Universe Manager ───────────────────────────────────
        self.leveraged_universe = LeveragedUniverse()
        if settings.USE_LEVERAGED_3X:
            self.universe: List[str] = (
                settings.WATCHLIST
                if settings.WATCHLIST
                else self.leveraged_universe.get_all_underlyings()
            )
        else:
            self.universe = (
                settings.WATCHLIST
                if settings.WATCHLIST
                else DEFAULT_UNIVERSE[: settings.UNIVERSE_SIZE]
            )

        # ── 3. Notification Engine ────────────────────────────────
        self.notifier = notifier or Notifier(
            telegram_bot_token=settings.TELEGRAM_BOT_TOKEN,
            telegram_chat_id=settings.TELEGRAM_CHAT_ID,
            discord_webhook_url=settings.DISCORD_WEBHOOK_URL,
            log_file=settings.NOTIFICATIONS_LOG_PATH,
            enable_desktop=settings.ENABLE_DESKTOP_NOTIFICATIONS,
        )

        # ── 4. System 1: Fast Screener ────────────────────────────
        self.screener = LayaScreener(
            min_setup_score=settings.SCREENER_MIN_SETUP_SCORE,
            max_catalyst_risk=settings.SCREENER_MAX_CATALYST_RISK,
            top_n=settings.SCREENER_TOP_N,
        )

        # ── 5. System 2: Deliberative Multi-Agent Stack ──────────
        self.technical_analyst = TechnicalAnalyst(
            openai_api_key=settings.OPENAI_API_KEY,
            model=settings.LLM_DEEP_MODEL,
        )
        self.fundamental_analyst = FundamentalAnalyst(
            openai_api_key=settings.OPENAI_API_KEY,
            model=settings.LLM_DEEP_MODEL,
        )
        self.debate_engine = DebateEngine(
            openai_api_key=settings.OPENAI_API_KEY,
            model=settings.LLM_DEEP_MODEL,
            quick_model=settings.LLM_QUICK_MODEL,
            debate_rounds=3,
            speculative_routing=settings.ENABLE_SPECULATIVE_ROUTING,
            speculative_threshold=settings.SPECULATIVE_CONFIDENCE_THRESHOLD,
            gemini_api_key=settings.GEMINI_API_KEY,
        )
        self.parallel_ingestion_workers = settings.PARALLEL_INGESTION_WORKERS
        self.parallel_deliberation_workers = settings.PARALLEL_DELIBERATION_WORKERS
        self.risk_manager = RiskManager(
            max_risk_per_trade=settings.MAX_RISK_PER_TRADE,
            max_position_pct=settings.MAX_POSITION_PCT,
            atr_sl_multiplier=settings.ATR_SL_MULTIPLIER,
            min_sl_pct=settings.MIN_SL_PCT,
            tp1_r=settings.TP1_R_MULTIPLE,
            tp2_r=settings.TP2_R_MULTIPLE,
            tp3_r=settings.TP3_R_MULTIPLE,
            tsl_atr_multiplier=settings.TSL_ATR_MULTIPLIER,
        )
        self.antigravity_agent = AntigravityTradingAgent(
            t212_client=self.t212,
            notifier=self.notifier,
            risk_manager=self.risk_manager,
            gemini_api_key=settings.GEMINI_API_KEY,
            model=settings.AGY_MODEL,
        )

        # ── 6. Active Position & Lifecycle Monitor ───────────────
        self.position_monitor = position_monitor or PositionMonitor(
            t212_client=self.t212,
            notifier=self.notifier,
            risk_manager=self.risk_manager,
            state_file=settings.ACTIVE_TRADES_PATH,
            dry_run=self.dry_run,
        )
        self.session_start_equity: Optional[float] = None
        self._last_reconciliation_time: float = 0.0
        self._scan_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="universe_scan")
        self._scan_future: Optional[concurrent.futures.Future] = None
        # Synchronize broker-native GTC Stop Loss / Limit orders for active positions
        if not self.dry_run:
            try:
                self.position_monitor.reconcile_with_broker(adopt_untracked=True)
                self.position_monitor.sync_broker_orders()
                self.position_monitor.apply_daily_rollover_stops()
            except Exception as exc:
                logger.warning("Initial broker stop sync deferred: %s", exc)

        # ── 7. Audit Logging ──────────────────────────────────────
        self.trades_log = Path(trades_log) if trades_log else Path(settings.TRADES_LOG_PATH)

        # ── 8. Embedded Dashboard Web Server ──────────────────────
        self.dashboard_thread = None
        self.dashboard_server = None
        self.dashboard_url = ""
        self._generate_html_dashboard = None

        if enable_dashboard is None:
            # Auto-detect test runner environment to avoid binding ports during unit tests
            is_testing = "unittest" in sys.modules and any("test" in arg.lower() for arg in sys.argv)
            enable_dashboard = False if is_testing else getattr(settings, "ENABLE_EMBEDDED_DASHBOARD", True)

        self.enable_dashboard = enable_dashboard
        self.dashboard_port = dashboard_port or getattr(settings, "DASHBOARD_PORT", 8888)

        if self.enable_dashboard:
            try:
                from trading_system.dashboard.server import (
                    generate_html_dashboard,
                    start_local_server_thread,
                )
                self._generate_html_dashboard = generate_html_dashboard
                self.dashboard_thread, self.dashboard_server, self.dashboard_url = start_local_server_thread(
                    port=self.dashboard_port,
                    open_browser=open_browser,
                )
            except Exception as exc:
                logger.warning("Failed to start embedded dashboard server: %s", exc)

        logger.info(
            "Orchestrator initialized — 3x Universe: %d assets, Mode: %s (DryRun: %s, Dashboard: %s)",
            len(self.universe),
            "LIVE" if settings.USE_LIVE else "DEMO",
            self.dry_run,
            self.dashboard_url if self.enable_dashboard and self.dashboard_url else "DISABLED",
        )

    def _update_dashboard(self) -> None:
        """Regenerate signals_dashboard.html if embedded dashboard is enabled."""
        if self.enable_dashboard and self._generate_html_dashboard:
            try:
                self._generate_html_dashboard("signals_dashboard.html")
            except Exception as exc:
                logger.debug("Dashboard regeneration skipped: %s", exc)

    def fetch_active_trades_metrics(self) -> Dict[str, Dict[str, Any]]:
        """Fetch targeted OHLCV & technical indicators for currently active trade underlyings.

        Runs in fractions of a second because it only fetches for active assets (typically 2-6)
        rather than scanning the full 120-asset universe.
        """
        if not self.position_monitor or not self.position_monitor.positions:
            return {}

        underlyings = list({
            pos.underlying for pos in self.position_monitor.positions.values()
            if not pos.is_closed and pos.underlying
        })
        if not underlyings:
            return {}

        active_metrics: Dict[str, Dict[str, Any]] = {}

        def _fetch_one(symbol: str) -> tuple[str, Optional[Dict[str, Any]]]:
            try:
                daily = MarketDataProvider.get_daily_candles(symbol, period="1y")
                hourly = MarketDataProvider.get_hourly_candles(symbol, period="1mo")
                metrics = MarketDataProvider.compute_indicators(daily, hourly, symbol=symbol)
                return symbol, metrics
            except Exception as exc:
                logger.debug("Active trade metric fetch notice for %s: %s", symbol, exc)
                return symbol, None

        workers = min(len(underlyings), 4)
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_sym = {executor.submit(_fetch_one, sym): sym for sym in underlyings}
            for future in concurrent.futures.as_completed(future_to_sym):
                sym, met = future.result()
                if met:
                    active_metrics[sym] = met

        return active_metrics

    # ─────────────────────────────────────────────────────────────
    # Phase 1 — Data Ingestion (Parallel Subagents)
    # ─────────────────────────────────────────────────────────────
    def ingest_universe_data(self, filter_active_sessions: bool = True) -> Dict[str, Dict[str, Any]]:
        """Fetch OHLCV and compute indicators for liquid underlyings in parallel using worker threads.

        Applies session-gated filtering to skip assets whose underlying and ETP exchanges
        are closed, eliminating API quota exhaustion on dormant markets.
        """
        universe_metrics: Dict[str, Dict[str, Any]] = {}
        start_time = time.time()

        # Session-aware filtering
        assets_to_ingest = self.universe
        if filter_active_sessions:
            active_assets = [
                sym for sym in self.universe
                if Trading212Client.is_asset_session_active(sym)
            ]
            dormant_count = len(self.universe) - len(active_assets)
            if dormant_count > 0:
                logger.info(
                    "Session-gated filter: %d / %d assets active; skipped %d closed/dormant assets to preserve API quota",
                    len(active_assets), len(self.universe), dormant_count
                )
            assets_to_ingest = active_assets

        total = len(assets_to_ingest)
        if total == 0:
            logger.info("Session-gated filter: 0 assets active in current session window.")
            return {}

        def _fetch_asset(symbol: str) -> tuple[str, Optional[Dict[str, Any]]]:
            try:
                daily = MarketDataProvider.get_daily_candles(symbol, period="1y")
                hourly = MarketDataProvider.get_hourly_candles(symbol, period="1mo")
                metrics = MarketDataProvider.compute_indicators(daily, hourly, symbol=symbol)
                if metrics:
                    metrics["is_synthetic"] = MarketDataProvider.is_ticker_synthetic(symbol)
                    if metrics.get("earnings_in_days") is None:
                        metrics["earnings_in_days"] = MarketDataProvider.get_days_to_earnings(symbol)
                return symbol, metrics
            except Exception as exc:
                logger.error("Failed to ingest %s: %s", symbol, exc)
                return symbol, None

        logger.info(
            "Ingesting market data for %d assets in parallel across %d subagent workers…",
            total, self.parallel_ingestion_workers
        )

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.parallel_ingestion_workers) as executor:
            future_to_symbol = {executor.submit(_fetch_asset, sym): sym for sym in assets_to_ingest}
            completed = 0
            for future in concurrent.futures.as_completed(future_to_symbol):
                completed += 1
                symbol, metrics = future.result()
                if metrics:
                    universe_metrics[symbol] = metrics
                    logger.debug("[%d/%d] Ingested %s (close=%.2f, RSI=%.1f, ADX=%.1f)",
                                 completed, total, symbol, metrics.get("close", 0.0),
                                 metrics.get("rsi", 0.0), metrics.get("adx", 0.0))
                else:
                    logger.warning("[%d/%d] %s — insufficient candle data", completed, total, symbol)

        elapsed = time.time() - start_time
        logger.info(
            "Ingestion complete: %d / %d assets ready for screening in %.2fs (%.1f assets/sec)",
            len(universe_metrics), total, elapsed, total / max(0.001, elapsed)
        )
        return universe_metrics

    # ─────────────────────────────────────────────────────────────
    # Phase 2 — System 1 Fast Screening (Long & Short)
    # ─────────────────────────────────────────────────────────────
    def screen_candidates(
        self, universe_metrics: Dict[str, Dict[str, Any]]
    ) -> List[ScreenerResult]:
        """Filter universe using Laya NLI router for Bullish and Bearish setups.

        Falls back to :class:`~trading_system.screener.laya_screener.TechnicalFallbackScreener`
        when the Laya NLI engine is not installed or returns zero candidates, ensuring
        the engine always has signals to act on even without the NLI model.
        """
        from trading_system.screener.laya_screener import HAS_LAYA, TechnicalFallbackScreener

        logger.info("Screening %d assets for 3x Long/Short momentum (ADX >= %.1f)…",
                    len(universe_metrics), settings.MIN_ADX_TREND)
        AntigravityTradingAgent.set_active_universe_metrics(universe_metrics)

        if not HAS_LAYA:
            logger.warning(
                "Laya NLI engine not installed — using TechnicalFallbackScreener for signal generation."
            )
            candidates = TechnicalFallbackScreener(top_n=10).scan_universe(universe_metrics)
        else:
            # LayaScreener.scan_universe() already falls back internally when it returns 0.
            candidates = self.screener.scan_universe(universe_metrics)

        # Final safety net: if still no candidates, run fallback directly.
        if not candidates:
            logger.warning(
                "Primary screener returned 0 candidates — running TechnicalFallbackScreener as safety net."
            )
            candidates = TechnicalFallbackScreener(top_n=10).scan_universe(universe_metrics)

        logger.info(
            "System 1 passed %d high-momentum candidates: %s",
            len(candidates),
            [f"{c.ticker} ({c.bias})" for c in candidates],
        )
        return candidates

    # ─────────────────────────────────────────────────────────────
    # Phase 3 — System 2 Multi-Agent Deliberation (Concurrent Subagents)
    # ─────────────────────────────────────────────────────────────
    def deep_analyse(
        self, candidate: ScreenerResult, account_equity: Optional[float] = None
    ) -> Optional[DecisionCard]:
        """Perform multi-agent debate and map candidate to exact 3x Long/Short ETP."""
        underlying = candidate.ticker
        metrics = candidate.metrics
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        direction = "LONG" if candidate.bias == "bullish" else "SHORT"
        strategy_type = getattr(candidate, "strategy_type", "momentum")

        logger.info("═══ System 2 Deliberation for %s (%s | %s) ═══", underlying, direction, strategy_type)

        # ── Pre-Trade Earnings Blackout Check (48h Gate - R2.2) ──
        earnings_raw = metrics.get("earnings_in_days")
        earnings_in_days: Optional[float] = None
        if earnings_raw is not None:
            try:
                earnings_in_days = float(earnings_raw)
            except (ValueError, TypeError):
                earnings_in_days = None

        if earnings_in_days is not None and 0 <= earnings_in_days <= 2.0:
            logger.warning(
                "Pre-trade earnings filter: %s rejected due to impending earnings in %.1f days (<= 48h blackout)",
                underlying, earnings_in_days
            )
            return None

        # ── 1. Map to Leveraged Execution Ticker (3x) ──────
        if settings.USE_LEVERAGED_3X:
            # Policy: if 5x exists, replace it with 3x
            target_leverage = 3
            t212_ticker = self.leveraged_universe.get_execution_ticker(underlying, direction, leverage=target_leverage)
            if not t212_ticker:
                logger.warning("No paired %dx %s ETP found for %s — using standard mapping", target_leverage, direction, underlying)
                instrument = self.t212.get_instrument_by_symbol(underlying)
                t212_ticker = instrument.ticker if instrument else f"{underlying}_US_EQ"
        else:
            instrument = self.t212.get_instrument_by_symbol(underlying)
            t212_ticker = instrument.ticker if instrument else f"{underlying}_US_EQ"

        # ── 2 & 3. Concurrent Technical & Fundamental Subagent Analysis ──
        tech_report = ""
        fund_report = ""

        def _run_technical() -> str:
            try:
                tech = self.technical_analyst.analyze(underlying, metrics, date=today)
                return self.technical_analyst.format_report(tech)
            except Exception as exc:
                logger.error("Technical analysis failed for %s: %s", underlying, exc)
                return f"Technical analysis unavailable ({exc})"

        def _run_fundamental() -> str:
            try:
                fund = self.fundamental_analyst.analyze(underlying, metrics, date=today)
                return self.fundamental_analyst.format_report(fund)
            except Exception as exc:
                logger.error("Fundamental analysis failed for %s: %s", underlying, exc)
                return f"Fundamental analysis unavailable ({exc})"

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            fut_tech = executor.submit(_run_technical)
            fut_fund = executor.submit(_run_fundamental)
            tech_report = fut_tech.result()
            fund_report = fut_fund.result()

        # ── 4. Speculative Model Routing / Bull vs Bear Debate ───
        try:
            outcome = self.debate_engine.run_debate(
                underlying, metrics, tech_report, fund_report, date=today,
                strategy_type=strategy_type,
            )
            logger.info("Debate outcome for %s: consensus=%s, conviction=%.2f",
                        underlying, outcome.consensus_decision, outcome.conviction_score)
        except Exception as exc:
            logger.error("Debate failed for %s: %s", underlying, exc)
            return None

        # ── 5. Risk Management & Sizing ──────────────────────────
        try:
            if account_equity is None or account_equity <= 0:
                account = self.t212.get_account_cash(fallback_equity=settings.DEFAULT_ACCOUNT_EQUITY)
                account_equity = account.total or settings.DEFAULT_ACCOUNT_EQUITY

            card = self.risk_manager.generate_decision_card(
                ticker=underlying,
                trading212_ticker=t212_ticker,
                direction=direction,
                metrics=metrics,
                debate_outcome=outcome,
                account_equity=account_equity,
                strategy_type=strategy_type,
                leverage=3 if settings.USE_LEVERAGED_3X else 1,
            )

            logger.info("Decision card generated:\n%s", self.risk_manager.format_decision_card(card))
            return card

        except Exception as exc:
            logger.error("Risk management failed for %s: %s", underlying, exc)
            return None

    # ─────────────────────────────────────────────────────────────
    # Phase 4 — Execution & Order Placement
    # ─────────────────────────────────────────────────────────────
    def execute_trade(
        self,
        card: DecisionCard,
        atr: float = 0.0,
        available_cash: Optional[float] = None,
        positions: Optional[List[Position]] = None,
    ) -> bool:
        """Execute trade on Trading 212 and dispatch BUY/SELL notification."""
        if card.decision not in ("BUY_ENTRY", "SELL_ENTRY"):
            logger.info("Skipping execution for %s — decision=%s", card.ticker, card.decision)
            self._log_trade(card, executed=False, reason=card.decision)
            return False

        # ── System Halt Check (from Panic Flatten) ───────────────
        if self.position_monitor and (
            getattr(self.position_monitor, "is_system_halted", lambda: False)()
            or getattr(self.position_monitor, "is_halted", False)
        ):
            logger.warning("Execution rejected for %s — system is currently HALTED following panic flatten", card.ticker)
            self._log_trade(card, executed=False, reason="System is HALTED")
            return False

        # ── Pre-trade validation ─────────────────────────────────
        account_total = getattr(settings, "DEFAULT_ACCOUNT_EQUITY", 10000.0)
        if available_cash is not None and positions is not None:
            account_free = available_cash
            current_positions = positions
        else:
            try:
                account = self.t212.get_account_cash(fallback_equity=settings.DEFAULT_ACCOUNT_EQUITY)
                account_free = account.free
                account_total = account.total or account_total
                current_positions = self.t212.get_portfolio()
            except Trading212Error as exc:
                if self.dry_run:
                    logger.warning("Pre-trade broker check unavailable (%s) — using dry-run simulation context", exc)
                    account_free = 10000.0
                    account_total = 10000.0
                    current_positions = []
                else:
                    logger.error("Pre-trade check failed: %s", exc)
                    self._log_trade(card, executed=False, reason=str(exc))
                    return False

        if self.session_start_equity is None or self.session_start_equity <= 0:
            self.session_start_equity = account_total

        # Daily Drawdown Circuit Breaker Gate
        if getattr(settings, "ENFORCE_DAILY_CIRCUIT_BREAKER", True):
            tripped, cb_reason = self.risk_manager.check_circuit_breaker(
                current_equity=account_total,
                session_start_equity=self.session_start_equity,
            )
            if tripped:
                logger.critical("Trade rejected: %s", cb_reason)
                self._log_trade(card, executed=False, reason=cb_reason)
                return False

        # Real-time Bid-Ask Spread Gate
        spread_pct = None
        if getattr(settings, "ENFORCE_SPREAD_GATE", True):
            spread_info = MarketDataProvider.get_bid_ask_spread(card.trading212_ticker)
            spread_pct = spread_info.get("spread_pct")

        valid, reason = self.risk_manager.validate_trade(
            card, account_free, current_positions, spread_pct=spread_pct, account_equity=account_total
        )
        if not valid:
            logger.warning("Trade validation FAILED for %s: %s", card.ticker, reason)
            self._log_trade(card, executed=False, reason=reason)
            return False

        # ── CRITICAL LIVE TRADING SAFETY GATE: Refuse live execution if data was synthetic ──
        is_synthetic = (
            getattr(card, "is_synthetic", False)
            or MarketDataProvider.is_ticker_synthetic(card.ticker)
            or (bool(card.trading212_ticker) and MarketDataProvider.is_ticker_synthetic(card.trading212_ticker))
        )
        if not self.dry_run and is_synthetic:
            logger.critical(
                "CRITICAL LIVE TRADING SAFETY GATE: Market data for %s (%s) was generated synthetically! "
                "Refusing to place real broker order. Verify live data connection or API keys.",
                card.trading212_ticker, card.ticker
            )
            self._log_trade(card, executed=False, reason="Blocked by Safety Interceptor: Market data was synthetic")
            return False
        # ── Pre-Execution Market Hours Gate ──────────────────────
        if not self.dry_run and not self.t212.is_market_open(card.trading212_ticker):
            info = Trading212Client.get_market_session_info(card.trading212_ticker)
            logger.warning(
                "Execution held: Market for %s (%s) is CLOSED right now (%s UTC, current %s UTC). Refusing to submit rejected order.",
                card.trading212_ticker, info["exchange"], info["open_time_utc"], info["current_time_utc"]
            )
            self._log_trade(card, executed=False, reason=f"Market closed ({info['exchange']} session: {info['open_time_utc']}-{info['close_time_utc']} UTC)")
            return False

        # ── Execute Entry Order on Trading 212 ───────────────────
        order_id = "DRY_RUN_ORDER"
        fill_price: Optional[float] = None
        if not self.dry_run:
            try:
                order_qty = card.position_size_shares
                try:
                    inst = self.t212.get_instrument_by_symbol(card.trading212_ticker)
                    if inst and inst.min_trade_quantity and order_qty < inst.min_trade_quantity:
                        est_notional = inst.min_trade_quantity * (card.entry_price or 1.0)
                        if est_notional <= (account_total * 0.25):
                            logger.info(
                                "Adjusting %s order quantity from %.3f to broker minimum %.3f",
                                card.trading212_ticker, order_qty, inst.min_trade_quantity,
                            )
                            order_qty = inst.min_trade_quantity
                            card.position_size_shares = order_qty
                except Exception as inst_exc:
                    logger.debug("Could not verify instrument min quantity for %s: %s", card.trading212_ticker, inst_exc)

                logger.info(
                    "Placing Trading 212 order: %s (%s) — %.3f shares @ ≈$%.2f",
                    card.trading212_ticker, card.direction, order_qty, card.entry_price
                )
                entry_order = self.t212.place_market_order(
                    ticker=card.trading212_ticker,
                    quantity=order_qty,
                )
                order_id = entry_order.id
                logger.info("Order executed successfully: id=%s", order_id)
                if entry_order.fill_price > 0:
                    fill_price = entry_order.fill_price
                else:
                    try:
                        time.sleep(1.0)
                        positions_fresh = self.t212.get_portfolio(force_refresh=True)
                        for p in positions_fresh:
                            if p.ticker == card.trading212_ticker and p.average_price > 0:
                                fill_price = p.average_price
                                break
                    except Exception as port_exc:
                        logger.debug("Could not retrieve post-order portfolio fill price: %s", port_exc)
            except Trading212Error as exc:
                logger.error("Execution failed for %s: %s", card.trading212_ticker, exc)
                self._log_trade(card, executed=False, reason=str(exc))
                return False
        else:
            logger.info("[DRY-RUN] Simulated execution for %s: %.3f shares @ $%.2f",
                        card.trading212_ticker, card.position_size_shares, card.entry_price)

        # ── Register with Active Position Monitor ────────────────
        pos = self.position_monitor.register_new_trade(
            card,
            atr=atr,
            earnings_in_days=card.earnings_in_days,
            fill_price=fill_price,
        )

        # ── Place Native Broker GTC Stop & Limit Orders on Trading 212 ────
        self.position_monitor.place_broker_stop(pos)
        self.position_monitor.place_broker_tp_limit(pos)

        # ── Dispatch Real-Time Notifications ─────────────────────
        notif_price = pos.entry_price
        notif_sl = pos.stop_loss
        notif_tp1 = pos.tp1
        notif_tp2 = pos.tp2
        notif_tp3 = pos.tp3
        if card.direction == "LONG":
            self.notifier.notify_buy(
                ticker=card.trading212_ticker,
                underlying=card.ticker,
                price=notif_price,
                quantity=card.position_size_shares,
                sl=notif_sl,
                tp1=notif_tp1,
                tp2=notif_tp2,
                tp3=notif_tp3,
                rationale=card.rationale,
            )
        else:
            self.notifier.notify_sell(
                ticker=card.trading212_ticker,
                underlying=card.ticker,
                price=notif_price,
                quantity=card.position_size_shares,
                sl=notif_sl,
                tp1=notif_tp1,
                tp2=notif_tp2,
                tp3=notif_tp3,
                is_short_entry=True,
                rationale=card.rationale,
            )

        self._log_trade(card, executed=True, reason="Entry order placed", entry_order_id=order_id)
        return True

    # ─────────────────────────────────────────────────────────────
    # Audit Log
    # ─────────────────────────────────────────────────────────────
    def _log_trade(
        self,
        card: DecisionCard,
        executed: bool,
        reason: str,
        entry_order_id: Optional[str] = None,
    ) -> None:
        """Persist trade record to JSONL audit log."""
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "executed": executed,
            "reason": reason,
            "entry_order_id": entry_order_id,
            **asdict(card),
        }
        try:
            with open(self.trades_log, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")
        except Exception as exc:
            logger.error("Failed to write audit log: %s", exc)

    def execute_trades_batch(
        self, candidate_cards: List[tuple[ScreenerResult, DecisionCard]]
    ) -> List[DecisionCard]:
        """Execute multiple candidate signals in synchronized sequence with capital pacing.

        Ensures:
        1. API calls are paced (>= 1.5s) to adhere strictly to broker mutation limits.
        2. High-conviction signals are prioritized first.
        3. Free cash and position allocations are tracked incrementally so concurrent signals
           do not over-commit account capital or trigger redundant broker GET requests.
        4. Cross-process flag warns background monitor daemons to yield bandwidth.
        """
        if not candidate_cards:
            return []

        # 1. Announce active execution to cross-process daemons
        Trading212Client.set_trade_active(True)

        try:
            # 2. Separate actionable entry signals from holds/skips
            actionable: List[tuple[ScreenerResult, DecisionCard]] = []
            for cand, card in candidate_cards:
                if card.decision in ("BUY_ENTRY", "SELL_ENTRY"):
                    actionable.append((cand, card))
                else:
                    self._log_trade(card, executed=False, reason=card.decision)

            if not actionable:
                return []

            # 3. Sort by conviction score descending (highest conviction gets first capital allocation)
            actionable.sort(key=lambda item: item[1].conviction_score, reverse=True)

            # 4. Fetch baseline cash and positions once for the entire batch
            try:
                account = self.t212.get_account_cash(fallback_equity=settings.DEFAULT_ACCOUNT_EQUITY)
                running_free_cash = account.free
            except Exception:
                running_free_cash = 10000.0

            running_positions = []
            try:
                running_positions = list(self.t212.get_portfolio())
            except Exception as exc:
                if not self.dry_run:
                    logger.error("Batch pre-trade broker check failed: %s", exc)
                    for _, card in actionable:
                        self._log_trade(card, executed=False, reason=f"Broker pre-check failed: {exc}")
                    return []
                logger.debug("Broker portfolio query notice in batch execution: %s", exc)

            # Ensure all local active positions from position_monitor are included in risk checks
            held_tickers = {getattr(p, "ticker", "") for p in running_positions}
            for p in self.position_monitor.positions.values():
                if not p.is_closed and p.remaining_quantity > 0 and p.ticker not in held_tickers:
                    running_positions.append(Position(
                        ticker=p.ticker,
                        quantity=p.remaining_quantity,
                        average_price=p.entry_price,
                        current_price=getattr(p, "highest_price", p.entry_price),
                        ppl=0.0,
                        max_buy=0.0,
                        max_sell=0.0,
                    ))
                    held_tickers.add(p.ticker)

            executed_cards: List[DecisionCard] = []
            is_test_mode = getattr(self.t212, "rate_limit_delay", 1.25) < 0.5
            pacing_delay = self.t212.rate_limit_delay if is_test_mode else 1.5

            for i, (cand, card) in enumerate(actionable):
                atr = cand.metrics.get("atr", card.entry_price * 0.02)

                # Validate against running capital allocation
                valid, reason = self.risk_manager.validate_trade(card, running_free_cash, running_positions)
                if not valid:
                    logger.warning(
                        "Batch trade validation FAILED for %s (conviction=%.2f): %s",
                        card.ticker, card.conviction_score, reason
                    )
                    self._log_trade(card, executed=False, reason=reason)
                    continue

                # Execute order with synchronized running cash and positions
                success = self.execute_trade(
                    card,
                    atr=atr,
                    available_cash=running_free_cash,
                    positions=running_positions,
                )
                if success:
                    executed_cards.append(card)
                    running_free_cash = max(0.0, running_free_cash - card.position_notional)
                    running_positions.append(Position(
                        ticker=card.trading212_ticker,
                        quantity=card.position_size_shares,
                        average_price=card.entry_price,
                        current_price=card.entry_price,
                        ppl=0.0,
                        max_buy=0.0,
                        max_sell=0.0,
                    ))

                    # Pace consecutive orders by broker quota minimum if more orders remain
                    if i < len(actionable) - 1:
                        time.sleep(pacing_delay)

            return executed_cards

        finally:
            Trading212Client.set_trade_active(False)

    # ─────────────────────────────────────────────────────────────
    # Complete Single Cycle & Deliberation Pipeline
    # ─────────────────────────────────────────────────────────────
    def _scan_and_deliberate(self) -> List[tuple[ScreenerResult, DecisionCard]]:
        """Ingest universe data, screen candidates, and deliberate via multi-agent stack.

        Returns candidate cards ready for batch execution. Can run safely in background.
        """
        if self.position_monitor and getattr(self.position_monitor, "is_system_halted", lambda: False)():
            logger.warning("Scan aborted: System is currently HALTED following emergency panic flatten.")
            return []

        # Step 1: Ingestion
        metrics_dict = self.ingest_universe_data()
        if not metrics_dict:
            logger.warning("No market data ingested — scan aborted")
            return []

        # Step 2: Screening
        candidates = self.screen_candidates(metrics_dict)
        if not candidates:
            logger.info("No candidates passed 3x momentum filter this cycle.")
            return []

        # Step 3: Parallel Deliberation across Candidates
        candidates_to_process = candidates[:settings.SCREENER_TOP_N]
        candidate_cards: List[tuple[ScreenerResult, DecisionCard]] = []
        n_delib_workers = max(1, min(len(candidates_to_process), self.parallel_deliberation_workers))

        logger.info("Deliberating on %d candidate(s) in parallel across %d subagent workers…",
                    len(candidates_to_process), n_delib_workers)

        try:
            account = self.t212.get_account_cash(fallback_equity=settings.DEFAULT_ACCOUNT_EQUITY)
            baseline_equity = account.total or settings.DEFAULT_ACCOUNT_EQUITY
        except Exception as exc:
            logger.warning("Baseline equity query failed (%s); using default $%.2f", exc, settings.DEFAULT_ACCOUNT_EQUITY)
            baseline_equity = settings.DEFAULT_ACCOUNT_EQUITY

        with concurrent.futures.ThreadPoolExecutor(max_workers=n_delib_workers) as executor:
            future_to_cand = {
                executor.submit(self.deep_analyse, cand, baseline_equity): cand
                for cand in candidates_to_process
            }
            for future in concurrent.futures.as_completed(future_to_cand):
                cand = future_to_cand[future]
                try:
                    card = future.result()
                    if card:
                        candidate_cards.append((cand, card))
                except Exception as exc:
                    logger.error("Parallel deliberation failed for %s: %s", cand.ticker, exc)

        return candidate_cards

    def run_cycle(self) -> List[DecisionCard]:
        """Execute one complete Scan → Filter → Debate → Execute cycle."""
        logger.info("━━━ Starting 3x Quantitative Cycle at %s ━━━",
                    datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"))

        # Step 0: Broker Portfolio Reconciliation & Position Adoption
        try:
            recon = self.position_monitor.reconcile_with_broker(adopt_untracked=True)
            if recon.get("adopted_from_broker"):
                logger.info(
                    "Synchronized with Trading 212 account: Adopted %d active broker positions: %s",
                    len(recon["adopted_from_broker"]), recon["adopted_from_broker"],
                )
            if recon.get("closed_externally"):
                logger.info(
                    "Reconciliation closed %d positions not found on broker: %s",
                    len(recon["closed_externally"]), recon["closed_externally"],
                )
            if not self.dry_run:
                self.position_monitor.apply_daily_rollover_stops()
                self.position_monitor.sync_broker_orders()
        except Exception as recon_exc:
            logger.debug("Initial broker reconciliation notice: %s", recon_exc)

        candidate_cards = self._scan_and_deliberate()
        if not candidate_cards:
            return []

        # Step 4: Execution & Logging via Synchronized Batch Coordinator
        executed_cards = self.execute_trades_batch(candidate_cards)
        self._update_dashboard()
        logger.info("Cycle finished. %d trades executed.", len(executed_cards))
        return executed_cards

    # ─────────────────────────────────────────────────────────────
    # Continuous Loop with Active-Trade Driven Architecture
    # ─────────────────────────────────────────────────────────────
    def run_loop(self, scan_interval_minutes: int = 60, monitor_interval_seconds: int = 30) -> None:
        """Run continuous engine loop.

        Prioritizes Active Trade Management on high-frequency intervals:
        - Reads Trading 212 active trades directly as primary ground truth.
        - Calculates ATR/SL/TP1/TP2/TP3 and sets native broker orders (stops & limits).
        - Fetches targeted market metrics for active trades (<0.5s) and executes if signals trigger.
        - Decouples 120-asset universe scan into background worker so active trades are never starved.
        """
        logger.info(
            "Starting continuous Active-Trade engine (Scan Interval: %dm, Monitor Interval: %ds). Press Ctrl+C to stop.",
            scan_interval_minutes, monitor_interval_seconds
        )
        last_scan_time = 0.0

        while True:
            try:
                now = time.time()
                now_utc = datetime.now(timezone.utc)

                # Daily Circuit Breaker Calendar Date Rollover Reset
                today_date = now_utc.strftime("%Y-%m-%d")
                if getattr(self, "_last_session_cb_date", "") != today_date:
                    try:
                        acc = self.t212.get_account_cash(fallback_equity=settings.DEFAULT_ACCOUNT_EQUITY)
                        new_eq = acc.total or settings.DEFAULT_ACCOUNT_EQUITY
                        self.session_start_equity = new_eq
                        if self.risk_manager:
                            self.risk_manager.reset_circuit_breaker(new_session_equity=new_eq)
                        self._last_session_cb_date = today_date
                        logger.info("🌅 New trading session (%s): Reset daily circuit breaker. Baseline equity: $%.2f", today_date, new_eq)
                    except Exception as cb_reset_exc:
                        logger.warning("Could not reset session circuit breaker on rollover: %s", cb_reset_exc)

                active_tickers = list(self.position_monitor.positions.keys()) if self.position_monitor.positions else ["QQQ3l_EQ", "SPY_US_EQ"]
                market_open = self.t212.is_any_market_open(active_tickers, current_time=now_utc)

                if not market_open and not self.dry_run:
                    if now - getattr(self, "_last_closed_loop_log", 0.0) >= 1800.0:
                        logger.info(
                            "🌙 Markets CLOSED (LSE 08:00-16:30 UTC, US 14:30-21:00 UTC). "
                            "Suspending universe scan and position polling to conserve API quota."
                        )
                        self._last_closed_loop_log = now

                    # Run periodic reconciliation even during off-hours to catch manual app trades or external closes
                    if now - getattr(self, "_last_reconciliation_time", 0.0) >= 300.0:
                        try:
                            rec = self.position_monitor.reconcile_with_broker()
                            if rec.get("closed_externally") or rec.get("quantity_synced") or rec.get("adopted_from_broker"):
                                logger.info("Off-hours broker reconciliation sync: %s", rec)
                                self._update_dashboard()
                        except Exception as e:
                            logger.debug("Off-hours reconciliation notice: %s", e)
                        self._last_reconciliation_time = now

                    time.sleep(min(300, monitor_interval_seconds * 10))
                    continue

                # ── PRIORITY 1: High-Frequency Active Position Management ──
                # 1. Reconcile with Trading 212 & adopt untracked positions
                if now - getattr(self, "_last_reconciliation_time", 0.0) >= getattr(settings, "RECONCILIATION_INTERVAL_SECONDS", 30):
                    rec = self.position_monitor.reconcile_with_broker(adopt_untracked=True)
                    if rec.get("closed_externally") or rec.get("quantity_synced") or rec.get("adopted_from_broker"):
                        logger.info("Active trade reconciliation: %s", rec)
                        self._update_dashboard()
                    self._last_reconciliation_time = now

                # 2. Sync native broker orders ("set the price" on broker server)
                if not self.dry_run:
                    self.position_monitor.sync_broker_orders()

                # 3. Active Position Price & Technical Signal Monitoring
                if self.position_monitor.positions:
                    logger.debug("Active management for %d positions…", len(self.position_monitor.positions))
                    current_prices = {}
                    try:
                        t212_positions = self.t212.get_portfolio()
                        for p in t212_positions:
                            p_price = p.current_price or p.average_price or 0.0
                            if p_price > 0:
                                current_prices[p.ticker] = p_price
                    except Exception as e:
                        logger.debug("T212 portfolio price fetch notice: %s", e)

                    # Targeted indicators for active trade underlyings (<0.5s)
                    active_metrics = self.fetch_active_trades_metrics()
                    for und, met in active_metrics.items():
                        if met.get("close"):
                            current_prices[und] = met["close"]

                    for ticker, pos in self.position_monitor.positions.items():
                        if ticker not in current_prices:
                            pos_price = getattr(pos, "current_price", 0.0) or getattr(pos, "entry_price", 0.0) or 0.0
                            if pos_price > 0:
                                current_prices[ticker] = pos_price

                    # Price-target tick (TP1/TP2/TP3/SL/TSL)
                    events = self.position_monitor.monitor_tick(current_prices, current_time=now_utc)

                    # Signal-driven execution on active trades
                    signal_events = self.position_monitor.evaluate_active_trade_signals(active_metrics)
                    if signal_events:
                        events.extend(signal_events)

                    if events:
                        self._update_dashboard()

                # ── PRIORITY 2: Decoupled Non-Blocking Universe Scan ──
                # Harvest completed background scan
                if self._scan_future is not None:
                    if self._scan_future.done():
                        try:
                            candidate_cards = self._scan_future.result()
                            if candidate_cards:
                                executed = self.execute_trades_batch(candidate_cards)
                                if executed:
                                    logger.info("Background universe scan batch finished: %d trades executed.", len(executed))
                                    self._update_dashboard()
                        except Exception as scan_err:
                            logger.error("Background universe scan error: %s", scan_err)
                        finally:
                            self._scan_future = None
                            last_scan_time = time.time()
                else:
                    # Launch background scan if interval elapsed or at startup
                    if now - last_scan_time >= scan_interval_minutes * 60 or last_scan_time == 0.0:
                        logger.info("Triggering non-blocking background 3x universe scan…")
                        self._scan_future = self._scan_executor.submit(self._scan_and_deliberate)
                        if last_scan_time == 0.0:
                            last_scan_time = time.time()

                time.sleep(monitor_interval_seconds)

            except KeyboardInterrupt:
                logger.info("Keyboard interrupt received — stopping loop.")
                break
            except Exception as exc:
                logger.error("Loop iteration error: %s", exc, exc_info=True)
                time.sleep(10)

    def shutdown(self) -> None:
        """Clean up resources."""
        logger.info("Shutting down trading engine…")
        if getattr(self, "dashboard_server", None):
            try:
                logger.info("Stopping embedded dashboard server…")
                self.dashboard_server.shutdown()
                self.dashboard_server.server_close()
            except Exception as exc:
                logger.debug("Dashboard server shutdown notice: %s", exc)
        if getattr(self, "_scan_executor", None):
            try:
                self._scan_executor.shutdown(wait=False)
            except Exception as exc:
                logger.debug("Scan executor shutdown notice: %s", exc)
        self.t212.close()
        logger.info("Shutdown complete.")


# ─────────────────────────────────────────────────────────────────
# CLI Entry Point
# ─────────────────────────────────────────────────────────────────
def main() -> None:
    """CLI runner with diagnostic and testing commands."""
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
        format="%(asctime)s | %(name)-28s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    parser = argparse.ArgumentParser(description="3x Leveraged Long/Short Quantitative Trading Engine")
    parser.add_argument("--once", action="store_true", help="Run a single scan & execution cycle and exit")
    parser.add_argument("--interval", type=int, default=60, help="Minutes between full universe scans (default: 60)")
    parser.add_argument("--monitor-interval", type=int, default=30, help="Seconds between position monitor ticks (default: 30)")
    parser.add_argument("--dry-run", action="store_true", help="Run in paper-trade mode without real order execution")
    parser.add_argument("--no-dashboard", action="store_true", help="Disable the embedded local signals dashboard web server")
    parser.add_argument("--dashboard-port", type=int, default=settings.DASHBOARD_PORT, help=f"Port for embedded dashboard server (default: {settings.DASHBOARD_PORT})")
    parser.add_argument("--open-browser", action="store_true", help="Automatically open dashboard in default browser on startup")
    parser.add_argument("--test-notifications", action="store_true", help="Dispatch test alerts for all 7 event types (BUY, SELL, TP1, TP2, TP3, SL, TSL)")
    parser.add_argument("--agentic", action="store_true", help="Enable Google Antigravity autonomous multi-agent reasoning system")
    parser.add_argument("--backtest", action="store_true", help="Run historical strategy backtest simulator and benchmark report")
    parser.add_argument("--backtest-symbol", type=str, default="QQQ", help="Underlying asset symbol to backtest (default: QQQ)")
    parser.add_argument("--backtest-period", type=str, default="1y", help="Historical backtest period (default: 1y)")
    parser.add_argument("--backtest-strategy", type=str, choices=["3rsi", "momentum", "both"], default="both", help="Strategy filter to backtest (default: both)")
    args = parser.parse_args()

    # Historical backtesting route
    if args.backtest:
        from trading_system.backtest.engine import BacktestEngine, generate_synthetic_candles, print_backtest_report
        candles = None
        if not args.dry_run:
            try:
                import yfinance as yf
                ticker = yf.Ticker(args.backtest_symbol)
                hist = ticker.history(period=args.backtest_period)
                if not hist.empty:
                    candles = []
                    for dt, row in hist.iterrows():
                        candles.append({
                            "date": dt.strftime("%Y-%m-%d"),
                            "open": float(row["Open"]),
                            "high": float(row["High"]),
                            "low": float(row["Low"]),
                            "close": float(row["Close"]),
                            "volume": float(row["Volume"]),
                        })
            except Exception as e:
                logger.warning("Could not fetch yfinance data for %s: %s", args.backtest_symbol, e)
        if not candles:
            candles = generate_synthetic_candles(args.backtest_symbol, count=160)

        engine_bt = BacktestEngine(
            initial_capital=settings.DEFAULT_ACCOUNT_EQUITY,
            leverage=3,
            max_risk_per_trade=settings.MAX_RISK_PER_TRADE,
            max_position_pct=settings.MAX_POSITION_PCT,
            target_vol=settings.TARGET_ANNUAL_VOLATILITY,
            max_hold_days=settings.MR_MAX_HOLD_DAYS,
        )
        res = engine_bt.run(symbol=args.backtest_symbol, candles=candles, strategy_filter=args.backtest_strategy)
        print_backtest_report(res)
        return

    engine = Orchestrator(
        dry_run=args.dry_run,
        enable_dashboard=not args.no_dashboard,
        dashboard_port=args.dashboard_port,
        open_browser=args.open_browser,
    )

    # Test all 7 notification formats if requested
    if args.test_notifications:
        print("\n🧪 Testing all 7 notification formats (Console, Desktop, Webhooks)...\n")
        engine.notifier.notify_buy(
            ticker="QQQ3l_EQ", underlying="QQQ", price=150.25, quantity=10.0,
            sl=145.00, tp1=155.50, tp2=160.75, tp3=166.00,
            rationale="High-conviction 3x Bullish breakout (ADX=29.4, 20 EMA > 50 EMA)"
        )
        time.sleep(0.5)
        engine.notifier.notify_tp1(
            ticker="QQQ3l_EQ", underlying="QQQ", direction="LONG", price=155.50,
            quantity_sold=3.33, pnl=17.48, pnl_pct=3.49, new_sl=150.25
        )
        time.sleep(0.5)
        engine.notifier.notify_tp2(
            ticker="QQQ3l_EQ", underlying="QQQ", direction="LONG", price=160.75,
            quantity_sold=3.33, pnl=34.97, pnl_pct=6.99, tsl=155.50
        )
        time.sleep(0.5)
        engine.notifier.notify_tsl(
            ticker="QQQ3l_EQ", underlying="QQQ", direction="LONG", current_price=163.00,
            new_tsl=158.20, locked_in_pnl_pct=5.29
        )
        time.sleep(0.5)
        engine.notifier.notify_tp3(
            ticker="QQQ3l_EQ", underlying="QQQ", direction="LONG", price=166.00,
            quantity_sold=3.34, pnl=52.61, pnl_pct=10.48
        )
        time.sleep(0.5)
        engine.notifier.notify_sell(
            ticker="QQQSl_EQ", underlying="QQQ", price=45.10, quantity=25.0,
            sl=47.50, tp1=42.70, tp2=40.30, tp3=37.90, is_short_entry=True,
            rationale="3x Bearish breakdown momentum detected (ADX=31.2, 20 EMA < 50 EMA)"
        )
        time.sleep(0.5)
        engine.notifier.notify_sl(
            ticker="QQQSl_EQ", underlying="QQQ", direction="SHORT", price=47.50,
            quantity=25.0, loss_amount=60.00, loss_pct=5.32
        )
        print("\n✅ All 7 notification event tests completed successfully!\n")
        return

    try:
        if args.once:
            engine.run_cycle()
        else:
            engine.run_loop(
                scan_interval_minutes=args.interval,
                monitor_interval_seconds=args.monitor_interval,
            )
    finally:
        engine.shutdown()


if __name__ == "__main__":
    main()
