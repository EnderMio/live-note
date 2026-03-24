from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from live_note.app.coordinator import FileImportCoordinator
from live_note.app.events import ProgressEvent
from live_note.app.task_errors import TaskCancelledError
from live_note.config import AppConfig
from live_note.utils import iso_now, slugify_filename


@dataclass(slots=True)
class RemoteImportTask:
    task_id: str
    filename: str
    title: str | None
    kind: str
    language: str | None
    status: str
    stage: str
    message: str
    created_at: str
    updated_at: str
    session_id: str | None = None
    current: int | None = None
    total: int | None = None
    error: str | None = None


class RemoteImportTaskManager:
    def __init__(self, config: AppConfig):
        self.config = config
        self._tasks: dict[str, RemoteImportTask] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def create_task(
        self,
        *,
        filename: str,
        title: str | None,
        kind: str,
        language: str | None,
        file_bytes: bytes,
    ) -> dict[str, Any]:
        normalized_name = slugify_filename(Path(filename).name.strip())
        if not normalized_name:
            raise ValueError("上传文件名不能为空。")
        if not file_bytes:
            raise ValueError("上传文件为空。")

        task_id = f"import-{uuid4().hex[:12]}"
        uploaded_path = self._uploads_dir() / task_id / normalized_name
        uploaded_path.parent.mkdir(parents=True, exist_ok=True)
        uploaded_path.write_bytes(file_bytes)

        now = iso_now()
        task = RemoteImportTask(
            task_id=task_id,
            filename=normalized_name,
            title=title,
            kind=kind,
            language=language,
            status="queued",
            stage="queued",
            message="已接收上传，等待远端导入。",
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._tasks[task_id] = task
            self._cancel_events[task_id] = threading.Event()

        thread = threading.Thread(
            target=self._run_import,
            args=(task_id, uploaded_path, title, kind, language),
            daemon=True,
            name=f"remote-import-{task_id}",
        )
        thread.start()
        return self.task_payload(task_id)

    def task_payload(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise FileNotFoundError(f"未找到远端导入任务: {task_id}")
            return asdict(task)

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            task = self._tasks.get(task_id)
            cancel_event = self._cancel_events.get(task_id)
            if task is None or cancel_event is None:
                raise FileNotFoundError(f"未找到远端导入任务: {task_id}")
            if task.status == "cancelled":
                return asdict(task)
            cancel_event.set()
            if task.status not in {"completed", "failed"}:
                self._tasks[task_id] = replace(
                    task,
                    status="cancelling",
                    stage="cancelling",
                    message="已请求取消远端导入。",
                    updated_at=iso_now(),
                )
            return asdict(self._tasks[task_id])

    def _run_import(
        self,
        task_id: str,
        uploaded_path: Path,
        title: str | None,
        kind: str,
        language: str | None,
    ) -> None:
        self._update_task(
            task_id,
            status="running",
            stage="starting",
            message="远端开始导入音频。",
        )
        try:
            runner = FileImportCoordinator(
                config=self.config,
                file_path=str(uploaded_path),
                title=title,
                kind=kind,
                language=language,
                on_progress=lambda event: self._record_progress(task_id, event),
                cancel_event=self._cancel_events[task_id],
            )
            exit_code = runner.run()
            if exit_code != 0:
                raise RuntimeError(f"远端导入返回非零退出码: {exit_code}")
            snapshot = self.task_payload(task_id)
            if snapshot["status"] != "completed":
                self._update_task(
                    task_id,
                    status="completed",
                    stage="done",
                    message="远端导入已完成。",
                )
        except TaskCancelledError:
            self._update_task(
                task_id,
                status="cancelled",
                stage="cancelled",
                message="远端导入已取消。",
                error=None,
            )
        except Exception as exc:
            self._update_task(
                task_id,
                status="failed",
                stage="error",
                message=f"远端导入失败：{exc}",
                error=str(exc),
            )

    def _record_progress(self, task_id: str, event: ProgressEvent) -> None:
        status = "running"
        if event.stage == "done":
            status = "completed"
        elif event.stage == "error":
            status = "failed"
        self._update_task(
            task_id,
            status=status,
            stage=event.stage,
            message=event.message,
            session_id=event.session_id,
            current=event.current,
            total=event.total,
            error=event.error,
        )

    def _update_task(self, task_id: str, **changes: Any) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            self._tasks[task_id] = replace(task, updated_at=iso_now(), **changes)

    def _uploads_dir(self) -> Path:
        return self.config.root_dir / ".live-note" / "remote-imports"
