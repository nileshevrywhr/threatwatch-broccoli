import unittest
from unittest.mock import MagicMock, patch
from fastapi import Request, HTTPException
from utils.rate_limit import RateLimiter

class TestRateLimiter(unittest.TestCase):
    def setUp(self):
        self.mock_redis = MagicMock()
        # Patch the global redis_client in utils.rate_limit
        self.redis_patcher = patch('utils.rate_limit.redis_client', self.mock_redis)
        self.redis_patcher.start()

        self.req = MagicMock(spec=Request)
        self.user_id = "test-user-123"

    def tearDown(self):
        self.redis_patcher.stop()

    def test_allow_request(self):
        limiter = RateLimiter(requests=10, window=60)
        self.mock_redis.incr.return_value = 1

        # Should not raise exception
        try:
            limiter(self.req, user_id=self.user_id)
        except HTTPException:
            self.fail("RateLimiter raised HTTPException unexpectedly!")

        self.mock_redis.incr.assert_called()
        # Verify key contains user_id
        args, _ = self.mock_redis.incr.call_args
        self.assertIn(self.user_id, args[0])
        self.assertIn("user", args[0])

    def test_block_request(self):
        limiter = RateLimiter(requests=10, window=60)
        self.mock_redis.incr.return_value = 11

        with self.assertRaises(HTTPException) as cm:
            limiter(self.req, user_id=self.user_id)

        self.assertEqual(cm.exception.status_code, 429)

    def test_fail_open_on_redis_error(self):
        limiter = RateLimiter(requests=10, window=60)
        self.mock_redis.incr.side_effect = Exception("Redis connection failed")

        # Should log error but not raise exception (fail open)
        try:
            limiter(self.req, user_id=self.user_id)
        except Exception:
            self.fail("RateLimiter failed closed on Redis error!")

    def test_fail_open_if_redis_client_none(self):
        with patch('utils.rate_limit.redis_client', None):
            limiter = RateLimiter(requests=10, window=60)
            # Should not raise exception
            limiter(self.req, user_id=self.user_id)

if __name__ == '__main__':
    unittest.main()
