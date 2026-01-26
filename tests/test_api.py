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

if __name__ == "__main__":
    unittest.main()
