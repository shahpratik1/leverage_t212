"""Unit tests for Server-Sent Events (SSE) live telemetry stream."""

import json
import queue
import unittest
from trading_system.dashboard.server import SSEBroadcaster, build_dashboard_data


class TestDashboardSSE(unittest.TestCase):
    """Test suite for SSE Broadcaster and real-time event streaming."""

    def setUp(self):
        self.broadcaster = SSEBroadcaster()

    def test_subscribe_and_broadcast(self):
        """Verify broadcaster delivers events to active subscribers."""
        q1 = self.broadcaster.subscribe()
        q2 = self.broadcaster.subscribe()

        test_data = {"ticker": "QQQ3l_EQ", "price": 412.50}
        self.broadcaster.broadcast("trade_update", test_data)

        # Both subscribers receive event
        msg1 = q1.get(timeout=1.0)
        msg2 = q2.get(timeout=1.0)

        self.assertEqual(msg1["event"], "trade_update")
        self.assertEqual(msg1["data"]["ticker"], "QQQ3l_EQ")
        self.assertEqual(msg2["event"], "trade_update")
        self.assertEqual(msg2["data"]["price"], 412.50)

    def test_unsubscribe(self):
        """Verify unsubscribed queues no longer receive broadcasts."""
        q = self.broadcaster.subscribe()
        self.broadcaster.unsubscribe(q)

        self.broadcaster.broadcast("ping", {"status": "alive"})
        with self.assertRaises(queue.Empty):
            q.get(timeout=0.1)

    def test_dead_queue_pruning(self):
        """Verify full queues are safely pruned without throwing exceptions."""
        q = self.broadcaster.subscribe()
        # Max out the queue to test overflow pruning
        while not q.full():
            q.put_nowait({"filler": True})

        # Broadcasting to a full queue should drop the subscriber cleanly
        self.broadcaster.broadcast("new_event", {"val": 123})
        self.assertNotIn(q, self.broadcaster._subscribers)

    def test_dashboard_data_snapshot_structure(self):
        """Verify snapshot dictionary generated for SSE contains required fields."""
        data = build_dashboard_data()
        self.assertIn("active_signals", data)
        self.assertIn("generated_at", data)
        self.assertIn("mode", data)


if __name__ == "__main__":
    unittest.main()
