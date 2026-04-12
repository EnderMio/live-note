from __future__ import annotations

from live_note.runtime.domain.task_state import TaskRecord
from live_note.runtime.live_control import LiveControlState, get_live_control_state
from live_note.runtime.store import ControlDb, LogRepo, TaskRepo


def get_live_task_control(db: ControlDb, task_id: str) -> LiveControlState:
    return get_live_control_state(LogRepo(db), task_id)


def get_active_live_task(db: ControlDb) -> TaskRecord | None:
    tasks = [
        item
        for item in TaskRepo(db).list_by_status("queued", "running")
        if item.action == "live"
    ]
    if not tasks:
        return None
    return min(
        tasks,
        key=lambda item: (
            0 if item.status == "running" else 1,
            item.created_at,
            item.task_id,
        ),
    )
