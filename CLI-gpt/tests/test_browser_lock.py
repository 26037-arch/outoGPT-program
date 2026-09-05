import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from cli_gpt.browser import BrowserProfileLock
from cli_gpt.errors import BrowserLaunchFailed


class BrowserProfileLockTests(unittest.TestCase):
    def test_lock_excludes_second_owner_and_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "browser.lock"
            first = BrowserProfileLock(path)
            second = BrowserProfileLock(path)
            first.acquire()
            try:
                self.assertTrue(path.exists())
                with self.assertRaises(BrowserLaunchFailed):
                    second.acquire()
            finally:
                first.release()
            self.assertFalse(path.exists())

    def test_dead_process_lock_is_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "browser.lock"
            path.write_text(
                json.dumps({"pid": 2_147_483_647, "created_at": time.time(), "token": "old"}),
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

