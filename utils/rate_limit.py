import os
import time
import logging
import redis
from fastapi import Request, HTTPException

logger = logging.getLogger(__name__)

# Reusing CELERY_BROKER_URL as suggested by memory/architecture
REDIS_URL = os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0")

class RateLimiter:
    """
    Fixed Window Rate Limiter using Redis.
    Fails open (allows request) if Redis is unavailable.
    """
    def __init__(self, limit: int = 10, window: int = 60):
        self.limit = limit
        self.window = window
        self.redis_client = None
        self._connect_redis()

    def _connect_redis(self):
        try:
            self.redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        except Exception as e:
            logger.error(f"Failed to initialize Redis for Rate Limiter: {e}")
            self.redis_client = None

    def __call__(self, request: Request):
        """
        Synchronous __call__ to be run in a threadpool by FastAPI,
        avoiding event loop blocking with sync Redis client.
        """
        # Lazy reconnection attempt if not connected
        if not self.redis_client:
            self._connect_redis()

        # If still not connected, fail open
        if not self.redis_client:
            return

        try:
            client_ip = request.client.host
            # Key format: rate_limit:<ip>:<window_timestamp>
            current_window = int(time.time() // self.window)
            key = f"rate_limit:{client_ip}:{current_window}"

            # Pipeline to ensure atomicity of incr and expire
            pipe = self.redis_client.pipeline()
            pipe.incr(key)
            pipe.expire(key, self.window + 10) # Set expiry slightly longer than window
            result = pipe.execute()

            request_count = result[0]

            if request_count > self.limit:
                logger.warning(f"Rate limit exceeded for {client_ip}")
                raise HTTPException(status_code=429, detail="Too Many Requests")

        except HTTPException:
            raise
        except Exception as e:
            # Fail-open on Redis errors during execution
            logger.error(f"Rate limiting error: {e}")
