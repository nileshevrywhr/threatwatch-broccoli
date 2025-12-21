import os
import logging
import time
import ssl
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
            "schedule": crontab(minute="*/5"),
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

@app.task(name="ping")
def ping():
    logger.info("Ping task received.")
    return "pong"
