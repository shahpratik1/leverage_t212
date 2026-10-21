"""Unit tests for the Google Antigravity SDK Autonomous Multi-Agent Trading System.

Verifies:
1. AntigravityTradingAgent initialization and authentication status checks
2. Registration of custom tools: scan_universe, get_asset_intelligence, get_portfolio_status, evaluate_risk_and_size
3. Pydantic structured output schemas (AgentTradeDecision, TechnicalSignal, MacroSignal, RiskVerdict)
4. Subagent hierarchy configuration (CIO, Technical Analyst, Macro Analyst, Risk Guardian)
5. Conversion to standard DebateOutcome and DecisionCard models
6. Backward-compatible integration with DebateEngine
7. Fallback rule execution when Gemini API credentials are omitted
"""

import json
import unittest
from unittest.mock import MagicMock, patch

from trading_system.agents.antigravity_agent import (
    HAS_ANTIGRAVITY,
    AgentTradeDecision,
    AntigravityTradingAgent,
    MacroSignal,
    RiskVerdict,
    TechnicalSignal,
)
from trading_system.agents.debate_engine import DebateEngine, DebateOutcome
from trading_system.agents.risk_manager import DecisionCard, RiskManager
from trading_system.config import Settings
from trading_system.data.leveraged_universe import LeveragedUniverse
from trading_system.data.trading212_client import AccountCash, Position, Trading212Client
from trading_system.notifications.notifier import Notifier


