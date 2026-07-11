import os
import logging
import redis
from datetime import datetime, timezone
from typing import Literal, List, Optional, Any

from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi import Request
from pydantic import BaseModel, Field
from supabase import create_client, Client

import celery_app
from celery_tasks import scan_monitor_task
from utils.schedule_utils import calculate_next_run_at
from utils.auth import verify_token
from utils.billing import (
    create_lemonsqueezy_checkout,
    verify_lemonsqueezy_signature,
    cancel_lemonsqueezy_subscription,
)
from utils.rate_limit import RateLimiter, IPRateLimiter

# Configure Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Validate Essential Environment Variables at Startup
if not os.environ.get("SUPABASE_JWT_SECRET"):
    logger.critical("SUPABASE_JWT_SECRET is missing. Server cannot start.")
    raise RuntimeError("SUPABASE_JWT_SECRET environment variable is required.")

app = FastAPI()

# CORS Configuration
# Rules:
# 1. ALLOWED_ORIGINS: Comma-separated list of exact origins (default: [])
# 2. ALLOWED_ORIGIN_REGEX: Regex string for Vercel preview/branch deploys (default: None)
# 3. If ALLOWED_ORIGIN_REGEX is not set, no regex matching is applied.
# 4. If ALLOWED_ORIGINS is not set, it defaults to empty list.
# 5. Localhost must be explicitly added to ALLOWED_ORIGINS env var to work.

allowed_origins_env = os.environ.get("ALLOWED_ORIGINS")
if allowed_origins_env:
    allow_origins = [origin.strip()
                     for origin in allowed_origins_env.split(",") if origin.strip()]
else:
    allow_origins = []

allow_origin_regex = os.environ.get("ALLOWED_ORIGIN_REGEX")

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_origin_regex=allow_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Supabase Client Setup
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        logger.error(f"Failed to initialize Supabase client: {e}")


class MonitorRequest(BaseModel):
    # user_id is removed as it's derived from the token
    term: str = Field(..., min_length=1, max_length=100)
    frequency: Literal['daily', 'weekly', 'monthly']


class MonitorResponse(BaseModel):
    monitor_id: str
    term: str
    frequency: str
    created_at: datetime
    next_run_at: datetime
    status: str


class ReportResponse(BaseModel):
    report_id: str
    created_at: datetime
    severity: str
    summary: str
    status: str
    download_url: str
    executive_summary: Optional[str] = None
    top_threats: Optional[List[Any]] = None
    source_references: Optional[List[Any]] = None


def _derive_report_severity(report: dict) -> str:
    report_json = report.get("report_json") or {}
    ranked_threats = report_json.get(
        "ranked_threats") if isinstance(report_json, dict) else None
    if isinstance(ranked_threats, list) and ranked_threats:
        top_threat = ranked_threats[0] if isinstance(
            ranked_threats[0], dict) else {}
        impact_score = top_threat.get("impact_score", 0)
        try:
            impact_score = float(impact_score)
        except (TypeError, ValueError):
            impact_score = 0

        if impact_score >= 80:
            return "high"
        if impact_score >= 50:
            return "medium"
        return "low"

    item_count = report.get("item_count", 0)
    if item_count > 5:
        return "high"
    if item_count > 0:
        return "medium"
    return "low"


