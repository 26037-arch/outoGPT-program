"""SQLite persistence for chats and controller operations."""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .models import ChatRecord, OperationRecord, OperationState, OperationType
from .paths import DEFAULT_DATABASE_PATH


SCHEMA_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Registry:
    def __init__(self, database_path: Path = DEFAULT_DATABASE_PATH):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chats (
                    chat_id TEXT PRIMARY KEY,
                    project_url TEXT NOT NULL,
                    chat_url TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_operation_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY,
                    chat_id TEXT NULL,
                    type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NULL,
                    error_code TEXT NULL,
                    error_message TEXT NULL
                );
                CREATE INDEX IF NOT EXISTS operations_chat_started
                    ON operations(chat_id, started_at DESC);
                """
            )
            row = connection.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,)
                )
            elif row["version"] != SCHEMA_VERSION:
                raise RuntimeError(
                    f"Unsupported registry schema version: {row['version']}"
                )

    def create_operation(
        self,
        operation_type: OperationType,
        chat_id: str | None = None,
    ) -> OperationRecord:
        operation_id = f"op_{uuid.uuid4().hex}"
        started_at = _now()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO operations(
                    operation_id, chat_id, type, status, started_at
                ) VALUES (?, ?, ?, ?, ?)""",
                (
                    operation_id,
                    chat_id,
                    operation_type.value,
                    OperationState.CREATED.value,
                    started_at,
                ),
            )
            if chat_id is not None:
                connection.execute(
                    """UPDATE chats
                       SET last_operation_id = ?, updated_at = ?
                       WHERE chat_id = ?""",
                    (operation_id, started_at, chat_id),
                )
        return self.get_operation(operation_id)

    def transition(
        self,
        operation_id: str,
        state: OperationState,
        *,
        chat_id: str | None = None,
    ) -> OperationRecord:
        with self._connect() as connection:
            if chat_id is None:
                connection.execute(
                    "UPDATE operations SET status = ? WHERE operation_id = ?",
                    (state.value, operation_id),
                )
            else:
                connection.execute(
                    "UPDATE operations SET status = ?, chat_id = ? WHERE operation_id = ?",
                    (state.value, chat_id, operation_id),
                )
        return self.get_operation(operation_id)

    def attach_chat(self, operation_id: str, chat_id: str) -> OperationRecord:
        with self._connect() as connection:
            connection.execute(
                "UPDATE operations SET chat_id = ? WHERE operation_id = ?",
                (chat_id, operation_id),
            )
        return self.get_operation(operation_id)

    def complete_operation(self, operation_id: str) -> OperationRecord:
        with self._connect() as connection:
            connection.execute(
                """UPDATE operations
                   SET status = ?, finished_at = ?, error_code = NULL, error_message = NULL
                   WHERE operation_id = ?""",
                (OperationState.COMPLETED.value, _now(), operation_id),
            )
        return self.get_operation(operation_id)

    def fail_operation(
        self,
        operation_id: str,
        error_code: str,
        error_message: str,
        *,
        chat_id: str | None = None,
    ) -> OperationRecord:
        with self._connect() as connection:
            connection.execute(
                """UPDATE operations
                   SET status = ?, finished_at = ?, error_code = ?, error_message = ?,
                       chat_id = COALESCE(?, chat_id)
                   WHERE operation_id = ?""",
                (
                    OperationState.FAILED.value,
                    _now(),
                    error_code,
                    error_message,
                    chat_id,
                    operation_id,
                ),
            )
        return self.get_operation(operation_id)

    def save_chat(
        self,
        chat_id: str,
        project_url: str,
        chat_url: str,
        operation_id: str,
    ) -> ChatRecord:
        now = _now()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO chats(
                    chat_id, project_url, chat_url, created_at, updated_at, last_operation_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    project_url = excluded.project_url,
                    chat_url = excluded.chat_url,
                    updated_at = excluded.updated_at,
                    last_operation_id = excluded.last_operation_id""",
                (chat_id, project_url, chat_url, now, now, operation_id),
            )
        return self.get_chat(chat_id)

    def get_chat(self, chat_id: str) -> ChatRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM chats WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        return ChatRecord(**dict(row)) if row is not None else None

    def get_operation(self, operation_id: str) -> OperationRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
            ).fetchone()
        if row is None:
            raise KeyError(operation_id)
        values = dict(row)
        values["status"] = OperationState(values["status"])
        return OperationRecord(**values)

    def get_latest_operation(self, chat_id: str) -> OperationRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM operations
                   WHERE chat_id = ? ORDER BY started_at DESC, rowid DESC LIMIT 1""",
                (chat_id,),
            ).fetchone()
        if row is None:
            return None
        values = dict(row)
        values["status"] = OperationState(values["status"])
        return OperationRecord(**values)
