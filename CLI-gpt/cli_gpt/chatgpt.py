"""Minimal ChatGPT web actions exposed as a Python API."""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Callable
from urllib.parse import urlsplit

from .browser import BrowserSession
from .config import validate_chat_url, validate_project_url
from .errors import (
    GenerationNotStarted,
    GenerationTimeout,
    LoginRequired,
    ProjectAccessFailed,
    PageStructureChanged,
    PromptBoxNotFound,
    PromptSendFailed,
)
from .selectors import (
    assistant_response_count,
    find_new_chat_control,
    find_account_control,
    find_login_control,
    find_prompt_box,
    find_send_button,
    find_stop_button,
    latest_assistant_fingerprint,
    login_or_challenge_visible,
    project_access_error_visible,
)


PAGE_LOAD_TIMEOUT = 60
LOGIN_WAIT_TIMEOUT = 300
PROMPT_BOX_TIMEOUT = 30
GENERATION_START_TIMEOUT = 30
GENERATION_TIMEOUT = 600
CHAT_URL_TIMEOUT = 30
POLL_INTERVAL = 0.5

ProgressCallback = Callable[[str], None]


def _authentication_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return True
    host = (parts.hostname or "").lower()
    path = parts.path.lower()
    return host in {"auth.openai.com", "login.openai.com"} or "/auth/" in path


def verify_chatgpt_login(page: Any) -> bool:
    """Use independent URL, account/UI, and composer signals to verify login."""
    if _authentication_url(getattr(page, "url", "")):
        return False
    if login_or_challenge_visible(page) or find_login_control(page) is not None:
        return False
    try:
        composer_ready = find_prompt_box(page) is not None
    except PromptBoxNotFound:
        composer_ready = False
    account_ready = find_account_control(page) is not None
    app_navigation_ready = find_new_chat_control(page) is not None
    # A usable composer plus the absence of all authentication signals is the
    # strongest stable cross-layout signal. Account/new-chat controls add an
    # independent positive signal when the current responsive layout exposes one.
    return composer_ready and (
        account_ready or app_navigation_ready or "chatgpt.com" in getattr(page, "url", "")
    )


def _project_marker(url: str) -> str:
    try:
        parts = [part for part in urlsplit(url).path.split("/") if part]
    except ValueError:
        return ""
    return next((part for part in parts if part.startswith("g-p-")), "")


def _normalized_path(url: str) -> str:
    try:
        return urlsplit(url).path.rstrip("/")
    except ValueError:
        return ""


def verify_project_access(page: Any, project_url: str) -> bool:
    """Navigate to the configured project and prove that its composer is usable."""
    project_url = validate_project_url(project_url)
    try:
        page.goto(project_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT * 1000)
    except Exception as exc:
        raise ProjectAccessFailed(f"Could not open the configured ChatGPT project: {exc}") from exc
    if not verify_chatgpt_login(page) or project_access_error_visible(page):
        return False
    expected_marker = _project_marker(project_url)
    if expected_marker and expected_marker not in getattr(page, "url", ""):
        return False
    if not expected_marker and _normalized_path(project_url) != _normalized_path(
        getattr(page, "url", "")
    ):
        return False
    try:
        return find_prompt_box(page) is not None
    except PromptBoxNotFound:
        return False


class GenerationState(Enum):
    IDLE = auto()
    PROMPT_SENT = auto()
    WAITING_FOR_GENERATION = auto()
    GENERATING = auto()
    COMPLETE = auto()


@dataclass(frozen=True)
class GenerationSignals:
    stop_visible: bool
    composer_ready: bool
    send_ready: bool
    assistant_count: int
    response_fingerprint: str = ""


