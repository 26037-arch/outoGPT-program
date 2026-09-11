"""Atomic project state and append-only per-conversation Markdown storage."""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from cli_gpt.project import QAPair

from .errors import MarkdownArchiveError, ProjectStateError


STATE_VERSION = 1
_INVALID_COMPONENT = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_QA_END = re.compile(r"<!-- outogpt-qa-end:(\d+) -->")


def sanitize_component(value: str, fallback: str) -> str:
    cleaned = _INVALID_COMPONENT.sub("-", value).strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        cleaned = fallback
    if cleaned.upper() in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }:
        cleaned = f"_{cleaned}"
    return cleaned[:120].rstrip(" .") or fallback


def _atomic_text(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


@dataclass(frozen=True)
class ChatState:
    chat_id: str
    chat_url: str
    title: str
    qa_count: int
    file: str

    @classmethod
    def from_dict(cls, value: Any) -> "ChatState":
        if not isinstance(value, dict):
            raise ProjectStateError("A project chat state entry is not an object.")
        try:
            state = cls(
                chat_id=str(value["chat_id"]),
                chat_url=str(value["chat_url"]),
                title=str(value["title"]),
                qa_count=int(value["qa_count"]),
                file=str(value["file"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProjectStateError("A project chat state entry is malformed.") from exc
        expected_file = f"chats/{state.chat_id}.md"
        if (
            state.qa_count < 0
            or not re.fullmatch(r"[A-Za-z0-9_-]+", state.chat_id)
            or state.file.replace("\\", "/") != expected_file
        ):
            raise ProjectStateError("A project chat state entry has unsafe values.")
        return state


@dataclass
class ProjectState:
    project_id: str
    project_url: str
    project_name: str
    chats: dict[str, ChatState] = field(default_factory=dict)
    version: int = STATE_VERSION

    @classmethod
    def from_dict(cls, value: Any) -> "ProjectState":
        if not isinstance(value, dict) or value.get("version") != STATE_VERSION:
            raise ProjectStateError(
                "project.json has an unsupported or missing version."
            )
        try:
            raw_chats = value["chats"]
            if not isinstance(raw_chats, dict):
                raise TypeError
            chats = {
                str(key): ChatState.from_dict(item) for key, item in raw_chats.items()
            }
            state = cls(
                project_id=str(value["project_id"]),
                project_url=str(value["project_url"]),
                project_name=str(value["project_name"]),
                chats=chats,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProjectStateError("project.json is malformed.") from exc
        if any(key != chat.chat_id for key, chat in chats.items()):
            raise ProjectStateError(
                "project.json chat keys do not match their chat IDs."
            )
        return state

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "project_id": self.project_id,
            "project_url": self.project_url,
            "project_name": self.project_name,
            "chats": {
                chat_id: asdict(chat) for chat_id, chat in sorted(self.chats.items())
            },
        }


class ProjectArchive:
    def __init__(self, directory: Path, state: ProjectState, *, is_new: bool):
        self.directory = Path(directory)
        self.state = state
        self.is_new = is_new
        self.state_path = self.directory / "project.json"
        self.index_path = self.directory / "index.md"
        self.chats_directory = self.directory / "chats"

    @classmethod
    def open(
        cls,
        root: Path,
        project_id: str,
        project_name: str,
        project_url: str,
    ) -> "ProjectArchive":
        root = Path(root).expanduser()
        desired_name = sanitize_component(project_name, project_id)
        existing: tuple[Path, ProjectState] | None = None
        if root.is_dir():
            for state_path in sorted(root.glob("*/project.json")):
                try:
                    value = json.loads(state_path.read_text(encoding="utf-8"))
                    state = ProjectState.from_dict(value)
                except (OSError, json.JSONDecodeError, ProjectStateError) as exc:
                    if state_path.parent.name == desired_name:
                        raise ProjectStateError(
                            f"Could not safely read existing state: {state_path}"
                        ) from exc
                    continue
                if state.project_id == project_id:
                    existing = (state_path.parent, state)
                    break

        if existing is not None:
            directory, state = existing
            state.project_url = project_url
            state.project_name = project_name
            return cls(directory, state, is_new=False)

        directory = root / desired_name
        if directory.exists() and any(directory.iterdir()):
            directory = root / sanitize_component(
                f"{project_name}--{project_id}", project_id
            )
        state = ProjectState(project_id, project_url, project_name)
        return cls(directory, state, is_new=True)

    def save_state(self) -> None:
        try:
            _atomic_text(
                self.state_path,
                json.dumps(self.state.to_dict(), ensure_ascii=False, indent=2) + "\n",
            )
            self.is_new = False
        except OSError as exc:
            raise ProjectStateError(
                f"Could not atomically save {self.state_path}: {exc}"
            ) from exc

    def chat_path(self, chat_id: str) -> Path:
        return self.chats_directory / f"{chat_id}.md"

    @staticmethod
    def _document_header(title: str, chat_id: str, chat_url: str) -> str:
        safe_title = " ".join(title.splitlines()).strip() or "Untitled conversation"
        return f"# {safe_title}\n\n- Chat ID: `{chat_id}`\n- URL: {chat_url}\n\n---\n"

    @staticmethod
    def _render_pairs(pairs: list[QAPair], start: int) -> str:
        sections: list[str] = []
        for number, pair in enumerate(pairs, start=start):
            sections.append(
                f"\n## Q{number}\n\n{pair.user.strip()}\n\n"
                f"## A{number}\n\n{pair.assistant.strip()}\n\n"
                f"<!-- outogpt-qa-end:{number} -->\n\n---\n"
            )
        return "".join(sections)

    def create_chat(
        self,
        chat_id: str,
        title: str,
        chat_url: str,
        pairs: list[QAPair],
    ) -> None:
        if not pairs:
            raise MarkdownArchiveError(
                "Refusing to create a chat archive with no QA pairs."
            )
        path = self.chat_path(chat_id)
        contents = self._document_header(title, chat_id, chat_url)
        contents += self._render_pairs(pairs, 1)
        try:
            _atomic_text(path, contents)
        except OSError as exc:
            raise MarkdownArchiveError(f"Could not create {path}: {exc}") from exc

    def append_pairs(self, chat_id: str, pairs: list[QAPair], start: int) -> None:
        if not pairs:
            return
        path = self.chat_path(chat_id)
        if not path.is_file():
            raise MarkdownArchiveError(f"Known chat Markdown is missing: {path}")
        payload = self._render_pairs(pairs, start)
        try:
            with path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            raise MarkdownArchiveError(f"Could not append to {path}: {exc}") from exc

    def completed_qa_count(self, chat_id: str) -> int:
        path = self.chat_path(chat_id)
        if not path.is_file():
            raise MarkdownArchiveError(f"Known chat Markdown is missing: {path}")
        try:
            markers = [
                int(value)
                for value in _QA_END.findall(path.read_text(encoding="utf-8"))
            ]
        except OSError as exc:
            raise MarkdownArchiveError(f"Could not inspect {path}: {exc}") from exc
        if markers != list(range(1, len(markers) + 1)):
            raise MarkdownArchiveError(
                f"QA completion markers are inconsistent in {path}."
            )
        return len(markers)

    def write_index(self) -> None:
        title = (
            " ".join(self.state.project_name.splitlines()).strip()
            or self.state.project_id
        )
        lines = [f"# {title}", "", "## Chats", ""]
        for chat in sorted(
            self.state.chats.values(), key=lambda item: item.title.casefold()
        ):
            link_title = (
                " ".join(chat.title.splitlines()).strip() or "Untitled conversation"
            )
            link_title = (
                link_title.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
            )
            lines.append(f"- [{link_title}]({chat.file.replace(os.sep, '/')})")
        lines.append("")
        try:
            _atomic_text(self.index_path, "\n".join(lines))
        except OSError as exc:
            raise MarkdownArchiveError(
                f"Could not write {self.index_path}: {exc}"
            ) from exc
