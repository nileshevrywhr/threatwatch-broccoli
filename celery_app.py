import os
import logging
import time
from celery import Celery
from celery.signals import worker_ready

# Configure logging
logging.Formatter.converter = time.gmtime
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Read environment variables
BROKER_URL = os.environ.get("CELERY_BROKER_URL")
RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND")

if not BROKER_URL:
    logger.warning("CELERY_BROKER_URL not set, using default redis://localhost:6379/0")
    BROKER_URL = "redis://localhost:6379/0"

if not RESULT_BACKEND:
    logger.warning("CELERY_RESULT_BACKEND not set, using default redis://localhost:6379/0")
    RESULT_BACKEND = "redis://localhost:6379/0"

app = Celery("threatwatch", broker=BROKER_URL, backend=RESULT_BACKEND)

# Configuration
app.conf.update(
    task_serializer="json",
    accept_content=["json"],  # Ignore other content
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    worker_hijack_root_logger=False, # Allow custom logging config
)

@worker_ready.connect
def log_worker_start(sender, **kwargs):
    logger.info("Celery worker started successfully.")

@app.task(name="ping")
def ping():
    logger.info("Ping task received.")
    return "pong"
