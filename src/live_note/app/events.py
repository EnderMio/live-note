from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    stage: str
    message: str
    session_id: str | None = None
    current: int | None = None
    total: int | None = None
    error: str | None = None


ProgressCallback = Callable[[ProgressEvent], None]
