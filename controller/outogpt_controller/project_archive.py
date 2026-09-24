"""Atomic project state and append-only per-conversation Markdown storage."""

from __future__ import annotations

import json
import hashlib
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
    if cleaned.split(".", 1)[0].upper() in {
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
    try:
        if path.is_file() and path.read_text(encoding="utf-8") == contents:
            return
    except (OSError, UnicodeError):
        # The verified replacement below reports any durable read/write failure.
        pass
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        if temporary.read_text(encoding="utf-8") != contents:
            raise OSError(f"Temporary-file verification failed for {temporary}")
        os.replace(temporary, path)
        if path.read_text(encoding="utf-8") != contents:
            raise OSError(f"Committed-file verification failed for {path}")
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
    content_sha256: str | None = None

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
                content_sha256=value.get("content_sha256"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProjectStateError("A project chat state entry is malformed.") from exc
        expected_file = f"chats/{state.chat_id}.md"
        if (
            state.qa_count < 0
            or (
                state.content_sha256 is not None
                and (
                    not isinstance(state.content_sha256, str)
                    or not re.fullmatch(r"[a-f0-9]{64}", state.content_sha256)
                )
            )
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", state.chat_id)
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
        if not re.fullmatch(r"g-p-[A-Za-z0-9_-]+", state.project_id):
            raise ProjectStateError("project.json contains an unsafe project id.")
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
    def __init__(
        self,
        directory: Path,
        state: ProjectState,
        *,
        is_new: bool,
        metadata_changed: bool = False,
    ):
        self.directory = Path(directory)
        self.state = state
        self.is_new = is_new
        self.metadata_changed = metadata_changed
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
        if root.exists() and not root.is_dir():
            raise ProjectStateError(f"Archive root is not a directory: {root}")
        if not re.fullmatch(r"g-p-[A-Za-z0-9_-]{1,128}", project_id):
            raise ProjectStateError("Refusing to use an unsafe project id.")
        desired_name = sanitize_component(project_name, project_id)
        existing: tuple[Path, ProjectState] | None = None
        if root.is_dir():
            for state_path in sorted(root.glob("*/project.json")):
                try:
                    value = json.loads(state_path.read_text(encoding="utf-8"))
                    state = ProjectState.from_dict(value)
                except (
                    OSError,
                    UnicodeError,
                    json.JSONDecodeError,
                    ProjectStateError,
                ) as exc:
                    raise ProjectStateError(
                        f"Could not safely read existing state: {state_path}"
                    ) from exc
                if state.project_id == project_id:
                    existing = (state_path.parent, state)
                    break

        if existing is not None:
            directory, state = existing
            metadata_changed = (
                state.project_url != project_url or state.project_name != project_name
            )
            state.project_url = project_url
            state.project_name = project_name
            return cls(
                directory,
                state,
                is_new=False,
                metadata_changed=metadata_changed,
            )

        directory = root / desired_name
        if directory.exists() and any(directory.iterdir()):
            directory = root / sanitize_component(
                f"{project_name}--{project_id}", project_id
            )
            if directory.exists() and any(directory.iterdir()):
                raise ProjectStateError(
                    f"Archive directory collision cannot be resolved safely: {directory}"
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
            self.metadata_changed = False
        except OSError as exc:
            raise ProjectStateError(
                f"Could not atomically save {self.state_path}: {exc}"
            ) from exc

    def chat_path(self, chat_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", chat_id):
            raise MarkdownArchiveError(
                f"Unsafe chat id cannot be archived: {chat_id!r}"
            )
        return self.chats_directory / f"{chat_id}.md"

    def save_progress(self, progress: dict) -> None:
        """Separate recovery journal; never claims that a chat has been saved."""
        _atomic_text(
            self.directory / "update-progress.json",
            json.dumps(progress, ensure_ascii=False, indent=2) + "\n",
        )

    def load_progress(self) -> dict:
        path = self.directory / "update-progress.json"
        if not path.exists():
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ProjectStateError("Update recovery journal is not an object.")
        pending = value.get("pending_chat_id")
        discovered = value.get("discovered", [])
        if (
            value.get("status") not in {"running", "paused", "complete"}
            or (
                pending is not None
                and (
                    not isinstance(pending, str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", pending)
                )
            )
            or not isinstance(discovered, list)
        ):
            raise ProjectStateError(
                "Update recovery journal has invalid status or chat IDs."
            )
        from cli_gpt.config import validate_chat_url
        from cli_gpt.project import extract_chat_id

        for item in discovered:
            try:
                if (
                    not isinstance(item, dict)
                    or set(item) != {"chat_id", "chat_url", "title"}
                    or extract_chat_id(validate_chat_url(item["chat_url"]))
                    != item["chat_id"]
                ):
                    raise ValueError("Invalid pending chat")
            except Exception as exc:
                raise ProjectStateError(
                    "Update recovery journal contains an invalid chat URL."
                ) from exc
        return value

    @staticmethod
    def _snapshot_body(snapshot) -> str:
        body = ProjectArchive._document_header(
            snapshot.title, snapshot.chat_id, snapshot.chat_url
        )
        body += ProjectArchive._render_pairs(list(snapshot.qa_pairs), 1)
        # QA pairing cannot represent a final unanswered user, empty conversations,
        # or hidden messages. Retain every verified UUID and exact network content
        # alongside the existing Markdown conversion, inside the same MD file.
        if snapshot.messages or snapshot.non_ui_messages:
            body += "\n## Verified messages\n"
            for message in snapshot.messages:
                body += f"\n### {message['role']} ({message['id']})\n\n{message['markdown']}\n"
            sources = {
                "messages": list(snapshot.messages),
                "non_ui_messages": list(snapshot.non_ui_messages),
            }
            raw = json.dumps(sources, ensure_ascii=False, indent=2)
            fence = "`" * max(
                3, 1 + max((len(m[0]) for m in re.finditer(r"`+", raw)), default=0)
            )
            body += f"\n## Message evidence\n\n{fence}json\n{raw}\n{fence}\n"
        return body

    def sync_snapshot(self, snapshot) -> tuple[bool, str]:
        """Append a content-verified revision without ever deleting prior MD bytes.

        Length framing avoids treating marker-like text inside user content as a
        checkpoint. An orphan revision is adopted only after exact body comparison.
        Historical edits and count regressions create revisions too.
        """
        path = self.chat_path(snapshot.chat_id)
        known = self.state.chats.get(snapshot.chat_id)
        if known and not path.is_file():
            raise MarkdownArchiveError(f"Known chat Markdown is missing: {path}")
        try:
            existing = path.read_text(encoding="utf-8") if path.exists() else ""
            body = self._snapshot_body(snapshot)
            digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
            pattern = re.compile(
                r"(?m)^<!-- outogpt-snapshot:([a-f0-9]{64}):(\d+) -->\n"
            )
            offset = 0
            latest = None
            hashes = set()
            while match := pattern.search(existing, offset):
                end = match.end() + int(match[2])
                stored = existing[match.end() : end]
                trailer = "<!-- outogpt-snapshot-end -->\n"
                if hashlib.sha256(stored.encode("utf-8")).hexdigest() != match[
                    1
                ] or not existing[end:].startswith(trailer):
                    raise MarkdownArchiveError(
                        "Archived snapshot failed length/content verification."
                    )
                latest = (match[1], stored)
                hashes.add(match[1])
                offset = end + len(trailer)
            if known and known.content_sha256 and known.content_sha256 not in hashes:
                raise MarkdownArchiveError("Registered snapshot content is missing.")
            if latest == (digest, body) and offset == len(existing):
                return False, digest
            revision = f"\n<!-- outogpt-snapshot:{digest}:{len(body)} -->\n{body}<!-- outogpt-snapshot-end -->\n"
            _atomic_text(path, existing + revision)
            if path.read_text(encoding="utf-8") != existing + revision:
                raise MarkdownArchiveError(
                    "Saved snapshot content differs from the verified candidate."
                )
            return True, digest
        except (OSError, UnicodeError) as exc:
            raise MarkdownArchiveError(
                f"Could not save and verify {path}: {exc}"
            ) from exc

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
        if path.exists():
            raise MarkdownArchiveError(
                f"Refusing to overwrite an existing unregistered chat archive: {path}"
            )
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
        physical_count = self.recover_completed_qa_count(chat_id)
        if start != physical_count + 1:
            raise MarkdownArchiveError(
                f"Refusing a non-contiguous append to {path}: "
                f"expected Q{physical_count + 1}, got Q{start}."
            )
        payload = self._render_pairs(pairs, start)
        try:
            existing = path.read_text(encoding="utf-8")
            candidate = existing + payload
            markers = [int(match.group(1)) for match in _QA_END.finditer(candidate)]
            expected = list(range(1, physical_count + len(pairs) + 1))
            if markers != expected:
                raise MarkdownArchiveError(
                    f"Rendered QA completion markers are inconsistent for {path}."
                )
            _atomic_text(path, candidate)
            verified = self.completed_qa_count(
                chat_id, minimum_expected=physical_count + len(pairs)
            )
            if verified != physical_count + len(pairs):
                raise MarkdownArchiveError(
                    f"Committed Markdown verification failed for {path}."
                )
        except MarkdownArchiveError:
            raise
        except (OSError, UnicodeError) as exc:
            raise MarkdownArchiveError(
                f"Could not atomically update {path}: {exc}"
            ) from exc

    def recover_completed_qa_count(
        self, chat_id: str, *, minimum_expected: int = 0
    ) -> int:
        """Validate legacy markers without deleting unmarked user content."""
        path = self.chat_path(chat_id)
        if not path.is_file():
            raise MarkdownArchiveError(f"Known chat Markdown is missing: {path}")
        try:
            contents = path.read_text(encoding="utf-8")
            matches = list(_QA_END.finditer(contents))
            markers = [int(match.group(1)) for match in matches]
        except (OSError, UnicodeError) as exc:
            raise MarkdownArchiveError(f"Could not inspect {path}: {exc}") from exc
        if markers != list(range(1, len(markers) + 1)):
            raise MarkdownArchiveError(
                f"QA completion markers are inconsistent in {path}."
            )
        if len(markers) < minimum_expected:
            raise MarkdownArchiveError(
                f"The chat Markdown contains fewer completed QA markers than expected: {path}"
            )
        if not matches:
            raise MarkdownArchiveError(
                f"Chat Markdown contains no completed QA marker: {path}"
            )

        complete_end = matches[-1].end()
        separator = "\n\n---\n"
        if contents[complete_end:].startswith(separator):
            complete_end += len(separator)
        tail = contents[complete_end:]
        if tail:
            raise MarkdownArchiveError(
                f"Legacy Markdown has an unmarked tail; existing bytes were preserved: {path}"
            )
        return len(markers)

    def completed_qa_count(self, chat_id: str, *, minimum_expected: int = 0) -> int:
        return self.recover_completed_qa_count(
            chat_id, minimum_expected=minimum_expected
        )

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
