import os
import redis
import logging
import time
from fastapi import Request, HTTPException

logger = logging.getLogger(__name__)

# Initialize Redis client globally for rate limiting to reuse connection pool
CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0")

# Use a connection pool to manage connections efficiently
try:
    pool = redis.ConnectionPool.from_url(CELERY_BROKER_URL)
    redis_client = redis.Redis(connection_pool=pool)
except Exception as e:
    logger.error(f"Failed to initialize Redis for rate limiting: {e}")
    redis_client = None

def check_rate_limit(request: Request):
    """
    Simple rate limiter: 10 requests per minute per IP.
    Uses a Fixed Window algorithm (key based on IP + current minute).
    Fails open (allows request) if Redis is unavailable.
    """
    if not redis_client:
        return

    client_ip = request.client.host if request.client else "unknown"

    # Handle X-Forwarded-For if behind proxy (e.g. Railway/Nginx)
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        client_ip = forwarded.split(",")[0].strip()

    # Use Fixed Window: key changes every minute
    current_minute = int(time.time() // 60)
    key = f"rate_limit:{client_ip}:{current_minute}"

    try:
        # Pipeline execution for atomicity (mostly) and performance
        pipe = redis_client.pipeline()
        pipe.incr(key)
        # Set expiry slightly longer than window to ensure cleanup
        pipe.expire(key, 90)
        result = pipe.execute()

        request_count = result[0]

        if request_count > 10:
            logger.warning(f"Rate limit exceeded for IP: {client_ip}")
            raise HTTPException(status_code=429, detail="Too Many Requests")

    except redis.RedisError as e:
        # Fail open: If Redis is down, allow the request but log it
        logger.error(f"Redis error in rate limiter: {e}")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error in rate limiter: {e}")