@app.post("/api/monitors", dependencies=[Depends(RateLimiter(requests=10, window=60, fail_closed=True))])
async def create_monitor(monitor: MonitorRequest, user_id: str = Depends(verify_token)):
    if not supabase:
        raise HTTPException(
            status_code=503, detail="Database service unavailable")

    try:
        profile_res = supabase.table("profiles").select("subscription_plan, subscription_status").eq("id", user_id).execute()
        profile = profile_res.data[0] if profile_res.data else {}
        subscription_plan = profile.get("subscription_plan", "free")
        subscription_status = profile.get("subscription_status", "inactive")

        if subscription_status != "active":
            raise HTTPException(status_code=402, detail="Active subscription required")

        plan_limits = {"free": 0, "pro": 10, "enterprise": 50}
        max_monitors = plan_limits.get(subscription_plan, 0)

        if max_monitors == 0:
            raise HTTPException(
                status_code=403,
                detail="Free plan does not allow monitor creation. Upgrade to Pro or Enterprise."
            )

        existing_res = supabase.table("monitors").select("id", count="exact").eq("user_id", user_id).eq("active", True).execute()
        if (existing_res.count or 0) >= max_monitors:
            raise HTTPException(
                status_code=429,
                detail=f"Monitor limit reached (max {max_monitors} active monitors for {subscription_plan} plan)"
            )

        now = datetime.now(timezone.utc)
        # We want the *next* scheduled run to be in the future,
        # but we also want to trigger an immediate scan now.
        # calculate_next_run_at(freq, now) returns now + freq (strictly future).
        next_run_at = calculate_next_run_at(monitor.frequency, now)

        new_monitor = {
            "user_id": user_id,
            "query_text": monitor.term,
            "frequency": monitor.frequency,
            "next_run_at": next_run_at.isoformat(),
            "active": True,
            # 'created_at' is usually handled by DB default, but we can send it if needed.
            # Assuming DB defaults handle 'created_at' and 'id'.
        }

        # Insert into Supabase
        response = supabase.table("monitors").insert(new_monitor).execute()

        if not response.data:
            raise HTTPException(
                status_code=500, detail="Failed to create monitor")

        monitor_id = response.data[0].get("id")

        # Trigger immediate scan asynchronously
        # We use delay() to send it to the Celery broker.
        # This returns immediately (<200ms requirement).
        # Optimization: Pass the new monitor data to avoid an extra DB read in the worker
        task_payload = new_monitor.copy()
        task_payload["id"] = monitor_id
        scan_monitor_task.delay(monitor_id, monitor_data=task_payload)

        return {"monitor_id": monitor_id}

    except Exception as e:
        logger.error(f"Error creating monitor: {e}")
        raise HTTPException(status_code=500, detail="Internal Server Error")


@app.get("/api/monitors", response_model=List[MonitorResponse], dependencies=[Depends(RateLimiter(requests=60, window=60))])
async def get_monitors(user_id: str = Depends(verify_token)):
    if not supabase:
        raise HTTPException(
            status_code=503, detail="Database service unavailable")

    try:
        response = supabase.table("monitors") \
            .select("id, query_text, frequency, created_at, next_run_at, active") \
            .eq("user_id", user_id) \
            .order("created_at", desc=True) \
            .execute()

        if not response.data:
            return []

        monitors = [
            MonitorResponse(
                monitor_id=m["id"],
                term=m["query_text"],
                frequency=m["frequency"],
                created_at=m["created_at"],
                next_run_at=m["next_run_at"],
                status="active" if m["active"] else "inactive"
            ) for m in response.data
        ]
        return monitors

    except Exception as e:
        logger.error(f"Error fetching monitors: {e}")
        raise HTTPException(status_code=500, detail="Internal Server Error")


