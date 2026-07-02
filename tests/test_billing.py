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
    def test_create_checkout_active_enterprise_returns_conflict(self, mock_supabase):
        mock_profile = MagicMock()
        mock_profile.data = [{
            "id": "user-123",
            "email": "test@example.com",
            "subscription_plan": "enterprise",
            "subscription_status": "active",
        }]
        mock_supabase.table.return_value.select.return_value.eq.return_value.execute.return_value = mock_profile

        response = self.client.post("/api/billing/create-checkout", json={"plan": "pro"})

        self.assertEqual(response.status_code, 409)
        payload = response.json()["detail"]
        self.assertEqual(payload["code"], "ACTIVE_SUBSCRIPTION")
        self.assertIn("active subscription", payload["message"].lower())

    @patch("main.supabase")
    def test_cancel_active_paid_user_success(self, mock_supabase):
        mock_profile = MagicMock()
        mock_profile.data = [{
            "id": "user-123",
            "subscription_plan": "pro",
            "subscription_status": "active",
        }]
        mock_supabase.table.return_value.select.return_value.eq.return_value.execute.return_value = mock_profile
        mock_supabase.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock()

        response = self.client.post("/api/billing/cancel")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["code"], "SUBSCRIPTION_CANCELLED")
        self.assertEqual(payload["effective_plan"], "free")
        self.assertEqual(payload["subscription_status"], "cancelled")
        self.assertTrue(payload["effective_at"])

    @patch("main.supabase")
    @patch("main.create_lemonsqueezy_checkout", new_callable=AsyncMock)
    def test_create_checkout_after_cancel_returns_checkout_url(self, mock_create_checkout, mock_supabase):
        mock_create_checkout.return_value = "https://checkout.url/resubscribe"

        mock_profile = MagicMock()
        mock_profile.data = [{
            "id": "user-123",
            "email": "test@example.com",
            "subscription_plan": "free",
            "subscription_status": "cancelled",
        }]
        mock_supabase.table.return_value.select.return_value.eq.return_value.execute.return_value = mock_profile

        response = self.client.post("/api/billing/create-checkout", json={"plan": "pro"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["checkout_url"], "https://checkout.url/resubscribe")

    @patch("main.supabase")
    def test_cancel_without_paid_subscription_returns_conflict(self, mock_supabase):
        mock_profile = MagicMock()
        mock_profile.data = [{
            "id": "user-123",
            "subscription_plan": "free",
            "subscription_status": "inactive",
        }]
        mock_supabase.table.return_value.select.return_value.eq.return_value.execute.return_value = mock_profile

        response = self.client.post("/api/billing/cancel")

        self.assertEqual(response.status_code, 409)
        payload = response.json()["detail"]
        self.assertEqual(payload["code"], "NO_ACTIVE_PAID_SUBSCRIPTION")

    @patch("main.supabase")
    @patch("main.create_lemonsqueezy_checkout", new_callable=AsyncMock)
    def test_create_checkout_profile_missing_returns_404(self, mock_create_checkout, mock_supabase):
        mock_create_checkout.return_value = "https://checkout.url"

        mock_profile = MagicMock()
        mock_profile.data = []
        mock_supabase.table.return_value.select.return_value.eq.return_value.execute.return_value = mock_profile

        response = self.client.post("/api/billing/create-checkout", json={"plan": "pro"})

        self.assertEqual(response.status_code, 404)
        payload = response.json()["detail"]
        self.assertEqual(payload["code"], "PROFILE_NOT_FOUND")

    @patch("main.supabase")
    @patch("main.create_lemonsqueezy_checkout", new_callable=AsyncMock)
    def test_create_checkout_creation_failure_returns_500(self, mock_create_checkout, mock_supabase):
        mock_create_checkout.return_value = None

        mock_profile = MagicMock()
        mock_profile.data = [{
            "id": "user-123",
            "email": "test@example.com",
            "subscription_status": "inactive",
        }]
        mock_supabase.table.return_value.select.return_value.eq.return_value.execute.return_value = mock_profile

        response = self.client.post("/api/billing/create-checkout", json={"plan": "pro"})

        self.assertEqual(response.status_code, 500)
        payload = response.json()["detail"]
        self.assertEqual(payload["code"], "CHECKOUT_CREATION_FAILED")

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
                "custom_data": {"user_id": "user-123"},
                "event_id": "evt-1"
            },
            "data": {
                "id": "sub-1",
                "attributes": {
                    "status": "active",
                    "variant_id": 123,
                    "customer_id": 456
                }
            }
        }
        payload_bytes = json.dumps(payload_dict).encode('utf-8')

        # Calculate correct signature
        signature = hmac.new(b"webhook-secret", payload_bytes, hashlib.sha256).hexdigest()

        # Mock idempotency check (not found)
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

        # Verify that idempotency check used event_id "evt-1" and NOT resource id "sub-1"
        mock_supabase.table.return_value.select.return_value.eq.assert_any_call("id", "evt-1")

    @patch("main.supabase")
    @patch("utils.billing.LEMONSQUEEZY_WEBHOOK_SECRET", "webhook-secret")
    def test_webhook_idempotency_duplicate(self, mock_supabase):
        payload_dict = {
            "meta": {
                "event_name": "subscription_created",
                "custom_data": {"user_id": "user-123"},
                "event_id": "evt-1"
            },
            "data": {
                "id": "sub-1",
                "attributes": {"status": "active"}
            }
        }
        payload_bytes = json.dumps(payload_dict).encode('utf-8')
        signature = hmac.new(b"webhook-secret", payload_bytes, hashlib.sha256).hexdigest()

        # Mock idempotency check (found)
        mock_idempotency = MagicMock()
        mock_idempotency.data = [{"id": "evt-1"}]
        mock_supabase.table.return_value.select.return_value.eq.return_value.execute.return_value = mock_idempotency

        response = self.client.post(
            "/api/webhooks/lemonsqueezy",
            content=payload_bytes,
            headers={"X-Signature": signature, "Content-Type": "application/json"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ignored")

if __name__ == "__main__":
    unittest.main()


class TestBillingAuth(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides = {}

    def test_create_checkout_without_token_returns_401(self):
        response = self.client.post("/api/billing/create-checkout", json={"plan": "pro"})
        self.assertEqual(response.status_code, 401)

    def test_cancel_without_token_returns_401(self):
        response = self.client.post("/api/billing/cancel")
        self.assertEqual(response.status_code, 401)
