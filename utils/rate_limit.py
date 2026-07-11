import os
import time
import uuid
import logging
import redis
from fastapi import HTTPException, Request, Depends
from utils.auth import verify_token

logger = logging.getLogger(__name__)

REDIS_URL = os.environ.get("CELERY_BROKER_URL")
if not REDIS_URL:
    raise RuntimeError("CELERY_BROKER_URL is required for rate limiting")

_redis = redis.from_url(REDIS_URL, decode_responses=True)

_SLIDING_WINDOW = _redis.register_script("""
local key = KEYS[1]
local now  = tonumber(ARGV[1])
local win  = tonumber(ARGV[2])
local lim  = tonumber(ARGV[3])
local mbr  = ARGV[4]
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - win)
local cnt = redis.call('ZCARD', key)
if cnt >= lim then
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local retry = win
    if #oldest > 0 then
        retry = math.ceil(tonumber(oldest[2]) + win - now)
    end
    return {-1, retry}
end
redis.call('ZADD', key, now, mbr)
redis.call('EXPIRE', key, math.ceil(win))
return {cnt + 1, 0}
""")


def _enforce(identifier: str, limit: int, window: int, fail_closed: bool) -> None:
    now = time.time()
    member = f"{now}:{uuid.uuid4().hex[:8]}"
    try:
        result = _SLIDING_WINDOW(
            keys=[f"rl:{identifier}"],
            args=[now, window, limit, member],
        )
    except Exception as e:
        logger.error("Rate limit Redis error: %s", e)
        if fail_closed:
            raise HTTPException(status_code=503, detail="Service temporarily unavailable")
        return

    count, retry_after = result
    if count == -1:
        raise HTTPException(
            status_code=429,
            detail="Too Many Requests",
            headers={
                "Retry-After": str(max(1, int(retry_after))),
                "X-RateLimit-Limit": str(limit),
                "X-RateLimit-Remaining": "0",
            },
        )


class RateLimiter:
    def __init__(self, requests: int, window: int, fail_closed: bool = False):
        self.requests = requests
        self.window = window
        self.fail_closed = fail_closed

    def __call__(self, user_id: str = Depends(verify_token)) -> None:
        _enforce(f"user:{user_id}", self.requests, self.window, self.fail_closed)


class IPRateLimiter:
    def __init__(self, requests: int, window: int):
        self.requests = requests
        self.window = window

    def __call__(self, request: Request) -> None:
        ip = request.client.host if request.client else "unknown"
        _enforce(f"ip:{ip}", self.requests, self.window, fail_closed=False)
