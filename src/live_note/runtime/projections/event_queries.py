from __future__ import annotations

from live_note.runtime.domain.events import EventRecord
from live_note.runtime.store import ControlDb, LogRepo


def list_events_after(
    db: ControlDb,
    *,
    after_sequence: int = 0,
    session_id: str | None = None,
    task_id: str | None = None,
) -> list[EventRecord]:
    items = LogRepo(db).list_events(session_id=session_id, task_id=task_id)
    return [item for item in items if int(item.sequence or 0) > after_sequence]
