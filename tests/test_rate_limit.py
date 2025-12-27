import unittest
from unittest.mock import MagicMock, patch
from fastapi import Request, HTTPException
from utils.rate_limit import RateLimiter

class TestRateLimiter(unittest.TestCase):
    def test_allow_request(self):
        # Mock Redis
        mock_redis = MagicMock()
        mock_pipeline = MagicMock()
        mock_redis.pipeline.return_value = mock_pipeline
        mock_pipeline.execute.return_value = [1] # First request

        with patch('redis.from_url', return_value=mock_redis):
            limiter = RateLimiter(limit=5, window=60)

            mock_request = MagicMock(spec=Request)
            mock_request.client.host = "127.0.0.1"

            # Synchronous call
            limiter(mock_request)

            mock_pipeline.incr.assert_called()
            mock_pipeline.expire.assert_called()

    def test_block_request(self):
        # Mock Redis
        mock_redis = MagicMock()
        mock_pipeline = MagicMock()
        mock_redis.pipeline.return_value = mock_pipeline
        mock_pipeline.execute.return_value = [6] # 6th request (limit is 5)

        with patch('redis.from_url', return_value=mock_redis):
            limiter = RateLimiter(limit=5, window=60)

            mock_request = MagicMock(spec=Request)
            mock_request.client.host = "127.0.0.1"

            with self.assertRaises(HTTPException) as cm:
                limiter(mock_request)

            self.assertEqual(cm.exception.status_code, 429)

    def test_redis_fail_open(self):
        # Mock Redis failure on init
        with patch('redis.from_url', side_effect=Exception("Redis down")):
            limiter = RateLimiter(limit=5, window=60)

            mock_request = MagicMock(spec=Request)
            mock_request.client.host = "127.0.0.1"

            # Should not raise exception (fail open)
            limiter(mock_request)

            # Verify client is None
            self.assertIsNone(limiter.redis_client)

    def test_redis_reconnect(self):
        # Mock Redis failure on init, then success
        mock_redis = MagicMock()
        mock_pipeline = MagicMock()
        mock_redis.pipeline.return_value = mock_pipeline
        mock_pipeline.execute.return_value = [1] # Return valid count

        side_effects = [Exception("Redis down"), mock_redis]

        with patch('redis.from_url', side_effect=side_effects):
            limiter = RateLimiter(limit=5, window=60)
            self.assertIsNone(limiter.redis_client)

            mock_request = MagicMock(spec=Request)
            mock_request.client.host = "127.0.0.1"

            # Second call should try to connect and succeed
            limiter(mock_request)
            self.assertIsNotNone(limiter.redis_client)
            mock_pipeline.execute.assert_called()

    def test_redis_fail_open_runtime(self):
        # Mock Redis failure during execution
        mock_redis = MagicMock()
        mock_redis.pipeline.side_effect = Exception("Redis connection lost")

        with patch('redis.from_url', return_value=mock_redis):
            limiter = RateLimiter(limit=5, window=60)

            mock_request = MagicMock(spec=Request)
            mock_request.client.host = "127.0.0.1"

            # Should not raise exception (fail open)
            limiter(mock_request)

if __name__ == '__main__':
    unittest.main()
