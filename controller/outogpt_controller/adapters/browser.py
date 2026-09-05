"""Adapter from controller operations to the existing CLI-GPT implementation."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from cli_gpt.browser import BrowserSession
from cli_gpt.chatgpt import continue_chat_in_page, create_chat_in_page

from ..errors import BrowserNotOpenError
from ..paths import extension_directory


class BrowserAdapter:
    def __init__(
        self,
        extension_path: Path | None = None,
        *,
        session_factory: Callable[..., Any] = BrowserSession,
    ):
        self.extension_path = extension_directory(extension_path)
        self.session_factory = session_factory
        self.session: Any = None
        self.page: Any = None

    def open(self) -> "BrowserAdapter":
        if self.session is not None:
            return self
        session = self.session_factory(extension_path=self.extension_path)
        entered = session.__enter__()
        self.session = session
        self.page = entered.page
        return self

    def close(self) -> None:
        session, self.session = self.session, None
        self.page = None
        if session is not None:
            session.__exit__(None, None, None)

    def _page(self) -> Any:
        if self.page is None:
            raise BrowserNotOpenError("BrowserAdapter.open() must be called first.")
        return self.page

    def create_chat(
        self,
        project_url: str,
        prompt: str,
        *,
        progress: Callable[[str], None] | None = None,
    ) -> str:
        return create_chat_in_page(
            self._page(), project_url, prompt, progress=progress
        )

    def send_prompt(
        self,
        chat_url: str,
        prompt: str,
        *,
        progress: Callable[[str], None] | None = None,
    ) -> str:
        return continue_chat_in_page(
            self._page(), chat_url, prompt, progress=progress
        )

    def __enter__(self) -> "BrowserAdapter":
        return self.open()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()