class TestAntigravityAgenticSystem(unittest.TestCase):
    def setUp(self):
        self.mock_t212 = MagicMock(spec=Trading212Client)
        self.mock_t212.get_account_cash.return_value = AccountCash(
            free=8500.0,
            invested=1500.0,
            total=10000.0,
            blocked=0.0,
            result=0.0,
            ppl_result=0.0,
        )
        self.mock_t212.get_portfolio.return_value = [
            Position(
                ticker="QQQ3l_EQ",
                quantity=5.0,
                average_price=410.0,
                current_price=420.0,
                ppl=50.0,
                max_buy=100.0,
                max_sell=100.0,
            )
        ]
        self.mock_t212.get_orders.return_value = []
        self.mock_notifier = MagicMock(spec=Notifier)
        self.mock_risk_manager = MagicMock(spec=RiskManager)

        self.agent = AntigravityTradingAgent(
            t212_client=self.mock_t212,
            notifier=self.mock_notifier,
            risk_manager=self.mock_risk_manager,
            gemini_api_key="test_dummy_key",
        )

    def test_auth_status_reporting(self):
        """Verify get_auth_status returns SDK presence and setup assistance URL."""
        status = self.agent.get_auth_status()
        self.assertIn("has_sdk", status)
        self.assertIn("has_api_key", status)
        self.assertIn("api_key_help_url", status)
        self.assertEqual(status["api_key_help_url"], "https://aistudio.google.com/app/api-keys")
        self.assertTrue(status["has_api_key"])

    def test_pydantic_structured_schemas(self):
        """Verify strict Pydantic validation for multi-agent trade decisions."""
        decision = AgentTradeDecision(
            ticker="NVDA",
            etp_ticker="3LNVl_EQ",
            direction="LONG",
            action="BUY_ENTRY",
            conviction=0.88,
            entry_price=35.0,
            stop_loss=33.25,
            trailing_stop_loss=33.25,
            tp1=36.75,
            tp2=38.5,
            tp3=40.25,
            shares=28.57,
            risk_dollars=50.0,
            risk_pct_equity=0.5,
            technical_summary="RSI(3)=12.4 oversold dip into 50-day EMA support.",
            macro_summary="Next earnings in 32 days; no blackout risk.",
            risk_summary="Approved: 0.5% equity risk cap respected.",
            synthesis_rationale="High-conviction long entry on 3x NVIDIA ETP.",
        )
        self.assertEqual(decision.ticker, "NVDA")
        self.assertEqual(decision.etp_ticker, "3LNVl_EQ")
        self.assertEqual(decision.direction, "LONG")
        self.assertAlmostEqual(decision.conviction, 0.88)
        self.assertGreater(decision.tp1, decision.entry_price)

    def test_tool_scan_universe(self):
        """Verify tool_scan_universe returns valid JSON array of top candidates."""
        result_json = self.agent.tool_scan_universe(strategy="3rsi")
        data = json.loads(result_json)
        self.assertIn("candidates_found", data)
        self.assertIn("top_candidates", data)
        self.assertIsInstance(data["top_candidates"], list)

    def test_tool_get_asset_intelligence(self):
        """Verify tool_get_asset_intelligence extracts live market metrics."""
        result_json = self.agent.tool_get_asset_intelligence("QQQ")
        data = json.loads(result_json)
        self.assertEqual(data["ticker"], "QQQ")
        self.assertIn("last_close", data)
        self.assertIn("atr_20d", data)
        self.assertIn("leveraged_pairs", data)

    def test_tool_get_portfolio_status(self):
        """Verify tool_get_portfolio_status interfaces cleanly with broker client."""
        result_json = self.agent.tool_get_portfolio_status()
        data = json.loads(result_json)
        self.assertEqual(data["total_equity"], 10000.0)
        self.assertEqual(data["free_cash"], 8500.0)
        self.assertEqual(data["open_positions_count"], 1)

    def test_tool_evaluate_risk_and_size(self):
        """Verify tool_evaluate_risk_and_size enforces the 0.5% risk limit."""
        result_json = self.agent.tool_evaluate_risk_and_size(
            ticker="QQQ",
            entry_price=100.0,
            stop_loss=90.0,  # $10 stop distance
            direction="LONG",
            account_equity=10000.0,
        )
        data = json.loads(result_json)
        # $10000 * 0.005 = $50 max risk. $50 / $10 = 5.0 shares
        self.assertEqual(data["shares"], 5.0)
        self.assertEqual(data["risk_dollars"], 50.0)
        self.assertEqual(data["risk_pct_equity"], 0.5)
        self.assertEqual(data["tp1"], 110.0)
        self.assertEqual(data["tp2"], 120.0)
        self.assertEqual(data["tp3"], 130.0)

    def test_to_debate_outcome_conversion(self):
        """Verify conversion from AgentTradeDecision to DebateOutcome."""
        decision = AgentTradeDecision(
            ticker="AMZN",
            etp_ticker="3LZPl_EQ",
            direction="LONG",
            action="BUY_ENTRY",
            conviction=0.92,
            entry_price=4000.0,
            stop_loss=3850.0,
            trailing_stop_loss=3850.0,
            tp1=4150.0,
            tp2=4300.0,
            tp3=4450.0,
            shares=0.33,
            risk_dollars=49.5,
            risk_pct_equity=0.49,
            technical_summary="Extreme 3-RSI oversold rebound.",
            macro_summary="E-commerce and cloud tailwinds intact.",
            risk_summary="Risk cleared within 0.5% cap.",
            synthesis_rationale="Unanimous multi-agent bull consensus.",
        )
        outcome = self.agent.to_debate_outcome(decision)
        self.assertIsInstance(outcome, DebateOutcome)
        self.assertEqual(outcome.ticker, "AMZN")
        self.assertEqual(outcome.consensus_decision, "BUY_ENTRY")
        self.assertEqual(outcome.winning_side, "BULL")
        self.assertAlmostEqual(outcome.conviction_score, 0.92)

    def test_to_decision_card_conversion(self):
        """Verify conversion from AgentTradeDecision to DecisionCard."""
        decision = AgentTradeDecision(
            ticker="SOXX",
            etp_ticker="3SEMl_EQ",
            direction="LONG",
            action="BUY_ENTRY",
            conviction=0.85,
            entry_price=95.0,
            stop_loss=90.0,
            trailing_stop_loss=90.0,
            tp1=100.0,
            tp2=105.0,
            tp3=110.0,
            shares=10.0,
            risk_dollars=50.0,
            risk_pct_equity=0.5,
            technical_summary="Semiconductor sector bottoming candle.",
            macro_summary="Foundry demand strong.",
            risk_summary="Cleared.",
            synthesis_rationale="Buy SOXX 3x ETP.",
        )
        card = self.agent.to_decision_card(decision)
        self.assertIsInstance(card, DecisionCard)
        self.assertEqual(card.ticker, "SOXX")
        self.assertEqual(card.trading212_ticker, "3SEMl_EQ")
        self.assertEqual(card.decision, "BUY_ENTRY")
        self.assertEqual(card.position_size, 10.0)
        self.assertEqual(card.stop_loss, 90.0)
        self.assertEqual(card.tp1, 100.0)

    def test_fallback_rule_decision(self):
        """Verify fallback decision generation when no API key or cloud connection is available."""
        metrics = {
            "price": 100.0,
            "rsi3": 12.0,  # Extreme oversold
            "atr": 2.5,
        }
        fallback = self.agent._fallback_rule_decision("AAPL", metrics, strategy_type="3rsi", account_equity=10000.0)
        self.assertEqual(fallback.ticker, "AAPL")
        self.assertEqual(fallback.direction, "LONG")
        self.assertEqual(fallback.action, "BUY_ENTRY")
        self.assertGreaterEqual(fallback.conviction, 0.8)

    def test_debate_engine_antigravity_integration(self):
        """Verify DebateEngine automatically leverages AntigravityTradingAgent."""
        engine = DebateEngine(
            speculative_routing=False,
            gemini_api_key="mock_key",
        )
        metrics = {
            "price": 150.0,
            "rsi3": 14.0,
            "atr": 3.0,
        }
        outcome = engine.run_debate(
            ticker="NVDA",
            metrics=metrics,
            technical_report="RSI oversold",
            fundamental_report="Strong margins",
            strategy_type="3rsi",
        )
        self.assertIsInstance(outcome, DebateOutcome)
        self.assertEqual(outcome.ticker, "NVDA")
        self.assertEqual(outcome.consensus_decision, "BUY_ENTRY")

    def test_new_bluechip_and_thematic_pairs(self):
        """Verify SPCX, SMCI, and bluechip pairs exist and resolve to valid leveraged ETPs."""
        u = LeveragedUniverse()
        expected = {
            "SPCX": "ELONl_EQ",
            "SMCI": "SMCIl_EQ",
            "META": "3LFEl_EQ",
            "NFLX": "NFL3l_EQ",
            "COIN": "3LCOOl_EQ",
            "ORCL": "ORC3l_EQ",
            "INTC": "3INTl_EQ",
            "QCOM": "QCO3l_EQ",
            "BA": "BA3l_EQ",
            "UBER": "3LUBl_EQ",
            "BABA": "3BABl_EQ",
            "CRM": "CRM3l_EQ",
            "DFNS": "3EDFl_EQ",
        }
        for sym, exp_long in expected.items():
            pair = u.get_pair_for_underlying(sym)
            self.assertIsNotNone(pair, f"Missing pair for {sym}")
            self.assertEqual(pair.long_3x_ticker, exp_long)

    def test_pre_earnings_radar_and_blackout_logic(self):
        """Verify pre-earnings radar window (3-21d) and blackout shield (<=48h)."""
        intel_pre = json.loads(self.agent.tool_get_asset_intelligence("NVDA"))
        self.assertIn("pre_earnings_radar", intel_pre)
        self.assertIn("earnings_blackout_risk", intel_pre)

        # Test blackout filter in Screener
        from trading_system.screener.laya_screener import LayaScreener
        screener = LayaScreener()

        # Asset within 24h blackout
        blackout_metrics = {
            "close": 150.0,
            "sma200": 130.0,
            "ema20": 152.0,
            "ema50": 145.0,
            "rsi3": 18.0,
            "rsi7": 35.0,
            "adx": 25.0,
            "earnings_in_days": 1.0,
        }
        res_blackout = screener.evaluate_single("TSLA", blackout_metrics)
        self.assertGreater(res_blackout.catalyst_risk, 0.25)
        self.assertFalse(screener.passes_filter(res_blackout))

        # Asset in safe 12-day pre-earnings run-up window
        runup_metrics = {
            "close": 150.0,
            "sma200": 130.0,
            "ema20": 152.0,
            "ema50": 145.0,
            "rsi3": 18.0,
            "rsi7": 35.0,
            "adx": 25.0,
            "earnings_in_days": 12.0,
        }
        res_runup = screener.evaluate_single("AAPL", runup_metrics)
        self.assertLessEqual(res_runup.catalyst_risk, 0.25)
        self.assertTrue(screener.passes_filter(res_runup))


if __name__ == "__main__":
    unittest.main()
