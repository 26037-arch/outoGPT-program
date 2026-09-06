"""Single-owner Playwright Chromium lifecycle and conversation page registry."""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .config import LOCK_FILE, PROFILE_DIR
from .errors import (
    BrowserLaunchFailed,
    ConversationPageClosed,
    ExtensionLoadFailed,
    InvalidExtensionPath,
    PlaywrightChromiumNotInstalled,
    PlaywrightNotInstalled,
    ProfileInUse,
)

LOCK_STALE_AFTER = 24 * 60 * 60
EXTENSION_START_TIMEOUT = 15.0
PAGE_LOAD_TIMEOUT_MS = 60_000


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
    """Cross-platform, process-aware lock for the persistent user-data dir."""

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
        # Never steal a lock from a live process, even after a long-running
        # controller exceeds the historical age threshold.
        return not _process_is_alive(pid)

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
                raise ProfileInUse(
                    "The OutoGPT Chromium profile is already in use by another process."
                )
            else:
                with os.fdopen(descriptor, "w", encoding="utf-8") as lock_file:
                    lock_file.write(payload)
                self.acquired = True
                return
        raise ProfileInUse("Could not acquire the OutoGPT Chromium profile lock.")

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


class ChatSessionStatus(str, Enum):
    IDLE = "idle"
    SUBMITTING = "submitting"
    GENERATING = "generating"
    COMPLETED = "completed"
    ERROR = "error"


class BrowserPhase(str, Enum):
    STOPPED = "stopped"
    MANUAL_BOOTSTRAP = "manual_bootstrap"
    AUTOMATION = "automation"


@dataclass
class ChatSession:
    """The explicit invariant connecting one conversation to one Chromium tab."""

    internal_id: str
    page: Any
    project_url: str
    conversation_url: str | None = None
    conversation_id: str | None = None
    status: ChatSessionStatus = ChatSessionStatus.IDLE


def _page_is_closed(page: Any) -> bool:
    try:
        return bool(page.is_closed())
    except (AttributeError, TypeError):
        return bool(getattr(page, "closed", False))


def _conversation_id(url: str) -> str | None:
    parts = [part for part in urlsplit(url).path.split("/") if part]
    try:
        return parts[parts.index("c") + 1]
    except (ValueError, IndexError):
        return None


