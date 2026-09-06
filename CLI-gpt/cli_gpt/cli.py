"""Command-line entry point for CLI-GPT."""

from __future__ import annotations

import argparse
import sys
import traceback
from collections.abc import Sequence

from .chatgpt import continue_chat, create_chat
from .config import (
    DEFAULT_EXTENSION_DIR,
    load_project_url,
    save_project_url,
    validate_project_url,
)
from .errors import (
    BrowserLaunchFailed,
    ChromeCdpConnectionFailed,
    ChromeClosedUnexpectedly,
    ChromeDebugPortUnavailable,
    ChromeNotFound,
    ChromeProfileInUse,
    CliGptError,
    GenerationNotStarted,
    GenerationTimeout,
    InvalidChatUrl,
    InvalidExtensionPath,
    InvalidProjectUrl,
    LoginNotReady,
    LoginRequired,
    NoChatGPTPage,
    NoChromeContext,
    PageStructureChanged,
    PromptBoxNotFound,
    PromptSendFailed,
)
from .setup import interactive_setup


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gpt", description="Send prompts through the ChatGPT web UI."
    )
    parser.add_argument(
        "--debug", action="store_true", help="Show a traceback for failures."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    setup_parser = commands.add_parser(
        "setup", help="Prepare the dedicated Chrome profile and save the Project URL."
    )
    setup_parser.add_argument("project_url")

    new_parser = commands.add_parser(
        "new", help="Create a chat inside the configured project."
    )
    new_parser.add_argument("prompt")

    send_parser = commands.add_parser(
        "send", help="Continue an existing ChatGPT conversation."
    )
    send_parser.add_argument("chat_url")
    send_parser.add_argument("prompt")
    return parser


def friendly_error(error: CliGptError) -> str:
    labels = {
        InvalidProjectUrl: "Invalid project URL or configuration",
        InvalidChatUrl: "Invalid chat URL",
        LoginRequired: "Login or user interaction required",
        InvalidExtensionPath: "Invalid extension directory",
        PromptBoxNotFound: "Prompt box not found",
        PromptSendFailed: "Prompt could not be sent",
        GenerationNotStarted: "Response generation did not start",
        GenerationTimeout: "Response generation timed out",
        ChromeNotFound: "Google Chrome was not found",
        ChromeDebugPortUnavailable: "Chrome debugging port is unavailable",
        ChromeCdpConnectionFailed: "Chrome CDP connection failed",
        ChromeProfileInUse: "OutoGPT Chrome profile is already in use",
        ChromeClosedUnexpectedly: "Google Chrome closed unexpectedly",
        NoChromeContext: "Chrome profile context is unavailable",
        NoChatGPTPage: "ChatGPT page is not open",
        LoginNotReady: "Manual Chrome setup is incomplete",
        BrowserLaunchFailed: "Browser could not be started",
        PageStructureChanged: "ChatGPT page could not be handled",
    }
    label = next(
        (text for kind, text in labels.items() if isinstance(error, kind)),
        "CLI-GPT error",
    )
    return f"{label}: {error}"


def _print_progress(event: str) -> None:
    messages = {
        "login_required": "Login or CAPTCHA interaction is required in the opened browser.",
        "prompt_sent": "Prompt sent.",
        "waiting": "Waiting for response...",
    }
    if event in messages:
        print(messages[event], flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "setup":
            project_url = validate_project_url(args.project_url)
            interactive_setup(extension_path=DEFAULT_EXTENSION_DIR)
            save_project_url(project_url)
            print("Project URL saved. Setup completed.")
            return 0

        if args.command == "new":
            print("Opening ChatGPT project...", flush=True)
            chat_url = create_chat(
                load_project_url(), args.prompt, progress=_print_progress
            )
        else:
            print("Opening conversation...", flush=True)
            chat_url = continue_chat(
                args.chat_url, args.prompt, progress=_print_progress
            )

        print("Completed.\n")
        print("status: completed")
        print(f"chat_url: {chat_url}")
        return 0
    except CliGptError as error:
        if args.debug:
            traceback.print_exc()
        else:
            print(f"error: {friendly_error(error)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
