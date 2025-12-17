import os
import logging
import time
from datetime import datetime, timedelta, timezone
from celery import Task
from supabase import create_client, Client
from celery_app import app

# Configure logging
logger = logging.getLogger(__name__)

# Supabase Client Setup
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
else:
    logger.warning("Supabase credentials not found. DB operations will fail.")

class BaseTask(Task):
    """
    Base Celery Task class that handles structured logging for start, success, and failure.
    """
    def __call__(self, *args, **kwargs):
        self.start_time = time.time()
        logger.info(f"task={self.name} status=start args={args} kwargs={kwargs}")
        return super().__call__(*args, **kwargs)

    def on_success(self, retval, task_id, args, kwargs):
        duration = time.time() - self.start_time
        logger.info(f"task={self.name} status=success duration={duration:.2f}s result={retval}")
        super().on_success(retval, task_id, args, kwargs)

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        duration = time.time() - self.start_time
        logger.error(f"task={self.name} status=error duration={duration:.2f}s error={str(exc)}")
        super().on_failure(exc, task_id, args, kwargs, einfo)

@app.task(
    base=BaseTask,
    bind=True,
    name="run_monitor_scan",
    soft_time_limit=60,
    time_limit=90
)
def run_monitor_scan(self, monitor_id: str):
    """
    Executes a scan for a specific monitor.
    Creates records in 'searches' and 'reports' tables.
    """
    if not supabase:
        raise RuntimeError("Supabase client not initialized")

    logger.info(f"Starting scan for monitor_id={monitor_id}")

    # 1. Fetch monitor details
    response = supabase.table("monitors").select("*").eq("id", monitor_id).execute()
    if not response.data:
        logger.error(f"Monitor not found: {monitor_id}")
        return f"Monitor {monitor_id} not found"

    monitor = response.data[0]

    # 2. Create 'searches' record
    search_data = {
        "query_text": monitor.get("query_text", ""),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "completed"
    }
    search_res = supabase.table("searches").insert(search_data).execute()
    # Assuming we get an ID back, usually likely.

    # 3. Create 'reports' record
    report_data = {
        "user_id": monitor.get("user_id"),
        "monitor_id": monitor_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "pdf_url": "https://example.com/dummy_report.pdf" # Placeholder
    }
    supabase.table("reports").insert(report_data).execute()

    return f"Scan completed for {monitor_id}"

@app.task(
    base=BaseTask,
    bind=True,
    name="scan_due_monitors",
    soft_time_limit=60,
    time_limit=90
)
def scan_due_monitors(self):
    """
    Scheduler task: Finds monitors due for a run, enqueues scans, and updates next_run_at.
    """
    if not supabase:
        raise RuntimeError("Supabase client not initialized")

    now_iso = datetime.now(timezone.utc).isoformat()

    # 1. Query monitors
    response = supabase.table("monitors")\
        .select("*")\
        .eq("active", True)\
        .lte("next_run_at", now_iso)\
        .execute()

    monitors = response.data
    count_found = len(monitors)
    count_enqueued = 0

    logger.info(f"Found {count_found} monitors due for scan.")

    for monitor in monitors:
        monitor_id = monitor["id"]

        # 2. Enqueue task
        run_monitor_scan.delay(monitor_id)
        count_enqueued += 1

        # 3. Update next_run_at
        frequency = monitor.get("frequency", "daily").lower()
        current_next_run = datetime.fromisoformat(monitor["next_run_at"].replace("Z", "+00:00"))

        if frequency == "weekly":
            next_date = current_next_run + timedelta(weeks=1)
        elif frequency == "monthly":
            # Simple 30 day add for MVP
            next_date = current_next_run + timedelta(days=30)
        else: # daily or default
            next_date = current_next_run + timedelta(days=1)

        supabase.table("monitors")\
            .update({"next_run_at": next_date.isoformat()})\
            .eq("id", monitor_id)\
            .execute()

    return f"Found {count_found}, Enqueued {count_enqueued}"

@app.task(
    base=BaseTask,
    bind=True,
    name="cleanup_old_reports",
    soft_time_limit=60,
    time_limit=90
)
def cleanup_old_reports(self):
    """
    Hygiene task: Deletes reports older than 30 days.
    """
    if not supabase:
        raise RuntimeError("Supabase client not initialized")

    cutoff_date = datetime.now(timezone.utc) - timedelta(days=30)
    cutoff_iso = cutoff_date.isoformat()

    response = supabase.table("reports")\
        .delete()\
        .lt("created_at", cutoff_iso)\
        .execute()

    # Supabase-py delete response format depends on version/setup,
    # but normally data contains deleted rows
    deleted_count = len(response.data) if response.data else 0

    return f"Deleted {deleted_count} old reports"