class BrowserManager:
    """Own Playwright, one persistent context, and all conversation pages."""

    def __init__(
        self,
        profile_dir: Path = PROFILE_DIR,
        lock_path: Path = LOCK_FILE,
        extension_path: Path | None = None,
    ):
        self.profile_dir = Path(profile_dir).expanduser()
        self.lock = BrowserProfileLock(lock_path)
        self.extension_path = (
            self._validate_extension_path(extension_path)
            if extension_path is not None
            else None
        )
        self.playwright: Any = None
        self.context: Any = None
        self.setup_page: Any = None
        self.page: Any = None
        self.extension_worker: Any = None
        self.sessions: dict[str, ChatSession] = {}
        self.phase = BrowserPhase.STOPPED

    @staticmethod
    def _validate_extension_path(extension_path: Path) -> Path:
        path = Path(extension_path).expanduser().resolve()
        if not path.is_dir():
            raise InvalidExtensionPath(f"Extension directory does not exist: {path}")
        if not (path / "manifest.json").is_file():
            raise InvalidExtensionPath(f"Extension directory has no manifest.json: {path}")
        return path

    def _launch_options(self, *, with_extension: bool = True) -> dict[str, Any]:
        options: dict[str, Any] = {
            "channel": "chromium",
            "headless": False,
            "viewport": {"width": 1280, "height": 900},
        }
        if with_extension and self.extension_path is not None:
            extension = str(self.extension_path)
            options["args"] = [
                f"--disable-extensions-except={extension}",
                f"--load-extension={extension}",
            ]
        return options

    def _start_runtime(self, *, with_extension: bool) -> "BrowserManager":
        if self.context is not None:
            expected = (
                BrowserPhase.AUTOMATION
                if with_extension
                else BrowserPhase.MANUAL_BOOTSTRAP
            )
            if self.phase is not expected:
                raise BrowserLaunchFailed(
                    f"BrowserManager is already running in {self.phase.value} phase."
                )
            return self
        if not self.lock.acquired:
            self.lock.acquire()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            try:
                from playwright.sync_api import sync_playwright
            except ImportError as exc:
                raise PlaywrightNotInstalled(
                    "Playwright is not installed. Run: python -m pip install playwright"
                ) from exc
            self.playwright = sync_playwright().start()
            try:
                self.context = self.playwright.chromium.launch_persistent_context(
                    user_data_dir=str(self.profile_dir),
                    **self._launch_options(with_extension=with_extension),
                )
            except Exception as exc:
                message = str(exc)
                lowered = message.lower()
                if (
                    "processsingleton" in lowered
                    or "profile appears to be in use" in lowered
                    or "user data directory is already in use" in lowered
                ):
                    raise ProfileInUse(
                        "The OutoGPT Chromium profile is already in use."
                    ) from exc
                if "Executable doesn't exist" in message or "playwright install" in message:
                    raise PlaywrightChromiumNotInstalled(
                        "Playwright Chromium is not installed. Run: "
                        "python -m playwright install chromium"
                    ) from exc
                raise BrowserLaunchFailed(f"Could not start OutoGPT Chromium: {exc}") from exc
            self.phase = (
                BrowserPhase.AUTOMATION
                if with_extension
                else BrowserPhase.MANUAL_BOOTSTRAP
            )
            # During manual bootstrap, do not inspect pages at all. The user
            # owns the browser until Enter is pressed.
            if with_extension:
                self.setup_page = (
                    self.context.pages[0]
                    if self.context.pages
                    else self.context.new_page()
                )
                self.page = self.setup_page
            return self
        except Exception:
            self.close()
            raise

    def start_bootstrap(self) -> "BrowserManager":
        """Start extension-free Chromium without inspecting or navigating pages."""
        return self._start_runtime(with_extension=False)

    def start_automation(self) -> "BrowserManager":
        """Start extension-enabled Chromium for controlled automation."""
        return self._start_runtime(with_extension=True)

    def start(self) -> "BrowserManager":
        """Backward-compatible entry point for the automation phase."""
        return self.start_automation()

    def find_open_chatgpt_page(self) -> Any | None:
        """Find a user-opened ChatGPT tab; call only after the user presses Enter."""
        if self.phase is not BrowserPhase.MANUAL_BOOTSTRAP or self.context is None:
            raise BrowserLaunchFailed(
                "A ChatGPT page can only be inspected during manual bootstrap."
            )
        for page in reversed(list(self.context.pages)):
            try:
                host = (urlsplit(page.url).hostname or "").lower()
            except Exception:
                continue
            if host == "chatgpt.com" or host.endswith(".chatgpt.com"):
                return page
        return None

    def _close_runtime(self) -> None:
        try:
            if self.context is not None:
                self.context.close()
        finally:
            self.context = None
            self.setup_page = None
            self.page = None
            self.extension_worker = None
            self.sessions.clear()
            try:
                if self.playwright is not None:
                    self.playwright.stop()
            finally:
                self.playwright = None
                self.phase = BrowserPhase.STOPPED

    def finish_bootstrap_and_start_automation(self) -> "BrowserManager":
        """Flush bootstrap state, then relaunch the same profile with extension."""
        if self.phase is not BrowserPhase.MANUAL_BOOTSTRAP:
            raise BrowserLaunchFailed("Manual bootstrap is not running.")
        self._close_runtime()
        return self.start_automation()

    def restart_for_manual_bootstrap(self) -> "BrowserManager":
        """Leave automation and return control to an extension-free browser."""
        self._close_runtime()
        return self.start_bootstrap()

    def _worker_script_path(self) -> str:
        if self.extension_path is None:
            raise ExtensionLoadFailed("No OutoGPT extension directory was configured.")
        manifest_path = self.extension_path / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            script = manifest["background"]["service_worker"]
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ExtensionLoadFailed(
                f"Could not read the extension service worker from {manifest_path}."
            ) from exc
        return "/" + str(script).replace("\\", "/").lstrip("/")

    @staticmethod
    def _worker_matches(worker: Any, script_path: str) -> bool:
        try:
            parts = urlsplit(worker.url)
        except Exception:
            return False
        if parts.scheme != "chrome-extension" or parts.path != script_path:
            return False
        evaluate = getattr(worker, "evaluate", None)
        if evaluate is None:
            return True
        try:
            return evaluate("chrome.runtime.id") == parts.hostname
        except Exception:
            return False

    def verify_extension(self, timeout: float = EXTENSION_START_TIMEOUT) -> Any:
        """Prove that this extension's Manifest V3 service worker is running."""
        if self.context is None:
            raise BrowserLaunchFailed("BrowserManager.start() must be called first.")
        if self.phase is not BrowserPhase.AUTOMATION:
            raise ExtensionLoadFailed(
                "Extensions are intentionally disabled during manual bootstrap."
            )
        script_path = self._worker_script_path()
        for worker in list(self.context.service_workers):
            if self._worker_matches(worker, script_path):
                self.extension_worker = worker
                return worker
        deadline = time.monotonic() + timeout
        while True:
            remaining_ms = int(max(0.0, deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                break
            try:
                with self.context.expect_event("serviceworker", timeout=remaining_ms) as info:
                    pass
                worker = info.value
            except Exception:
                break
            if self._worker_matches(worker, script_path):
                self.extension_worker = worker
                return worker
        raise ExtensionLoadFailed(
            "The OutoGPT extension service worker did not appear in time "
            f"(expected chrome-extension://...{script_path})."
        )

    def new_conversation(self, project_url: str) -> ChatSession:
        """Create exactly one new tab for a new logical conversation."""
        if self.context is None:
            raise BrowserLaunchFailed("BrowserManager.start() must be called first.")
        internal_id = f"pending-{uuid.uuid4().hex}"
        session = ChatSession(internal_id, self.context.new_page(), project_url)
        self.sessions[internal_id] = session
        return session

    def bind_conversation(
        self,
        session: ChatSession,
        conversation_url: str,
        conversation_id: str | None = None,
    ) -> ChatSession:
        conversation_id = conversation_id or _conversation_id(conversation_url)
        if not conversation_id:
            raise ConversationPageClosed(
                "A conversation page cannot be registered without an identifier."
            )
        session.conversation_url = conversation_url
        session.conversation_id = conversation_id
        if session.internal_id != conversation_id:
            self.sessions.pop(session.internal_id, None)
        self.sessions[conversation_id] = session
        return session

    def page_for_conversation(
        self,
        conversation_id: str,
        conversation_url: str,
        project_url: str,
    ) -> ChatSession:
        """Reuse an open tab, or recover a manually closed tab by its URL."""
        if self.context is None:
            raise BrowserLaunchFailed("BrowserManager.start() must be called first.")
        session = self.sessions.get(conversation_id)
        if session is not None and not _page_is_closed(session.page):
            return session
        if not conversation_url:
            raise ConversationPageClosed(
                f"Conversation page {conversation_id} is closed and has no recovery URL."
            )
        page = self.context.new_page()
        try:
            page.goto(conversation_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
        except Exception as exc:
            try:
                page.close()
            except Exception:
                pass
            raise ConversationPageClosed(
                f"Could not recover conversation {conversation_id}: {exc}"
            ) from exc
        if session is None:
            session = ChatSession(
                f"recovered-{uuid.uuid4().hex}", page, project_url,
                conversation_url, conversation_id,
            )
        else:
            session.page = page
            session.conversation_url = conversation_url
            session.status = ChatSessionStatus.IDLE
        self.sessions[conversation_id] = session
        return session

    def close(self) -> None:
        try:
            self._close_runtime()
        finally:
            self.lock.release()

    def __enter__(self) -> "BrowserManager":
        return self.start()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


BrowserSession = BrowserManager
