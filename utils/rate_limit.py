import os
import time
import logging
import redis
from fastapi import HTTPException, Request, Depends
from typing import Optional
from utils.auth import verify_token

logger = logging.getLogger(__name__)

# Initialize Redis client globally to reuse connection pool
REDIS_URL = os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0")
try:
    redis_client = redis.from_url(REDIS_URL, decode_responses=True)
except Exception as e:
    logger.error(f"Failed to initialize Redis for rate limiting: {e}")
    redis_client = None

class RateLimiter:
    def __init__(self, requests: int = 60, window: int = 60, user_id: Optional[str] = None):
        self.requests = requests
        self.window = window
        self.user_id = user_id

    def __call__(self, request: Request, user_id: str = Depends(verify_token)):
        if not redis_client:
            return # Fail open

        try:
            # Prefer user_id for rate limiting if available (Authenticated endpoints)
            identifier = user_id
            prefix = "user"

            # If we were to support public endpoints, we would handle missing user_id here
            # but since verify_token raises 401, we are guaranteed a user_id here.

            # Simple fixed window
            current_window = int(time.time() // self.window)
            key = f"rate_limit:{prefix}:{identifier}:{current_window}"

            count = redis_client.incr(key)
            if count == 1:
                redis_client.expire(key, self.window)

            if count > self.requests:
                logger.warning(f"Rate limit exceeded for {prefix} {identifier}")
                raise HTTPException(status_code=429, detail="Too Many Requests")

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Rate limiting error: {e}")
            # Fail open
