"""Repository-relative defaults used by the controller."""

from __future__ import annotations

import os
from pathlib import Path

from cli_gpt.errors import InvalidExtensionPath


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
EXTENSION_DIR = REPOSITORY_ROOT / "extension"
DEFAULT_DATA_DIR = Path(
    os.environ.get("OUTOGPT_DATA_DIR", Path.home() / ".outogpt")
).expanduser()
DEFAULT_DATABASE_PATH = DEFAULT_DATA_DIR / "registry.sqlite3"


def extension_directory(path: Path | None = None) -> Path:
    candidate = (Path(path) if path is not None else EXTENSION_DIR).expanduser().resolve()
    if not candidate.is_dir():
        raise InvalidExtensionPath(f"Extension directory does not exist: {candidate}")
    if not (candidate / "manifest.json").is_file():
        raise InvalidExtensionPath(
            f"Extension manifest does not exist: {candidate / 'manifest.json'}"
        )
    return candidate
