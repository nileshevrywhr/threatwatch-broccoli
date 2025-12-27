import unittest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from main import app, verify_token

class TestGetFeedPerformance(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.mock_user_id = "user-123"
        self.app_dependency_overrides = {verify_token: lambda: self.mock_user_id}
        app.dependency_overrides = self.app_dependency_overrides

    def tearDown(self):
        app.dependency_overrides = {}

    def test_get_feed_call_count(self):
        """
        Verifies the number of DB calls made by get_feed.
        Expected: 1 (reports with embedded monitors).
        """

        # Mock Data with Embedded Monitors
        # The Supabase response structure changes when using embedding.
        # Instead of monitors being separate, they are inside the report object.
        mock_reports = [
            {
                "id": "r1",
                "monitor_id": "m1",
                "user_id": "user-123",
                "created_at": "2023-01-01T00:00:00Z",
                "item_count": 6,
                "monitors": {"query_text": "query1"}
            },
            {
                "id": "r2",
                "monitor_id": "m2",
                "user_id": "user-123",
                "created_at": "2023-01-02T00:00:00Z",
                "item_count": 0,
                "monitors": {"query_text": "query2"}
            }
        ]

        with patch("main.supabase") as mock_supabase:
            # Create a mock for the query builder
            reports_query = MagicMock()
            reports_query.execute.return_value.data = mock_reports
            # Chain setup
            reports_query.select.return_value = reports_query
            reports_query.eq.return_value = reports_query
            reports_query.order.return_value = reports_query
            reports_query.range.return_value = reports_query

            # We only expect "reports" table to be accessed
            def table_side_effect(table_name):
                if table_name == "reports":
                    return reports_query
                return MagicMock()

            mock_supabase.table.side_effect = table_side_effect

            response = self.client.get("/api/feed")

            self.assertEqual(response.status_code, 200)
            data = response.json()

            # Verify data integrity
            self.assertEqual(len(data), 2)
            self.assertEqual(data[0]["report_id"], "r1")
            self.assertEqual(data[0]["severity"], "high")
            self.assertEqual(data[0]["term"], "query1")

            self.assertEqual(data[1]["report_id"], "r2")
            self.assertEqual(data[1]["severity"], "low")
            self.assertEqual(data[1]["term"], "query2")

            # Verify call counts
            # Expecting 1 call to table("reports")
            self.assertEqual(mock_supabase.table.call_count, 1)
            mock_supabase.table.assert_called_once_with("reports")

if __name__ == "__main__":
    unittest.main()
