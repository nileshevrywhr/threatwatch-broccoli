import os
import logging
import redis
from datetime import datetime, timezone
from typing import Literal

from fastapi import FastAPI, HTTPException, BackgroundTasks, Depends
from pydantic import BaseModel, Field
from supabase import create_client, Client

import celery_app
from celery_tasks import scan_monitor_task
from utils.schedule_utils import calculate_next_run_at
from utils.rate_limit import check_rate_limit

# Configure Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

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
    user_id: str = Field(..., min_length=1, max_length=50)
    term: str = Field(..., min_length=1, max_length=100)
    frequency: Literal['daily', 'weekly', 'monthly']

@app.post("/api/monitors", dependencies=[Depends(check_rate_limit)])
async def create_monitor(monitor: MonitorRequest):
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
            "user_id": monitor.user_id,
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

@app.post("/api/monitors/{monitor_id}/test", dependencies=[Depends(check_rate_limit)])
async def test_monitor(monitor_id: str):
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
