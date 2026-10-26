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
        """Major indices, commodities, and fixed income track 5x market availability and enforce 5x execution."""
        expected_5x = ["QQQ", "SPY", "GLD", "TLT", "MAGS", "^FTSE", "^GDAXI", "^STOXX50E"]
        for symbol in expected_5x:
            pair = self.universe.get_pair_for_underlying(symbol)
            self.assertIsNotNone(pair, f"Missing pair for {symbol}")
            self.assertTrue(pair.has_5x, f"Expected has_5x=True for {symbol}")
            # Policy: if 5x exists, available leverage multiplier is [5] (prioritizing 5x)
            self.assertEqual(self.universe.get_available_leverage_multipliers(symbol), [5])

    def test_single_stocks_capped_at_3x(self):
        """Single equities remain 3x due to European UCITS concentration regulations."""
        single_stocks = ["NVDA", "TSLA", "AAPL", "MSFT", "AMZN", "GOOGL", "TSM"]
        for symbol in single_stocks:
            pair = self.universe.get_pair_for_underlying(symbol)
            self.assertIsNotNone(pair, f"Missing pair for {symbol}")
            self.assertFalse(pair.has_5x, f"Expected has_5x=False for single stock {symbol}")
            self.assertEqual(self.universe.get_available_leverage_multipliers(symbol), [3])

    def test_5x_ticker_resolution_long_and_short(self):
        """Resolution of 5x tickers prioritizes 5x for both Long and Inverse Short."""
        # QQQ (Nasdaq 100): 5x resolves to 5QQEl_EQ / SQQEl_EQ
        self.assertEqual(self.universe.get_execution_ticker("QQQ", "LONG", leverage=5), "5QQEl_EQ")
        self.assertEqual(self.universe.get_execution_ticker("QQQ", "SHORT", leverage=5), "SQQEl_EQ")

        # SPY (S&P 500): 5x resolves to 5SPEl_EQ / SSPEl_EQ
        self.assertEqual(self.universe.get_execution_ticker("SPY", "LONG", leverage=5), "5SPEl_EQ")
        self.assertEqual(self.universe.get_execution_ticker("SPY", "SHORT", leverage=5), "SSPEl_EQ")

        # TLT (20+ Year Treasury Bond): 5x resolves to TLT5d_EQ / 5STLd_EQ
        self.assertEqual(self.universe.get_execution_ticker("TLT", "LONG", leverage=5), "TLT5d_EQ")
        self.assertEqual(self.universe.get_execution_ticker("TLT", "SHORT", leverage=5), "5STLd_EQ")

        # MAGS (Magnificent 7): 5x resolves to MAG7m_EQ
        self.assertEqual(self.universe.get_execution_ticker("MAGS", "LONG", leverage=5), "MAG7m_EQ")

    def test_5x_fallback_to_3x_when_unavailable(self):
        """Requesting 5x on a single stock gracefully falls back to 3x."""
        # NVDA has 3x Long (3LVEl_EQ) and 3x Short (3SVEl_EQ) in EUR
        self.assertEqual(self.universe.get_execution_ticker("NVDA", "LONG", leverage=5), "3LVEl_EQ")
        self.assertEqual(self.universe.get_execution_ticker("NVDA", "SHORT", leverage=5), "3SVEl_EQ")

        # TSM has 3x Long EUR (TS3El_EQ)
        self.assertEqual(self.universe.get_execution_ticker("TSM", "LONG", leverage=5), "TS3El_EQ")

    def test_standard_3x_resolution_preserved_when_no_5x(self):
        """Default leverage=3 resolution remains standard for assets without 5x."""
        self.assertEqual(self.universe.get_execution_ticker("NVDA", "LONG", leverage=3), "3LVEl_EQ")
        self.assertEqual(self.universe.get_execution_ticker("NVDA", "SHORT", leverage=3), "3SVEl_EQ")

    def test_decision_card_leverage_tagging(self):
        """DecisionCard resolves leverage=5 and 5x tickers when present per policy."""
        debate_mock = MagicMock()
        debate_mock.consensus_decision = "BUY_ENTRY"
        debate_mock.conviction_score = 0.90
        debate_mock.synthesis = "High conviction tech breakout."

        # Pass QQQ which has 5x leverage
        card_5x = self.risk_manager.calculate_decision_card(
            ticker="QQQ",
            trading212_ticker="5QQEl_EQ",
            direction="LONG",
            metrics={"close": 120.0, "atr": 2.5},
            debate_outcome=debate_mock,
            account_equity=10000.0,
            leverage=5,
        )
        self.assertEqual(card_5x.leverage, 5)
        self.assertEqual(card_5x.trading212_ticker, "5QQEl_EQ")

        # Single stock NVDA caps at 3x
        card_3x = self.risk_manager.calculate_decision_card(
            ticker="NVDA",
            trading212_ticker="3LVEl_EQ",
            direction="LONG",
            metrics={"close": 415.0, "atr": 8.5},
            debate_outcome=debate_mock,
            account_equity=10000.0,
            leverage=3,
        )
        self.assertEqual(card_3x.leverage, 3)
        self.assertEqual(card_3x.trading212_ticker, "3LVEl_EQ")

    def test_validate_trade_allows_5x_leverage_and_rejects_excess(self):
        """RiskManager.validate_trade allows 5x when configured and rejects leverage > MAX_LEVERAGE."""
        debate_mock = MagicMock()
        debate_mock.consensus_decision = "BUY_ENTRY"
        debate_mock.conviction_score = 0.90
        debate_mock.synthesis = "Test"

        # Card with 5x leverage should be accepted
        card_valid_5x = DecisionCard(
            ticker="QQQ",
            trading212_ticker="5QQEl_EQ",
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
        valid, msg = self.risk_manager.validate_trade(card_valid_5x, account_cash=10000.0, existing_positions=[])
        self.assertTrue(valid, f"Expected 5x to be accepted, failed with: {msg}")

        # Card with excess leverage (e.g. 6x) should be rejected
        card_invalid_leverage = DecisionCard(
            ticker="QQQ",
            trading212_ticker="5QQEl_EQ",
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
            leverage=6,
        )
        valid_6x, msg_6x = self.risk_manager.validate_trade(card_invalid_leverage, account_cash=10000.0, existing_positions=[])
        self.assertFalse(valid_6x)
        self.assertIn("exceeds maximum allowed cap", msg_6x)

    def test_etp_direction_authoritative_resolution(self):
        """Authoritatively resolve ETP direction as LONG or SHORT and ensure IONQ is LONG."""
        from trading_system.data.leveraged_universe import get_etp_direction, get_ticker_currency

        # IONQ 3x ETPs are spot LONG instruments
        self.assertEqual(self.universe.get_etp_direction("IONQl1_EQ"), "LONG")
        self.assertEqual(self.universe.get_etp_direction("ION3a_EQ"), "LONG")
        self.assertEqual(get_etp_direction("IONQl1_EQ"), "LONG")
        self.assertEqual(get_etp_direction("ION3a_EQ"), "LONG")

        # Standard Long ETPs
        self.assertEqual(self.universe.get_etp_direction("QQQ3m_EQ"), "LONG")
        self.assertEqual(self.universe.get_etp_direction("3LVEl_EQ"), "LONG")
        self.assertEqual(self.universe.get_etp_direction("5QQEl_EQ"), "LONG")

        # Inverse Short ETPs
        self.assertEqual(self.universe.get_etp_direction("QQQSm_EQ"), "SHORT")
        self.assertEqual(self.universe.get_etp_direction("3SVEl_EQ"), "SHORT")
        self.assertEqual(self.universe.get_etp_direction("SQQEl_EQ"), "SHORT")
        self.assertEqual(self.universe.get_etp_direction("3SNIl_EQ"), "SHORT")
        self.assertEqual(self.universe.get_etp_direction("3DESl_EQ"), "SHORT")
        self.assertEqual(get_etp_direction("QQQSm_EQ"), "SHORT")
        self.assertEqual(get_etp_direction("3SVEl_EQ"), "SHORT")

        # IONQ USD resolution & currency
        pair = self.universe.get_pair_for_etp("IONQl1_EQ")
        self.assertIsNotNone(pair)
        self.assertEqual(pair.underlying_symbol, "IONQ")
        self.assertEqual(get_ticker_currency("IONQl1_EQ"), "USD")

        # IONQ has no inverse short ticker, short execution ticker must return None
        self.assertIsNone(self.universe.get_execution_ticker("IONQ", "SHORT"))



if __name__ == "__main__":
    unittest.main()
