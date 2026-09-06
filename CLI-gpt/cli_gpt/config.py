"""Local configuration and ChatGPT URL validation."""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from .errors import InvalidChatUrl, InvalidProjectUrl


PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Browser state is user state, not repository state.  Keeping this path stable is
# what lets a later OutoGPT process reuse the authenticated Chromium profile.
DATA_DIR = Path(
    os.environ.get("OUTOGPT_DATA_DIR", Path.home() / ".outogpt")
).expanduser()
CONFIG_FILE = DATA_DIR / "config.json"
PROFILE_DIR = DATA_DIR / "chromium-profile"
LOCK_FILE = DATA_DIR / "chromium-profile.lock"


def _validated_chatgpt_parts(url: str, error_type: type[Exception]):
    if not isinstance(url, str) or not url.strip():
        raise error_type("URL must not be empty.")
    try:
        parts = urlsplit(url.strip())
    except ValueError as exc:
        raise error_type("The URL is malformed.") from exc

    hostname = (parts.hostname or "").lower()
    if (
        parts.scheme != "https"
        or (hostname != "chatgpt.com" and not hostname.endswith(".chatgpt.com"))
        or parts.username is not None
        or parts.password is not None
    ):
        raise error_type("Expected an HTTPS URL on chatgpt.com.")
    return parts


def validate_project_url(url: str) -> str:
    parts = _validated_chatgpt_parts(url, InvalidProjectUrl)
    path_parts = [part for part in parts.path.split("/") if part]
    if not path_parts or "c" in path_parts:
        raise InvalidProjectUrl("Expected a ChatGPT Project URL, not a conversation URL.")
    return url.strip()


def validate_chat_url(url: str) -> str:
    parts = _validated_chatgpt_parts(url, InvalidChatUrl)
    path_parts = [part for part in parts.path.split("/") if part]
    try:
        conversation_index = path_parts.index("c")
    except ValueError as exc:
        raise InvalidChatUrl("Expected a ChatGPT conversation URL containing /c/<id>.") from exc
    if conversation_index + 1 >= len(path_parts):
        raise InvalidChatUrl("The ChatGPT conversation URL has no conversation identifier.")
    return url.strip()


def save_project_url(url: str, path: Path = CONFIG_FILE) -> None:
    validated = validate_project_url(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps({"project_url": validated}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def load_project_url(path: Path = CONFIG_FILE) -> str:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        url = raw["project_url"]
    except FileNotFoundError as exc:
        raise InvalidProjectUrl("No project is configured. Run: gpt setup <PROJECT_URL>") from exc
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise InvalidProjectUrl("The local configuration file is invalid. Run gpt setup again.") from exc
    return validate_project_url(url)
