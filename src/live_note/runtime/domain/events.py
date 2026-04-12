from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class EventRecord:
    event_id: str
    kind: str
    created_at: str
    session_id: str | None = None
    task_id: str | None = None
    payload: dict[str, object] = field(default_factory=dict)
    sequence: int | None = None
