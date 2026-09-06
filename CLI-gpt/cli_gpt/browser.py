"""Installed Google Chrome lifecycle and Playwright-over-CDP connection."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import urlopen

import psutil

from .config import (
    BROWSER_SETUP_FILE,
    CHROME_SESSION_FILE,
    DEFAULT_CDP_HOST,
    DEFAULT_CDP_PORT,
    LOCK_FILE,
    PROFILE_DIR,
)
from .errors import (
    ChromeCdpConnectionFailed,
    ChromeClosedUnexpectedly,
    ChromeDebugPortUnavailable,
    ChromeLaunchFailed,
    ChromeNotFound,
    ChromeProfileInUse,
    LoginNotReady,
    NoChromeContext,
)


LOCK_STALE_AFTER = 24 * 60 * 60
CHROME_START_TIMEOUT = 20.0
CDP_POLL_INTERVAL = 0.1


def _same_path(first: Path | str, second: Path | str) -> bool:
    """Compare paths without leaking Windows spelling differences into trust checks."""

    def unquoted(value: Path | str) -> str:
        raw = os.fspath(value)
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {'"', "'"}:
            return raw[1:-1]
        return raw

    try:
        first_path = os.path.abspath(os.path.realpath(unquoted(first)))
        second_path = os.path.abspath(os.path.realpath(unquoted(second)))
    except (OSError, TypeError, ValueError):
        return False
    return os.path.normcase(first_path) == os.path.normcase(second_path)


def _command_line_option(arguments: list[str], name: str) -> str | None:
    """Read one Chrome option in either ``--name=value`` or ``--name value`` form."""

    prefix = f"{name}="
    values: list[str] = []
    for index, argument in enumerate(arguments):
        if argument.startswith(prefix):
            values.append(argument[len(prefix) :])
        elif argument == name and index + 1 < len(arguments):
            values.append(arguments[index + 1])
    return values[0] if len(values) == 1 else None


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
    """Small cross-platform lock used to serialize Chrome profile launches."""

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
        return (time.time() - created_at) > self.stale_after or not _process_is_alive(
            pid
        )

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
                raise ChromeProfileInUse(
                    "The dedicated OutoGPT Chrome profile is being started by another process."
                )
            else:
                with os.fdopen(descriptor, "w", encoding="utf-8") as lock_file:
                    lock_file.write(payload)
                self.acquired = True
                return
        raise ChromeProfileInUse("Could not acquire the OutoGPT Chrome profile lock.")

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


class ChromeExecutableResolver:
    """Locate a normal installed Google Chrome executable."""

    def __init__(
        self,
        explicit_path: Path | str | None = None,
        *,
        environ: Mapping[str, str] | None = None,
        which: Callable[[str], str | None] = shutil.which,
    ):
        self.environ = os.environ if environ is None else environ
        configured = explicit_path or self.environ.get("OUTOGPT_CHROME_PATH")
        self.explicit_path = Path(configured).expanduser() if configured else None
        self.which = which

    def candidates(self) -> list[Path]:
        if self.explicit_path is not None:
            return [self.explicit_path]

        candidates: list[Path] = []
        for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            root = self.environ.get(variable)
            if root:
                candidates.append(
                    Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"
                )

        for executable in (
            "chrome.exe",
            "chrome",
            "google-chrome",
            "google-chrome-stable",
        ):
            found = self.which(executable)
            if found:
                candidates.append(Path(found))

        if os.name == "posix":
            candidates.extend(
                [
                    Path(
                        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
                    ),
                    Path("/usr/bin/google-chrome"),
                    Path("/usr/bin/google-chrome-stable"),
                ]
            )

        unique: list[Path] = []
        seen: set[str] = set()
        for candidate in candidates:
            key = os.path.normcase(str(candidate))
            if key not in seen:
                seen.add(key)
                unique.append(candidate)
        return unique

    def resolve(self) -> Path:
        for candidate in self.candidates():
            if candidate.is_file():
                return candidate.resolve()
        if self.explicit_path is not None:
            raise ChromeNotFound(
                f"Configured Google Chrome executable does not exist: {self.explicit_path}"
            )
        raise ChromeNotFound(
            "Google Chrome was not found. Install Chrome or set OUTOGPT_CHROME_PATH."
        )


@dataclass
class ChromeProcess:
    """A managed or previously running dedicated Chrome instance."""

    endpoint: str
    pid: int | None
    handle: Any = None
    owned: bool = False


class ChromeLauncher:
    """Start installed Chrome independently from Playwright and expose local CDP."""

    def __init__(
        self,
        executable: Path,
        profile_dir: Path = PROFILE_DIR,
        *,
        host: str = DEFAULT_CDP_HOST,
        port: int = DEFAULT_CDP_PORT,
        state_path: Path = CHROME_SESSION_FILE,
        lock: BrowserProfileLock | None = None,
        popen: Callable[..., Any] = subprocess.Popen,
        startup_timeout: float = CHROME_START_TIMEOUT,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if host != "127.0.0.1":
            raise ChromeDebugPortUnavailable("Chrome CDP must bind to 127.0.0.1.")
        if not 1 <= int(port) <= 65535:
            raise ChromeDebugPortUnavailable(f"Invalid Chrome CDP port: {port}")
        self.executable = Path(executable).resolve()
        self.profile_dir = Path(profile_dir).resolve()
        self.host = host
        self.port = int(port)
        self.state_path = Path(state_path)
        self.lock = lock or BrowserProfileLock()
        self.popen = popen
        self.startup_timeout = startup_timeout
        self.monotonic = monotonic
        self.sleep = sleep

    @property
    def endpoint(self) -> str:
        return f"http://{self.host}:{self.port}"

    def command(self) -> list[str]:
        return [
            str(self.executable),
            f"--remote-debugging-address={self.host}",
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.profile_dir}",
        ]

    def cdp_available(self) -> bool:
        try:
            with urlopen(f"{self.endpoint}/json/version", timeout=0.5) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (OSError, URLError, ValueError, json.JSONDecodeError):
            return False
        browser = str(data.get("Browser", ""))
        return bool(data.get("webSocketDebuggerUrl")) and "Chrome" in browser

    def port_in_use(self) -> bool:
        try:
            with socket.create_connection((self.host, self.port), timeout=0.25):
                return True
        except OSError:
            return False

    def _read_state(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        return data if isinstance(data, dict) else None

    def _state_matches(self, state: Mapping[str, Any]) -> bool:
        try:
            return (
                int(state["port"]) == self.port
                and _same_path(state["profile_dir"], self.profile_dir)
                and _same_path(state["executable"], self.executable)
            )
        except (KeyError, TypeError, ValueError, OSError):
            return False

    def _write_state_for_pid(self, pid: int) -> None:
        try:
            started_at = psutil.Process(pid).create_time()
        except (psutil.Error, OSError, ValueError):
            # For a just-launched process this is only descriptive metadata. The
            # live process, command line, listener, and CDP endpoint are trusted.
            started_at = time.time()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "pid": int(pid),
                    "port": self.port,
                    "profile_dir": str(self.profile_dir),
                    "executable": str(self.executable),
                    "started_at": started_at,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.state_path)

    def _write_state(self, process: Any) -> None:
        self._write_state_for_pid(int(process.pid))

    def _find_listener_pid(self) -> int | None:
        """Return the PID listening on this launcher's exact local CDP endpoint."""

        try:
            connections = psutil.net_connections(kind="tcp")
        except (psutil.Error, OSError):
            return None

        listener_pids: set[int] = set()
        for connection in connections:
            if connection.status != psutil.CONN_LISTEN or connection.pid is None:
                continue
            address = connection.laddr
            try:
                address_host = address.ip
                address_port = address.port
            except AttributeError:
                try:
                    address_host, address_port = address[0], address[1]
                except (IndexError, TypeError, ValueError):
                    continue
            if str(address_host) == self.host and int(address_port) == self.port:
                listener_pids.add(int(connection.pid))

        if len(listener_pids) != 1:
            return None
        return listener_pids.pop()

    def _process_matches_dedicated_chrome(self, pid: int) -> bool:
        """Verify the listener is precisely the configured OutoGPT Chrome."""

        try:
            process = psutil.Process(pid)
            executable = process.exe()
            command_line = [str(argument) for argument in process.cmdline()]
        except (psutil.Error, OSError, TypeError, ValueError):
            return False

        if not _same_path(executable, self.executable):
            return False

        address = _command_line_option(command_line, "--remote-debugging-address")
        port = _command_line_option(command_line, "--remote-debugging-port")
        profile = _command_line_option(command_line, "--user-data-dir")
        return (
            address == self.host
            and port == str(self.port)
            and profile is not None
            and _same_path(profile, self.profile_dir)
        )

    def _recover_dedicated_chrome(self) -> ChromeProcess | None:
        """Recover an unrecorded Chrome only after live identity verification."""

        if not self.cdp_available():
            return None
        pid = self._find_listener_pid()
        if pid is None or not self._process_matches_dedicated_chrome(pid):
            return None
        self._write_state_for_pid(pid)
        return ChromeProcess(self.endpoint, pid, handle=None, owned=False)

    def _remove_own_state(self, pid: int | None) -> None:
        state = self._read_state()
        if state is None or state.get("pid") != pid or not self._state_matches(state):
            return
        try:
            self.state_path.unlink()
        except FileNotFoundError:
            pass

    def _existing_managed_process(self) -> ChromeProcess | None:
        state = self._read_state()
        if state is None or not self._state_matches(state):
            return None
        try:
            pid = int(state["pid"])
        except (KeyError, TypeError, ValueError):
            return None
        if not _process_is_alive(pid):
            self._remove_own_state(pid)
            return None
        listener_pid = self._find_listener_pid()
        if listener_pid != pid or not self._process_matches_dedicated_chrome(pid):
            return None
        if self.cdp_available():
            return ChromeProcess(self.endpoint, pid, owned=False)
        raise ChromeCdpConnectionFailed(
            "The dedicated OutoGPT Chrome process is running, but its local CDP endpoint is unavailable."
        )

    def launch_or_attach(self) -> ChromeProcess:
        existing = self._existing_managed_process()
        if existing is not None:
            return existing
        if self.cdp_available():
            recovered = self._recover_dedicated_chrome()
            if recovered is not None:
                return recovered
            raise ChromeDebugPortUnavailable(
                f"Local port {self.port} is occupied by a Chrome instance that does "
                "not match the dedicated OutoGPT Chrome configuration."
            )
        if self.port_in_use():
            raise ChromeDebugPortUnavailable(
                f"Local port {self.port} is already in use by another process."
            )

        self.lock.acquire()
        process = None
        try:
            existing = self._existing_managed_process()
            if existing is not None:
                return existing
            if self.cdp_available():
                recovered = self._recover_dedicated_chrome()
                if recovered is not None:
                    return recovered
                raise ChromeDebugPortUnavailable(
                    f"Local port {self.port} became occupied by a Chrome instance that "
                    "does not match the dedicated OutoGPT Chrome configuration."
                )
            if self.port_in_use():
                raise ChromeDebugPortUnavailable(
                    f"Local port {self.port} became unavailable while starting Chrome."
                )
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            try:
                process = self.popen(
                    self.command(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                )
            except OSError as exc:
                raise ChromeLaunchFailed(
                    f"Could not start Google Chrome: {exc}"
                ) from exc

            deadline = self.monotonic() + self.startup_timeout
            while self.monotonic() < deadline:
                if self.cdp_available():
                    self._write_state(process)
                    return ChromeProcess(
                        self.endpoint, int(process.pid), handle=process, owned=True
                    )
                if process.poll() is not None:
                    raise ChromeProfileInUse(
                        "Chrome exited before CDP became ready. The dedicated OutoGPT profile may already be open."
                    )
                self.sleep(CDP_POLL_INTERVAL)
            raise ChromeCdpConnectionFailed(
                f"Chrome started, but CDP did not become available at {self.endpoint}."
            )
        except Exception:
            if process is not None and process.poll() is None:
                process.terminate()
            raise
        finally:
            self.lock.release()

    def stop(self, chrome: ChromeProcess) -> None:
        if not chrome.owned or chrome.handle is None:
            return
        process = chrome.handle
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        self._remove_own_state(chrome.pid)


class BrowserSession:
    """Own one Chrome/CDP connection and reuse Chrome's persistent context."""

    def __init__(
        self,
        profile_dir: Path = PROFILE_DIR,
        lock_path: Path = LOCK_FILE,
        extension_path: Path | None = None,
        *,
        chrome_path: Path | str | None = None,
        cdp_port: int = DEFAULT_CDP_PORT,
        session_state_path: Path = CHROME_SESSION_FILE,
        setup_state_path: Path = BROWSER_SETUP_FILE,
        require_setup: bool = True,
        keep_chrome_open: bool = False,
        launcher: ChromeLauncher | None = None,
        playwright_factory: Callable[[], Any] | None = None,
    ):
        self.profile_dir = Path(profile_dir).resolve()
        self.extension_path = Path(extension_path).resolve() if extension_path else None
        self.setup_state_path = Path(setup_state_path)
        self.require_setup = require_setup
        self.keep_chrome_open = keep_chrome_open
        if launcher is None:
            executable = ChromeExecutableResolver(chrome_path).resolve()
            launcher = ChromeLauncher(
                executable,
                self.profile_dir,
                port=cdp_port,
                state_path=session_state_path,
                lock=BrowserProfileLock(lock_path),
            )
        self.launcher = launcher
        self.playwright_factory = playwright_factory
        self.chrome: ChromeProcess | None = None
        self.playwright: Any = None
        self.browser: Any = None
        self.context: Any = None
        self.page: Any = None

    @property
    def cdp_endpoint(self) -> str:
        return self.launcher.endpoint

    def _setup_complete(self) -> bool:
        try:
            state = json.loads(self.setup_state_path.read_text(encoding="utf-8"))
            return Path(state["profile_dir"]).resolve() == self.profile_dir
        except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError, OSError):
            return False

    def mark_setup_complete(self, *, extension_path: Path | None = None) -> None:
        self.setup_state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.setup_state_path.with_suffix(
            self.setup_state_path.suffix + ".tmp"
        )
        temporary.write_text(
            json.dumps(
                {
                    "profile_dir": str(self.profile_dir),
                    "cdp_endpoint": self.cdp_endpoint,
                    "extension_path": str(Path(extension_path).resolve())
                    if extension_path
                    else None,
                    "confirmed_at": time.time(),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.setup_state_path)

    def start_chrome(self) -> ChromeProcess:
        if self.chrome is None:
            self.chrome = self.launcher.launch_or_attach()
        return self.chrome

    def _start_playwright(self) -> Any:
        if self.playwright_factory is not None:
            return self.playwright_factory().start()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise ChromeCdpConnectionFailed(
                "Playwright is not installed. Run: pip install -e ."
            ) from exc
        return sync_playwright().start()

    def connect(self) -> "BrowserSession":
        if self.context is not None:
            return self
        chrome = self.start_chrome()
        if (
            chrome.handle is not None
            and chrome.handle.poll() is not None
            and not self.launcher.cdp_available()
        ):
            raise ChromeClosedUnexpectedly(
                "Google Chrome closed before Playwright connected."
            )
        try:
            self.playwright = self._start_playwright()
            self.browser = self.playwright.chromium.connect_over_cdp(chrome.endpoint)
            contexts = self.browser.contexts
            if not contexts:
                raise NoChromeContext(
                    "The CDP-connected Chrome instance did not expose its persistent context."
                )
            self.context = contexts[0]
            self.page = self.context.pages[0] if self.context.pages else None
            return self
        except NoChromeContext:
            self._disconnect_playwright()
            raise
        except Exception as exc:
            self._disconnect_playwright()
            raise ChromeCdpConnectionFailed(
                f"Playwright could not connect to Chrome at {chrome.endpoint}: {exc}"
            ) from exc

    def open(self) -> "BrowserSession":
        if self.require_setup and not self._setup_complete():
            raise LoginNotReady(
                "Manual Chrome setup is incomplete. Run `gpt setup <PROJECT_URL>` or `outogpt setup` first."
            )
        return self.connect()

    def new_page(self) -> Any:
        self.ensure_running()
        if self.context is None:
            raise NoChromeContext("BrowserSession.open() must be called first.")
        return self.context.new_page()

    def find_page(self, url: str) -> Any | None:
        if self.context is None:
            raise NoChromeContext("BrowserSession.open() must be called first.")
        target_parts = urlsplit(url)
        target = (
            target_parts.scheme.lower(),
            target_parts.netloc.lower(),
            target_parts.path.rstrip("/"),
        )
        for page in self.context.pages:
            page_parts = urlsplit(page.url)
            page_key = (
                page_parts.scheme.lower(),
                page_parts.netloc.lower(),
                page_parts.path.rstrip("/"),
            )
            if page_key == target:
                return page
        return None

    def find_chatgpt_page(self) -> Any | None:
        pages = self.find_chatgpt_pages()
        return pages[0] if pages else None

    def find_chatgpt_pages(self) -> list[Any]:
        if self.context is None:
            raise NoChromeContext("BrowserSession.connect() must be called first.")
        return [
            page
            for page in self.context.pages
            if page.url.startswith("https://chatgpt.com/")
        ]

    def ensure_running(self) -> None:
        if self.chrome is None:
            raise ChromeClosedUnexpectedly(
                "The dedicated Google Chrome session is not running."
            )
        if (
            self.chrome.handle is not None
            and self.chrome.handle.poll() is not None
            and not self.launcher.cdp_available()
        ):
            raise ChromeClosedUnexpectedly(
                "The dedicated Google Chrome window was closed unexpectedly."
            )
        if self.chrome.handle is None and not self.launcher.cdp_available():
            raise ChromeClosedUnexpectedly(
                "The attached Google Chrome window was closed unexpectedly."
            )

    def _disconnect_playwright(self) -> None:
        # Stopping the client disconnects from CDP without closing Chrome's persistent context.
        try:
            if self.playwright is not None:
                self.playwright.stop()
        finally:
            self.playwright = None
            self.browser = None
            self.context = None
            self.page = None

    def close(self) -> None:
        self._disconnect_playwright()
        chrome, self.chrome = self.chrome, None
        if chrome is not None and chrome.owned and not self.keep_chrome_open:
            self.launcher.stop(chrome)

    def __enter__(self) -> "BrowserSession":
        return self.open()

    def __exit__(self, exc_type, exc, traceback):
        self.close()
