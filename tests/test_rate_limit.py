import unittest
from unittest.mock import MagicMock, patch
from fastapi import Request, HTTPException
from utils.rate_limit import check_rate_limit
import redis
import time

class TestRateLimit(unittest.TestCase):

    @patch('utils.rate_limit.redis_client')
    def test_rate_limit_under_limit(self, mock_redis):
        """Test request allowed when under limit."""
        # Setup mock pipeline
        mock_pipe = MagicMock()
        mock_redis.pipeline.return_value = mock_pipe
        # Return count 1 (first request)
        mock_pipe.execute.return_value = [1]

        # Create dummy request
        mock_request = MagicMock(spec=Request)
        mock_request.client.host = "127.0.0.1"
        mock_request.headers = {}

        # Call function - should not raise
        check_rate_limit(mock_request)

        # Verify redis calls
        mock_redis.pipeline.assert_called_once()

        # Verify key contains minute timestamp
        current_minute = int(time.time() // 60)
        expected_key = f"rate_limit:127.0.0.1:{current_minute}"

        mock_pipe.incr.assert_called_with(expected_key)
        mock_pipe.expire.assert_called_with(expected_key, 90)
        mock_pipe.execute.assert_called_once()

    @patch('utils.rate_limit.redis_client')
    def test_rate_limit_exceeded(self, mock_redis):
        """Test request denied when over limit."""
        mock_pipe = MagicMock()
        mock_redis.pipeline.return_value = mock_pipe
        # Return count 11 (limit is 10)
        mock_pipe.execute.return_value = [11]

        mock_request = MagicMock(spec=Request)
        mock_request.client.host = "127.0.0.1"
        mock_request.headers = {}

        with self.assertRaises(HTTPException) as cm:
            check_rate_limit(mock_request)

        self.assertEqual(cm.exception.status_code, 429)
        self.assertEqual(cm.exception.detail, "Too Many Requests")

    @patch('utils.rate_limit.redis_client')
    def test_rate_limit_fail_open(self, mock_redis):
        """Test fail-open behavior when Redis errors."""
        mock_redis.pipeline.side_effect = redis.RedisError("Connection failed")

        mock_request = MagicMock(spec=Request)
        mock_request.client.host = "127.0.0.1"
        mock_request.headers = {}

        # Should not raise exception
        try:
            check_rate_limit(mock_request)
        except Exception as e:
            self.fail(f"check_rate_limit raised exception on Redis failure: {e}")

    @patch('utils.rate_limit.redis_client')
    def test_x_forwarded_for(self, mock_redis):
        """Test IP extraction from X-Forwarded-For header."""
        mock_pipe = MagicMock()
        mock_redis.pipeline.return_value = mock_pipe
        mock_pipe.execute.return_value = [1]

        mock_request = MagicMock(spec=Request)
        mock_request.client.host = "10.0.0.1" # Proxy IP
        mock_request.headers = {"X-Forwarded-For": "203.0.113.195, 10.0.0.1"}

        check_rate_limit(mock_request)

        # Should use the first IP in the list and current minute
        current_minute = int(time.time() // 60)
        expected_key = f"rate_limit:203.0.113.195:{current_minute}"

        mock_pipe.incr.assert_called_with(expected_key)

if __name__ == '__main__':
    unittest.main()
