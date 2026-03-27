from __future__ import annotations

import queue
import threading
from collections.abc import Callable, Mapping

from .events import ProgressCallback
from .services import AppService
from .task_errors import TaskCancelledError
from .task_queue import QueuedTaskRecord
from .task_queue_runtime import TaskQueueRuntime


def next_queued_record_to_start(
    runtime: TaskQueueRuntime,
    *,
    queue_worker: threading.Thread | None,
    busy: bool,
    background_tasks: Mapping[str, str],
    config_exists: bool,
) -> QueuedTaskRecord | None:
    if queue_worker is not None or busy or background_tasks or not config_exists:
        return None
    return runtime.next_queued()


def run_queue_task_worker(
    service: AppService,
    record: QueuedTaskRecord,
    *,
    on_progress: ProgressCallback | None,
    cancel_event: threading.Event,
    remove_record: Callable[[str], None],
    event_queue: queue.Queue[object],
) -> None:
    try:
        result = service.run_queue_task(
            record,
            on_progress=on_progress,
            cancel_event=cancel_event,
        )
    except TaskCancelledError as exc:
        remove_record(record.task_id)
        event_queue.put(("task_cancelled", "queue", record.task_id, record.label, str(exc)))
    except Exception as exc:
        remove_record(record.task_id)
        event_queue.put(("task_error", "queue", record.task_id, record.label, str(exc)))
    else:
        remove_record(record.task_id)
        event_queue.put(("task_done", "queue", record.task_id, record.label, result))
