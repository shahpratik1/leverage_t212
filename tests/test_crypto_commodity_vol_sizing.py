"""Tests for Crypto & Commodity Universe expansion and dynamic Volatility-Targeted Sizing."""

import unittest
from trading_system.data.leveraged_universe import LeveragedUniverse
from trading_system.agents.risk_manager import RiskManager


class TestCryptoCommodityVolSizing(unittest.TestCase):
    def setUp(self):
        self.universe = LeveragedUniverse()
        self.risk_manager = RiskManager(
            max_risk_per_trade=0.005,  # 0.5%
            max_position_pct=0.05,     # 5.0%
            target_annual_volatility=0.20,
        )

    def test_crypto_universe_pairs_exist(self):
        """Verify Bitcoin and Ethereum 3x pairs resolve correctly."""
        btc_pair = self.universe.get_pair_for_underlying("BTC-USD")
        self.assertIsNotNone(btc_pair)
        self.assertEqual(btc_pair.category, "Crypto")
        self.assertEqual(btc_pair.long_3x_ticker, "BTC3EURs_EQ")
        self.assertEqual(btc_pair.short_3x_ticker, "BTC3SEURs_EQ")

        eth_pair = self.universe.get_pair_for_underlying("ETH-USD")
        self.assertIsNotNone(eth_pair)
        self.assertEqual(eth_pair.category, "Crypto")
        self.assertEqual(eth_pair.long_3x_ticker, "ETH3EURs_EQ")
        self.assertEqual(eth_pair.short_3x_ticker, "ETH3SEURs_EQ")

        # Execution ticker lookup
        long_btc = self.universe.get_execution_ticker("BTC-USD", "LONG")
        short_btc = self.universe.get_execution_ticker("BTC-USD", "SHORT")
        self.assertEqual(long_btc, "BTC3EURs_EQ")
        self.assertEqual(short_btc, "BTC3SEURs_EQ")

    def test_commodity_universe_pairs_exist(self):
        """Verify Copper and Brent Crude 3x pairs resolve correctly."""
        copper_pair = self.universe.get_pair_for_underlying("HG=F")
        self.assertIsNotNone(copper_pair)
        self.assertEqual(copper_pair.category, "Commodity")
        self.assertEqual(copper_pair.long_3x_ticker, "3HCLm_EQ")
        self.assertEqual(copper_pair.short_3x_ticker, "3HCSm_EQ")

        brent_pair = self.universe.get_pair_for_underlying("BNO")
        self.assertIsNotNone(brent_pair)
        self.assertEqual(brent_pair.category, "Commodity")
        self.assertEqual(brent_pair.long_3x_ticker, "3BRLm_EQ")
        self.assertEqual(brent_pair.short_3x_ticker, "3BRSm_EQ")

    def test_volatility_targeted_budget_formula(self):
        """Test the mathematical behavior of calculate_volatility_targeted_size."""
        equity = 10000.0  # $10k account
        # Ultra-low volatility asset: 5% annualized vol
        # Budget = 500 * (0.20 / (3 * 0.05)) = 500 * (0.20 / 0.15) = 666.67
        # Capped at 5% max position notional = $500.00
        low_vol_size = self.risk_manager.calculate_volatility_targeted_size(
            account_equity=equity,
            annualized_vol=0.05,
            leverage=3,
        )
        self.assertAlmostEqual(low_vol_size, 500.0, places=2)

        # Extreme volatility asset: 80% annualized vol (e.g. Crypto 3x or Oil spike)
        # Budget = 500 * (0.20 / (3 * 0.80)) = 500 * (0.20 / 2.40) = 41.67
        # Should be strictly bounded below 5% max position notional ($500)
        extreme_vol_size = self.risk_manager.calculate_volatility_targeted_size(
            account_equity=equity,
            annualized_vol=0.80,
            leverage=3,
        )
        self.assertAlmostEqual(extreme_vol_size, 41.67, places=1)
        self.assertLess(extreme_vol_size, 500.0)

    def test_calculate_position_size_damps_on_high_vol(self):
        """Test that position sizing shrinks when realized vol is elevated."""
        equity = 10000.0
        entry_price = 100.0
        stop_loss = 98.0  # 2% stop distance ($50 risk = $2500 max risk notional)

        # Normal/moderate volatility (20%)
        shares_norm, notional_norm, risk_norm = self.risk_manager.calculate_position_size(
            account_equity=equity,
            entry_price=entry_price,
            stop_loss=stop_loss,
            realized_vol=0.20,
            leverage=3,
        )

        # High volatility asset (75%)
        shares_high, notional_high, risk_high = self.risk_manager.calculate_position_size(
            account_equity=equity,
            entry_price=entry_price,
            stop_loss=stop_loss,
            realized_vol=0.75,
            leverage=3,
        )

        # High volatility must produce fewer shares and smaller notional exposure
        self.assertLess(notional_high, notional_norm)
        self.assertLess(shares_high, shares_norm)

    def test_decision_card_carries_realized_vol(self):
        """Test that generate_decision_card properly records realized_vol and sizes correctly."""
        class DummyDebate:
            consensus_decision = "BUY_ENTRY"
            conviction_score = 0.85
            synthesis = "High conviction mean reversion on oversold crypto"

        metrics = {
            "close": 50.0,
            "atr": 2.5,
            "realized_vol": 0.65,
            "earnings_in_days": None,
        }

        card = self.risk_manager.generate_decision_card(
            ticker="BTC-USD",
            trading212_ticker="3BTCl_EQ",
            direction="LONG",
            metrics=metrics,
            debate_outcome=DummyDebate(),
            account_equity=10000.0,
            strategy_type="3rsi_mean_reversion",
            leverage=3,
        )

        self.assertEqual(card.realized_vol, 0.65)
        self.assertEqual(card.ticker, "BTC-USD")
        self.assertEqual(card.trading212_ticker, "BTC3EURs_EQ")
        self.assertEqual(card.decision, "BUY_ENTRY")
        # Volatility budget for 0.65 vol: 10000 * (0.20 / 1.95) = ~102.56
        self.assertLess(card.position_notional, 500.0)


if __name__ == "__main__":
    unittest.main()
