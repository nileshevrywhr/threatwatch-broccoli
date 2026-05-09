import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone
from celery_tasks import scan_due_monitors


class TestSchedulerIntegration(unittest.TestCase):

    @patch('celery_tasks.supabase')
    @patch('celery_tasks.scan_monitor_task.delay')
    def test_scan_due_monitors_integration(self, mock_delay, mock_supabase):
        # Three monitors with different frequencies, all overdue
        past_iso = "2023-01-01T12:00:00+00:00"

        mock_data = [
            {
                "id": "monitor-1",
                "frequency": "daily",
                "next_run_at": past_iso,
                "query_text": "daily query",
                "user_id": "user-1",
                "active": True,
            },
            {
                "id": "monitor-2",
                "frequency": "weekly",
                "next_run_at": past_iso,
                "query_text": "weekly query",
                "user_id": "user-2",
                "active": True,
            },
            {
                "id": "monitor-3",
                "frequency": "monthly",
                "next_run_at": past_iso,
                "query_text": "monthly query",
                "user_id": "user-3",
                "active": True,
            },
        ]

        # Mock Supabase response chain
        # 1. Select query
        mock_select = MagicMock()
        mock_select.eq.return_value.lte.return_value.execute.return_value.data = mock_data
        mock_supabase.table.return_value.select.return_value = mock_select

        # 2. Upsert (batch next_run_at update)
        mock_supabase.table.return_value.upsert.return_value.execute.return_value.data = [
            {"id": m["id"]} for m in mock_data
        ]

        # Execute
        result = scan_due_monitors()

        # --- Verify each monitor was enqueued exactly once ---
        self.assertEqual(mock_delay.call_count, 3,
                         "Expected exactly 3 enqueue calls, one per monitor")

        enqueued_ids = {call.args[0] for call in mock_delay.call_args_list}
        self.assertEqual(enqueued_ids, {"monitor-1", "monitor-2", "monitor-3"})

        monitor_by_id = {m["id"]: m for m in mock_data}
        for call in mock_delay.call_args_list:
            called_id = call.args[0]
            self.assertEqual(
                call.kwargs.get("monitor_data"),
                monitor_by_id[called_id],
                f"monitor_data mismatch for {called_id}",
            )

        # --- Verify upsert was called with 3 updates, each with a future next_run_at ---
        upsert_call_args = mock_supabase.table.return_value.upsert.call_args
        self.assertIsNotNone(upsert_call_args, "Upsert was not called")

        update_payload = upsert_call_args[0][0]
        self.assertIsInstance(update_payload, list)
        self.assertEqual(len(update_payload), 3,
                         "Expected 3 items in upsert payload")

        now = datetime.now(timezone.utc)
        for item in update_payload:
            updated_next_run = datetime.fromisoformat(item["next_run_at"])
            self.assertGreater(
                updated_next_run, now,
                f"next_run_at for {item['id']} is not in the future: {updated_next_run}",
            )


if __name__ == '__main__':
    unittest.main()
