"""Append-only project-to-Markdown synchronization orchestration."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from .errors import MarkdownArchiveError
from .paths import DEFAULT_ARCHIVE_ROOT
from .project_archive import ChatState, ProjectArchive


PROJECT_ERROR_CODES = {
    "InvalidProjectUrl": "INVALID_PROJECT_URL",
    "InvalidArgumentError": "INVALID_ARGUMENT",
    "ProjectAccessFailed": "PROJECT_ACCESS_FAILED",
    "PageStructureChanged": "PAGE_STRUCTURE_CHANGED",
    "LoginRequired": "LOGIN_REQUIRED",
    "LoginNotReady": "LOGIN_REQUIRED",
    "ProjectStateError": "PROJECT_STATE_ERROR",
    "MarkdownArchiveError": "MARKDOWN_ARCHIVE_ERROR",
}


@dataclass
class ProjectUpdateResult:
    ok: bool
    project_url: str
    project_name: str = ""
    discovered_chats: int = 0
    updated_chats: int = 0
    new_chats: int = 0
    unchanged_chats: int = 0
    skipped_generating_chats: int = 0
    skipped_empty_chats: int = 0
    qa_pairs_appended: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)
    archive_directory: str | None = None

    def add_error(
        self,
        error: Exception,
        *,
        chat_id: str | None = None,
        chat_url: str | None = None,
    ) -> None:
        code = getattr(error, "code", None) or PROJECT_ERROR_CODES.get(
            type(error).__name__, "PROJECT_UPDATE_ERROR"
        )
        self.errors.append(
            {
                "chat_id": chat_id,
                "chat_url": chat_url,
                "code": code,
                "message": str(error),
            }
        )
        self.ok = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "project_url": self.project_url,
            "project_name": self.project_name,
            "discovered_chats": self.discovered_chats,
            "new_chats": self.new_chats,
            "updated_chats": self.updated_chats,
            "unchanged_chats": self.unchanged_chats,
            "skipped_generating_chats": self.skipped_generating_chats,
            "skipped_empty_chats": self.skipped_empty_chats,
            "qa_pairs_appended": self.qa_pairs_appended,
            "archive_directory": self.archive_directory,
            "errors": self.errors,
        }


class ProjectUpdater:
    def __init__(
        self,
        browser: Any,
        archive_root: Path = DEFAULT_ARCHIVE_ROOT,
        *,
        archive_factory: Callable[..., ProjectArchive] = ProjectArchive.open,
    ):
        self.browser = browser
        self.archive_root = Path(archive_root)
        self.archive_factory = archive_factory

    @staticmethod
    def _chat_state(snapshot: Any, qa_count: int) -> ChatState:
        return ChatState(
            chat_id=snapshot.chat_id,
            chat_url=snapshot.chat_url,
            title=snapshot.title,
            qa_count=qa_count,
            file=f"chats/{snapshot.chat_id}.md",
        )

    @staticmethod
    def _persist_chat(
        archive: ProjectArchive,
        chat_id: str,
        candidate: ChatState,
    ) -> None:
        previous = archive.state.chats.get(chat_id)
        archive.state.chats[chat_id] = candidate
        try:
            archive.save_state()
        except Exception:
            if previous is None:
                archive.state.chats.pop(chat_id, None)
            else:
                archive.state.chats[chat_id] = previous
            raise

    def update(self, project_url: str) -> ProjectUpdateResult:
        result = ProjectUpdateResult(True, project_url)
        try:
            discovery = self.browser.discover_project_chats(project_url)
        except Exception as error:
            result.add_error(error)
            return result

        result.project_url = discovery.project_url
        result.project_name = discovery.project_name
        result.discovered_chats = len(discovery.chats)
        try:
            archive = self.archive_factory(
                self.archive_root,
                discovery.project_id,
                discovery.project_name,
                discovery.project_url,
            )
            result.archive_directory = str(archive.directory)
        except Exception as error:
            result.add_error(error)
            return result

        for chat in discovery.chats:
            known = archive.state.chats.get(chat.chat_id)
            try:
                snapshot = self.browser.read_project_chat(chat)
                if snapshot.generating:
                    result.skipped_generating_chats += 1
                    continue

                pairs = list(snapshot.qa_pairs)
                if known is None:
                    if not pairs:
                        result.skipped_empty_chats += 1
                        continue
                    orphan_path = archive.chat_path(snapshot.chat_id)
                    if orphan_path.exists():
                        physical_count = archive.completed_qa_count(snapshot.chat_id)
                        self._persist_chat(
                            archive,
                            snapshot.chat_id,
                            self._chat_state(snapshot, physical_count),
                        )
                        known = archive.state.chats[snapshot.chat_id]
                    else:
                        archive.create_chat(
                            snapshot.chat_id,
                            snapshot.title,
                            snapshot.chat_url,
                            pairs,
                        )
                        self._persist_chat(
                            archive,
                            snapshot.chat_id,
                            self._chat_state(snapshot, len(pairs)),
                        )
                        result.new_chats += 1
                        result.qa_pairs_appended += len(pairs)
                        continue

                physical_count = archive.completed_qa_count(
                    chat.chat_id, minimum_expected=known.qa_count
                )
                if physical_count < known.qa_count:
                    raise MarkdownArchiveError(
                        "The chat Markdown contains fewer completed QA markers than project.json."
                    )
                saved_count = max(known.qa_count, physical_count)
                if physical_count > known.qa_count:
                    known = replace(known, qa_count=physical_count)
                    self._persist_chat(archive, chat.chat_id, known)

                current_count = len(pairs)
                if current_count > saved_count:
                    additions = pairs[saved_count:]
                    archive.append_pairs(chat.chat_id, additions, saved_count + 1)
                    self._persist_chat(
                        archive,
                        chat.chat_id,
                        self._chat_state(snapshot, current_count),
                    )
                    result.updated_chats += 1
                    result.qa_pairs_appended += len(additions)
                else:
                    # Pair-count regressions and historical edits are intentionally
                    # ignored. Metadata may still follow a renamed conversation.
                    if current_count == saved_count:
                        candidate = replace(
                            known,
                            chat_url=snapshot.chat_url,
                            title=snapshot.title,
                        )
                        if candidate != known:
                            self._persist_chat(archive, chat.chat_id, candidate)
                    result.unchanged_chats += 1
            except Exception as error:
                result.add_error(
                    error,
                    chat_id=chat.chat_id,
                    chat_url=chat.chat_url,
                )

        try:
            if archive.is_new or archive.metadata_changed:
                archive.save_state()
            archive.write_index()
        except Exception as error:
            result.add_error(error)
        return result
