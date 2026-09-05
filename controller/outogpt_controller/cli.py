"""Non-interactive command-line interface for the outoGPT controller."""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .controller import OutogptController, _error_details
from .errors import InvalidArgumentError
from .paths import DEFAULT_DATABASE_PATH
from .registry import Registry


class ControllerArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise InvalidArgumentError(message)


def _add_output_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="Emit one JSON object to stdout.")


def _add_prompt_options(parser: argparse.ArgumentParser) -> None:
    prompts = parser.add_mutually_exclusive_group(required=True)
    prompts.add_argument("--prompt")
    prompts.add_argument("--prompt-file", type=Path)


def build_parser() -> argparse.ArgumentParser:
    parser = ControllerArgumentParser(prog="outogpt")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    groups = parser.add_subparsers(dest="group", required=True)
    chat = groups.add_parser("chat")
    commands = chat.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create")
    create.add_argument("--project-url", required=True)
    _add_prompt_options(create)
    _add_output_option(create)

    send = commands.add_parser("send")
    send.add_argument("--chat-id", required=True)
    _add_prompt_options(send)
    _add_output_option(send)

    status = commands.add_parser("status")
    status.add_argument("--chat-id", required=True)
    _add_output_option(status)
    return parser


def _prompt(args: argparse.Namespace) -> str:
    if args.prompt is not None:
        value = args.prompt
    else:
        value = args.prompt_file.read_text(encoding="utf-8")
    if not value.strip():
        raise ValueError("Prompt must not be empty.")
    return value


def _write(payload: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return
    if payload.get("ok"):
        print(f"status: {payload.get('state') or payload.get('operation', {}).get('status')}")
        chat_id = payload.get("chat_id") or payload.get("chat", {}).get("chat_id")
        if chat_id:
            print(f"chat_id: {chat_id}")
        chat_url = payload.get("chat_url") or payload.get("chat", {}).get("chat_url")
        if chat_url:
            print(f"chat_url: {chat_url}")
    else:
        error = payload.get("error") or {}
        print(f"error: {error.get('code')}: {error.get('message')}", file=sys.stderr)


def main(
    argv: Sequence[str] | None = None,
    *,
    controller_factory=OutogptController,
) -> int:
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    args = None
    json_requested = "--json" in raw_argv
    try:
        args = build_parser().parse_args(raw_argv)
        controller = controller_factory(registry=Registry(args.database))
        if args.command == "create":
            payload = controller.create_chat(args.project_url, _prompt(args)).to_dict()
        elif args.command == "send":
            payload = controller.send_prompt(args.chat_id, _prompt(args)).to_dict()
        else:
            payload = controller.get_status(args.chat_id).to_dict()
        _write(payload, args.json)
        return 0 if payload["ok"] else 1
    except Exception as error:
        if args is not None and args.debug:
            traceback.print_exc(file=sys.stderr)
        code, message = _error_details(error)
        payload = {
            "ok": False,
            "operation_id": None,
            "chat_id": getattr(args, "chat_id", None) if args is not None else None,
            "state": "FAILED",
            "error": {"code": code, "message": message},
        }
        _write(payload, getattr(args, "json", json_requested) if args is not None else json_requested)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
