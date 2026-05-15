import os
import unittest
from unittest.mock import MagicMock, patch

# Must be set before importing main, which validates this at module load time
os.environ.setdefault("SUPABASE_JWT_SECRET", "test-secret")

# noqa: E402 - intentional late import so env var is set first
from fastapi.testclient import TestClient  # noqa: E402
from main import app, verify_token  # noqa: E402


class TestDownloadReport(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.mock_user_id = "user-123"
        self._redis_patcher = patch(
            "utils.rate_limit.redis_client", MagicMock())
        self._redis_patcher.start()

    def test_download_report_success(self):
        """Test that an owner can download their report (redirects to PDF)."""
        report_id = "report-abc"
        pdf_url = "https://example.com/report.pdf"

        # Mock Supabase response for successful fetch
        mock_response = MagicMock()
        mock_response.data = [{"pdf_url": pdf_url}]

        # Mock the chain: supabase.table().select().eq().eq().execute()
        with patch("main.supabase") as mock_supabase:
            mock_supabase.table.return_value \
                .select.return_value \
                .eq.return_value \
                .eq.return_value \
                .execute.return_value = mock_response

            # Override authentication to return our mock user
            app.dependency_overrides[verify_token] = lambda: self.mock_user_id

            response = self.client.get(
                f"/api/reports/{report_id}/download", follow_redirects=False)

            # Verify we get a 307 Redirect
            self.assertEqual(response.status_code, 307)
            self.assertEqual(response.headers["location"], pdf_url)

    def test_download_report_not_found_or_unauthorized(self):
        """Test that if report is missing OR belongs to another user, we get 404."""
        report_id = "report-xyz"

        # Mock Supabase response returning empty list (Not Found or Unauthorized)
        mock_response = MagicMock()
        mock_response.data = []

        with patch("main.supabase") as mock_supabase:
            mock_supabase.table.return_value \
                .select.return_value \
                .eq.return_value \
                .eq.return_value \
                .execute.return_value = mock_response

            app.dependency_overrides[verify_token] = lambda: self.mock_user_id

            response = self.client.get(
                f"/api/reports/{report_id}/download", follow_redirects=False)

            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json(), {"detail": "Report not found"})

    def test_download_report_invalid_uuid(self):
        """Test that an invalid UUID returns 404 instead of 500."""
        report_id = "invalid-uuid"

        # Mock Supabase raising an exception resembling a UUID error
        with patch("main.supabase") as mock_supabase:
            mock_supabase.table.return_value \
                .select.return_value \
                .eq.return_value \
                .eq.return_value \
                .execute.side_effect = Exception("invalid input syntax for type uuid: \"invalid-uuid\"")

            app.dependency_overrides[verify_token] = lambda: self.mock_user_id

            response = self.client.get(
                f"/api/reports/{report_id}/download", follow_redirects=False)

            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json(), {"detail": "Report not found"})

    def test_download_report_db_unavailable(self):
        """Test that 503 is returned if Supabase client is None."""
        # Patch main.supabase to be None
        with patch("main.supabase", None):
            app.dependency_overrides[verify_token] = lambda: self.mock_user_id
            response = self.client.get(
                "/api/reports/123/download", follow_redirects=False)
            self.assertEqual(response.status_code, 503)

    def tearDown(self):
        app.dependency_overrides = {}
        self._redis_patcher.stop()


if __name__ == "__main__":
    unittest.main()