class GenerationTracker:
    """Pure state transition logic used by the DOM polling loop and unit tests."""

    def __init__(self, baseline_assistant_count: int, stable_observations: int = 3):
        self.baseline_assistant_count = baseline_assistant_count
        self.stable_observations = stable_observations
        self.state = GenerationState.IDLE
        self.generation_observed = False
        self._stable_count = 0
        self._last_fingerprint: str | None = None

    def mark_prompt_sent(self) -> None:
        if self.state is not GenerationState.IDLE:
            raise RuntimeError("Prompt can only be marked sent from IDLE.")
        self.state = GenerationState.PROMPT_SENT

    def observe(self, signals: GenerationSignals) -> GenerationState:
        if self.state is GenerationState.IDLE:
            return self.state

        response_grew = signals.assistant_count > self.baseline_assistant_count
        if signals.stop_visible:
            self.generation_observed = True
            self._stable_count = 0
            self._last_fingerprint = signals.response_fingerprint
            self.state = GenerationState.GENERATING
            return self.state

        if response_grew:
            self.generation_observed = True

        if not self.generation_observed:
            self.state = GenerationState.WAITING_FOR_GENERATION
            return self.state

        controls_ready = signals.composer_ready and signals.send_ready
        if not controls_ready:
            self._stable_count = 0
            self._last_fingerprint = signals.response_fingerprint
            self.state = GenerationState.GENERATING
            return self.state

        if signals.response_fingerprint == self._last_fingerprint:
            self._stable_count += 1
        else:
            self._last_fingerprint = signals.response_fingerprint
            self._stable_count = 1

        if self._stable_count >= self.stable_observations:
            self.state = GenerationState.COMPLETE
        else:
            self.state = GenerationState.GENERATING
        return self.state


def _emit(callback: ProgressCallback | None, event: str) -> None:
    if callback is not None:
        callback(event)


def _prompt_text(prompt_box: Any) -> str:
    try:
        tag_name = prompt_box.evaluate("element => element.tagName.toLowerCase()")
        if tag_name in {"textarea", "input"}:
            return prompt_box.input_value()
        return prompt_box.inner_text()
    except Exception:
        try:
            return prompt_box.text_content() or ""
        except Exception:
            return ""


def _fill_prompt(prompt_box: Any, prompt: str) -> None:
    if not isinstance(prompt, str) or not prompt.strip():
        raise PromptSendFailed("Prompt must not be empty.")
    try:
        prompt_box.fill(prompt)
    except Exception:
        try:
            prompt_box.click()
            prompt_box.press("Control+A")
            prompt_box.insert_text(prompt)
        except Exception as exc:
            raise PromptSendFailed("Could not enter the prompt into ChatGPT.") from exc
    if not _prompt_text(prompt_box).strip():
        raise PromptSendFailed("The prompt box was still empty after text entry.")


def _sample_signals(page: Any) -> GenerationSignals:
    try:
        composer_ready = find_prompt_box(page) is not None
    except PromptBoxNotFound:
        composer_ready = False
    return GenerationSignals(
        stop_visible=find_stop_button(page) is not None,
        composer_ready=composer_ready,
        send_ready=find_send_button(page) is not None,
        assistant_count=assistant_response_count(page),
        response_fingerprint=latest_assistant_fingerprint(page),
    )


