"""Persistent Playwright browser lifecycle and profile locking."""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from .config import LOCK_FILE, PROFILE_DIR
from .errors import BrowserLaunchFailed, InvalidExtensionPath


LOCK_STALE_AFTER = 24 * 60 * 60


def _process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class BrowserProfileLock:
    """Small cross-platform lock based on atomic file creation."""

    def __init__(self, path: Path = LOCK_FILE, stale_after: float = LOCK_STALE_AFTER):
        self.path = Path(path)
        self.stale_after = stale_after
        self.token = uuid.uuid4().hex
        self.acquired = False

    def _payload(self) -> str:
        return json.dumps(
            {"pid": os.getpid(), "created_at": time.time(), "token": self.token},
            separators=(",", ":"),
        )

    def _existing_is_stale(self, contents: str) -> bool:
        try:
            data = json.loads(contents)
            pid = int(data["pid"])
            created_at = float(data["created_at"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return True
        return (time.time() - created_at) > self.stale_after or not _process_is_alive(pid)

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = self._payload()
        for _ in range(2):
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    contents = self.path.read_text(encoding="utf-8")
                except FileNotFoundError:
                    continue
                if self._existing_is_stale(contents):
                    try:
                        if self.path.read_text(encoding="utf-8") == contents:
                            self.path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
                raise BrowserLaunchFailed(
                    "The browser profile is already in use by another gpt process."
                )
            else:
                with os.fdopen(descriptor, "w", encoding="utf-8") as lock_file:
                    lock_file.write(payload)
                self.acquired = True
                return
        raise BrowserLaunchFailed("Could not acquire the browser profile lock.")

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("token") == self.token:
                self.path.unlink()
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        finally:
            self.acquired = False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.release()


class BrowserSession:
    """Own one headed persistent Chromium context and release it reliably."""

    def __init__(
        self,
        profile_dir: Path = PROFILE_DIR,
        lock_path: Path = LOCK_FILE,
        extension_path: Path | None = None,
    ):
        self.profile_dir = Path(profile_dir)
        self.lock = BrowserProfileLock(lock_path)
        self.extension_path = (
            self._validate_extension_path(extension_path)
            if extension_path is not None
            else None
        )
        self.playwright: Any = None
        self.context: Any = None
        self.page: Any = None

    @staticmethod
    def _validate_extension_path(extension_path: Path) -> Path:
        path = Path(extension_path).expanduser().resolve()
        if not path.is_dir():
            raise InvalidExtensionPath(
                f"Extension directory does not exist: {path}"
            )
        if not (path / "manifest.json").is_file():
            raise InvalidExtensionPath(
                f"Extension directory has no manifest.json: {path}"
            )
        return path

    def _launch_options(self) -> dict[str, Any]:
        options: dict[str, Any] = {
            "headless": False,
            "viewport": {"width": 1280, "height": 900},
        }
        if self.extension_path is not None:
            extension = str(self.extension_path)
            options["args"] = [
                f"--disable-extensions-except={extension}",
                f"--load-extension={extension}",
            ]
        return options

    def __enter__(self):
        self.lock.acquire()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            from playwright.sync_api import sync_playwright

            self.playwright = sync_playwright().start()
            self.context = self.playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                **self._launch_options(),
            )
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            return self
        except Exception as exc:
            self.close()
            if exc.__class__.__module__.startswith("playwright"):
                raise BrowserLaunchFailed(
                    "Chromium could not be launched. Run: playwright install chromium"
                ) from exc
            if isinstance(exc, ImportError):
                raise BrowserLaunchFailed(
                    "Playwright is not installed. Run: pip install -e ."
                ) from exc
            raise BrowserLaunchFailed(f"Could not launch the browser: {exc}") from exc

    def close(self) -> None:
        try:
            if self.context is not None:
                self.context.close()
        finally:
            self.context = None
            try:
                if self.playwright is not None:
                    self.playwright.stop()
            finally:
                self.playwright = None
                self.lock.release()

    def __exit__(self, exc_type, exc, traceback):
        self.close()
