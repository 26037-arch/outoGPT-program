"""Central orchestration for browser actions, archiving, and persistence."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any
from urllib.parse import unquote, urlsplit

from cli_gpt.errors import CliGptError

from .adapters.archive import ArchiveAdapter, PassiveExtensionArchiveAdapter
from .adapters.browser import BrowserAdapter
from .errors import ControllerError, InvalidChatUrlError, UnknownChatError
from .models import (
    ArchiveResult,
    ControllerResult,
    OperationState,
    OperationType,
    StatusResult,
)
from .registry import Registry


ERROR_CODES = {
    "BrowserLaunchFailed": "BROWSER_START_FAILED",
    "InvalidExtensionPath": "INVALID_EXTENSION_PATH",
    "PromptSendFailed": "PROMPT_SEND_FAILED",
    "PromptBoxNotFound": "PROMPT_BOX_NOT_FOUND",
    "GenerationNotStarted": "GENERATION_NOT_STARTED",
    "GenerationTimeout": "GENERATION_TIMEOUT",
    "LoginRequired": "LOGIN_REQUIRED",
    "PageStructureChanged": "PAGE_STRUCTURE_CHANGED",
    "InvalidProjectUrl": "INVALID_PROJECT_URL",
    "InvalidChatUrl": "INVALID_CHAT_URL",
}


def extract_chat_id(chat_url: str) -> str:
    try:
        parts = [unquote(part) for part in urlsplit(chat_url).path.split("/") if part]
        index = parts.index("c")
        chat_id = parts[index + 1]
    except (ValueError, IndexError) as exc:
        raise InvalidChatUrlError(
            "Expected a conversation URL containing /c/<conversation-id>."
        ) from exc
    if not chat_id or not re.fullmatch(r"[A-Za-z0-9_-]+", chat_id):
        raise InvalidChatUrlError("Conversation URL contains an invalid chat id.")
    return chat_id


def _error_details(error: Exception) -> tuple[str, str]:
    if isinstance(error, ControllerError):
        return error.code, str(error)
    if isinstance(error, CliGptError):
        return error.code, str(error)
    return ERROR_CODES.get(type(error).__name__, "CONTROLLER_INTERNAL_ERROR"), str(error)


class OutogptController:
    def __init__(
        self,
        registry: Registry | None = None,
        *,
        browser_factory: Callable[[], Any] | None = None,
        archive_adapter: ArchiveAdapter | None = None,
        archive_timeout: float | None = None,
    ):
        self.registry = registry or Registry()
        self.browser_factory = browser_factory or BrowserAdapter
        # The production controller owns one BrowserAdapter for its whole life.
        # An explicitly injected factory retains the old one-operation contract,
        # which keeps third-party/test adapters backward compatible.
        self._persistent_browser = browser_factory is None
        self._browser_instance: Any = None
        self.archive_adapter = archive_adapter or PassiveExtensionArchiveAdapter()
        self.archive_timeout = archive_timeout

    def _acquire_browser(self) -> Any:
        if self._persistent_browser:
            if self._browser_instance is None:
                self._browser_instance = self.browser_factory()
                try:
                    self._browser_instance.open()
                except Exception:
                    try:
                        self._browser_instance.close()
                    finally:
                        self._browser_instance = None
                    raise
            return self._browser_instance
        browser = self.browser_factory()
        try:
            browser.open()
        except Exception:
            browser.close()
            raise
        return browser

    def _release_browser(self, browser: Any) -> None:
        if not self._persistent_browser:
            browser.close()

    def setup(
        self,
        project_url: str,
        *,
        input_func=input,
        output=print,
    ) -> None:
        if self._persistent_browser:
            if self._browser_instance is None:
                self._browser_instance = self.browser_factory()
            browser = self._browser_instance
        else:
            browser = self.browser_factory()
        if hasattr(browser, "setup"):
            try:
                browser.setup(project_url, input_func=input_func, output=output)
                return
            finally:
                if not self._persistent_browser:
                    browser.close()
        raise ControllerError("The configured browser adapter does not support setup.")

    def close(self) -> None:
        browser, self._browser_instance = self._browser_instance, None
        if browser is not None:
            browser.close()

    def __enter__(self) -> "OutogptController":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def _transition_callback(self, operation_id: str):
        waiting_recorded = False

        def progress(event: str) -> None:
            nonlocal waiting_recorded
            if event in {"prompt_sent", "waiting"} and not waiting_recorded:
                self.registry.transition(operation_id, OperationState.WAITING_RESPONSE)
                waiting_recorded = True

        return progress

    def _failure(
        self,
        operation_id: str,
        error: Exception,
        *,
        chat_id: str | None = None,
        chat_url: str | None = None,
    ) -> ControllerResult:
        code, message = _error_details(error)
        self.registry.fail_operation(
            operation_id, code, message, chat_id=chat_id
        )
        return ControllerResult(
            ok=False,
            operation_id=operation_id,
            chat_id=chat_id,
            chat_url=chat_url,
            state=OperationState.FAILED,
            error_code=code,
            error_message=message,
        )

    def create_chat(self, project_url: str, prompt: str) -> ControllerResult:
        operation = self.registry.create_operation(OperationType.CREATE)
        operation_id = operation.operation_id
        browser = None
        chat_id = None
        chat_url = None
        archive: ArchiveResult | None = None
        try:
            self.registry.transition(operation_id, OperationState.BROWSER_STARTING)
            browser = self._acquire_browser()
            if hasattr(browser, "prepare"):
                browser.prepare(project_url)
            self.registry.transition(operation_id, OperationState.PROMPT_SENDING)
            chat_url = browser.create_chat(
                project_url,
                prompt,
                progress=self._transition_callback(operation_id),
            )
            self.registry.transition(operation_id, OperationState.RESPONSE_COMPLETED)
            chat_id = extract_chat_id(chat_url)
            self.registry.attach_chat(operation_id, chat_id)
            self.registry.save_chat(
                chat_id, project_url, chat_url, operation_id
            )
            self.registry.transition(operation_id, OperationState.ARCHIVING)
            archive = self.archive_adapter.wait_until_saved(
                chat_id, timeout=self.archive_timeout
            )
            self._release_browser(browser)
            browser = None
            self.registry.complete_operation(operation_id)
            return ControllerResult(
                ok=True,
                operation_id=operation_id,
                chat_id=chat_id,
                chat_url=chat_url,
                state=OperationState.COMPLETED,
                response_status="completed",
                archive_status=archive.status,
                archive_detail=archive.detail,
            )
        except Exception as error:
            return self._failure(
                operation_id,
                error,
                chat_id=chat_id,
                chat_url=chat_url,
            )
        finally:
            if browser is not None:
                try:
                    self._release_browser(browser)
                except Exception:
                    pass

    def send_prompt(self, chat_id: str, prompt: str) -> ControllerResult:
        operation = self.registry.create_operation(OperationType.SEND, chat_id)
        operation_id = operation.operation_id
        browser = None
        chat_url = None
        archive: ArchiveResult | None = None
        try:
            chat = self.registry.get_chat(chat_id)
            if chat is None:
                raise UnknownChatError(f"Unknown chat_id: {chat_id}")
            chat_url = chat.chat_url
            self.registry.transition(operation_id, OperationState.BROWSER_STARTING)
            browser = self._acquire_browser()
            if hasattr(browser, "prepare"):
                browser.prepare(chat.project_url)
            self.registry.transition(operation_id, OperationState.PROMPT_SENDING)
            if self._persistent_browser:
                resulting_url = browser.send_prompt(
                    chat_url,
                    prompt,
                    chat_id=chat_id,
                    project_url=chat.project_url,
                    progress=self._transition_callback(operation_id),
                )
            else:
                resulting_url = browser.send_prompt(
                    chat_url,
                    prompt,
                    progress=self._transition_callback(operation_id),
                )
            self.registry.transition(operation_id, OperationState.RESPONSE_COMPLETED)
            resulting_id = extract_chat_id(resulting_url)
            if resulting_id != chat_id:
                raise InvalidChatUrlError(
                    "Browser navigated to a different conversation after sending the prompt."
                )
            chat_url = resulting_url
            self.registry.save_chat(
                chat_id, chat.project_url, chat_url, operation_id
            )
            self.registry.transition(operation_id, OperationState.ARCHIVING)
            archive = self.archive_adapter.wait_until_saved(
                chat_id, timeout=self.archive_timeout
            )
            self._release_browser(browser)
            browser = None
            self.registry.complete_operation(operation_id)
            return ControllerResult(
                ok=True,
                operation_id=operation_id,
                chat_id=chat_id,
                chat_url=chat_url,
                state=OperationState.COMPLETED,
                response_status="completed",
                archive_status=archive.status,
                archive_detail=archive.detail,
            )
        except Exception as error:
            return self._failure(
                operation_id,
                error,
                chat_id=chat_id,
                chat_url=chat_url,
            )
        finally:
            if browser is not None:
                try:
                    self._release_browser(browser)
                except Exception:
                    pass

    def get_status(self, chat_id: str) -> StatusResult:
        chat = self.registry.get_chat(chat_id)
        operation = self.registry.get_latest_operation(chat_id)
        if chat is None or operation is None:
            raise UnknownChatError(f"Unknown chat_id: {chat_id}")
        return StatusResult(chat=chat, operation=operation)
