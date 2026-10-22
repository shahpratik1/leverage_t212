"""Unit tests for 5x & 3x Leveraged Asset Universe on Trading 212."""

import unittest
from unittest.mock import MagicMock

from trading_system.data.leveraged_universe import LeveragedPair, LeveragedUniverse, CURATED_3X_PAIRS
from trading_system.agents.risk_manager import DecisionCard, RiskManager
from trading_system.config import settings


class Test5xLeveragedUniverse(unittest.TestCase):
    """Test suite verifying 5x Leverage Shares ETP discovery, ticker mapping, and fallbacks."""

    def setUp(self):
        self.universe = LeveragedUniverse()
        self.risk_manager = RiskManager()

    def test_5x_pairs_presence_and_flag(self):
        """Major indices, commodities, and fixed income track 5x market availability, but enforce 3x execution."""
        expected_5x = ["QQQ", "SPY", "GLD", "TLT", "MAGS", "^FTSE", "^GDAXI", "^STOXX50E"]
        for symbol in expected_5x:
            pair = self.universe.get_pair_for_underlying(symbol)
            self.assertIsNotNone(pair, f"Missing pair for {symbol}")
            self.assertTrue(pair.has_5x, f"Expected has_5x=True for {symbol}")
            # Policy: if 5x exists, available leverage multiplier is capped at [3]
            self.assertEqual(self.universe.get_available_leverage_multipliers(symbol), [3])

    def test_single_stocks_capped_at_3x(self):
        """Single equities remain 3x due to European UCITS concentration regulations."""
        single_stocks = ["NVDA", "TSLA", "AAPL", "MSFT", "AMZN", "GOOGL", "TSM"]
        for symbol in single_stocks:
            pair = self.universe.get_pair_for_underlying(symbol)
            self.assertIsNotNone(pair, f"Missing pair for {symbol}")
            self.assertFalse(pair.has_5x, f"Expected has_5x=False for single stock {symbol}")
            self.assertEqual(self.universe.get_available_leverage_multipliers(symbol), [3])

    def test_5x_ticker_resolution_long_and_short(self):
        """Resolution of 5x tickers replaces 5x with 3x for both Long and Inverse Short."""
        # QQQ (Nasdaq 100): 5x replaced with 3x EUR (QQQ3m / QQQSm)
        self.assertEqual(self.universe.get_execution_ticker("QQQ", "LONG", leverage=5), "QQQ3m_EQ")
        self.assertEqual(self.universe.get_execution_ticker("QQQ", "SHORT", leverage=5), "QQQSm_EQ")

        # SPY (S&P 500): 5x replaced with 3x EUR (3USLm / 3USSm)
        self.assertEqual(self.universe.get_execution_ticker("SPY", "LONG", leverage=5), "3USLm_EQ")
        self.assertEqual(self.universe.get_execution_ticker("SPY", "SHORT", leverage=5), "3USSm_EQ")

        # TLT (20+ Year Treasury Bond): 5x replaced with 3x EUR (3TYLm)
        self.assertEqual(self.universe.get_execution_ticker("TLT", "LONG", leverage=5), "3TYLm_EQ")
        self.assertIsNone(self.universe.get_execution_ticker("TLT", "SHORT", leverage=5))

        # MAGS (Magnificent 7): 5x replaced with 3x EUR (MAG7m_EQ)
        self.assertEqual(self.universe.get_execution_ticker("MAGS", "LONG", leverage=5), "MAG7m_EQ")

    def test_5x_fallback_to_3x_when_unavailable(self):
        """Requesting 5x on a single stock gracefully falls back to 3x."""
        # NVDA has 3x Long (3LVEl_EQ) and 3x Short (3SVEl_EQ) in EUR
        self.assertEqual(self.universe.get_execution_ticker("NVDA", "LONG", leverage=5), "3LVEl_EQ")
        self.assertEqual(self.universe.get_execution_ticker("NVDA", "SHORT", leverage=5), "3SVEl_EQ")

        # TSM has 3x Long EUR (TS3El_EQ)
        self.assertEqual(self.universe.get_execution_ticker("TSM", "LONG", leverage=5), "TS3El_EQ")

    def test_standard_3x_resolution_preserved(self):
        """Default leverage=3 resolution remains standard (EUR first preference)."""
        self.assertEqual(self.universe.get_execution_ticker("QQQ", "LONG", leverage=3), "QQQ3m_EQ")
        self.assertEqual(self.universe.get_execution_ticker("QQQ", "SHORT", leverage=3), "QQQSm_EQ")
        self.assertEqual(self.universe.get_execution_ticker("SPY", "LONG", leverage=3), "3USLm_EQ")
        self.assertEqual(self.universe.get_execution_ticker("SPY", "SHORT", leverage=3), "3USSm_EQ")

    def test_decision_card_leverage_tagging(self):
        """DecisionCard replaces leverage=5 and 5x tickers with 3x per policy."""
        debate_mock = MagicMock()
        debate_mock.consensus_decision = "BUY_ENTRY"
        debate_mock.conviction_score = 0.90
        debate_mock.synthesis = "High conviction tech breakout."

        # Pass 5x ticker ("5QQEl_EQ") and leverage=5
        card_5x = self.risk_manager.calculate_decision_card(
            ticker="QQQ",
            trading212_ticker="5QQEl_EQ",
            direction="LONG",
            metrics={"close": 120.0, "atr": 2.5},
            debate_outcome=debate_mock,
            account_equity=10000.0,
            leverage=5,
        )
        # Both leverage and ticker must be replaced with 3x EUR
        self.assertEqual(card_5x.leverage, 3)
        self.assertEqual(card_5x.trading212_ticker, "QQQ3m_EQ")

        card_3x = self.risk_manager.calculate_decision_card(
            ticker="QQQ",
            trading212_ticker="QQQ3m_EQ",
            direction="LONG",
            metrics={"close": 415.0, "atr": 8.5},
            debate_outcome=debate_mock,
            account_equity=10000.0,
            leverage=3,
        )
        self.assertEqual(card_3x.leverage, 3)
        self.assertEqual(card_3x.trading212_ticker, "QQQ3m_EQ")

    def test_validate_trade_rejects_excess_leverage_and_5x_tickers(self):
        """RiskManager.validate_trade strictly rejects cards with leverage > 3 or 5x tickers."""
        debate_mock = MagicMock()
        debate_mock.consensus_decision = "BUY_ENTRY"
        debate_mock.conviction_score = 0.90
        debate_mock.synthesis = "Test"

        # Card with 5x leverage
        card_invalid_leverage = DecisionCard(
            ticker="QQQ",
            trading212_ticker="QQQ3m_EQ",
            direction="LONG",
            decision="BUY_ENTRY",
            entry_price=400.0,
            stop_loss=390.0,
            trailing_stop_loss=390.0,
            stop_distance_pct=0.025,
            take_profit_1=410.0,
            take_profit_2=420.0,
            take_profit_3=430.0,
            position_size_shares=5.0,
            position_notional=2000.0,
            risk_amount=50.0,
            risk_pct_of_equity=0.005,
            conviction_score=0.90,
            rationale="Test",
            leverage=5,
        )
        valid, msg = self.risk_manager.validate_trade(card_invalid_leverage, account_cash=10000.0, existing_positions=[])
        self.assertFalse(valid)
        self.assertIn("exceeds maximum allowed cap", msg)

        # Card with prohibited 5x ticker
        card_invalid_ticker = DecisionCard(
            ticker="QQQ",
            trading212_ticker="QQQ5l_EQ",
            direction="LONG",
            decision="BUY_ENTRY",
            entry_price=100.0,
            stop_loss=95.0,
            trailing_stop_loss=95.0,
            stop_distance_pct=0.05,
            take_profit_1=105.0,
            take_profit_2=110.0,
            take_profit_3=115.0,
            position_size_shares=10.0,
            position_notional=1000.0,
            risk_amount=50.0,
            risk_pct_of_equity=0.005,
            conviction_score=0.90,
            rationale="Test",
            leverage=3,
        )
        valid_t, msg_t = self.risk_manager.validate_trade(card_invalid_ticker, account_cash=10000.0, existing_positions=[])
        self.assertFalse(valid_t)
        self.assertIn("Prohibited 5x execution ticker detected", msg_t)


if __name__ == "__main__":
    unittest.main()
