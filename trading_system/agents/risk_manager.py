"""Risk Manager Agent for Institutional-Grade Execution.

Enforces strict position sizing, ATR-based Stop Loss, multi-target Take Profits
(TP1, TP2, TP3), and dynamic Trailing Stop Loss (TSL) for both Long and Short positions.
"""

from __future__ import annotations

import logging
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
    strategy_type: str = "momentum"     # 'momentum' or '3rsi_mean_reversion'
    leverage: int = 3                   # 3 or 5 (ETP leverage multiplier)
    earnings_in_days: Optional[float] = None
    is_synthetic: bool = False          # Flag indicating whether underlying data was synthetically generated
    timestamp: str = ""                 # ISO format timestamp

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()


class RiskManager:
    """Risk Manager agent that enforces position sizing and calculates entry/exit levels."""

    def __init__(
        self,
        max_risk_per_trade: float = 0.005,      # 0.5% equity risk default
        max_position_pct: float = 0.05,         # 5% max equity notional
        atr_sl_multiplier: float = 1.5,
        min_sl_pct: float = 0.005,              # 0.5% minimum stop distance
        tp1_r: float = 1.0,
        tp2_r: float = 2.0,
        tp3_r: float = 3.0,
        tsl_atr_multiplier: float = 1.2,
        tsl_hysteresis_atr_mult: float = 0.5,
        tp1_trim_pct: float = 0.50,
        tp2_trim_pct: float = 0.25,
        tp3_trim_pct: float = 0.25,
    ):
        self.max_risk_per_trade = max_risk_per_trade
        self.max_position_pct = max_position_pct
        self.atr_sl_multiplier = atr_sl_multiplier
        self.min_sl_pct = min_sl_pct
        self.tp1_r = tp1_r
        self.tp2_r = tp2_r
        self.tp3_r = tp3_r
        self.tsl_atr_multiplier = tsl_atr_multiplier
        self.tsl_hysteresis_atr_mult = tsl_hysteresis_atr_mult
        self.tp1_trim_pct = tp1_trim_pct
        self.tp2_trim_pct = tp2_trim_pct
        self.tp3_trim_pct = tp3_trim_pct
        logger.debug("Initialized RiskManager with max_risk_per_trade=%s", max_risk_per_trade)

    def calculate_stop_loss(self, entry_price: float, atr: float, direction: str = "LONG") -> float:
        """Calculate stop loss based on ATR and minimum percentage distance.

        For LONG:  SL = Entry - max(multiplier * ATR, Entry * min_sl_pct)
        For SHORT (if CFD): SL = Entry + max(multiplier * ATR, Entry * min_sl_pct)
        Note: When buying an Inverse 3x ETP, treat the ETP as a Long position with respect to its own price.
        """
        sl_distance = max(self.atr_sl_multiplier * atr, entry_price * self.min_sl_pct)
        if direction.upper() in ("SHORT", "SELL") and not direction.upper().endswith("_ETP"):
            stop_loss = entry_price + sl_distance
        else:
            stop_loss = max(0.01, entry_price - sl_distance)
        return round(stop_loss, 2)

    def calculate_take_profits(
        self, entry_price: float, stop_loss: float, direction: str = "LONG"
    ) -> Tuple[float, float, float]:
        """Calculate multiple take profit levels based on Risk (R).

        R = abs(entry_price - stop_loss)
        For LONG:  TP = Entry + (multiplier * R)
        For SHORT: TP = Entry - (multiplier * R)
        """
        r_distance = abs(entry_price - stop_loss)
        if direction.upper() in ("SHORT", "SELL") and not direction.upper().endswith("_ETP"):
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
    ) -> float:
        """Calculate the escape threshold for Schmitt Hysteresis past TP1.

        Requires price to advance beyond TP1 by (multiplier * ATR) before
        elevating the Stop Loss to TP1, eliminating zero-distance stop liquidations.
        """
        mult = multiplier if multiplier is not None else self.tsl_hysteresis_atr_mult
        buffer = mult * atr if atr > 0 else tp1 * 0.015
        if direction.upper() in ("SHORT", "SELL") and not direction.upper().endswith("_ETP"):
            escape_price = max(0.01, tp1 - buffer)
        else:
            escape_price = tp1 + buffer
        return round(escape_price, 2)

    def calculate_trailing_stop(
        self,
        current_price: float,
        peak_price: float,
        atr: float,
        current_tsl: float,
        direction: str = "LONG",
    ) -> float:
        """Calculate dynamic trailing stop level.

        The trailing stop ratchets ONLY in the favorable direction (never widens).
        """
        distance = max(self.tsl_atr_multiplier * atr, peak_price * 0.02)
        if direction.upper() in ("SHORT", "SELL") and not direction.upper().endswith("_ETP"):
            new_tsl = peak_price + distance
            # For short, lower is better; ratchet down
            return round(min(current_tsl, new_tsl) if current_tsl > 0 else new_tsl, 2)
        else:
            new_tsl = peak_price - distance
            # For long, higher is better; ratchet up
            return round(max(current_tsl, new_tsl), 2)

    def calculate_position_size(
        self, account_equity: float, entry_price: float, stop_loss: float
    ) -> Tuple[float, float, float]:
        """Calculate number of shares, notional position, and actual risk amount.

        Formula:
            Position Notional = min((Account Equity * max_risk) / Stop_Distance_Pct, Account Equity * max_position_pct)
        """
        if entry_price <= 0:
            return 0.0, 0.0, 0.0

        stop_distance_pct = abs(entry_price - stop_loss) / entry_price
        risk_amount = account_equity * self.max_risk_per_trade

        if stop_distance_pct <= 0:
            position_notional_from_risk = 0.0
        else:
            position_notional_from_risk = risk_amount / stop_distance_pct

        max_position_notional = account_equity * self.max_position_pct
        position_notional = min(position_notional_from_risk, max_position_notional)
        raw_shares = position_notional / entry_price
        shares = max(1.0, round(raw_shares, 2)) if position_notional > 0 else 0.0
        position_notional = round(shares * entry_price, 2)
        actual_risk = round(shares * abs(entry_price - stop_loss), 2)

        return shares, position_notional, actual_risk

    def generate_decision_card(
        self,
        ticker: str,
        trading212_ticker: str,
        direction: str,
        metrics: Dict[str, Any],
        debate_outcome: Any,
        account_equity: float,
        strategy_type: str = "momentum",
        leverage: Optional[int] = None,
    ) -> DecisionCard:
        """Generate a complete DecisionCard from technical metrics and debate consensus."""
        entry_price = metrics.get("close", 0.0)
        atr = metrics.get("atr", 0.0)

        # Policy: if 5x exists or is passed, replace it with 3x (cap at MAX_LEVERAGE = 3)
        from trading_system.data.leveraged_universe import LeveragedUniverse
        universe = LeveragedUniverse()
        sanitized_t212_ticker = universe.replace_5x_ticker(trading212_ticker)
        if sanitized_t212_ticker == trading212_ticker:
            pair = universe.get_pair_for_underlying(ticker)
            if pair:
                replacement = universe.get_execution_ticker(ticker, direction, leverage=3)
                if replacement:
                    sanitized_t212_ticker = replacement

        raw_leverage = leverage if leverage is not None else (
            5 if any(x in trading212_ticker for x in ("5l_EQ", "5Y", "5S", "5E", "5x", "5QQQ", "5SPY")) else 3
        )
        resolved_leverage = 3 if raw_leverage >= 5 else raw_leverage

        stop_loss = self.calculate_stop_loss(entry_price, atr, direction=direction)
        stop_distance_pct = (
            abs(entry_price - stop_loss) / entry_price if entry_price > 0 else 0.0
        )
        tp1, tp2, tp3 = self.calculate_take_profits(entry_price, stop_loss, direction=direction)

        consensus = getattr(debate_outcome, "consensus_decision", "DO_NOT_TRADE")
        conviction_score = getattr(debate_outcome, "conviction_score", 0.0)
        synthesis = getattr(debate_outcome, "synthesis", "No synthesis provided.")

        if consensus in ("DO_NOT_TRADE", "EXIT"):
            shares, position_notional, actual_risk, risk_pct_of_equity = 0.0, 0.0, 0.0, 0.0
            decision_action = "DO_NOT_TRADE"
        else:
            shares, position_notional, actual_risk = self.calculate_position_size(
                account_equity, entry_price, stop_loss
            )
            risk_pct_of_equity = (
                actual_risk / account_equity if account_equity > 0 else 0.0
            )
            decision_action = "BUY_ENTRY" if direction.upper() in ("LONG", "BUY") else "SELL_ENTRY"

        return DecisionCard(
            ticker=ticker,
            trading212_ticker=sanitized_t212_ticker,
            direction=direction.upper(),
            decision=decision_action,
            entry_price=entry_price,
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
            is_synthetic=bool(metrics.get("is_synthetic", False)),
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    calculate_decision_card = generate_decision_card

    def validate_trade(
        self, card: DecisionCard, account_cash: float, existing_positions: List[Any]
    ) -> Tuple[bool, str]:
        """Validate whether a trade should be executed."""
        if card.decision not in ("BUY_ENTRY", "SELL_ENTRY"):
            return False, f"Decision is {card.decision}, not an entry decision"

        from trading_system.config import settings
        max_allowed_leverage = getattr(settings, "MAX_LEVERAGE", 3)
        if card.leverage > max_allowed_leverage:
            return False, f"Leverage {card.leverage}x exceeds maximum allowed cap of {max_allowed_leverage}x"

        # Check for prohibited 5x execution tickers
        if any(x in card.trading212_ticker for x in ("5l_EQ", "5Yl_EQ", "5Sl_EQ", "5El_EQ", "5x", "QQQ5", "SP5Y", "GLD5", "TLT5", "DAX5", "5UKX", "5SX5")):
            return False, f"Prohibited 5x execution ticker detected: {card.trading212_ticker}"

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

        return True, "All checks passed"

    def format_decision_card(self, card: DecisionCard) -> str:
        """Format the DecisionCard into a readable string block."""
        lines = [
            f"=== DECISION CARD: {card.ticker} -> {card.trading212_ticker} ===",
            f"Timestamp:       {card.timestamp}",
            f"Direction:       {card.direction}",
            f"Decision:        {card.decision}",
            f"Conviction:      {card.conviction_score}/10",
            f"Entry Price:     ${card.entry_price:.2f}",
            f"Stop Loss:       ${card.stop_loss:.2f} ({card.stop_distance_pct*100:.2f}%)",
            f"Trailing Stop:   ${card.trailing_stop_loss:.2f}",
            f"Take Profits:    TP1=${card.take_profit_1:.2f} | TP2=${card.take_profit_2:.2f} | TP3=${card.take_profit_3:.2f}",
            f"Position Size:   {card.position_size_shares:.3f} shares",
            f"Notional Value:  ${card.position_notional:.2f}",
            f"Risk Amount:     ${card.risk_amount:.2f} ({card.risk_pct_of_equity*100:.2f}% of equity)",
            f"Rationale:       {card.rationale}",
            "==================================================",
        ]
        return "\n".join(lines)
