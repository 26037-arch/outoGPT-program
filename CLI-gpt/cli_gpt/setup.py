"""Explicit, manual first-run setup for the dedicated OutoGPT Chrome profile."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .browser import BrowserSession
from .errors import LoginNotReady, NoChatGPTPage, PromptBoxNotFound
from .selectors import find_prompt_box


def interactive_setup(
    *,
    extension_path: Path | None = None,
    session_factory: Callable[..., Any] = BrowserSession,
    input_func: Callable[[str], str] = input,
    output: Callable[[str], None] = print,
) -> None:
    """Wait for manual login before attaching Playwright or inspecting ChatGPT."""
    session = session_factory(require_setup=False, keep_chrome_open=True)
    try:
        session.start_chrome()
        extension = Path(extension_path).resolve() if extension_path else None
        output("Starting Google Chrome with the dedicated OutoGPT profile.")
        output("")
        output("Complete these steps in Chrome:")
        if extension is not None:
            output(
                "1. If this is the first run, open chrome://extensions, enable Developer mode,"
            )
            output(f"   and load this unpacked extension directory: {extension}")
            next_step = 2
        else:
            next_step = 1
        output(f"{next_step}. Open https://chatgpt.com/ yourself.")
        output(
            f"{next_step + 1}. Log in manually and complete any security verification."
        )
        output(
            f"{next_step + 2}. Leave a ChatGPT page open with the prompt box visible."
        )
        try:
            input_func("Return here and press Enter when ChatGPT is ready: ")
        except EOFError as exc:
            raise LoginNotReady(
                "Setup needs explicit confirmation from an interactive terminal."
            ) from exc

        # This is deliberately the first Playwright attachment and ChatGPT inspection.
        session.connect()
        pages = session.find_chatgpt_pages()
        if not pages:
            raise NoChatGPTPage(
                "No user-opened https://chatgpt.com/ tab was found after confirmation."
            )
        for page in pages:
            try:
                find_prompt_box(page)
                break
            except PromptBoxNotFound:
                continue
        else:
            raise LoginNotReady(
                "No open ChatGPT tab is ready: a prompt box is not available."
            )
        session.mark_setup_complete(extension_path=extension)
        output("Chrome setup completed. The browser will remain open for OutoGPT.")
    finally:
        session.close()
