import os
import logging
import time
import json
from datetime import datetime, timedelta, timezone
from celery import Task
from supabase import create_client, Client
from googleapiclient.discovery import build
from fpdf import FPDF
from celery_app import app

# Configure logging
logger = logging.getLogger(__name__)

# Supabase Client Setup
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        logger.error(f"Failed to initialize Supabase client: {e}")

# Google CSE Setup
GOOGLE_CSE_API_KEY = os.environ.get("GOOGLE_CSE_API_KEY")
GOOGLE_CSE_CX = os.environ.get("GOOGLE_CSE_CX")

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

def _calculate_score(item, now):
    """
    Calculates a simple score based on recency and keyword presence.
    """
    score = 0

    # 1. Recency Score
    # Google CSE returns snippet/pagemap/etc. trying to find date is tricky consistently.
    # We will look for "pagemap" -> "metatags" -> "article:published_time" or similar,
    # or rely on what's available. For MVP, we might skip complex date parsing if not readily available
    # or assign a default neutral score.
    # This is a placeholder for more robust extraction.

    # 2. Keyword Boost (Source Authority Proxy)
    text_to_scan = (item.get("title", "") + " " + item.get("snippet", "")).lower()
    keywords = ["attack", "breach", "malware", "ransomware", "vulnerability", "exploit"]

    for kw in keywords:
        if kw in text_to_scan:
            score += 10

    # Default base score
    score += 5

    return score

def _generate_pdf(report_content, monitor_id):
    """
    Generates a PDF from report content and uploads it to Supabase Storage.
    Returns the public URL.
    """
    try:
        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Arial", size=12)

        pdf.cell(200, 10, txt=f"Threat Report for Monitor {monitor_id}", ln=1, align="C")
        pdf.ln(10)

        pdf.set_font("Arial", size=10)
        pdf.cell(200, 10, txt=f"Generated at: {datetime.now(timezone.utc).isoformat()}", ln=1)
        pdf.ln(10)

        for item in report_content:
            title = item.get("title", "No Title").encode('latin-1', 'replace').decode('latin-1')
            link = item.get("link", "#").encode('latin-1', 'replace').decode('latin-1')
            snippet = item.get("snippet", "").encode('latin-1', 'replace').decode('latin-1')
            score = item.get("score", 0)

            pdf.set_font("Arial", 'B', 10)
            pdf.multi_cell(0, 5, txt=f"{title} (Score: {score})")
            pdf.set_font("Arial", '', 9)
            pdf.write(5, link, link)
            pdf.ln()
            pdf.multi_cell(0, 5, txt=snippet)
            pdf.ln(5)

        filename = f"report_{monitor_id}_{int(time.time())}.pdf"
        pdf_path = f"/tmp/{filename}"

        try:
            pdf.output(pdf_path)

            # Upload to Supabase Storage
            with open(pdf_path, 'rb') as f:
                supabase.storage.from_("reports").upload(path=filename, file=f, file_options={"content-type": "application/pdf"})

            # Get public URL
            # Note: Bucket must be public for this to work directly as a download link
            public_url = supabase.storage.from_("reports").get_public_url(filename)
            return public_url

        finally:
            if os.path.exists(pdf_path):
                os.remove(pdf_path)

    except Exception as e:
        logger.error(f"Failed to generate/upload PDF: {e}")
        return None

@app.task(
    base=BaseTask,
    bind=True,
    name="scan_monitor_task",
    soft_time_limit=60,
    time_limit=90
)
def scan_monitor_task(self, monitor_id: str):
    """
    Worker task: Scans for a monitor, generates a report, and saves it.
    """
    if not supabase:
        logger.error("Supabase client not initialized")
        return None

    if not GOOGLE_CSE_API_KEY or not GOOGLE_CSE_CX:
        logger.error("Google CSE credentials not set")
        return None

    try:
        # 1. Fetch monitor configuration
        response = supabase.table("monitors").select("*").eq("id", monitor_id).execute()
        if not response.data:
            logger.error(f"Monitor not found: {monitor_id}")
            return None

        monitor = response.data[0]
        query_text = monitor.get("query_text")

        if not query_text:
            logger.warning(f"Monitor {monitor_id} has no query_text")
            return None

        # 2. Run Google CSE Search
        service = build("customsearch", "v1", developerKey=GOOGLE_CSE_API_KEY, cache_discovery=False)
        res = service.cse().list(q=query_text, cx=GOOGLE_CSE_CX, num=10).execute()
        items = res.get("items", [])

        # 3. Rank results
        ranked_items = []
        now = datetime.now(timezone.utc)
        for item in items:
            score = _calculate_score(item, now)
            item["score"] = score
            ranked_items.append(item)

        ranked_items.sort(key=lambda x: x["score"], reverse=True)

        # 4. Generate Report Content & PDF
        pdf_url = _generate_pdf(ranked_items, monitor_id)

        # 5. Store in Supabase
        now_iso = datetime.now(timezone.utc).isoformat()

        # Insert Search Record (Pipeline)
        search_data = {
            "query_text": query_text,
            "created_at": now_iso,
            "status": "completed"
        }
        supabase.table("searches").insert(search_data).execute()

        # Insert Report Record
        report_data = {
            "user_id": monitor.get("user_id"),
            "monitor_id": monitor_id,
            "created_at": now_iso,
            "pdf_url": pdf_url
        }
        report_res = supabase.table("reports").insert(report_data).execute()

        if report_res.data:
             report_id = report_res.data[0].get("id")
             return report_id

        # Fallback if return data isn't immediate (though it usually is with explicit return)
        return "success_no_id"

    except Exception as e:
        # Re-raising ensures BaseTask.on_failure is called and the task state is set to FAILURE
        logger.error(f"Error in scan_monitor_task: {e}", exc_info=True)
        raise e

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
        scan_monitor_task.delay(monitor_id)
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
    Hygiene task: Deletes reports older than RETENTION_DAYS (env var, default 30).
    """
    if not supabase:
        raise RuntimeError("Supabase client not initialized")

    retention_days = int(os.environ.get("RETENTION_DAYS", 30))
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=retention_days)
    cutoff_iso = cutoff_date.isoformat()

    response = supabase.table("reports")\
        .delete()\
        .lt("created_at", cutoff_iso)\
        .execute()

    # Supabase-py delete response format depends on version/setup,
    # but normally data contains deleted rows
    deleted_count = len(response.data) if response.data else 0

    logger.info(f"Deleted {deleted_count} old reports.")
    return f"Deleted {deleted_count} old reports"
