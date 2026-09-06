"""Controller adapter for two-phase manual authentication and automation."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from cli_gpt.browser import BrowserManager, ChatSessionStatus
from cli_gpt.chatgpt import (
    continue_chat_in_page,
    create_chat_in_page,
    verify_chatgpt_login,
    verify_project_access,
)
from cli_gpt.config import validate_project_url
from cli_gpt.errors import ChatGPTSessionRestoreFailed, ProjectAccessFailed

from ..errors import BrowserNotOpenError
from ..paths import extension_directory


class BrowserAdapter:
    """Own the browser manager used by one long-lived controller."""

    def __init__(
        self,
        extension_path: Path | None = None,
        *,
        session_factory: Callable[..., Any] = BrowserManager,
    ):
        self.extension_path = extension_directory(extension_path)
        self.session_factory = session_factory
        self.manager: Any = None
        self.session: Any = None  # Backward-compatible attribute.
        self.page: Any = None
        self._ready_projects: set[str] = set()

    def _attach(self, manager: Any) -> None:
        self.manager = manager
        self.session = manager
        self.page = getattr(manager, "page", None)

    def open(self, *, verify_extension: bool = True) -> "BrowserAdapter":
        """Open the extension-enabled automation phase."""
        if self.manager is None:
            manager = self.session_factory(extension_path=self.extension_path)
            if hasattr(manager, "start_automation"):
                entered = manager.start_automation()
            else:
                entered = manager.__enter__()
            self._attach(manager)
            self.page = getattr(entered, "page", self.page)
        try:
            if verify_extension and hasattr(self.manager, "verify_extension"):
                self.manager.verify_extension()
        except Exception:
            self.close()
            raise
        return self

    def close(self) -> None:
        manager, self.manager = self.manager, None
        self.session = None
        self.page = None
        self._ready_projects.clear()
        if manager is not None:
            manager.__exit__(None, None, None)

    def _manager(self) -> Any:
        if self.manager is None:
            raise BrowserNotOpenError("BrowserAdapter.open() must be called first.")
        return self.manager

    @staticmethod
    def _manual_instructions(output: Callable[[str], None]) -> None:
        output("")
        output("Use the opened Chromium window manually.")
        output("")
        output("1. Open ChatGPT yourself.")
        output("2. Complete login yourself.")
        output("3. Complete any CAPTCHA, bot verification, MFA,")
        output("   or additional authentication yourself.")
        output("")
        output("OutoGPT will not interact with the browser during this step.")
        output("")
        output("When everything is complete, return to this terminal")
        output("and press Enter to continue.")

    def _manual_authentication(
        self,
        project_url: str,
        *,
        input_func: Callable[[str], str],
        output: Callable[[str], None],
        initial: bool,
    ) -> None:
        """Run an extension-free phase with a strict user-controlled boundary."""
        if self.manager is None:
            manager = self.session_factory(extension_path=self.extension_path)
            manager.start_bootstrap()
            self._attach(manager)
        else:
            manager = self._manager()
            manager.restart_for_manual_bootstrap()
            self.page = None

        if initial:
            output("[setup] OutoGPT Chromium started.")
        else:
            output("[auth] Extension-free Chromium started for manual authentication.")
        self._manual_instructions(output)

        while True:
            # Absolutely no page access occurs before this blocking call returns.
            input_func("")
            page = manager.find_open_chatgpt_page()
            if page is not None and verify_chatgpt_login(page):
                break
            output("[setup] ChatGPT login was not detected.")
            output("")
            output(
                "Return to Chromium and complete the login or verification manually."
            )
            output("")
            output("Press Enter when you are ready for another verification.")

        output("[setup] Login verified. Restarting Chromium for automation...")
        manager.finish_bootstrap_and_start_automation()
        self.page = manager.page

        output("[setup] Loading OutoGPT extension...")
        manager.verify_extension()
        output("[setup] Verifying restored ChatGPT session and project access...")
        if not verify_project_access(manager.setup_page, project_url):
            if not verify_chatgpt_login(manager.setup_page):
                raise ChatGPTSessionRestoreFailed(
                    "ChatGPT login was verified during manual bootstrap, but the "
                    "authenticated session was not restored after Chromium restarted."
                )
            raise ProjectAccessFailed(
                "The configured ChatGPT project does not exist or is not accessible "
                "to the authenticated account."
            )
        self._ready_projects.add(project_url)

    def setup(
        self,
        project_url: str,
        *,
        input_func: Callable[[str], str] = input,
        output: Callable[[str], None] = print,
    ) -> None:
        """Authenticate manually, then transition to verified automation."""
        project_url = validate_project_url(project_url)
        if self.manager is not None:
            raise BrowserNotOpenError("Setup must begin before browser automation.")
        output("[setup] Starting manual authentication bootstrap...")
        self._manual_authentication(
            project_url,
            input_func=input_func,
            output=output,
            initial=True,
        )

    def prepare(
        self,
        project_url: str,
        *,
        input_func: Callable[[str], str] = input,
        output: Callable[[str], None] = print,
    ) -> None:
        """Verify a saved session, falling back to manual authentication if expired."""
        project_url = validate_project_url(project_url)
        self.open()
        if project_url in self._ready_projects:
            return
        manager = self._manager()
        output("[startup] Verifying saved ChatGPT session and project access...")
        if verify_project_access(manager.setup_page, project_url):
            self._ready_projects.add(project_url)
            return
        if verify_chatgpt_login(manager.setup_page):
            raise ProjectAccessFailed(
                "The configured ChatGPT project does not exist or is not accessible "
                "to the authenticated account."
            )
        output("[startup] The saved ChatGPT session requires manual authentication.")
        self._manual_authentication(
            project_url,
            input_func=input_func,
            output=output,
            initial=False,
        )

    def create_chat(
        self,
        project_url: str,
        prompt: str,
        *,
        progress: Callable[[str], None] | None = None,
    ) -> str:
        manager = self._manager()
        if hasattr(manager, "new_conversation"):
            chat_session = manager.new_conversation(project_url)
            page = chat_session.page
        else:
            chat_session = None
            page = self.page
        try:
            if chat_session is not None:
                chat_session.status = ChatSessionStatus.SUBMITTING
            chat_url = create_chat_in_page(page, project_url, prompt, progress=progress)
            if chat_session is not None:
                manager.bind_conversation(chat_session, chat_url)
                chat_session.status = ChatSessionStatus.COMPLETED
            return chat_url
        except Exception:
            if chat_session is not None:
                chat_session.status = ChatSessionStatus.ERROR
            raise

    def send_prompt(
        self,
        chat_url: str,
        prompt: str,
        *,
        chat_id: str | None = None,
        project_url: str = "",
        progress: Callable[[str], None] | None = None,
    ) -> str:
        manager = self._manager()
        if hasattr(manager, "page_for_conversation"):
            if chat_id is None:
                from ..controller import extract_chat_id

                chat_id = extract_chat_id(chat_url)
            chat_session = manager.page_for_conversation(
                chat_id, chat_url, project_url
            )
            page = chat_session.page
        else:
            chat_session = None
            page = self.page
        try:
            if chat_session is not None:
                chat_session.status = ChatSessionStatus.SUBMITTING
            resulting_url = continue_chat_in_page(
                page, chat_url, prompt, progress=progress
            )
            if chat_session is not None:
                manager.bind_conversation(chat_session, resulting_url, chat_id)
                chat_session.status = ChatSessionStatus.COMPLETED
            return resulting_url
        except Exception:
            if chat_session is not None:
                chat_session.status = ChatSessionStatus.ERROR
            raise

    def __enter__(self) -> "BrowserAdapter":
        return self.open()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()
