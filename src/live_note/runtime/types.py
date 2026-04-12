from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    stage: str
    message: str
    session_id: str | None = None
    current: int | None = None
    total: int | None = None
    error: str | None = None
    source: str | None = None
    task_id: str | None = None


ProgressCallback = Callable[[ProgressEvent], None]


class LiveRunner(Protocol):
    @property
    def is_paused(self) -> bool: ...

    def request_stop(self) -> None: ...

    def request_pause(self) -> None: ...

    def request_resume(self) -> None: ...
