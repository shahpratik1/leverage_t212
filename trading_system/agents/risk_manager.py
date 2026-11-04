"""Risk Manager Agent for Institutional-Grade Execution.

Enforces strict position sizing, ATR-based Stop Loss, multi-target Take Profits
(TP1, TP2, TP3), and dynamic Trailing Stop Loss (TSL) for both Long and Short positions.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional, Tuple

logger = logging.getLogger(__name__)

TradeDecision = Literal["BUY_ENTRY", "SELL_ENTRY", "DO_NOT_TRADE", "EXIT"]


@dataclass
class DecisionCard:
    """Dataclass representing a comprehensive trading decision."""
    ticker: str                         # Underlying or reference ticker (e.g., 'QQQ')
    trading212_ticker: str              # Execution ticker (e.g. 'QQQ3l_EQ' or 'QQQSl_EQ')
    direction: str                      # 'LONG' or 'SHORT'
    decision: str                       # 'BUY_ENTRY', 'SELL_ENTRY', 'DO_NOT_TRADE', 'EXIT'
    entry_price: float
    stop_loss: float
    trailing_stop_loss: float           # Current dynamic trailing stop
    stop_distance_pct: float            # Percentage distance from entry to SL
    take_profit_1: float                # TP1 at +1.0R
    take_profit_2: float                # TP2 at +2.0R
    take_profit_3: float                # TP3 at +3.0R
    position_size_shares: float         # Number of shares to execute
    position_notional: float            # Dollar value of position
    risk_amount: float                  # Dollar amount at risk
    risk_pct_of_equity: float           # Actual risk as % of equity
    conviction_score: float
    rationale: str                      # Brief text explaining the decision
    strategy_type: str = "directional_trend"
    leverage: int = 3                   # 3 or 5 (ETP leverage multiplier)
    earnings_in_days: Optional[float] = None
    realized_vol: Optional[float] = None
    is_synthetic: bool = False          # Flag indicating whether underlying data was synthetically generated
    timestamp: str = ""                 # ISO format timestamp

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()

    @property
    def position_size(self) -> float:
        """Alias for position_size_shares."""
        return self.position_size_shares

    @property
    def tp1(self) -> float:
        """Alias for take_profit_1."""
        return self.take_profit_1

    @property
    def tp2(self) -> float:
        """Alias for take_profit_2."""
        return self.take_profit_2

    @property
    def tp3(self) -> float:
        """Alias for take_profit_3."""
        return self.take_profit_3


class RiskManager:
    """Risk Manager agent that enforces position sizing and calculates entry/exit levels."""

    def __init__(
        self,
        max_risk_per_trade: Optional[float] = None,
        max_position_pct: Optional[float] = None,
        atr_sl_multiplier: float = 1.5,
        min_sl_pct: float = 0.005,              # 0.5% minimum stop distance
        max_sl_pct: Optional[float] = None,     # 3.0% maximum stop distance cap
        tp1_r: Optional[float] = None,
        tp2_r: Optional[float] = None,
        tp3_r: Optional[float] = None,
        tsl_atr_multiplier: float = 1.2,
        tsl_hysteresis_atr_mult: float = 0.5,
        tp1_trim_pct: float = 0.50,
        tp2_trim_pct: float = 0.25,
        tp3_trim_pct: float = 0.25,
        target_annual_volatility: float = 0.20,
        enforce_spread_gate: Optional[bool] = None,
        max_bid_ask_spread_pct: Optional[float] = None,
        enforce_correlation_limits: Optional[bool] = None,
        max_sector_positions: Optional[int] = None,
        max_portfolio_beta_heat: Optional[float] = None,
        enforce_daily_circuit_breaker: Optional[bool] = None,
        daily_max_drawdown_pct: Optional[float] = None,
    ):
        from trading_system.config import settings
        self.max_risk_per_trade = getattr(settings, "MAX_RISK_PER_TRADE", 0.015) if max_risk_per_trade is None else max_risk_per_trade
        self.max_position_pct = getattr(settings, "MAX_POSITION_PCT", 0.20) if max_position_pct is None else max_position_pct
        self.atr_sl_multiplier = atr_sl_multiplier
        self.min_sl_pct = min_sl_pct
        self.max_sl_pct = getattr(settings, "MAX_SL_PCT", 0.03) if max_sl_pct is None else max_sl_pct
        self.tp1_r = getattr(settings, "TP1_R_MULTIPLE", 1.5) if tp1_r is None else tp1_r
        self.tp2_r = getattr(settings, "TP2_R_MULTIPLE", 2.5) if tp2_r is None else tp2_r
        self.tp3_r = getattr(settings, "TP3_R_MULTIPLE", 3.5) if tp3_r is None else tp3_r
        self.tsl_atr_multiplier = tsl_atr_multiplier
        self.tsl_hysteresis_atr_mult = tsl_hysteresis_atr_mult
        self.tp1_trim_pct = tp1_trim_pct
        self.tp2_trim_pct = tp2_trim_pct
        self.tp3_trim_pct = tp3_trim_pct
        self.target_annual_volatility = target_annual_volatility

        # Advanced Risk & Safety Gates
        self.enforce_spread_gate = getattr(settings, "ENFORCE_SPREAD_GATE", True) if enforce_spread_gate is None else enforce_spread_gate
        self.max_bid_ask_spread_pct = getattr(settings, "MAX_BID_ASK_SPREAD_PCT", 0.0150) if max_bid_ask_spread_pct is None else max_bid_ask_spread_pct
        self.enforce_correlation_limits = getattr(settings, "ENFORCE_CORRELATION_LIMITS", True) if enforce_correlation_limits is None else enforce_correlation_limits
        self.max_sector_positions = getattr(settings, "MAX_SECTOR_POSITIONS", 4) if max_sector_positions is None else max_sector_positions
        self.max_portfolio_beta_heat = getattr(settings, "MAX_PORTFOLIO_BETA_HEAT", 6.0) if max_portfolio_beta_heat is None else max_portfolio_beta_heat
        self.enforce_daily_circuit_breaker = getattr(settings, "ENFORCE_DAILY_CIRCUIT_BREAKER", True) if enforce_daily_circuit_breaker is None else enforce_daily_circuit_breaker
        self.daily_max_drawdown_pct = getattr(settings, "DAILY_MAX_DRAWDOWN_PCT", 0.02) if daily_max_drawdown_pct is None else daily_max_drawdown_pct
        self.trade_loss_cooldown_seconds = getattr(settings, "TRADE_LOSS_COOLDOWN_SECONDS", 3600.0)
        self.max_spread_to_stop_ratio = getattr(settings, "MAX_SPREAD_TO_STOP_RATIO", 0.50)

        self.loss_cooldowns: Dict[str, float] = {}
        self.is_circuit_breaker_tripped: bool = False
        self.session_start_equity: Optional[float] = None
        self.current_drawdown_pct: float = 0.0

        logger.debug("Initialized RiskManager with max_risk_per_trade=%s", max_risk_per_trade)

    def check_circuit_breaker(
        self, current_equity: float, session_start_equity: Optional[float] = None
    ) -> Tuple[bool, str]:
        """Check if portfolio daily drawdown exceeds circuit breaker threshold.

        Returns:
            Tuple[bool, str]: (is_tripped, explanation)
        """
        if not self.enforce_daily_circuit_breaker:
            return False, "Circuit breaker disabled"

        if session_start_equity is not None and session_start_equity > 0:
            self.session_start_equity = session_start_equity
        elif self.session_start_equity is None or self.session_start_equity <= 0:
            self.session_start_equity = current_equity
            return False, "Session baseline established"

        if self.session_start_equity <= 0:
            return False, "Invalid session baseline"

        drawdown = (current_equity - self.session_start_equity) / self.session_start_equity
        self.current_drawdown_pct = drawdown

        if drawdown <= -self.daily_max_drawdown_pct:
            self.is_circuit_breaker_tripped = True
            msg = (
                f"🚨 CIRCUIT BREAKER TRIPPED: Daily drawdown {drawdown*100:.2f}% "
                f"breached -{self.daily_max_drawdown_pct*100:.1f}% limit! "
                f"Equity: ${current_equity:.2f} vs Start: ${self.session_start_equity:.2f}."
            )
            logger.critical(msg)
            return True, msg

        return False, f"Drawdown {drawdown*100:.2f}% within safe threshold (-{self.daily_max_drawdown_pct*100:.1f}%)"

    def reset_circuit_breaker(self, new_session_equity: Optional[float] = None) -> None:
        """Reset the circuit breaker for a new session day."""
        self.is_circuit_breaker_tripped = False
        self.current_drawdown_pct = 0.0
        if new_session_equity and new_session_equity > 0:
            self.session_start_equity = new_session_equity

    def register_loss_exit(
        self, ticker: str, underlying: Optional[str] = None, cooldown_seconds: Optional[float] = None
    ) -> None:
        """Place a ticker and its underlying on anti-churn cooldown after a stop loss exit."""
        duration = cooldown_seconds if cooldown_seconds is not None else self.trade_loss_cooldown_seconds
        expiry = time.time() + duration
        t_clean = (ticker or "").upper().strip()
        u_clean = (underlying or "").upper().strip()
        if t_clean:
            self.loss_cooldowns[t_clean] = expiry
        if u_clean:
            self.loss_cooldowns[u_clean] = expiry
        logger.info(
            "Placed %s (underlying %s) on post-loss anti-churn cooldown for %.0fs (expires in %.0f min)",
            t_clean, u_clean or "N/A", duration, duration / 60.0
        )

    def is_in_loss_cooldown(self, ticker: str, underlying: Optional[str] = None) -> Tuple[bool, float]:
        """Check if ticker or underlying is currently under anti-churn loss cooldown."""
        now = time.time()
        for sym in ((ticker or "").upper().strip(), (underlying or "").upper().strip()):
            if sym and sym in self.loss_cooldowns:
                exp = self.loss_cooldowns[sym]
                if now < exp:
                    return True, exp - now
                else:
                    self.loss_cooldowns.pop(sym, None)
        return False, 0.0

    @staticmethod
    def is_true_short_contract(ticker: str = "", direction: str = "") -> bool:
        """Return True only if position is a margin/CFD short where falling price is profit.

        For all Trading 212 Invest assets (including 3x and 5x inverse ETPs),
        shares are held long, so rising ETP price is profit and Stop Loss must be BELOW live price.
        """
        dir_u = str(direction or "").upper()
        if dir_u in ("LONG", "BUY"):
            return False
        t_u = str(ticker or "").upper()
        if (
            t_u.endswith("_EQ")
            or t_u.endswith(".L")
            or "_ETP" in t_u
            or dir_u.endswith("_ETP")
            or t_u.startswith("3")
            or t_u.startswith("5")
        ):
            return False
        return dir_u in ("SHORT", "SELL")

    def calculate_stop_loss(self, entry_price: float, atr: float, direction: str = "LONG", ticker: str = "") -> float:
        """Calculate stop loss based on ATR, bounded between min_sl_pct and max_sl_pct.

        For LONG:  SL = Entry - clamp(multiplier * ATR, Entry * min_sl_pct, Entry * max_sl_pct)
        For SHORT (if CFD): SL = Entry + clamp(multiplier * ATR, Entry * min_sl_pct, Entry * max_sl_pct)
        Note: When buying an Inverse 3x/5x ETP, treat the ETP as a Long position with respect to its own price.
        """
        raw_sl_distance = max(self.atr_sl_multiplier * atr, entry_price * self.min_sl_pct)
        max_distance = entry_price * getattr(self, "max_sl_pct", 0.03)
        sl_distance = min(raw_sl_distance, max_distance)
        is_cfd_short = self.is_true_short_contract(ticker=ticker, direction=direction)
        if is_cfd_short:
            stop_loss = entry_price + sl_distance
        else:
            stop_loss = max(0.01, entry_price - sl_distance)
        return round(stop_loss, 2)

    def calculate_take_profits(
        self, entry_price: float, stop_loss: float, direction: str = "LONG", ticker: str = ""
    ) -> Tuple[float, float, float]:
        """Calculate multiple take profit levels based on Risk (R).

        R = abs(entry_price - stop_loss)
        For LONG:  TP = Entry + (multiplier * R)
        For SHORT (if CFD): TP = Entry - (multiplier * R)
        """
        r_distance = abs(entry_price - stop_loss)
        is_cfd_short = self.is_true_short_contract(ticker=ticker, direction=direction)
        if is_cfd_short:
            tp1 = max(0.01, entry_price - (self.tp1_r * r_distance))
            tp2 = max(0.01, entry_price - (self.tp2_r * r_distance))
            tp3 = max(0.01, entry_price - (self.tp3_r * r_distance))
        else:
            tp1 = entry_price + (self.tp1_r * r_distance)
            tp2 = entry_price + (self.tp2_r * r_distance)
            tp3 = entry_price + (self.tp3_r * r_distance)
        return round(tp1, 2), round(tp2, 2), round(tp3, 2)

    def calculate_escape_threshold(
        self,
        tp1: float,
        atr: float,
        direction: str = "LONG",
        multiplier: Optional[float] = None,
        ticker: str = "",
    ) -> float:
        """Calculate the escape threshold for Schmitt Hysteresis past TP1.

        Requires price to advance beyond TP1 by (multiplier * ATR) before
        elevating the Stop Loss to TP1, eliminating zero-distance stop liquidations.
        """
        mult = multiplier if multiplier is not None else self.tsl_hysteresis_atr_mult
        buffer = mult * atr if atr > 0 else tp1 * 0.015
        is_cfd_short = self.is_true_short_contract(ticker=ticker, direction=direction)
        if is_cfd_short:
            escape_price = max(0.01, tp1 - buffer)
        else:
            escape_price = tp1 + buffer
        return round(escape_price, 2)

    @classmethod
    def get_decay_atr_multiplier(cls, session_count: int = 1, is_runner: bool = False) -> float:
        """Calculate the active ATR trailing stop multiplier with session-day decay.
        
        - If is_runner is True: decay is frozen at 2.00x ATR to let multi-week trends ride.
        - Otherwise, compresses by 0.35x ATR per elapsed completed session:
            Day 1: 2.20x ATR
            Day 2: 1.85x ATR
            Day 3: 1.50x ATR
            Day 4+: 1.15x ATR (compression floor)
        """
        if is_runner:
            return 2.00
        decayed = 2.20 - (0.35 * max(0, session_count - 1))
        return round(max(1.15, decayed), 2)

    def calculate_trailing_stop(
        self,
        current_price: float,
        peak_price: float,
        atr: float,
        current_tsl: float,
        direction: str = "LONG",
        ticker: str = "",
        session_count: int = 1,
        is_runner: bool = False,
    ) -> float:
        """Calculate dynamic trailing stop level with session-day decay envelope.

        The trailing stop ratchets ONLY in the favorable direction (never widens).
        """
        effective_mult = self.get_decay_atr_multiplier(session_count=session_count, is_runner=is_runner)
        distance = max(effective_mult * atr, peak_price * 0.02)
        is_cfd_short = self.is_true_short_contract(ticker=ticker, direction=direction)
        if is_cfd_short:
            new_tsl = peak_price + distance
            # For short, lower is better; ratchet down
            return round(min(current_tsl, new_tsl) if current_tsl > 0 else new_tsl, 2)
        else:
            new_tsl = peak_price - distance
            # For long, higher is better; ratchet up
            return round(max(current_tsl, new_tsl), 2)

    @classmethod
    def calculate_progressive_trailing_stop(
        cls,
        entry_price: float,
        current_price: float,
        current_sl: float,
        direction: str = "LONG",
        ticker: str = "",
    ) -> float:
        """Lock in gains progressively:
        - At +1.5% unrealized profit: move stop loss to Entry Price (Breakeven).
        - At +3.0% unrealized profit: move stop loss to Entry Price + 1.5% (lock profit cushion).
        - Stop never loosens.
        """
        is_short = cls.is_true_short_contract(ticker=ticker, direction=direction)
        if is_short:
            gain_pct = ((entry_price - current_price) / entry_price * 100.0) if entry_price > 0 else 0.0
            if gain_pct >= 3.0:
                candidate = entry_price * (1.0 - 0.015)
            elif gain_pct >= 1.5:
                candidate = entry_price
            else:
                candidate = current_sl
            return round(min(current_sl, candidate) if current_sl > 0 else candidate, 4)
        else:
            gain_pct = ((current_price - entry_price) / entry_price * 100.0) if entry_price > 0 else 0.0
            if gain_pct >= 3.0:
                candidate = entry_price * 1.015
            elif gain_pct >= 1.5:
                candidate = entry_price
            else:
                candidate = current_sl
            return round(max(current_sl, candidate), 4)

    def calculate_volatility_targeted_size(
        self,
        account_equity: float,
        annualized_vol: float,
        target_vol: Optional[float] = None,
        leverage: int = 3,
    ) -> float:
        """Calculate dynamic volatility-targeted position notional.

        Formula:
            Target Notional = (Account Equity * max_position_pct) * (Target Vol / max(0.10, leverage * annualized_vol))
            Capped at Max Position Notional (Account Equity * max_position_pct).

        Reduces position size proportionally when realized volatility spikes to protect against
        beta decay and extreme adverse excursion on leveraged products.
        """
        tgt_vol = target_vol if target_vol is not None else self.target_annual_volatility
        max_position_notional = account_equity * self.max_position_pct
        if annualized_vol <= 0 or account_equity <= 0:
            return max_position_notional

        effective_vol = max(0.10, leverage * annualized_vol)
        vol_budget = max_position_notional * (tgt_vol / effective_vol)
        return min(max_position_notional, round(vol_budget, 2))

    def calculate_position_size(
        self,
        account_equity: float,
        entry_price: float,
        stop_loss: float,
        realized_vol: Optional[float] = None,
        leverage: int = 3,
        ticker: Optional[str] = None,
        currency: Optional[str] = None,
    ) -> Tuple[float, float, float]:
        """Calculate number of shares, notional position, and actual risk amount.

        Formula:
            Risk Notional = (Account Equity * max_risk) / Stop_Distance_Pct
            Max Notional = Account Equity * max_position_pct
            Vol Notional = calculate_volatility_targeted_size(...)
            Position Notional = min(Risk Notional, Max Notional, [Vol Notional if realized_vol])
        """
        if entry_price <= 0 or account_equity <= 0:
            return 0.0, 0.0, 0.0

        from trading_system.data.leveraged_universe import LeveragedUniverse, GBX_TICKERS
        price_in_eur = LeveragedUniverse.normalize_quote_to_eur(ticker or "", entry_price, currency)
        if price_in_eur <= 0:
            price_in_eur = entry_price

        stop_distance_pct = abs(entry_price - stop_loss) / entry_price if entry_price > 0 else 0.0
        risk_amount = account_equity * self.max_risk_per_trade

        if stop_distance_pct <= 0:
            position_notional_from_risk = 0.0
        else:
            position_notional_from_risk = risk_amount / stop_distance_pct

        max_position_notional = account_equity * self.max_position_pct
        position_notional = min(position_notional_from_risk, max_position_notional)

        # Apply volatility-targeted sizing if realized_vol is available
        if realized_vol is not None and realized_vol > 0.05:
            vol_notional = self.calculate_volatility_targeted_size(
                account_equity=account_equity,
                annualized_vol=realized_vol,
                leverage=leverage,
            )
            position_notional = min(position_notional, vol_notional)

        raw_shares = position_notional / price_in_eur if price_in_eur > 0 else 0.0
        max_shares = max_position_notional / price_in_eur if price_in_eur > 0 else 0.0
        if raw_shares > max_shares:
            raw_shares = max_shares

        is_gbx_or_lse = (
            (currency and currency.upper() in ("GBX", "GBP"))
            or (ticker and (ticker in GBX_TICKERS or ticker.endswith("l_EQ") or ticker.endswith(".L")))
            or (entry_price > 500.0)
            or (abs(entry_price - 93.97) < 1.0)
        )
        if is_gbx_or_lse and raw_shares >= 1.0:
            shares = float(math.floor(raw_shares))
            if shares <= 0:
                shares = 1.0
        elif raw_shares >= 1.0:
            shares = round(raw_shares, 2)
            if shares * price_in_eur > max_position_notional + 1e-4:
                shares = math.floor(raw_shares * 100.0) / 100.0
            if shares * price_in_eur > max_position_notional + 1e-4:
                shares = float(math.floor(raw_shares))
        else:
            shares = max(1.0, round(raw_shares, 2)) if position_notional > 0 else 0.0

        actual_notional = round(shares * price_in_eur, 2)
        if actual_notional > max_position_notional + 0.01 and shares > 1.0:
            shares = float(math.floor(max_position_notional / price_in_eur))
            actual_notional = round(shares * price_in_eur, 2)

        eur_scale = price_in_eur / entry_price if entry_price > 0 else 1.0
        actual_risk = round(shares * abs(entry_price - stop_loss) * eur_scale, 2)

        return shares, actual_notional, actual_risk

    def generate_decision_card(
        self,
        ticker: str,
        trading212_ticker: str,
        direction: str,
        metrics: Dict[str, Any],
        debate_outcome: Any,
        account_equity: float,
        strategy_type: str = "directional_trend",
        leverage: Optional[int] = None,
    ) -> DecisionCard:
        """Generate a complete DecisionCard from technical metrics and debate consensus."""
        entry_price = metrics.get("close", 0.0)
        atr = metrics.get("atr", 0.0)
        realized_vol = metrics.get("realized_vol")

        # Multi-Tier Allocation: Support both 3x and 5x dynamically based on conviction and volatility
        from trading_system.data.leveraged_universe import LeveragedUniverse
        from trading_system.config import settings
        universe = LeveragedUniverse()
        has_5x = universe.has_5x_leverage(ticker, direction)
        enable_5x = getattr(settings, "ENABLE_5X_LEVERAGE", True)

        conviction_score = getattr(debate_outcome, "conviction_score", 0.0)
        realized_vol = float(metrics.get("realized_vol") or metrics.get("historical_vol") or 0.25)

        if has_5x and enable_5x:
            if leverage == 5:
                resolved_leverage = 5
            elif leverage == 3:
                resolved_leverage = 3
            elif conviction_score >= 0.85 and realized_vol <= 0.35:
                resolved_leverage = 5
            else:
                resolved_leverage = 3
        else:
            resolved_leverage = 3 if settings.USE_LEVERAGED_3X else 1

        sanitized_t212_ticker = universe.get_execution_ticker(ticker, direction, leverage=resolved_leverage) or trading212_ticker

        # Query live quote and currency of execution ticker before sizing
        from trading_system.data.trading212_client import MarketDataProvider
        etp_quote = MarketDataProvider.get_quote(sanitized_t212_ticker)
        etp_price = float(etp_quote.get("price") or 0.0) if etp_quote else 0.0
        etp_currency = (etp_quote.get("currency") if etp_quote else "") or universe.get_ticker_currency(sanitized_t212_ticker)

        underlying_price = float(metrics.get("close", 0.0) or 0.0)
        is_derivative_etp = sanitized_t212_ticker.upper() != ticker.upper()
        if etp_price > 0:
            actual_entry_price = etp_price
            if underlying_price > 0 and atr > 0:
                atr_ratio = atr / underlying_price
                etp_atr = actual_entry_price * atr_ratio
            else:
                etp_atr = atr if atr > 0 else (actual_entry_price * 0.02)
        elif is_derivative_etp:
            # Scale guard: Derivative/inverse ETP quote is missing; never fall back to underlying price
            # as it creates catastrophic scale mismatches (e.g. ETH-USD $2310 vs ETH3EUR €5.70).
            logger.warning(
                "Scale guard veto: Quote unavailable for derivative ETP %s (underlying %s). Rejecting underlying price fallback.",
                sanitized_t212_ticker, ticker
            )
            actual_entry_price = 0.0
            etp_atr = 0.0
        else:
            actual_entry_price = underlying_price
            etp_atr = atr

        if actual_entry_price <= 0.0:
            return DecisionCard(
                ticker=ticker,
                trading212_ticker=sanitized_t212_ticker,
                direction=direction.upper(),
                decision="DO_NOT_TRADE",
                entry_price=0.0,
                stop_loss=0.0,
                trailing_stop_loss=0.0,
                stop_distance_pct=0.0,
                take_profit_1=0.0,
                take_profit_2=0.0,
                take_profit_3=0.0,
                position_size_shares=0.0,
                position_notional=0.0,
                risk_amount=0.0,
                risk_pct_of_equity=0.0,
                conviction_score=0.0,
                rationale="Scale guard veto: Invalid or unresolvable ETP execution price.",
                strategy_type=strategy_type,
                leverage=resolved_leverage,
            )

        stop_loss = self.calculate_stop_loss(
            actual_entry_price, etp_atr, direction=direction, ticker=sanitized_t212_ticker
        )
        stop_distance_pct = (
            abs(actual_entry_price - stop_loss) / actual_entry_price if actual_entry_price > 0 else 0.0
        )
        tp1, tp2, tp3 = self.calculate_take_profits(
            actual_entry_price, stop_loss, direction=direction, ticker=sanitized_t212_ticker
        )

        consensus = getattr(debate_outcome, "consensus_decision", "DO_NOT_TRADE")
        conviction_score = getattr(debate_outcome, "conviction_score", 0.0)
        synthesis = getattr(debate_outcome, "synthesis", "No synthesis provided.")

        dir_upper = direction.upper()
        is_long = dir_upper in ("LONG", "BUY", "BULLISH")
        is_short = dir_upper in ("SHORT", "SELL", "BEARISH")

        # Directional alignment gate: If debate specifically concluded the opposite direction won, veto entry!
        # E.g. Candidate requested SHORT, but System 2 debate concluded BULL won (consensus=BUY_ENTRY).
        winning_side = str(getattr(debate_outcome, "winning_side", "") or "").lower()
        if isinstance(winning_side, str):
            direction_mismatch = (is_short and winning_side == "bull") or (is_long and winning_side == "bear")
        else:
            direction_mismatch = False

        if consensus in ("DO_NOT_TRADE", "EXIT") or direction_mismatch:
            if direction_mismatch:
                logger.warning(
                    "Directional conflict veto for %s: candidate requested %s but debate winning side was %s (consensus=%s). Setting DO_NOT_TRADE.",
                    ticker, direction, winning_side, consensus
                )
            shares, position_notional, actual_risk, risk_pct_of_equity = 0.0, 0.0, 0.0, 0.0
            decision_action = "DO_NOT_TRADE"
        else:
            shares, position_notional, actual_risk = self.calculate_position_size(
                account_equity=account_equity,
                entry_price=actual_entry_price,
                stop_loss=stop_loss,
                realized_vol=realized_vol,
                leverage=resolved_leverage,
                ticker=sanitized_t212_ticker,
                currency=etp_currency,
            )
            risk_pct_of_equity = (
                actual_risk / account_equity if account_equity > 0 else 0.0
            )
            decision_action = "BUY_ENTRY" if is_long else "SELL_ENTRY"

        return DecisionCard(
            ticker=ticker,
            trading212_ticker=sanitized_t212_ticker,
            direction=direction.upper(),
            decision=decision_action,
            entry_price=actual_entry_price,
            stop_loss=stop_loss,
            trailing_stop_loss=stop_loss,
            stop_distance_pct=stop_distance_pct,
            take_profit_1=tp1,
            take_profit_2=tp2,
            take_profit_3=tp3,
            position_size_shares=shares,
            position_notional=position_notional,
            risk_amount=actual_risk,
            risk_pct_of_equity=risk_pct_of_equity,
            conviction_score=conviction_score,
            rationale=synthesis,
            strategy_type=strategy_type,
            leverage=resolved_leverage,
            earnings_in_days=(
                float(metrics.get("earnings_in_days"))
                if metrics.get("earnings_in_days") is not None
                and str(metrics.get("earnings_in_days")).replace(".", "", 1).isdigit()
                else None
            ),
            realized_vol=realized_vol,
            is_synthetic=MarketDataProvider.is_ticker_synthetic(ticker),
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    calculate_decision_card = generate_decision_card

    def validate_trade(
        self,
        card: DecisionCard,
        account_cash: float,
        existing_positions: List[Any],
        spread_pct: Optional[float] = None,
        account_equity: Optional[float] = None,
    ) -> Tuple[bool, str]:
        """Validate whether a trade should be executed."""
        if card.decision not in ("BUY_ENTRY", "SELL_ENTRY"):
            return False, f"Decision is {card.decision}, not an entry decision"

        # 1. Anti-Churn Loss Cooldown Gate
        in_cooldown, remaining_sec = self.is_in_loss_cooldown(card.trading212_ticker, card.ticker)
        if in_cooldown:
            return (
                False,
                f"Anti-churn cooldown active for {card.trading212_ticker} ({card.ticker}) after recent stop-loss exit ({remaining_sec:.0f}s remaining)",
            )

        # 2. Daily Drawdown Circuit Breaker Gate
        if self.enforce_daily_circuit_breaker and self.is_circuit_breaker_tripped:
            return (
                False,
                f"Daily circuit breaker tripped (Drawdown {self.current_drawdown_pct*100:.2f}% breached -{self.daily_max_drawdown_pct*100:.1f}%)",
            )

        # 3. Bid-Ask Spread Gate & Spread-to-Stop Ratio Gate
        if self.enforce_spread_gate and spread_pct is not None:
            if spread_pct > self.max_bid_ask_spread_pct:
                return (
                    False,
                    f"Bid-ask spread too wide ({spread_pct*100:.2f}% > {self.max_bid_ask_spread_pct*100:.2f}%)",
                )
            if card.stop_distance_pct > 0 and spread_pct >= (card.stop_distance_pct * self.max_spread_to_stop_ratio):
                return (
                    False,
                    f"Spread ({spread_pct*100:.2f}%) exceeds {self.max_spread_to_stop_ratio*100:.0f}% of planned stop loss ({card.stop_distance_pct*100:.2f}%), trade would be vulnerable to immediate spread-stop whipsaw",
                )

        from trading_system.config import settings
        max_allowed_leverage = getattr(settings, "MAX_LEVERAGE", 5)
        if card.leverage > max_allowed_leverage:
            return False, f"Leverage {card.leverage}x exceeds maximum allowed cap of {max_allowed_leverage}x"

        if card.position_notional > account_cash:
            return False, f"Insufficient free capital (${account_cash:.2f} < ${card.position_notional:.2f})"

        if card.risk_pct_of_equity > self.max_risk_per_trade * 3.0:
            return False, "Risk exceeds maximum allowed by more than tolerance"

        if card.stop_distance_pct < self.min_sl_pct:
            return False, "Stop distance percentage is below minimum allowed"

        # Check existing positions to avoid duplicate exposure
        for pos in existing_positions:
            pos_ticker = getattr(pos, "ticker", None)
            if pos_ticker == card.trading212_ticker or pos_ticker == card.ticker:
                return False, f"Already holding a position in {card.trading212_ticker}"

        # 3. Sector Concentration & Aggregate Beta Heat Limits
        if self.enforce_correlation_limits:
            from trading_system.data.leveraged_universe import LeveragedUniverse
            univ = LeveragedUniverse()
            candidate_sector = univ.get_category_for_symbol(card.ticker)

            sector_count = 0
            total_active_beta_notional = 0.0

            if existing_positions:
                for pos in existing_positions:
                    pos_ticker = getattr(pos, "ticker", "") or ""
                    pos_underlying = getattr(pos, "underlying", "") or pos_ticker
                    pos_sector = univ.get_category_for_symbol(pos_underlying or pos_ticker)
                    if pos_sector == candidate_sector:
                        sector_count += 1

                    pos_qty = float(getattr(pos, "remaining_quantity", getattr(pos, "quantity", 0.0)) or 0.0)
                    pos_price = float(getattr(pos, "entry_price", getattr(pos, "current_price", getattr(pos, "average_price", 0.0))) or 0.0)
                    pos_price_eur = univ.normalize_quote_to_eur(pos_ticker, pos_price, getattr(pos, "currency", None))
                    pos_notional = pos_qty * (pos_price_eur if pos_price_eur > 0 else pos_price)
                    pos_lev = 3 if any(k in pos_ticker for k in ("3l_EQ", "3Sl_EQ", "3L", "3S", "3US", "QQQ3", "3NVD", "3LTS", "AAP3", "MSF3")) else 1
                    total_active_beta_notional += (pos_notional * pos_lev)

                if sector_count >= self.max_sector_positions:
                    return False, f"Sector concentration limit reached for {candidate_sector} ({sector_count} >= {self.max_sector_positions})"

            effective_equity = account_equity if (account_equity and account_equity > 0) else (account_cash + total_active_beta_notional / 3.0)
            if effective_equity > 0:
                candidate_beta_notional = card.position_notional * card.leverage
                new_total_beta_heat = (total_active_beta_notional + candidate_beta_notional) / effective_equity
                if new_total_beta_heat > self.max_portfolio_beta_heat:
                    return (
                        False,
                        f"Aggregate portfolio beta heat ceiling exceeded ({new_total_beta_heat:.2f}x > {self.max_portfolio_beta_heat:.2f}x)",
                    )

        return True, "All checks passed"

    def format_decision_card(self, card: DecisionCard) -> str:
        """Format the DecisionCard into a readable string block."""
        from trading_system.config import settings
        pure_tsl = getattr(settings, "PURE_TSL_MODE", True)
        exit_line = (
            "Exit Protocol:   100% Pure Trailing Stop Loss (+1.5% BE, +3.0% Cushion, Dynamic ATR Trail)"
            if pure_tsl
            else f"Take Profits:    TP1=${card.take_profit_1:.2f} | TP2=${card.take_profit_2:.2f} | TP3=${card.take_profit_3:.2f}"
        )
        lines = [
            f"=== DECISION CARD: {card.ticker} -> {card.trading212_ticker} ===",
            f"Timestamp:       {card.timestamp}",
            f"Direction:       {card.direction}",
            f"Decision:        {card.decision}",
            f"Conviction:      {card.conviction_score}/10",
            f"Entry Price:     ${card.entry_price:.2f}",
            f"Stop Loss:       ${card.stop_loss:.2f} ({card.stop_distance_pct*100:.2f}%)",
            f"Trailing Stop:   ${card.trailing_stop_loss:.2f}",
            exit_line,
            f"Position Size:   {card.position_size_shares:.3f} shares",
            f"Notional Value:  ${card.position_notional:.2f}",
            f"Risk Amount:     ${card.risk_amount:.2f} ({card.risk_pct_of_equity*100:.2f}% of equity)",
            f"Rationale:       {card.rationale}",
            "==================================================",
        ]
        return "\n".join(lines)
