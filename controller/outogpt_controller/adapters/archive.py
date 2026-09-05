"""Archive acknowledgement interface and passive same-browser implementation."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Protocol

from ..models import ArchiveResult


class ArchiveAdapter(Protocol):
    def wait_until_saved(
        self,
        chat_id: str,
        *,
        timeout: float | None = None,
    ) -> ArchiveResult: ...


class PassiveExtensionArchiveAdapter:
    """Allow the loaded extension time to save without claiming acknowledgement."""

    def __init__(
        self,
        grace_period: float = 2.0,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if grace_period < 0:
            raise ValueError("grace_period must not be negative")
        self.grace_period = grace_period
        self.sleep = sleep

    def wait_until_saved(
        self,
        chat_id: str,
        *,
        timeout: float | None = None,
    ) -> ArchiveResult:
        delay = self.grace_period
        if timeout is not None:
            delay = min(delay, max(timeout, 0.0))
        if delay:
            self.sleep(delay)
        return ArchiveResult(
            status="unconfirmed",
            detail=(
                "Extension runs passively; save completion is not directly observable."
            ),
        )
