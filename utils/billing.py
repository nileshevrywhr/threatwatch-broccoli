import os
import hmac
import hashlib
import httpx
import logging
from typing import Optional, Dict, Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

LEMONSQUEEZY_API_KEY = os.environ.get("LEMONSQUEEZY_API_KEY")
LEMONSQUEEZY_WEBHOOK_SECRET = os.environ.get("LEMONSQUEEZY_WEBHOOK_SECRET")
LEMONSQUEEZY_STORE_ID = os.environ.get("LEMONSQUEEZY_STORE_ID")
PRO_VARIANT_ID = os.environ.get("LEMONSQUEEZY_PRO_VARIANT_ID")
ENTERPRISE_VARIANT_ID = os.environ.get("LEMONSQUEEZY_ENTERPRISE_VARIANT_ID")
DEFAULT_FRONTEND_BASE_URL = "https://signalcanary.fyi"
FRONTEND_BASE_URL = os.environ.get("FRONTEND_BASE_URL", DEFAULT_FRONTEND_BASE_URL)

LEMONSQUEEZY_API_URL = "https://api.lemonsqueezy.com/v1"

__all__ = [
    "verify_lemonsqueezy_signature",
    "create_lemonsqueezy_checkout",
    "cancel_lemonsqueezy_subscription",
]


def _normalize_origin(raw_origin: Optional[str]) -> Optional[str]:
    if not raw_origin:
        return None

    origin = raw_origin.strip()
    if not origin:
        return None

    parsed = urlparse(origin)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        return None

    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        return None

    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def _is_allowed_checkout_origin(origin: str) -> bool:
    parsed = urlparse(origin)
    hostname = (parsed.hostname or "").lower()
    default_origin = _normalize_origin(FRONTEND_BASE_URL) or DEFAULT_FRONTEND_BASE_URL

    if origin == default_origin:
        return True

    if hostname in {"localhost", "127.0.0.1"}:
        return True

    if hostname.endswith(".vercel.app"):
        return True

    return False


def _resolve_checkout_base_url(app_origin: Optional[str]) -> str:
    default_origin = _normalize_origin(FRONTEND_BASE_URL) or DEFAULT_FRONTEND_BASE_URL
    candidate = _normalize_origin(app_origin)

    if not candidate:
        return default_origin

    if _is_allowed_checkout_origin(candidate):
        return candidate

    logger.warning("Ignoring unsupported checkout origin: %s", app_origin)
    return default_origin


def verify_lemonsqueezy_signature(body: bytes, signature: str) -> bool:
    """
    Verify the HMAC-SHA256 signature of a Lemon Squeezy webhook request.

    Args:
        body: Raw request body bytes
        signature: The X-Signature header value from the webhook

    Returns:
        True if the signature is valid, False otherwise
    """
    if not LEMONSQUEEZY_WEBHOOK_SECRET:
        logger.error("LEMONSQUEEZY_WEBHOOK_SECRET not configured - cannot verify signature")
        return False

    expected = hmac.new(
        key=LEMONSQUEEZY_WEBHOOK_SECRET.encode("utf-8"),
        msg=body,
        digestmod=hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(expected, signature)


async def create_lemonsqueezy_checkout(user_id: str, email: str, plan: str, app_origin: Optional[str] = None) -> Optional[str]:
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

    checkout_base_url = _resolve_checkout_base_url(app_origin)

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
                    "redirect_url": f"{checkout_base_url}/payment-success"
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

    headers = {
        "Authorization": f"Bearer {LEMONSQUEEZY_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/vnd.api+json"
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{LEMONSQUEEZY_API_URL}/checkouts",
                json=payload,
                headers=headers,
                timeout=30.0
            )

            if response.status_code in (200, 201):
                data = response.json()
                checkout_url = data["data"]["attributes"]["url"]
                logger.info(f"Checkout created for user {user_id}")
                return checkout_url
            else:
                logger.error(f"Lemon Squeezy API error: {response.status_code} - {response.text}")
                return None

    except Exception as e:
        logger.error(f"Error creating checkout: {str(e)}")
        return None


async def cancel_lemonsqueezy_subscription(subscription_id: str) -> Optional[Dict[str, Any]]:
    """Cancels a Lemon Squeezy subscription and returns parsed attributes."""
    if not LEMONSQUEEZY_API_KEY:
        logger.error("Lemon Squeezy API Key not configured")
        return None

    headers = {
        "Authorization": f"Bearer {LEMONSQUEEZY_API_KEY}",
        "Content-Type": "application/vnd.api+json",
        "Accept": "application/vnd.api+json",
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.delete(
                f"{LEMONSQUEEZY_API_URL}/subscriptions/{subscription_id}",
                headers=headers,
                timeout=30.0,
            )

            if response.status_code in (200, 201):
                data = response.json()
                attributes = data.get("data", {}).get("attributes", {})
                logger.info(f"Subscription cancelled on Lemon Squeezy: {subscription_id}")
                return {
                    "status": attributes.get("status"),
                    "cancelled": attributes.get("cancelled"),
                    "ends_at": attributes.get("ends_at"),
                }

            logger.error(
                "Lemon Squeezy cancel API error for subscription %s: %s - %s",
                subscription_id,
                response.status_code,
                response.text,
            )
            return None
    except Exception as e:
        logger.error("Error cancelling Lemon Squeezy subscription %s: %s", subscription_id, str(e))
        return None
