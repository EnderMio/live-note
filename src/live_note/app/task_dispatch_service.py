from __future__ import annotations

import threading
from collections.abc import Callable

from .events import ProgressCallback
from .task_queue import QueuedTaskRecord


class TaskDispatchService:
    def __init__(
        self,
        *,
        create_import_coordinator: Callable[..., object],
        merge: Callable[..., int],
        retranscribe: Callable[..., int],
        refine: Callable[..., int],
        republish: Callable[..., int],
        resync_notes: Callable[..., int],
    ):
        self._create_import_coordinator = create_import_coordinator
        self._merge = merge
        self._retranscribe = retranscribe
        self._refine = refine
        self._republish = republish
        self._resync_notes = resync_notes

    def run_queue_task(
        self,
        record: QueuedTaskRecord,
        *,
        on_progress: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
    ) -> int:
        payload = record.payload
        if record.action == "import":
            language = payload.get("language")
            speaker_enabled = payload.get("speaker_enabled")
            kwargs = dict(
                file_path=str(payload["file_path"]),
                title=payload.get("title") or None,
                kind=str(payload.get("kind") or "generic"),
                language=language if isinstance(language, str) else None,
                on_progress=on_progress,
                speaker_enabled=(
                    bool(speaker_enabled) if isinstance(speaker_enabled, bool) else None
                ),
            )
            if cancel_event is not None:
                kwargs["cancel_event"] = cancel_event
            runner = self._create_import_coordinator(**kwargs)
            return runner.run()
        if record.action == "merge":
            return self._merge(
                [str(item) for item in payload.get("session_ids", [])],
                title=payload.get("title") if isinstance(payload.get("title"), str) else None,
                on_progress=on_progress,
            )
        if record.action == "session_action":
            operation = payload.get("action") or payload.get("operation")
            session_id = str(payload["session_id"])
            if operation == "retranscribe":
                return self._retranscribe(session_id, on_progress=on_progress)
            if operation == "refine":
                return self._refine(session_id, on_progress=on_progress)
            if operation == "republish":
                return self._republish(session_id, on_progress=on_progress)
            if operation == "resync":
                return self._resync_notes(session_id, on_progress=on_progress)
        raise RuntimeError(f"不支持的队列任务：{record.action}")
