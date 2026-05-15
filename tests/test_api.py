from main import app, verify_token
from fastapi.testclient import TestClient
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

# Must be set before importing main, which validates this at module load time
os.environ.setdefault("SUPABASE_JWT_SECRET", "test-secret")


class TestApi(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.user_id = "test-user-id"
        self._orig_healthcheck_deep_celery = os.environ.get(
            "HEALTHCHECK_DEEP_CELERY")
        self._redis_patcher = patch(
            "utils.rate_limit.redis_client", MagicMock())
        self._redis_patcher.start()
        # Mock verify_token to bypass actual token verification
        app.dependency_overrides[verify_token] = lambda: self.user_id

    def tearDown(self):
        # Clear the dependency override after each test
        app.dependency_overrides = {}
        self._redis_patcher.stop()
        if self._orig_healthcheck_deep_celery is None:
            os.environ.pop("HEALTHCHECK_DEEP_CELERY", None)
        else:
            os.environ["HEALTHCHECK_DEEP_CELERY"] = self._orig_healthcheck_deep_celery

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
    def test_get_monitor_reports_structured_payload(self, mock_supabase):
        monitor_id = "monitor-structured"

        mock_monitor_execute = MagicMock()
        mock_monitor_execute.data = [{"id": monitor_id}]

        mock_reports_execute = MagicMock()
        mock_reports_execute.data = [
            {
                "id": "report-structured-1",
                "created_at": datetime(2023, 1, 3, 12, 0, 0, tzinfo=timezone.utc).isoformat(),
                "item_count": 2,
                "report_json": {
                    "executive_summary": "Active exploitation observed on exposed services.",
                    "ranked_threats": [
                        {
                            "rank": 1,
                            "title": "Exploit chain in the wild",
                            "impact_score": 92,
                            "confidence_score": 84,
                            "urgency": "high",
                        },
                        {
                            "rank": 2,
                            "title": "Lower confidence follow-on issue",
                            "impact_score": 55,
                            "confidence_score": 60,
                            "urgency": "medium",
                        },
                    ],
                    "source_references": [
                        {"title": "Source A", "url": "https://example.com/a"},
                        {"title": "Source B", "url": "https://example.com/b"},
                    ],
                },
            }
        ]

        mock_supabase.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value = mock_monitor_execute
        mock_supabase.table.return_value.select.return_value.eq.return_value.order.return_value.execute.return_value = mock_reports_execute

        response = self.client.get(f"/api/monitors/{monitor_id}/reports")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data[0]["severity"], "high")
        self.assertEqual(
            data[0]["summary"], "Active exploitation observed on exposed services.")
        self.assertEqual(data[0]["executive_summary"],
                         "Active exploitation observed on exposed services.")
        self.assertEqual(len(data[0]["top_threats"]), 2)
        self.assertEqual(data[0]["top_threats"][0]["impact_score"], 92)
        self.assertEqual(len(data[0]["source_references"]), 2)

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

    @patch("main.supabase")
    def test_get_feed_structured_payload(self, mock_supabase):
        mock_feed_execute = MagicMock()
        mock_feed_execute.data = [
            {
                "id": "report-feed-1",
                "created_at": datetime(2023, 1, 4, 12, 0, 0, tzinfo=timezone.utc).isoformat(),
                "item_count": 1,
                "report_json": {
                    "executive_summary": "Critical issue requires immediate action.",
                    "ranked_threats": [
                        {"rank": 1, "title": "Critical issue",
                            "impact_score": 88, "confidence_score": 77},
                    ],
                    "source_references": [
                        {"title": "Source Feed", "url": "https://example.com/feed"},
                    ],
                },
                "monitors": {"query_text": "critical issue"},
            }
        ]

        mock_supabase.table.return_value.select.return_value.eq.return_value.order.return_value.range.return_value.execute.return_value = mock_feed_execute

        response = self.client.get("/api/feed")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["severity"], "high")
        self.assertEqual(data[0]["summary"],
                         "Critical issue requires immediate action.")
        self.assertEqual(data[0]["executive_summary"],
                         "Critical issue requires immediate action.")
        self.assertEqual(data[0]["top_threats"][0]["impact_score"], 88)

    @patch("main.celery_app.ping.delay")
    @patch("main.celery_app.app.control.inspect")
    @patch("main.redis.from_url")
    def test_health_celery_light_mode(self, mock_from_url, mock_inspect, mock_delay):
        os.environ["HEALTHCHECK_DEEP_CELERY"] = "false"

        mock_redis_client = MagicMock()
        mock_from_url.return_value = mock_redis_client

        inspect_instance = MagicMock()
        inspect_instance.ping.return_value = {"worker@local": {"ok": "pong"}}
        mock_inspect.return_value = inspect_instance

        response = self.client.get("/health/celery")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["mode"], "light")
        mock_delay.assert_not_called()
        inspect_instance.ping.assert_called_once()

    @patch("main.celery_app.ping.delay")
    @patch("main.celery_app.app.control.inspect")
    @patch("main.redis.from_url")
    def test_health_celery_deep_mode(self, mock_from_url, mock_inspect, mock_delay):
        os.environ["HEALTHCHECK_DEEP_CELERY"] = "true"

        mock_redis_client = MagicMock()
        mock_from_url.return_value = mock_redis_client

        async_result = MagicMock()
        async_result.get.return_value = "pong"
        mock_delay.return_value = async_result

        response = self.client.get("/health/celery")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["mode"], "deep")
        mock_delay.assert_called_once()
        async_result.get.assert_called_once_with(timeout=3)
        mock_inspect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
