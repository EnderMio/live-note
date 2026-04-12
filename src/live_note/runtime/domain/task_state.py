from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


ACTIVE_TASK_STATUSES = frozenset(
    {
        TaskStatus.QUEUED.value,
        TaskStatus.RUNNING.value,
    }
)
TERMINAL_TASK_STATUSES = frozenset(
    {
        TaskStatus.SUCCEEDED.value,
        TaskStatus.FAILED.value,
        TaskStatus.CANCELLED.value,
        TaskStatus.INTERRUPTED.value,
    }
)


def normalize_resource_keys(resource_keys: tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
    if not resource_keys:
        return ()
    normalized = {item.strip() for item in resource_keys if item and item.strip()}
    return tuple(sorted(normalized))


@dataclass(frozen=True, slots=True)
class TaskRecord:
    task_id: str
    action: str
    label: str
    status: str
    stage: str
    created_at: str
    updated_at: str
    session_id: str | None = None
    request_id: str | None = None
    dedupe_key: str | None = None
    resource_keys: tuple[str, ...] = field(default_factory=tuple)
    payload: dict[str, object] = field(default_factory=dict)
    can_cancel: bool = False
    started_at: str | None = None
    completed_at: str | None = None
    attempt: int = 0
    error: str | None = None
    message: str = ""
    current: int | None = None
    total: int | None = None
    result_version: int = 0
    cancel_requested: bool = False
