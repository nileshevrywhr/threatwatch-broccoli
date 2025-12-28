import os
import logging
import redis
from datetime import datetime, timezone
from typing import Literal, List, Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from supabase import create_client, Client

import celery_app
from celery_tasks import scan_monitor_task
from utils.schedule_utils import calculate_next_run_at
from utils.auth import verify_token
from utils.rate_limit import RateLimiter

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
    allow_origins = [origin.strip() for origin in allowed_origins_env.split(",") if origin.strip()]
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

@app.post("/api/monitors", dependencies=[Depends(RateLimiter(requests=10, window=60))])
async def create_monitor(monitor: MonitorRequest, user_id: str = Depends(verify_token)):
    if not supabase:
        raise HTTPException(status_code=503, detail="Database service unavailable")

    try:
        # Calculate next_run_at
        # We want the *next* scheduled run to be in the future,
        # but we also want to trigger an immediate scan now.
        # calculate_next_run_at(freq, now) returns now + freq (strictly future).
        now = datetime.now(timezone.utc)
        next_run_at = calculate_next_run_at(monitor.frequency, now)

        # Prepare data for insertion
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
            raise HTTPException(status_code=500, detail="Failed to create monitor")

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

@app.post("/api/monitors/{monitor_id}/test", dependencies=[Depends(RateLimiter(requests=5, window=60))])
async def test_monitor(monitor_id: str, user_id: str = Depends(verify_token)):
    """
    Triggers an immediate scan for a specific monitor.
    Does not synchronously validate existence (worker handles it).
    Returns the Celery task ID.
    """
    try:
        task = scan_monitor_task.delay(monitor_id)
        return {"task_id": task.id}
    except Exception as e:
        logger.error(f"Error triggering test scan for monitor {monitor_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal Server Error")

@app.get("/health")
def health_check():
    return {"status": "ok"}

@app.get("/api/reports/{report_id}/download", dependencies=[Depends(RateLimiter(requests=30, window=60))])
def download_report(report_id: str, user_id: str = Depends(verify_token)):
    if not supabase:
        raise HTTPException(status_code=503, detail="Database service unavailable")

    try:
        # Fetch report verifying ownership
        # We explicitly catch API errors that might result from invalid UUIDs
        try:
            response = supabase.table("reports").select("pdf_url").eq("id", report_id).eq("user_id", user_id).execute()
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

@app.get("/api/feed", dependencies=[Depends(RateLimiter(requests=60, window=60))])
def get_feed(limit: int = 20, offset: int = 0, user_id: str = Depends(verify_token)):
    if not supabase:
        raise HTTPException(status_code=503, detail="Database service unavailable")

    try:
        # 1. Fetch Reports
        reports_response = supabase.table("reports")\
            .select("*")\
            .eq("user_id", user_id)\
            .order("created_at", desc=True)\
            .range(offset, offset + limit - 1)\
            .execute()

        reports = reports_response.data
        if not reports:
            return []

        # 2. Extract Monitor IDs to fetch queries
        monitor_ids = list(set([r["monitor_id"] for r in reports if r.get("monitor_id")]))

        # 3. Fetch Monitors
        monitors_map = {}
        if monitor_ids:
             monitors_res = supabase.table("monitors").select("id, query_text").in_("id", monitor_ids).execute()
             for m in monitors_res.data:
                 monitors_map[m["id"]] = m["query_text"]

        # 4. Construct Response
        feed = []
        for report in reports:
            item_count = report.get("item_count", 0)

            # Derive Severity
            if item_count > 5:
                severity = "high"
            elif item_count > 0:
                severity = "medium"
            else:
                severity = "low"

            # Derive Summary
            summary = f"Found {item_count} relevant threat items"

            feed_item = {
                "report_id": report["id"],
                "term": monitors_map.get(report["monitor_id"], "Unknown Monitor"),
                "created_at": report["created_at"],
                "status": "completed",
                "severity": severity,
                "summary": summary,
                "download_url": f"/api/reports/{report['id']}/download"
            }
            feed.append(feed_item)

        return feed

    except Exception as e:
        logger.error(f"Error in get_feed: {e}")
        raise HTTPException(status_code=500, detail="Internal Server Error")

@app.get("/health/celery")
def health_check_celery():
    redis_status = "ok"
    celery_status = "ok"
    details = []

    # 1. Check Redis
    try:
        broker_url = os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0")
        r = redis.from_url(broker_url)
        r.ping()
    except Exception as e:
        redis_status = "error"
        details.append(f"Redis error: {str(e)}")
        logger.error(f"Health check Redis failed: {e}")

    # 2. Check Celery Worker
    try:
        # Check celery ping task
        # timeout=3s as requested
        res = celery_app.ping.delay()
        res.get(timeout=3)
    except Exception as e:
        celery_status = "error"
        details.append(f"Celery error: {str(e)}")
        logger.error(f"Health check Celery failed: {e}")

    response = {
        "redis": redis_status,
        "celery": celery_status
    }

    if details:
        response["detail"] = "; ".join(details)

    if redis_status != "ok" or celery_status != "ok":
        raise HTTPException(status_code=503, detail=response)
    return response
