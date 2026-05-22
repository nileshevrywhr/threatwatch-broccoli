import os
import logging
import time
import ssl
import redis
from celery import Celery
from celery.schedules import crontab
from celery.signals import worker_ready

# Configure logging
logging.Formatter.converter = time.gmtime
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
logger.info("Initializing Celery application with SSL support")

# Read environment variables
BROKER_URL = os.environ.get("CELERY_BROKER_URL")
RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND")

if not BROKER_URL:
    logger.warning("CELERY_BROKER_URL not set, using default redis://localhost:6379/0")
    BROKER_URL = "redis://localhost:6379/0"

if not RESULT_BACKEND:
    logger.warning("CELERY_RESULT_BACKEND not set, using default redis://localhost:6379/0")
    RESULT_BACKEND = "redis://localhost:6379/0"

# Add SSL parameters to Redis URLs if using rediss://
if BROKER_URL.startswith("rediss://"):
    BROKER_URL = f"{BROKER_URL}?ssl_cert_reqs={ssl.CERT_NONE}"
    logger.info("Added SSL configuration to broker URL")

if RESULT_BACKEND.startswith("rediss://"):
    RESULT_BACKEND = f"{RESULT_BACKEND}?ssl_cert_reqs={ssl.CERT_NONE}"
    logger.info("Added SSL configuration to result backend URL")

app = Celery("threatwatch", broker=BROKER_URL, backend=RESULT_BACKEND)

# Configuration
app.conf.update(
    task_serializer="json",
    accept_content=["json"],  # Ignore other content
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    worker_hijack_root_logger=False, # Allow custom logging config
    # Redis SSL configuration for broker
    broker_use_ssl={
        'ssl_cert_reqs': ssl.CERT_NONE
    } if BROKER_URL.startswith("rediss://") else None,
    # Redis SSL configuration for result backend
    redis_backend_use_ssl={
        'ssl_cert_reqs': ssl.CERT_NONE
    } if RESULT_BACKEND.startswith("rediss://") else None,
    beat_schedule={
        "scan_due_monitors": {
            "task": "scan_due_monitors",
            "schedule": crontab(minute="*/30"),
        },
        "cleanup_old_reports": {
            "task": "cleanup_old_reports",
            "schedule": crontab(hour=2, minute=0),
        },
    }
)

# Import tasks to ensure they are registered
import celery_tasks

@worker_ready.connect
def log_worker_start(sender, **kwargs):
    logger.info("Celery worker started successfully.")
    enabled = os.environ.get(
        "WORKER_STARTUP_SCAN_DUE_MONITORS", "true").lower() in ("true", "1", "yes")
    if not enabled:
        logger.info("Worker startup catch-up scan is disabled by env var.")
        return

    lock_acquired = True
    lock_key = "threatwatch:startup_scan_due_monitors_lock"
    lock_ttl_seconds = int(os.environ.get("WORKER_STARTUP_SCAN_LOCK_TTL", "300"))

    try:
        lock_client = redis.from_url(BROKER_URL, decode_responses=True)
        # Avoid duplicate catch-up runs when multiple worker replicas restart together.
        lock_acquired = bool(lock_client.set(lock_key, "1", nx=True, ex=lock_ttl_seconds))
    except Exception as e:
        logger.warning(f"Could not acquire startup scan lock; proceeding anyway: {e}")

    if not lock_acquired:
        logger.info("Skipping startup catch-up scan; lock already held by another worker.")
        return

    try:
        celery_sender = sender.app if sender and getattr(sender, "app", None) else app
        celery_sender.send_task("scan_due_monitors")
        logger.info("Enqueued startup catch-up task: scan_due_monitors")
    except Exception as e:
        logger.error(f"Failed to enqueue startup catch-up task: {e}")

@app.task(name="ping")
def ping():
    logger.info("Ping task received.")
    return "pong"
