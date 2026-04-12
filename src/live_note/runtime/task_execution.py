from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from live_note.runtime.domain.task_state import TERMINAL_TASK_STATUSES, TaskRecord, TaskStatus
from live_note.runtime.supervisors.runtime_host import RuntimeHost

TaskDispatch = Callable[[TaskRecord, threading.Event | None], int]
TaskTerminalHook = Callable[[TaskRecord], None]

LOGGER = logging.getLogger(__name__)


class RuntimeQueueExecutor:
    def __init__(
        self,
        runtime: RuntimeHost,
        *,
        dispatch_task: TaskDispatch,
        poll_interval_seconds: float = 0.2,
        thread_name: str = "runtime-queue",
        on_task_terminal: TaskTerminalHook | None = None,
    ) -> None:
        self.runtime = runtime
        self._dispatch_task = dispatch_task
        self.poll_interval_seconds = max(0.05, float(poll_interval_seconds))
        self._thread_name = thread_name
        self._on_task_terminal = on_task_terminal
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel_events: dict[str, threading.Event] = {}

    def start_background(self) -> None:
        with self._lock:
            thread = self._thread
            if thread is not None and thread.is_alive():
                return
            self._stop_event.clear()
            thread = threading.Thread(
                target=self.run_forever,
                daemon=True,
                name=self._thread_name,
            )
            self._thread = thread
        thread.start()

    def shutdown(self) -> None:
        self._stop_event.set()
        with self._lock:
            thread = self._thread
            cancel_events = tuple(self._cancel_events.values())
        for event in cancel_events:
            event.set()
        if thread is not None:
            thread.join(timeout=1.0)
        with self._lock:
            if self._thread is thread:
                self._thread = None
            self._cancel_events.clear()

    def signal_cancel(self, task_id: str) -> bool:
        with self._lock:
            event = self._cancel_events.get(task_id)
        if event is None:
            return False
        event.set()
        return True

    def run_forever(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                LOGGER.exception("runtime queue executor loop failed")
            self._stop_event.wait(self.poll_interval_seconds)

    def run_once(self) -> bool:
        queued = [
            item
            for item in self.runtime.tasks.list_by_status(TaskStatus.QUEUED.value)
            if item.action != "live"
        ]
        for item in sorted(queued, key=lambda task: (task.created_at, task.task_id)):
            if self._stop_event.is_set():
                return False
            conflict = self.runtime.tasks.find_running_resource_conflict(item.resource_keys)
            if conflict is not None:
                continue
            self._run_task(item.task_id)
            return True
        return False

    def _run_task(self, task_id: str) -> None:
        try:
            started = self.runtime.task_supervisor.start_task(task_id)
        except Exception:
            LOGGER.exception("failed to start runtime queue task: %s", task_id)
            return
        cancel_event = threading.Event() if started.can_cancel else None
        if cancel_event is not None and started.cancel_requested:
            cancel_event.set()
        with self._lock:
            if cancel_event is not None:
                self._cancel_events[started.task_id] = cancel_event
        try:
            self.runtime.task_supervisor.run_started_task(
                started.task_id,
                runner=lambda current, **_kwargs: self._dispatch_task(current, cancel_event),
            )
        except Exception:
            final = self.runtime.tasks.get(started.task_id)
            if final is None or final.status != TaskStatus.CANCELLED.value:
                LOGGER.exception("runtime queue task failed: %s", started.task_id)
        finally:
            with self._lock:
                self._cancel_events.pop(started.task_id, None)
            final = self.runtime.tasks.get(started.task_id)
            if final is None or final.status not in TERMINAL_TASK_STATUSES:
                return
            if self._on_task_terminal is None:
                return
            try:
                self._on_task_terminal(final)
            except Exception:
                LOGGER.exception("runtime queue terminal hook failed: %s", final.task_id)