def wait_for_generation(
    page: Any,
    baseline_assistant_count: int,
    *,
    start_timeout: float = GENERATION_START_TIMEOUT,
    generation_timeout: float = GENERATION_TIMEOUT,
    poll_interval: float = POLL_INTERVAL,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    tracker = GenerationTracker(baseline_assistant_count)
    tracker.mark_prompt_sent()
    started_at = monotonic()
    while True:
        state = tracker.observe(_sample_signals(page))
        if state is GenerationState.COMPLETE:
            return
        now = monotonic()
        if not tracker.generation_observed and now - started_at >= start_timeout:
            raise GenerationNotStarted(
                "ChatGPT did not show evidence that response generation started."
            )
        if now - started_at >= generation_timeout:
            raise GenerationTimeout("ChatGPT response generation exceeded the timeout.")
        sleep(poll_interval)


def _wait_for_prompt_box(
    page: Any,
    *,
    allow_new_chat_control: bool,
    progress: ProgressCallback | None,
):
    started_at = time.monotonic()
    new_chat_attempted = False
    intervention_announced = False
    while time.monotonic() - started_at < LOGIN_WAIT_TIMEOUT:
        try:
            return find_prompt_box(page)
        except PromptBoxNotFound:
            pass

        needs_intervention = login_or_challenge_visible(page) or "/auth/" in page.url
        if needs_intervention and not intervention_announced:
            _emit(progress, "login_required")
            intervention_announced = True

        elapsed = time.monotonic() - started_at
        if allow_new_chat_control and not needs_intervention and elapsed >= 3 and not new_chat_attempted:
            control = find_new_chat_control(page)
            if control is not None:
                try:
                    control.click()
                except Exception:
                    pass
            new_chat_attempted = True
        page.wait_for_timeout(int(POLL_INTERVAL * 1000))

    if intervention_announced:
        raise LoginRequired(
            "Login or CAPTCHA interaction was not completed in the opened browser within five minutes."
        )
    raise PromptBoxNotFound(
        "Could not find the ChatGPT prompt box. The web UI may have changed."
    )


def _open(page: Any, url: str, *, new_chat: bool, progress: ProgressCallback | None):
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT * 1000)
    except Exception as exc:
        raise PageStructureChanged(f"Could not load the ChatGPT page: {exc}") from exc
    return _wait_for_prompt_box(
        page,
        allow_new_chat_control=new_chat,
        progress=progress,
    )


def _send_prompt_and_wait(
    page: Any,
    prompt_box: Any,
    prompt: str,
    progress: ProgressCallback | None,
) -> None:
    baseline = assistant_response_count(page)
    _fill_prompt(prompt_box, prompt)

    send_button = find_send_button(page)
    try:
        if send_button is not None:
            send_button.click()
        else:
            prompt_box.press("Enter")
    except Exception as exc:
        raise PromptSendFailed("The prompt was entered but could not be sent.") from exc

    _emit(progress, "prompt_sent")
    _emit(progress, "waiting")
    wait_for_generation(page, baseline)


def _wait_for_new_chat_url(page: Any, original_url: str) -> str:
    deadline = time.monotonic() + CHAT_URL_TIMEOUT
    while time.monotonic() < deadline:
        current = page.url
        if current != original_url:
            try:
                return validate_chat_url(current)
            except Exception:
                pass
        page.wait_for_timeout(int(POLL_INTERVAL * 1000))
    raise PageStructureChanged(
        "The response completed, but ChatGPT did not expose a conversation URL."
    )


def create_chat_in_page(
    page: Any,
    project_url: str,
    prompt: str,
    *,
    progress: ProgressCallback | None = None,
) -> str:
    """Create a conversation using a caller-owned Playwright page."""
    project_url = validate_project_url(project_url)
    prompt_box = _open(page, project_url, new_chat=True, progress=progress)
    original_url = page.url
    _send_prompt_and_wait(page, prompt_box, prompt, progress)
    return _wait_for_new_chat_url(page, original_url)


def continue_chat_in_page(
    page: Any,
    chat_url: str,
    prompt: str,
    *,
    progress: ProgressCallback | None = None,
) -> str:
    """Continue a conversation using a caller-owned Playwright page."""
    chat_url = validate_chat_url(chat_url)
    prompt_box = _open(page, chat_url, new_chat=False, progress=progress)
    _send_prompt_and_wait(page, prompt_box, prompt, progress)
    return page.url


def create_chat(
    project_url: str,
    prompt: str,
    *,
    progress: ProgressCallback | None = None,
) -> str:
    """Create a conversation inside a ChatGPT project and return its final URL."""
    project_url = validate_project_url(project_url)
    with BrowserSession() as browser:
        return create_chat_in_page(
            browser.page,
            project_url,
            prompt,
            progress=progress,
        )


def continue_chat(
    chat_url: str,
    prompt: str,
    *,
    progress: ProgressCallback | None = None,
) -> str:
    """Continue an existing ChatGPT conversation and return its URL."""
    chat_url = validate_chat_url(chat_url)
    with BrowserSession() as browser:
        return continue_chat_in_page(
            browser.page,
            chat_url,
            prompt,
            progress=progress,
        )
