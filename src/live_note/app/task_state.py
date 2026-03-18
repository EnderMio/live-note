from __future__ import annotations

from dataclasses import dataclass, field

from .events import ProgressEvent


@dataclass(slots=True)
class GuiTaskState:
    busy: bool = False
    current_task_id: str | None = None
    current_task_label: str | None = None
    current_task_session_id: str | None = None
    current_live_task_id: str | None = None
    queue_current_task_id: str | None = None
    queue_current_task_label: str | None = None
    background_tasks: dict[str, str] = field(default_factory=dict)
    background_task_sessions: dict[str, str | None] = field(default_factory=dict)

    def start_live(self, *, task_id: str, label: str) -> None:
        self.start_foreground(task_id=task_id, label=label, detachable_live=True)

    def start_foreground(self, *, task_id: str, label: str, detachable_live: bool = False) -> None:
        self.busy = True
        self.current_task_id = task_id
        self.current_task_label = label
        self.current_task_session_id = None
        self.current_live_task_id = task_id if detachable_live else None

    def finish_foreground(self) -> None:
        self.busy = False
        self.current_task_id = None
        self.current_task_label = None
        self.current_task_session_id = None
        self.current_live_task_id = None

    def detach_live(self, *, session_id: str | None) -> str | None:
        task_id = self.current_task_id
        if task_id is None:
            return None
        resolved_session_id = session_id or self.current_task_session_id
        self.background_tasks[task_id] = self.current_task_label or "实时录音"
        self.background_task_sessions[task_id] = resolved_session_id
        self.finish_foreground()
        return task_id

    def finish_background(self, task_id: str) -> None:
        self.background_tasks.pop(task_id, None)
        self.background_task_sessions.pop(task_id, None)

    def mark_queue_running(self, *, task_id: str, label: str) -> None:
        self.queue_current_task_id = task_id
        self.queue_current_task_label = label

    def finish_queue(self, task_id: str) -> None:
        if task_id != self.queue_current_task_id:
            return
        self.queue_current_task_id = None
        self.queue_current_task_label = None

    def find_background_task_by_session(self, session_id: str) -> str | None:
        for task_id, task_session_id in self.background_task_sessions.items():
            if task_session_id == session_id:
                return task_id
        for task_id, task_session_id in self.background_task_sessions.items():
            if task_session_id is None:
                return task_id
        return None

    def is_foreground_event(self, event: ProgressEvent) -> bool:
        if self.current_task_id is None:
            return False
        if event.session_id:
            if self.current_task_session_id is None:
                background_task_id = self.find_background_task_by_session(event.session_id)
                if background_task_id is None:
                    self.current_task_session_id = event.session_id
            if self.current_task_session_id == event.session_id:
                return True
            background_task_id = self.find_background_task_by_session(event.session_id)
            if (
                background_task_id is not None
                and self.background_task_sessions[background_task_id] is None
            ):
                self.background_task_sessions[background_task_id] = event.session_id
            return False
        return True

