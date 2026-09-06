"""Adapter from controller operations to the existing CLI-GPT implementation."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from cli_gpt.browser import BrowserSession
from cli_gpt.chatgpt import continue_chat_in_page, create_chat_in_page

from ..errors import BrowserNotOpenError
from ..paths import EXTENSION_DIR


class BrowserAdapter:
    def __init__(
        self,
        extension_path: Path | None = None,
        *,
        session_factory: Callable[..., Any] = BrowserSession,
    ):
        # The unpacked extension is installed manually into the persistent Chrome
        # profile. Its path is retained for setup/documentation, not passed to Chrome.
        self.extension_path = Path(extension_path or EXTENSION_DIR).expanduser().resolve()
        self.session_factory = session_factory
        self.session: Any = None
        self._session_owner: Any = None

    def open(self) -> "BrowserAdapter":
        if self.session is not None:
            return self
        session = self.session_factory()
        entered = session.__enter__()
        self._session_owner = session
        self.session = entered
        return self

    def close(self) -> None:
        owner, self._session_owner = self._session_owner, None
        self.session = None
        if owner is not None:
            owner.__exit__(None, None, None)

    def _session(self) -> Any:
        if self.session is None:
            raise BrowserNotOpenError("BrowserAdapter.open() must be called first.")
        return self.session

    def create_chat(
        self,
        project_url: str,
        prompt: str,
        *,
        progress: Callable[[str], None] | None = None,
    ) -> str:
        session = self._session()
        page = session.new_page()
        return create_chat_in_page(page, project_url, prompt, progress=progress)

    def send_prompt(
        self,
        chat_url: str,
        prompt: str,
        *,
        progress: Callable[[str], None] | None = None,
    ) -> str:
        session = self._session()
        page = session.find_page(chat_url) or session.new_page()
        return continue_chat_in_page(page, chat_url, prompt, progress=progress)

    def __enter__(self) -> "BrowserAdapter":
        return self.open()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()
