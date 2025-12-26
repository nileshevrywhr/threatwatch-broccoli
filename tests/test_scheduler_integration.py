import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone
from celery_tasks import scan_due_monitors

class TestSchedulerIntegration(unittest.TestCase):

    @patch('celery_tasks.supabase')
    @patch('celery_tasks.scan_monitor_task.delay')
    def test_scan_due_monitors_integration(self, mock_delay, mock_supabase):
        # Setup mock data
        now_iso = datetime.now(timezone.utc).isoformat()

        # Mock monitor that is due (run yesterday)
        monitor_id = "test-monitor-123"
        last_run_iso = "2023-01-01T12:00:00+00:00" # Far past

        mock_data = [{
            "id": monitor_id,
            "frequency": "daily",
            "next_run_at": last_run_iso,
            "query_text": "test query",
            "user_id": "user-1",
            "active": True
        }]

        # Mock Supabase response chain
        # 1. Select query
        mock_select = MagicMock()
        mock_select.eq.return_value.lte.return_value.execute.return_value.data = mock_data
        mock_supabase.table.return_value.select.return_value = mock_select

        # 2. Upsert query (Batch update)
        mock_upsert = MagicMock()
        mock_upsert.execute.return_value.data = [{"id": monitor_id}]
        mock_supabase.table.return_value.upsert.return_value = mock_upsert

        # Execute
        result = scan_due_monitors()

        # Verify
        # 1. Task was enqueued with monitor data
        mock_delay.assert_called_with(monitor_id, monitor_data=mock_data[0])

        # 2. Supabase Upsert was called with a FUTURE date
        upsert_call_args = mock_supabase.table.return_value.upsert.call_args
        upsert_payload = upsert_call_args[0][0] # First arg of upsert() which is the list of dicts

        # Should be a list with 1 item
        self.assertIsInstance(upsert_payload, list)
        self.assertEqual(len(upsert_payload), 1)

        updated_monitor = upsert_payload[0]

        # Verify strict payload (only id and next_run_at)
        self.assertEqual(set(updated_monitor.keys()), {"id", "next_run_at"})

        updated_next_run = datetime.fromisoformat(updated_monitor['next_run_at'])
        self.assertGreater(updated_next_run, datetime.now(timezone.utc))

        print(f"Verified next_run_at update via upsert: {updated_next_run} is in the future")

if __name__ == '__main__':
    unittest.main()
