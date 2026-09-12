import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import psutil

from cli_gpt.browser import BrowserProfileLock, _process_is_alive
from cli_gpt.errors import ChromeProfileInUse


class ProcessAliveTests(unittest.TestCase):
    @patch("cli_gpt.browser.psutil.pid_exists")
    def test_non_positive_pid_is_rejected_without_a_system_call(self, pid_exists):
        self.assertFalse(_process_is_alive(0))
        self.assertFalse(_process_is_alive(-1))
        pid_exists.assert_not_called()

    @patch("cli_gpt.browser.psutil.pid_exists", side_effect=[True, False])
    def test_pid_exists_result_is_used_directly(self, pid_exists):
        self.assertTrue(_process_is_alive(1234))
        self.assertFalse(_process_is_alive(5678))
        self.assertEqual(
            [call.args[0] for call in pid_exists.call_args_list], [1234, 5678]
        )

    def test_pid_lookup_errors_are_treated_as_not_alive(self):
        for error in (psutil.Error(), OSError(), ValueError()):
            with self.subTest(error=type(error).__name__):
                with patch("cli_gpt.browser.psutil.pid_exists", side_effect=error):
                    self.assertFalse(_process_is_alive(1234))


class BrowserProfileLockTests(unittest.TestCase):
    def test_lock_excludes_second_owner_and_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "browser.lock"
            first = BrowserProfileLock(path)
            second = BrowserProfileLock(path)
            first.acquire()
            try:
                self.assertTrue(path.exists())
                with self.assertRaises(ChromeProfileInUse):
                    second.acquire()
            finally:
                first.release()
            self.assertFalse(path.exists())

    def test_dead_process_lock_is_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "browser.lock"
            path.write_text(
                json.dumps(
                    {"pid": 2_147_483_647, "created_at": time.time(), "token": "old"}
                ),
                encoding="utf-8",
            )
            lock = BrowserProfileLock(path)
            lock.acquire()
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(data["pid"], os.getpid())
                self.assertEqual(data["token"], lock.token)
            finally:
                lock.release()


if __name__ == "__main__":
    unittest.main()
