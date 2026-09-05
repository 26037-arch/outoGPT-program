"""Data models shared by the controller, registry, adapters, and CLI."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any


class OperationState(str, Enum):
    CREATED = "CREATED"
    BROWSER_STARTING = "BROWSER_STARTING"
    PROMPT_SENDING = "PROMPT_SENDING"
    WAITING_RESPONSE = "WAITING_RESPONSE"
    RESPONSE_COMPLETED = "RESPONSE_COMPLETED"
    ARCHIVING = "ARCHIVING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class OperationType(str, Enum):
    CREATE = "create"
    SEND = "send"


@dataclass(frozen=True)
class ArchiveResult:
    status: str
    detail: str


@dataclass(frozen=True)
class ChatRecord:
    chat_id: str
    project_url: str
    chat_url: str
    created_at: str
    updated_at: str
    last_operation_id: str


@dataclass(frozen=True)
class OperationRecord:
    operation_id: str
    chat_id: str | None
    type: str
    status: OperationState
    started_at: str
    finished_at: str | None
    error_code: str | None
    error_message: str | None


@dataclass(frozen=True)
class ControllerResult:
    ok: bool
    operation_id: str
    chat_id: str | None
    chat_url: str | None
    state: OperationState
    response_status: str | None = None
    archive_status: str | None = None
    archive_detail: str | None = None
    error_code: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        error = None
        if self.error_code is not None:
            error = {"code": self.error_code, "message": self.error_message or ""}
        return {
            "ok": self.ok,
            "operation_id": self.operation_id,
            "chat_id": self.chat_id,
            "chat_url": self.chat_url,
            "state": self.state.value,
            "response_status": self.response_status,
            "archive_status": self.archive_status,
            "archive_detail": self.archive_detail,
            "error": error,
        }


@dataclass(frozen=True)
class StatusResult:
    chat: ChatRecord
    operation: OperationRecord

    def to_dict(self) -> dict[str, Any]:
        chat = asdict(self.chat)
        operation = asdict(self.operation)
        operation["status"] = self.operation.status.value
        return {"ok": True, "chat": chat, "operation": operation, "error": None}
