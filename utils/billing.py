import os
import hmac
import hashlib
import httpx
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

LEMONSQUEEZY_API_KEY = os.environ.get("LEMONSQUEEZY_API_KEY")
LEMONSQUEEZY_WEBHOOK_SECRET = os.environ.get("LEMONSQUEEZY_WEBHOOK_SECRET")
LEMONSQUEEZY_STORE_ID = os.environ.get("LEMONSQUEEZY_STORE_ID")
PRO_VARIANT_ID = os.environ.get("LEMONSQUEEZY_PRO_VARIANT_ID")
ENTERPRISE_VARIANT_ID = os.environ.get("LEMONSQUEEZY_ENTERPRISE_VARIANT_ID")
FRONTEND_BASE_URL = os.environ.get("FRONTEND_BASE_URL", "https://signalcanary.fyi")

LEMONSQUEEZY_API_URL = "https://api.lemonsqueezy.com/v1"

async def create_lemonsqueezy_checkout(user_id: str, email: str, plan: str) -> Optional[str]:
    """
    Creates a Lemon Squeezy checkout and returns the URL.
    """
    if not LEMONSQUEEZY_API_KEY or not LEMONSQUEEZY_STORE_ID:
        logger.error("Lemon Squeezy API Key or Store ID not configured")
        return None

    variant_id = PRO_VARIANT_ID if plan == "pro" else ENTERPRISE_VARIANT_ID
    if not variant_id:
        logger.error(f"Variant ID for plan {plan} not configured")
        return None

    payload = {
        "data": {
            "type": "checkouts",
            "attributes": {
                "checkout_data": {
                    "custom": {
                        "user_id": user_id,
                        "email": email
                    },
                    "email": email
                },
                "product_options": {
                    "redirect_url": f"{FRONTEND_BASE_URL}/billing/success"
                }
            },
            "relationships": {
                "store": {
                    "data": {
                        "type": "stores",
                        "id": str(LEMONSQUEEZY_STORE_ID)
                    }
                },
                "variant": {
                    "data": {
                        "type": "variants",
                        "id": str(variant_id)
                    }
                }
            }
        }
    }

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                f"{LEMONSQUEEZY_API_URL}/checkouts",
                json=payload,
                headers={
                    "Accept": "application/vnd.api+json",
                    "Content-Type": "application/vnd.api+json",
                    "Authorization": f"Bearer {LEMONSQUEEZY_API_KEY}"
                },
                timeout=10.0
            )
            response.raise_for_status()
            data = response.json()
            return data["data"]["attributes"]["url"]
        except Exception as e:
            logger.error(f"Error creating Lemon Squeezy checkout: {e}")
            if hasattr(e, 'response'):
                logger.error(f"Response body: {e.response.text}")
            return None

def verify_lemonsqueezy_signature(payload: bytes, signature: str) -> bool:
    """
    Verifies the Lemon Squeezy webhook signature.
    """
    if not LEMONSQUEEZY_WEBHOOK_SECRET:
        logger.error("LEMONSQUEEZY_WEBHOOK_SECRET not configured")
        return False

    secret = LEMONSQUEEZY_WEBHOOK_SECRET.encode('utf-8')
    digest = hmac.new(secret, payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, signature)
