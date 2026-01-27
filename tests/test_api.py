import os
# Set a dummy JWT secret for testing purposes before importing main
os.environ['SUPABASE_JWT_SECRET'] = 'test-secret'

import unittest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from main import app, verify_token
from datetime import datetime, timezone


class TestApi(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.user_id = "test-user-id"
        # Mock verify_token to bypass actual token verification
        app.dependency_overrides[verify_token] = lambda: self.user_id

    def tearDown(self):
        # Clear the dependency override after each test
        app.dependency_overrides = {}

    @patch("main.supabase")
    def test_get_monitors_success(self, mock_supabase):
        # Mock the Supabase client and its chain of calls
        mock_execute = MagicMock()
        mock_execute.data = [
            {
                "id": "monitor-1",
                "query_text": "Test Monitor 1",
                "frequency": "daily",
                "created_at": datetime(2023, 1, 1, 12, 0, 0, tzinfo=timezone.utc).isoformat(),
                "next_run_at": datetime(2023, 1, 2, 12, 0, 0, tzinfo=timezone.utc).isoformat(),
                "active": True,
            },
            {
                "id": "monitor-2",
                "query_text": "Test Monitor 2",
                "frequency": "weekly",
                "created_at": datetime(2023, 1, 2, 12, 0, 0, tzinfo=timezone.utc).isoformat(),
                "next_run_at": datetime(2023, 1, 9, 12, 0, 0, tzinfo=timezone.utc).isoformat(),
                "active": False,
            },
        ]

        mock_supabase.table.return_value.select.return_value.eq.return_value.order.return_value.execute.return_value = mock_execute

        # Make the request
        response = self.client.get("/api/monitors")

        # Assertions
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 2)

        # Check the first monitor
        self.assertEqual(data[0]["monitor_id"], "monitor-1")
        self.assertEqual(data[0]["term"], "Test Monitor 1")
        self.assertEqual(data[0]["status"], "active")

        # Check the second monitor
        self.assertEqual(data[1]["monitor_id"], "monitor-2")
        self.assertEqual(data[1]["term"], "Test Monitor 2")
        self.assertEqual(data[1]["status"], "inactive")

    @patch("main.supabase")
    def test_get_monitors_no_monitors(self, mock_supabase):
        # Mock the Supabase client for a user with no monitors
        mock_execute = MagicMock()
        mock_execute.data = []
        mock_supabase.table.return_value.select.return_value.eq.return_value.order.return_value.execute.return_value = mock_execute

        # Make the request
        response = self.client.get("/api/monitors")

        # Assertions
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    @patch("main.supabase")
    def test_get_monitor_reports_success(self, mock_supabase):
        monitor_id = "monitor-123"

        # Mock monitor ownership check
        mock_monitor_execute = MagicMock()
        mock_monitor_execute.data = [{"id": monitor_id}]

        # Mock fetching reports
        mock_reports_execute = MagicMock()
        mock_reports_execute.data = [
            {
                "id": "report-1",
                "created_at": datetime(2023, 1, 1, 12, 0, 0, tzinfo=timezone.utc).isoformat(),
                "item_count": 3,
            },
            {
                "id": "report-2",
                "created_at": datetime(2023, 1, 2, 12, 0, 0, tzinfo=timezone.utc).isoformat(),
                "item_count": 7,
            },
        ]

        # Configure the mock to handle both calls
        mock_supabase.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value = mock_monitor_execute
        mock_supabase.table.return_value.select.return_value.eq.return_value.order.return_value.execute.return_value = mock_reports_execute

        response = self.client.get(f"/api/monitors/{monitor_id}/reports")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 2)
        self.assertEqual(data[0]["report_id"], "report-1")
        self.assertEqual(data[0]["severity"], "medium")
        self.assertEqual(data[1]["report_id"], "report-2")
        self.assertEqual(data[1]["severity"], "high")

    @patch("main.supabase")
    def test_get_monitor_reports_not_found(self, mock_supabase):
        monitor_id = "monitor-not-found"

        # Mock monitor ownership check to return no data
        mock_monitor_execute = MagicMock()
        mock_monitor_execute.data = []
        mock_supabase.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value = mock_monitor_execute

        response = self.client.get(f"/api/monitors/{monitor_id}/reports")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "Monitor not found"})

    @patch("main.supabase")
    def test_get_monitor_reports_no_reports(self, mock_supabase):
        monitor_id = "monitor-no-reports"

        # Mock monitor ownership check
        mock_monitor_execute = MagicMock()
        mock_monitor_execute.data = [{"id": monitor_id}]

        # Mock fetching reports to return no data
        mock_reports_execute = MagicMock()
        mock_reports_execute.data = []

        mock_supabase.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value = mock_monitor_execute
        mock_supabase.table.return_value.select.return_value.eq.return_value.order.return_value.execute.return_value = mock_reports_execute

        response = self.client.get(f"/api/monitors/{monitor_id}/reports")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

if __name__ == "__main__":
    unittest.main()
