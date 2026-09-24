"""Append-only project-to-Markdown synchronization orchestration."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from cli_gpt.errors import (
    ConversationHistoryIncomplete,
    ConversationLoadingUnknown,
    ProjectDiscoveryIncomplete,
)

from .paths import DEFAULT_ARCHIVE_ROOT
from .project_archive import ChatState, ProjectArchive


PROJECT_ERROR_CODES = {
    "InvalidProjectUrl": "INVALID_PROJECT_URL",
    "InvalidArgumentError": "INVALID_ARGUMENT",
    "ProjectAccessFailed": "PROJECT_ACCESS_FAILED",
    "ProjectDiscoveryIncomplete": "PROJECT_DISCOVERY_INCOMPLETE",
    "ConversationLoadingUnknown": "CONVERSATION_LOADING_UNKNOWN",
    "ConversationHistoryIncomplete": "CONVERSATION_HISTORY_INCOMPLETE",
    "ConversationStructureError": "CONVERSATION_STRUCTURE_ERROR",
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
    discovery_complete: bool | None = None
    discovery_diagnostic: str | None = None
    saved_chats: int = 0
    pending_unknown_loading_chats: int = 0
    failed_chats: int = 0
    archive_root: str | None = None
    archive_root_source: str | None = None
    paused: bool = False
    pending_chat_id: str | None = None

    def add_error(
        self,
        error: Exception,
        *,
        chat_id: str | None = None,
        chat_url: str | None = None,
        stage: str | None = None,
    ) -> None:
        code = getattr(error, "code", None) or PROJECT_ERROR_CODES.get(
            type(error).__name__, "PROJECT_UPDATE_ERROR"
        )
        self.errors.append(
            {
                "chat_id": chat_id,
                "chat_url": chat_url,
                "stage": stage,
                "code": code,
                "message": str(error),
            }
        )
        self.ok = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "paused": self.paused,
            "pending_chat_id": self.pending_chat_id,
            "project_url": self.project_url,
            "project_name": self.project_name,
            "discovered_chats": self.discovered_chats,
            "discovery_complete": self.discovery_complete,
            "discovery_diagnostic": self.discovery_diagnostic,
            "saved_chats": self.saved_chats,
            "new_chats": self.new_chats,
            "updated_chats": self.updated_chats,
            "unchanged_chats": self.unchanged_chats,
            "skipped_generating_chats": self.skipped_generating_chats,
            "skipped_empty_chats": self.skipped_empty_chats,
            "pending_unknown_loading_chats": self.pending_unknown_loading_chats,
            "failed_chats": self.failed_chats,
            "qa_pairs_appended": self.qa_pairs_appended,
            "qa_pairs_persisted": self.qa_pairs_appended,
            "archive_directory": self.archive_directory,
            "archive_root": self.archive_root,
            "archive_root_source": self.archive_root_source,
            "errors": self.errors,
        }


class ProjectUpdater:
    def __init__(
        self,
        browser: Any,
        archive_root: Path = DEFAULT_ARCHIVE_ROOT,
        *,
        archive_factory: Callable[..., ProjectArchive] = ProjectArchive.open,
        archive_root_source: str | None = None,
        max_attempts: int = 3,
    ):
        self.max_attempts = max(1, max_attempts)
        self.browser = browser
        self.archive_root = Path(archive_root)
        self.archive_factory = archive_factory
        self.archive_root_source = archive_root_source

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
        result.archive_root = str(self.archive_root.expanduser())
        result.archive_root_source = self.archive_root_source
        archive = None
        progress = {}
        discovery = None
        discovery_error = None
        for attempt in range(self.max_attempts):
            try:
                discovery = self.browser.discover_project_chats(project_url)
                result.discovered_chats = len(discovery.chats)
                result.discovery_complete = bool(discovery.complete)
                result.discovery_diagnostic = discovery.diagnostic
                if not discovery.complete:
                    raise ProjectDiscoveryIncomplete(
                        discovery.diagnostic or "Project pagination incomplete."
                    )
                discovery_error = None
                break
            except (Exception, KeyboardInterrupt) as error:
                discovery_error = error
                if isinstance(error, KeyboardInterrupt):
                    break
        # Open even on partial discovery so its IDs and failed stage survive.
        if discovery is not None:
            result.project_url = discovery.project_url
            result.project_name = discovery.project_name
            try:
                archive = self.archive_factory(
                    self.archive_root,
                    discovery.project_id,
                    discovery.project_name,
                    discovery.project_url,
                )
                result.archive_directory = str(archive.directory)
                progress = archive.load_progress()
            except Exception as error:
                result.add_error(error, stage="archive_setup")
                result.paused = True
                return result
        if discovery_error is not None:
            result.add_error(discovery_error, stage="discovery")
            result.paused = True
            if archive is None:
                try:
                    from cli_gpt.config import validate_project_url
                    from cli_gpt.project import extract_project_id

                    validate_project_url(project_url)
                    project_id = extract_project_id(project_url)
                    archive = self.archive_factory(
                        self.archive_root, project_id, project_id, project_url
                    )
                    progress = archive.load_progress()
                    result.archive_directory = str(archive.directory)
                except Exception as error:
                    result.add_error(error, stage="checkpoint")
            if archive is not None:
                try:
                    if archive.is_new:
                        archive.save_state()
                    archive.save_progress(
                        {
                            **progress,
                            "status": "paused",
                            "stage": "discovery",
                            "discovered": [asdict(c) for c in discovery.chats]
                            if discovery
                            else progress.get("discovered", []),
                            "error": str(discovery_error),
                        }
                    )
                except Exception as error:
                    result.add_error(error, stage="checkpoint")
            return result

        # On resume, revisit the interrupted chat first. Completed files are
        # revalidated on every run, so stale checkpoint flags never skip reads.
        chats = list(discovery.chats)
        pending = (
            progress.get("pending_chat_id")
            if progress.get("status") != "complete"
            else None
        )
        if pending:
            found = next((chat for chat in chats if chat.chat_id == pending), None)
            if found is None:
                raw = next(
                    (
                        item
                        for item in progress.get("discovered", [])
                        if item["chat_id"] == pending
                    ),
                    None,
                )
                if raw is not None:
                    from cli_gpt.project import ProjectChat

                    found = ProjectChat(**raw)
            if found is not None:
                chats = [found] + [chat for chat in chats if chat.chat_id != pending]
            else:
                result.paused = True
                result.pending_chat_id = pending
                result.add_error(
                    ConversationHistoryIncomplete(
                        "Recovery journal's pending chat has no recoverable URL; refusing to skip it."
                    ),
                    chat_id=pending,
                    stage="checkpoint",
                )
                return result
        progress = {
            "status": "running",
            "stage": "extraction",
            "pending_chat_id": pending,
            "discovered": [asdict(c) for c in chats],
            "completed": [],
        }
        try:
            archive.save_state()
            archive.save_progress(progress)
            for chat in chats:
                result.pending_chat_id = chat.chat_id
                progress.update(pending_chat_id=chat.chat_id, stage="extraction")
                archive.save_progress(progress)
                snapshot = None
                error = None
                for attempt in range(self.max_attempts):
                    try:
                        snapshot = self.browser.read_project_chat(chat)
                        if snapshot.chat_id != chat.chat_id:
                            raise ConversationHistoryIncomplete(
                                "Extracted chat identity does not match the pending chat."
                            )
                        if snapshot.generating or snapshot.status != "complete":
                            raise ConversationLoadingUnknown(
                                snapshot.diagnostic
                                or "Conversation is still generating/loading."
                            )
                        error = None
                        break
                    except Exception as exc:
                        error = exc
                if error is not None:
                    raise error
                progress["stage"] = "persistence"
                archive.save_progress(progress)
                known = archive.state.chats.get(chat.chat_id)
                changed = False
                # Retain the verified snapshot when saving fails; do not navigate.
                for attempt in range(self.max_attempts):
                    try:
                        wrote, digest = archive.sync_snapshot(snapshot)
                        changed = changed or wrote
                        candidate = replace(
                            self._chat_state(snapshot, len(snapshot.qa_pairs)),
                            content_sha256=digest,
                        )
                        self._persist_chat(archive, chat.chat_id, candidate)
                        error = None
                        break
                    except Exception as exc:
                        error = exc
                if error is not None:
                    raise error
                if known is None:
                    result.new_chats += 1
                elif changed:
                    result.updated_chats += 1
                else:
                    result.unchanged_chats += 1
                if changed or known is None:
                    result.saved_chats += 1
                    if changed:
                        result.qa_pairs_appended += max(
                            0, len(snapshot.qa_pairs) - (known.qa_count if known else 0)
                        )
                progress["completed"].append(chat.chat_id)
                progress["pending_chat_id"] = None
                archive.save_progress(progress)
            progress.update(stage="index", pending_chat_id=None)
            archive.save_progress(progress)
            archive.write_index()
            progress.update(status="complete", stage="complete")
            archive.save_progress(progress)
            result.pending_chat_id = None
        except (Exception, KeyboardInterrupt) as error:
            result.paused = True
            result.pending_chat_id = progress.get("pending_chat_id")
            if isinstance(error, ConversationLoadingUnknown):
                result.pending_unknown_loading_chats += 1
            else:
                result.failed_chats += 1
            result.add_error(
                error,
                chat_id=progress.get("pending_chat_id"),
                stage=progress.get("stage"),
            )
            progress.update(status="paused", error=str(error))
            try:
                archive.save_progress(progress)
            except Exception as checkpoint_error:
                result.add_error(checkpoint_error, stage="checkpoint")
        return result
