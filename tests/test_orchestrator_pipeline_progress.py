import copy
import unittest
from unittest.mock import MagicMock, patch

from trading_system.orchestrator import Orchestrator


class TestOrchestratorPipelineProgress(unittest.TestCase):
    """Test real-time 7-step pipeline progress event broadcasting and snapshot enrichment."""

    def setUp(self):
        self.orchestrator = Orchestrator(dry_run=True)

    def test_default_pipeline_progress_state(self):
        """Orchestrator initializes with standby step 7 progress state."""
        self.assertIsNotNone(self.orchestrator._current_pipeline_progress)
        self.assertEqual(self.orchestrator._current_pipeline_progress["step"], 7)
        self.assertEqual(self.orchestrator._current_pipeline_progress["total_steps"], 7)
        self.assertEqual(self.orchestrator._current_pipeline_progress["name"], "Standby")
        self.assertEqual(self.orchestrator._current_pipeline_progress["status"], "standby")

    def test_read_model_snapshot_includes_pipeline_progress(self):
        """Dashboard read-model snapshot includes pipeline_progress dictionary."""
        snapshot = self.orchestrator.get_dashboard_snapshot()
        self.assertIn("pipeline_progress", snapshot)
        self.assertIsInstance(snapshot["pipeline_progress"], dict)
        self.assertEqual(snapshot["pipeline_progress"]["step"], 7)

    def test_broadcast_pipeline_progress_emits_sse(self):
        """_broadcast_pipeline_progress broadcasts over SSE with zero broker impact."""
        with patch.object(self.orchestrator, "_broadcast_sse") as mock_broadcast:
            progress = self.orchestrator._broadcast_pipeline_progress(
                step=3,
                name="Trend Filter",
                detail="Screening universe indicators against directional_trend rules.",
                active_assets=["QQQ3l_EQ", "3LUSl_EQ"],
                cycle_id="cycle-test-123",
            )
            self.assertEqual(progress["step"], 3)
            self.assertEqual(progress["name"], "Trend Filter")
            self.assertEqual(progress["active_assets"], ["QQQ3l_EQ", "3LUSl_EQ"])
            mock_broadcast.assert_called_once()
            args, _ = mock_broadcast.call_args
            self.assertEqual(args[0], "pipeline_progress")
            self.assertEqual(args[1]["step"], 3)
            self.assertEqual(args[1]["cycle_id"], "cycle-test-123")

    def test_scan_cycle_emits_intermediate_steps(self):
        """A scan cycle emits sequential step events."""
        events = []
        with patch.object(self.orchestrator, "_broadcast_sse", side_effect=lambda ev, data: events.append((ev, data))):
            with patch.object(self.orchestrator, "ingest_universe_data", return_value={}):
                self.orchestrator.run_cycle()

        step_events = [data for ev, data in events if ev == "pipeline_progress"]
        self.assertGreaterEqual(len(step_events), 3)
        self.assertEqual(step_events[0]["step"], 1)
        self.assertEqual(step_events[0]["name"], "Market Check")
        self.assertEqual(step_events[1]["step"], 2)
        self.assertEqual(step_events[1]["name"], "Fetch Data")
        self.assertEqual(step_events[-1]["step"], 7)
        self.assertEqual(step_events[-1]["name"], "Standby")

    def test_scan_cycle_halted_state_transitions_to_standby(self):
        """A halted system emits Step 1 then transitions directly to Standby (halted)."""
        events = []
        with patch.object(self.orchestrator, "_is_pm_halted", return_value=True):
            with patch.object(self.orchestrator, "_broadcast_sse", side_effect=lambda ev, data: events.append((ev, data))):
                self.orchestrator.run_cycle()

        step_events = [data for ev, data in events if ev == "pipeline_progress"]
        self.assertEqual(len(step_events), 2)
        self.assertEqual(step_events[0]["step"], 1)
        self.assertEqual(step_events[1]["step"], 7)
        self.assertEqual(step_events[1]["status"], "halted")


if __name__ == "__main__":
    unittest.main()
