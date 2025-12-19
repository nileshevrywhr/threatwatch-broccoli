import os
import logging
from datetime import datetime, timezone
from typing import Literal

from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field
from supabase import create_client, Client

from celery_tasks import scan_monitor_task
from utils.schedule_utils import calculate_next_run_at

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
    user_id: str
    term: str
    frequency: Literal['daily', 'weekly', 'monthly']

@app.post("/api/monitors")
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
        scan_monitor_task.delay(monitor_id)

        return {"monitor_id": monitor_id}

    except Exception as e:
        logger.error(f"Error creating monitor: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
def health_check():
    return {"status": "ok"}