@app.get("/api/monitors/{monitor_id}/reports", response_model=List[ReportResponse], dependencies=[Depends(RateLimiter(requests=60, window=60))])
async def get_monitor_reports(monitor_id: str, user_id: str = Depends(verify_token)):
    if not supabase:
        raise HTTPException(
            status_code=503, detail="Database service unavailable")

    try:
        # 1. Verify monitor ownership
        monitor_response = supabase.table("monitors").select("id").eq(
            "id", monitor_id).eq("user_id", user_id).execute()
        if not monitor_response.data:
            raise HTTPException(status_code=404, detail="Monitor not found")

        # 2. Fetch reports for the monitor
        reports_response = supabase.table("reports").select(
            "*").eq("monitor_id", monitor_id).order("created_at", desc=True).execute()

        if not reports_response.data:
            return []

        # 3. Construct response
        reports = []
        for report in reports_response.data:
            item_count = report.get("item_count", 0)
            report_json = report.get("report_json") or {}

            severity = _derive_report_severity(report)

            executive_summary = report_json.get(
                "executive_summary") if isinstance(report_json, dict) else None
            summary = executive_summary or f"Found {item_count} relevant threat items"

            top_threats = []
            source_references = []
            if isinstance(report_json, dict):
                ranked_threats = report_json.get("ranked_threats") or []
                if isinstance(ranked_threats, list):
                    top_threats = ranked_threats[:3]
                refs = report_json.get("source_references") or []
                if isinstance(refs, list):
                    source_references = refs[:5]

            report_item = ReportResponse(
                report_id=report["id"],
                created_at=report["created_at"],
                severity=severity,
                summary=summary,
                status="completed",
                download_url=f"/api/reports/{report['id']}/download",
                executive_summary=executive_summary,
                top_threats=top_threats,
                source_references=source_references,
            )
            reports.append(report_item)

        return reports

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching reports for monitor {monitor_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal Server Error")


@app.post("/api/monitors/{monitor_id}/test", dependencies=[Depends(RateLimiter(requests=5, window=60, fail_closed=True))])
async def test_monitor(monitor_id: str, user_id: str = Depends(verify_token)):
    if supabase and os.environ.get("ENABLE_BILLING", "false").lower() in ("true", "1", "yes"):
        profile_res = supabase.table("profiles").select("subscription_status").eq("id", user_id).execute()
        status = profile_res.data[0].get("subscription_status") if profile_res.data else "inactive"
        if status != "active":
            raise HTTPException(status_code=402, detail="Active subscription required")
    """
    Triggers an immediate scan for a specific monitor.
    Does not synchronously validate existence (worker handles it).
    Returns the Celery task ID.
    """
    try:
        task = scan_monitor_task.delay(monitor_id)
        return {"task_id": task.id}
    except Exception as e:
        logger.error(
            f"Error triggering test scan for monitor {monitor_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal Server Error")


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.get("/api/reports/{report_id}/download", dependencies=[Depends(RateLimiter(requests=120, window=300))])
async def download_report(report_id: str, user_id: str = Depends(verify_token)):
    if supabase and os.environ.get("ENABLE_BILLING", "false").lower() in ("true", "1", "yes"):
        profile_res = supabase.table("profiles").select("subscription_status").eq("id", user_id).execute()
        status = profile_res.data[0].get("subscription_status") if profile_res.data else "inactive"
        if status != "active":
            raise HTTPException(status_code=402, detail="Active subscription required")
    if not supabase:
        raise HTTPException(status_code=503, detail="Database service unavailable")
    try:
        # Fetch report verifying ownership
        # We explicitly catch API errors that might result from invalid UUIDs
        try:
            response = supabase.table("reports").select("pdf_url").eq(
                "id", report_id).eq("user_id", user_id).execute()
        except Exception as e:
            # Check if this is an invalid input syntax error (e.g. invalid UUID)
            if "invalid input syntax for type uuid" in str(e) or "22P02" in str(e):
                raise HTTPException(status_code=404, detail="Report not found")
            raise e

        if not response.data:
            raise HTTPException(status_code=404, detail="Report not found")

        pdf_url = response.data[0].get("pdf_url")
        if not pdf_url:
            raise HTTPException(status_code=404, detail="Report URL not found")

        return RedirectResponse(url=pdf_url, status_code=307)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in download_report: {e}")
        raise HTTPException(status_code=500, detail="Internal Server Error")


@app.get("/api/feed", dependencies=[Depends(RateLimiter(requests=300, window=300))])
def get_feed(limit: int = 20, offset: int = 0, user_id: str = Depends(verify_token)):
    if not supabase:
        raise HTTPException(
            status_code=503, detail="Database service unavailable")

    try:
        # 1. Fetch Reports with Monitors (N+1 Optimization)
        # We use Resource Embedding to fetch related monitor data in a single query
        reports_response = supabase.table("reports")\
            .select("*, monitors(id, query_text)")\
            .eq("user_id", user_id)\
            .order("created_at", desc=True)\
            .range(offset, offset + limit - 1)\
            .execute()

        reports = reports_response.data
        if not reports:
            return []

        # 2. Construct Response
        feed = []
        for report in reports:
            item_count = report.get("item_count", 0)
            report_json = report.get("report_json") or {}

            severity = _derive_report_severity(report)

            executive_summary = report_json.get(
                "executive_summary") if isinstance(report_json, dict) else None
            summary = executive_summary or f"Found {item_count} relevant threat items"

            top_threats = []
            source_references = []
            if isinstance(report_json, dict):
                ranked_threats = report_json.get("ranked_threats") or []
                if isinstance(ranked_threats, list):
                    top_threats = ranked_threats[:3]
                refs = report_json.get("source_references") or []
                if isinstance(refs, list):
                    source_references = refs[:5]

            # Extract term from embedded monitor data
            term = "Unknown Monitor"
            monitor_data = report.get("monitors")
            if monitor_data and isinstance(monitor_data, dict):
                term = monitor_data.get("query_text", "Unknown Monitor")
            # Handle case where monitor might be null or format differs
            elif monitor_data and isinstance(monitor_data, list) and len(monitor_data) > 0:
                term = monitor_data[0].get("query_text", "Unknown Monitor")

            feed_item = {
                "report_id": report["id"],
                "term": term,
                "created_at": report["created_at"],
                "status": "completed",
                "severity": severity,
                "summary": summary,
                "executive_summary": executive_summary,
                "top_threats": top_threats,
                "source_references": source_references,
                "download_url": f"/api/reports/{report['id']}/download"
            }
            feed.append(feed_item)

        return feed

    except Exception as e:
        logger.error(f"Error in get_feed: {e}")
        raise HTTPException(status_code=500, detail="Internal Server Error")


@app.get("/health/celery", dependencies=[Depends(IPRateLimiter(requests=10, window=60))])
def health_check_celery():
    redis_status = "ok"
    celery_status = "ok"
    details = []
    deep_mode = os.environ.get(
        "HEALTHCHECK_DEEP_CELERY", "false").lower() in ("true", "1", "yes")

    # 1. Check Redis
    try:
        broker_url = os.environ.get(
            "CELERY_BROKER_URL", "redis://localhost:6379/0")
        r = redis.from_url(broker_url)
        r.ping()
    except Exception as e:
        redis_status = "error"
        details.append(f"Redis error: {str(e)}")
        logger.error(f"Health check Redis failed: {e}")

    # 2. Check Celery Worker
    try:
        if deep_mode:
            # Deep mode verifies end-to-end task execution and result retrieval.
            res = celery_app.ping.delay()
            res.get(timeout=3)
        else:
            # Light mode avoids backend result reads and uses control ping only.
            inspect = celery_app.app.control.inspect(timeout=1)
            ping_response = inspect.ping() if inspect else None
            if not ping_response:
                raise RuntimeError(
                    "No Celery workers responded to inspect ping")
    except Exception as e:
        celery_status = "error"
        details.append(f"Celery error: {str(e)}")
        logger.error(f"Health check Celery failed: {e}")

    response = {
        "redis": redis_status,
        "celery": celery_status,
        "mode": "deep" if deep_mode else "light"
    }

    if details:
        response["detail"] = "; ".join(details)

    if redis_status != "ok" or celery_status != "ok":
        raise HTTPException(status_code=503, detail=response)
    return response

# Billing Models
class CheckoutRequest(BaseModel):
    plan: Literal["pro", "enterprise"]


class CheckoutResponse(BaseModel):
    checkout_url: str


class BillingCancelResponse(BaseModel):
    code: str
    message: str
    effective_plan: str
    effective_at: str
    subscription_status: str


class BillingErrorResponse(BaseModel):
    code: str
    message: str
    details: Optional[Any] = None

class SubscriptionResponse(BaseModel):
    plan: str
    status: str
    lemonsqueezy_subscription_id: Optional[str] = None


def _billing_error(code: str, message: str, status_code: int, details: Optional[Any] = None):
    return HTTPException(
        status_code=status_code,
        detail={
            "code": code,
            "message": message,
            "details": details,
        },
    )

@app.post(
    "/api/billing/create-checkout",
    response_model=CheckoutResponse,
    dependencies=[Depends(RateLimiter(requests=3, window=60, fail_closed=True))],
    responses={
        401: {
            "model": BillingErrorResponse,
            "description": "Unauthorized",
            "content": {
                "application/json": {
                    "example": {
                        "code": "UNAUTHORIZED",
                        "message": "Invalid or expired bearer token",
                        "details": None,
                    }
                }
            },
        },
        403: {
            "model": BillingErrorResponse,
            "description": "Forbidden",
            "content": {
                "application/json": {
                    "example": {
                        "code": "FORBIDDEN",
                        "message": "Not authenticated",
                        "details": None,
                    }
                }
            },
        },
        409: {
            "model": BillingErrorResponse,
            "description": "Active subscription already exists",
            "content": {
                "application/json": {
                    "example": {
                        "code": "ACTIVE_SUBSCRIPTION",
                        "message": "You already have an active subscription. Cancel it first to start a new checkout.",
                        "details": {
                            "subscription_plan": "enterprise",
                            "subscription_status": "active",
                        },
                    }
                }
            },
        },
    },
)
async def create_checkout(request: CheckoutRequest, http_request: Request, user_id: str = Depends(verify_token)):
    if not supabase:
        raise _billing_error(
            code="DATABASE_UNAVAILABLE",
            message="Database service unavailable",
            status_code=503,
        )

    # 1. Fetch user email and check existing subscription
    try:
        profile_res = supabase.table("profiles").select("*").eq("id", user_id).execute()
        if not profile_res.data:
            raise _billing_error(
                code="PROFILE_NOT_FOUND",
                message="User profile not found",
                status_code=404,
                details={"user_id": user_id},
            )

        profile = profile_res.data[0]
        email = profile.get("email")
        if not email:
            raise _billing_error(
                code="PROFILE_EMAIL_MISSING",
                message="User email not found in profile",
                status_code=400,
                details={"user_id": user_id},
            )

        if profile.get("subscription_status") == "active":
            raise _billing_error(
                code="ACTIVE_SUBSCRIPTION",
                message="You already have an active subscription. Cancel it first to start a new checkout.",
                status_code=409,
                details={
                    "subscription_plan": profile.get("subscription_plan", "free"),
                    "subscription_status": profile.get("subscription_status", "inactive"),
                },
            )

        # 2. Create Lemon Squeezy checkout
        app_origin = http_request.headers.get("x-app-origin")
        checkout_url = await create_lemonsqueezy_checkout(user_id, email, request.plan, app_origin=app_origin)
        if not checkout_url:
            raise _billing_error(
                code="CHECKOUT_CREATION_FAILED",
                message="Failed to create checkout session",
                status_code=500,
                details={"plan": request.plan},
            )

        return {"checkout_url": checkout_url}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in create_checkout: {e}")
        raise _billing_error(
            code="INTERNAL_ERROR",
            message="Internal Server Error",
            status_code=500,
        )


@app.post(
    "/api/billing/cancel",
    response_model=BillingCancelResponse,
    dependencies=[Depends(RateLimiter(requests=3, window=60, fail_closed=True))],
    responses={
        401: {
            "model": BillingErrorResponse,
            "description": "Unauthorized",
            "content": {
                "application/json": {
                    "example": {
                        "code": "UNAUTHORIZED",
                        "message": "Invalid or expired bearer token",
                        "details": None,
                    }
                }
            },
        },
        403: {
            "model": BillingErrorResponse,
            "description": "Forbidden",
            "content": {
                "application/json": {
                    "example": {
                        "code": "FORBIDDEN",
                        "message": "Not authenticated",
                        "details": None,
                    }
                }
            },
        },
        409: {
            "model": BillingErrorResponse,
            "description": "No paid subscription to cancel",
            "content": {
                "application/json": {
                    "example": {
                        "code": "NO_ACTIVE_PAID_SUBSCRIPTION",
                        "message": "No active paid subscription to cancel.",
                        "details": {
                            "subscription_plan": "free",
                            "subscription_status": "inactive",
                        },
                    }
                }
            },
        },
    },
)
async def cancel_checkout(user_id: str = Depends(verify_token)):
    if not supabase:
        raise _billing_error(
            code="DATABASE_UNAVAILABLE",
            message="Database service unavailable",
            status_code=503,
        )

    try:
        profile_res = supabase.table("profiles").select("subscription_plan, subscription_status, lemonsqueezy_subscription_id").eq("id", user_id).execute()
        if not profile_res.data:
            raise _billing_error(
                code="PROFILE_NOT_FOUND",
                message="User profile not found",
                status_code=404,
                details={"user_id": user_id},
            )

        profile = profile_res.data[0]
        current_plan = profile.get("subscription_plan", "free")
        current_status = profile.get("subscription_status", "inactive")
        lemonsqueezy_subscription_id = profile.get("lemonsqueezy_subscription_id")
        has_paid_plan = current_plan in ("pro", "enterprise")

        if not has_paid_plan and current_status != "active":
            raise _billing_error(
                code="NO_ACTIVE_PAID_SUBSCRIPTION",
                message="No active paid subscription to cancel.",
                status_code=409,
                details={
                    "subscription_plan": current_plan,
                    "subscription_status": current_status,
                },
            )

        if not lemonsqueezy_subscription_id:
            raise _billing_error(
                code="PROVIDER_SUBSCRIPTION_MISSING",
                message="Unable to cancel because provider subscription ID is missing.",
                status_code=409,
                details={"user_id": user_id},
            )

        provider_result = await cancel_lemonsqueezy_subscription(str(lemonsqueezy_subscription_id))
        if not provider_result:
            raise _billing_error(
                code="PROVIDER_CANCEL_FAILED",
                message="Failed to cancel subscription with billing provider.",
                status_code=502,
                details={"provider": "lemonsqueezy", "subscription_id": str(lemonsqueezy_subscription_id)},
            )

        effective_at = datetime.now(timezone.utc).isoformat()
        supabase.table("profiles").update({
            "subscription_plan": "free",
            "subscription_status": provider_result.get("status") or "cancelled",
        }).eq("id", user_id).execute()

        return {
            "code": "SUBSCRIPTION_CANCELLED",
            "message": "Subscription cancelled successfully.",
            "effective_plan": "free",
            "effective_at": provider_result.get("ends_at") or effective_at,
            "subscription_status": provider_result.get("status") or "cancelled",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in cancel_checkout: {e}")
        raise _billing_error(
            code="INTERNAL_ERROR",
            message="Internal Server Error",
            status_code=500,
        )

@app.get("/api/billing/subscription", response_model=SubscriptionResponse, dependencies=[Depends(RateLimiter(requests=60, window=60))])
async def get_subscription(user_id: str = Depends(verify_token)):
    if not supabase:
        raise HTTPException(status_code=503, detail="Database service unavailable")

    try:
        response = supabase.table("profiles").select("subscription_plan, subscription_status, lemonsqueezy_subscription_id").eq("id", user_id).execute()
        if not response.data:
            return SubscriptionResponse(plan="free", status="inactive")

        data = response.data[0]
        return SubscriptionResponse(
            plan=data.get("subscription_plan", "free"),
            status=data.get("subscription_status", "inactive"),
            lemonsqueezy_subscription_id=data.get("lemonsqueezy_subscription_id")
        )
    except Exception as e:
        logger.error(f"Error fetching subscription: {e}")
        raise HTTPException(status_code=500, detail="Internal Server Error")

@app.post("/api/webhooks/lemonsqueezy")
async def lemonsqueezy_webhook(request: Request):
    if not supabase:
        return {"status": "error", "message": "Database unavailable"}

    # 1. Verify signature
    signature = request.headers.get("X-Signature")
    if not signature:
        logger.warning("Missing X-Signature header")
        raise HTTPException(status_code=401, detail="Missing signature")

    body = await request.body()
    if not verify_lemonsqueezy_signature(body, signature):
        logger.warning("Invalid signature")
        raise HTTPException(status_code=401, detail="Invalid signature")

    # 2. Parse data
    import json
    try:
        data = json.loads(body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event_name = data.get("meta", {}).get("event_name")
    attributes = data.get("data", {}).get("attributes", {})
    custom_data = data.get("meta", {}).get("custom_data", {})

    user_id = custom_data.get("user_id")
    # Lemon Squeezy sends a unique event ID in the meta object
    event_id = data.get("meta", {}).get("event_id") or data.get("data", {}).get("id") # Fallback to resource ID

    if not user_id:
        # Some events might not have user_id in custom_data if they are not from checkouts
        # But for subscription events we expect it.
        logger.info(f"Webhook received without user_id: {event_name}")
        return {"status": "ignored"}

    # 3. Idempotency check
    if event_id:
        existing_event = supabase.table("webhook_events").select("id").eq("id", str(event_id)).execute()
        if existing_event.data:
            logger.info(f"Duplicate webhook event: {event_id}")
            return {"status": "ignored"}

    # 4. Handle events
    if event_name in ["subscription_created", "subscription_updated"]:
        status = attributes.get("status")
        variant_id = str(attributes.get("variant_id"))
        customer_id = str(attributes.get("customer_id"))
        subscription_id = str(data.get("data", {}).get("id"))

        # Determine plan
        pro_id = os.environ.get("LEMONSQUEEZY_PRO_VARIANT_ID")
        ent_id = os.environ.get("LEMONSQUEEZY_ENTERPRISE_VARIANT_ID")

        plan = "free"
        if variant_id == pro_id:
            plan = "pro"
        elif variant_id == ent_id:
            plan = "enterprise"

        supabase.table("profiles").update({
            "subscription_plan": plan,
            "subscription_status": status,
            "lemonsqueezy_customer_id": customer_id,
            "lemonsqueezy_subscription_id": subscription_id
        }).eq("id", user_id).execute()

    elif event_name == "subscription_cancelled":
        # Subscription cancelled means it will not renew, but it might still be active until period end.
        # Lemon Squeezy status usually stays 'active' until it actually expires.
        # We'll just update the status as reported.
        status = attributes.get("status")
        supabase.table("profiles").update({
            "subscription_status": status
        }).eq("id", user_id).execute()

    if event_id:
        supabase.table("webhook_events").insert({"id": str(event_id), "type": event_name}).execute()
    return {"status": "success"}
