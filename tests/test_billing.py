import unittest
from unittest.mock import MagicMock, patch, AsyncMock
from fastapi.testclient import TestClient
import os
import json
import hmac
import hashlib

# Mock env vars before importing app
os.environ["SUPABASE_JWT_SECRET"] = "test-secret"
os.environ["LEMONSQUEEZY_API_KEY"] = "test-key"
os.environ["LEMONSQUEEZY_STORE_ID"] = "test-store"
os.environ["LEMONSQUEEZY_PRO_VARIANT_ID"] = "123"
os.environ["LEMONSQUEEZY_WEBHOOK_SECRET"] = "webhook-secret"
os.environ["ENABLE_BILLING"] = "true"

from main import app, verify_token

class TestBilling(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.user_id = "user-123"
        # Mock verify_token to bypass actual token verification
        app.dependency_overrides[verify_token] = lambda: self.user_id

    def tearDown(self):
        app.dependency_overrides = {}

    @patch("main.supabase")
    @patch("main.create_lemonsqueezy_checkout", new_callable=AsyncMock)
    def test_create_checkout_success(self, mock_create_checkout, mock_supabase):
        mock_create_checkout.return_value = "https://checkout.url"

        mock_profile = MagicMock()
        mock_profile.data = [{"id": "user-123", "email": "test@example.com", "subscription_status": "inactive"}]
        mock_supabase.table.return_value.select.return_value.eq.return_value.execute.return_value = mock_profile

        response = self.client.post("/api/billing/create-checkout", json={"plan": "pro"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["checkout_url"], "https://checkout.url")

    @patch("main.supabase")
    def test_get_subscription(self, mock_supabase):
        mock_response = MagicMock()
        mock_response.data = [{"subscription_plan": "pro", "subscription_status": "active", "lemonsqueezy_subscription_id": "sub-1"}]
        mock_supabase.table.return_value.select.return_value.eq.return_value.execute.return_value = mock_response

        response = self.client.get("/api/billing/subscription")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["plan"], "pro")
        self.assertEqual(response.json()["status"], "active")

    @patch("main.supabase")
    def test_webhook_verification_failure(self, mock_supabase):
        payload = {"meta": {"event_name": "subscription_created"}}
        headers = {"X-Signature": "wrong-signature"}

        response = self.client.post("/api/webhooks/lemonsqueezy", json=payload, headers=headers)
        self.assertEqual(response.status_code, 401)

    @patch("main.supabase")
    @patch("utils.billing.LEMONSQUEEZY_WEBHOOK_SECRET", "webhook-secret")
    def test_webhook_success(self, mock_supabase):
        payload_dict = {
            "meta": {
                "event_name": "subscription_created",
                "custom_data": {"user_id": "user-123"}
            },
            "data": {
                "id": "event-1",
                "attributes": {
                    "status": "active",
                    "variant_id": "123",
                    "customer_id": "456"
                }
            }
        }
        payload_bytes = json.dumps(payload_dict).encode('utf-8')

        # Calculate correct signature
        signature = hmac.new(b"webhook-secret", payload_bytes, hashlib.sha256).hexdigest()

        # Mock idempotency check
        mock_idempotency = MagicMock()
        mock_idempotency.data = []
        mock_supabase.table.return_value.select.return_value.eq.return_value.execute.return_value = mock_idempotency

        # Mock updates
        mock_supabase.table.return_value.insert.return_value.execute.return_value = MagicMock()
        mock_supabase.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock()

        response = self.client.post(
            "/api/webhooks/lemonsqueezy",
            content=payload_bytes,
            headers={"X-Signature": signature, "Content-Type": "application/json"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "success")

if __name__ == "__main__":
    unittest.main()
