import os
import unittest
from unittest.mock import MagicMock, patch

import celery_app


class TestCeleryAppWorkerStartup(unittest.TestCase):
    def setUp(self):
        self._orig_enabled = os.environ.get("WORKER_STARTUP_SCAN_DUE_MONITORS")
        self._orig_ttl = os.environ.get("WORKER_STARTUP_SCAN_LOCK_TTL")

    def tearDown(self):
        if self._orig_enabled is None:
            os.environ.pop("WORKER_STARTUP_SCAN_DUE_MONITORS", None)
        else:
            os.environ["WORKER_STARTUP_SCAN_DUE_MONITORS"] = self._orig_enabled

        if self._orig_ttl is None:
            os.environ.pop("WORKER_STARTUP_SCAN_LOCK_TTL", None)
        else:
            os.environ["WORKER_STARTUP_SCAN_LOCK_TTL"] = self._orig_ttl

    @patch("celery_app.redis.from_url")
    def test_worker_start_enqueues_catchup_when_lock_acquired(self, mock_from_url):
        os.environ["WORKER_STARTUP_SCAN_DUE_MONITORS"] = "true"
        os.environ["WORKER_STARTUP_SCAN_LOCK_TTL"] = "300"

        lock_client = MagicMock()
        lock_client.set.return_value = True
        mock_from_url.return_value = lock_client

        sender = MagicMock()
        sender.app = MagicMock()

        celery_app.log_worker_start(sender=sender)

        lock_client.set.assert_called_once_with(
            "threatwatch:startup_scan_due_monitors_lock", "1", nx=True, ex=300
        )
        sender.app.send_task.assert_called_once_with("scan_due_monitors")

    @patch("celery_app.redis.from_url")
    def test_worker_start_skips_when_lock_not_acquired(self, mock_from_url):
        os.environ["WORKER_STARTUP_SCAN_DUE_MONITORS"] = "true"

        lock_client = MagicMock()
        lock_client.set.return_value = False
        mock_from_url.return_value = lock_client

        sender = MagicMock()
        sender.app = MagicMock()

        celery_app.log_worker_start(sender=sender)

        sender.app.send_task.assert_not_called()

    @patch("celery_app.redis.from_url")
    def test_worker_start_respects_disable_flag(self, mock_from_url):
        os.environ["WORKER_STARTUP_SCAN_DUE_MONITORS"] = "false"

        sender = MagicMock()
        sender.app = MagicMock()

        celery_app.log_worker_start(sender=sender)

        mock_from_url.assert_not_called()
        sender.app.send_task.assert_not_called()


if __name__ == "__main__":
    unittest.main()
