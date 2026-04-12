from __future__ import annotations

from live_note.runtime.domain.task_state import TaskRecord
from live_note.runtime.store import ControlDb, TaskRepo


def get_task(db: ControlDb, task_id: str) -> TaskRecord | None:
    return TaskRepo(db).get(task_id)


def list_active_tasks(db: ControlDb) -> list[TaskRecord]:
    tasks = TaskRepo(db).list_by_status("queued", "running")
    return sorted(
        tasks,
        key=lambda item: (
            0 if item.status == "running" else 1,
            item.updated_at,
            item.task_id,
        ),
        reverse=False,
    )
