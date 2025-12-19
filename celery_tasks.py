import os
import logging
import time
import json
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta, timezone
from celery import Task
from supabase import create_client, Client
from googleapiclient.discovery import build
from fpdf import FPDF
from celery_app import app
from utils.schedule_utils import calculate_next_run_at

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
        item_count = len(ranked_items)
        report_data = {
            "user_id": monitor.get("user_id"),
            "monitor_id": monitor_id,
            "created_at": now_iso,
            "pdf_url": pdf_url
        }

        # Try to insert with item_count, fallback if schema doesn't match
        try:
            data_with_count = report_data.copy()
            data_with_count["item_count"] = item_count
            report_res = supabase.table("reports").insert(data_with_count).execute()
        except Exception as insert_error:
            logger.warning(f"Failed to insert report with item_count, falling back to base schema: {insert_error}")
            report_res = supabase.table("reports").insert(report_data).execute()

        if report_res.data:
             report_id = report_res.data[0].get("id")

             # Trigger email delivery
             send_report_email_task.delay(report_id)

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

        try:
            next_date = calculate_next_run_at(frequency, current_next_run)
        except ValueError:
            logger.warning(f"Invalid frequency '{frequency}' for monitor {monitor_id}, defaulting to daily.")
            next_date = calculate_next_run_at('daily', current_next_run)

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

    try:
        retention_days = int(os.environ.get("RETENTION_DAYS", 30))
        if retention_days <= 0:
            logger.warning("RETENTION_DAYS must be positive, using default of 30")
            retention_days = 30
    except ValueError:
        logger.warning("Invalid RETENTION_DAYS value, using default of 30")
        retention_days = 30
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

@app.task(
    base=BaseTask,
    bind=True,
    name="send_report_email_task",
    soft_time_limit=30,
    time_limit=60
)
def send_report_email_task(self, report_id: str):
    """
    Delivery task: Sends an email notification for a generated report.
    """
    if not supabase:
        logger.error("Supabase client not initialized")
        return None

    try:
        # 1. Fetch report details
        response = supabase.table("reports").select("*").eq("id", report_id).execute()
        if not response.data:
            logger.error(f"Report not found: {report_id}")
            return None

        report = response.data[0]
        user_id = report.get("user_id")
        pdf_url = report.get("pdf_url")
        item_count = report.get("item_count") # Might be None if schema doesn't match

        # 2. Determine User Email
        recipient_email = None
        email_override = os.environ.get("EMAIL_OVERRIDE")

        if email_override:
            recipient_email = email_override
            logger.info(f"Using EMAIL_OVERRIDE: {recipient_email}")
        else:
            # Fetch from Supabase Auth
            try:
                user = supabase.auth.admin.get_user_by_id(user_id)
                if user and user.user and user.user.email:
                     recipient_email = user.user.email
                else:
                    logger.error(f"Could not find email for user_id: {user_id}")
                    return None
            except Exception as auth_error:
                 logger.error(f"Failed to fetch user email: {auth_error}")
                 return None

        if not recipient_email:
             logger.error("No recipient email resolved.")
             return None

        # 3. Prepare Email Content
        smtp_host = os.environ.get("SMTP_HOST")
        smtp_port = os.environ.get("SMTP_PORT")
        smtp_user = os.environ.get("SMTP_USERNAME")
        smtp_password = os.environ.get("SMTP_PASSWORD")
        email_from = os.environ.get("EMAIL_FROM")

        if not all([smtp_host, smtp_port, email_from]):
            logger.error("SMTP configuration missing (HOST, PORT, or EMAIL_FROM)")
            return None

        # Construct Summary
        if item_count is not None:
             summary_text = f"- {item_count} potential threats identified"
        else:
             summary_text = "- Potential threats identified"

        subject = "Your ThreatWatch report is ready"
        body = f"""Your ThreatWatch report has been generated successfully.

Summary:
{summary_text}

You can download the full report here:
{pdf_url}

— ThreatWatch
"""

        msg = MIMEMultipart()
        msg['From'] = email_from
        msg['To'] = recipient_email
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))

        # 4. Send Email
        try:
             port = int(smtp_port)
             server = smtplib.SMTP(smtp_host, port)
             server.starttls()
             if smtp_user and smtp_password:
                 server.login(smtp_user, smtp_password)

             server.sendmail(email_from, recipient_email, msg.as_string())
             server.quit()

             logger.info(f"Email sent to {recipient_email} for report {report_id}")
             return "email_sent"

        except Exception as smtp_error:
             logger.error(f"SMTP error: {smtp_error}")
             raise smtp_error

    except Exception as e:
        logger.error(f"Error in send_report_email_task: {e}", exc_info=True)
        raise e
