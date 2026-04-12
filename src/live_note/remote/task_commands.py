from __future__ import annotations

import threading
from pathlib import Path

from live_note.config import AppConfig
from live_note.runtime import REMOTE_RECOVERABLE_ACTIONS, RuntimeHost
from live_note.runtime.domain.task_state import TERMINAL_TASK_STATUSES, TaskRecord, TaskStatus
from live_note.runtime.server_identity import load_or_create_server_id
from live_note.runtime.task_execution import RuntimeQueueExecutor
from live_note.runtime.types import ProgressEvent
from live_note.task_errors import TaskCancelledError

from .import_uploads import RemoteImportUploads
from .task_builders import RemoteTaskRunnerFactory

class RemoteTaskCommands:
    def __init__(self, config: AppConfig):
        self.config = config
        self.uploads = RemoteImportUploads(config.root_dir)
        self.runtime = RuntimeHost.for_root(
            config.root_dir,
            cancelled_exceptions=(TaskCancelledError,),
            recoverable_actions=set(REMOTE_RECOVERABLE_ACTIONS),
        )
        self._server_id = load_or_create_server_id(self.runtime.db)
        self._task_runners = RemoteTaskRunnerFactory(
            config,
            record_progress=self._record_task_progress,
            mark_completed=self._safe_mark_completed,
        )
        self._executor = RuntimeQueueExecutor(
            self.runtime,
            dispatch_task=self._task_runners.run_task_record,
            thread_name="runtime-remote-queue",
            on_task_terminal=self._cleanup_task_artifacts,
        )
        self._lifecycle_lock = threading.Lock()
        self._started = False

    @property
    def server_id(self) -> str:
        return self._server_id

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._started:
                return
            self.runtime.start()
            self._executor.start_background()
            self._started = True

    def shutdown(self) -> None:
        with self._lifecycle_lock:
            if not self._started:
                return
            self._executor.shutdown()
            self.runtime.shutdown()
            self._started = False

    def request_refine(
        self,
        session_id: str,
        *,
        request_id: str | None = None,
    ) -> dict[str, object]:
        return self._submit_task(
            action="refine",
            label="离线精修并重写",
            payload={
                "action": "refine",
                "session_id": session_id,
            },
            session_id=session_id,
            request_id=request_id,
            dedupe_key=f"refine:{session_id}",
        )

    def request_republish(
        self,
        session_id: str,
        *,
        request_id: str | None = None,
    ) -> dict[str, object]:
        return self._submit_task(
            action="republish",
            label="重新生成整理",
            payload={
                "action": "republish",
                "session_id": session_id,
            },
            session_id=session_id,
            request_id=request_id,
            dedupe_key=f"republish:{session_id}",
        )

    def request_retranscribe(
        self,
        session_id: str,
        *,
        request_id: str | None = None,
    ) -> dict[str, object]:
        return self._submit_task(
            action="retranscribe",
            label="重转写并重写",
            payload={
                "action": "retranscribe",
                "session_id": session_id,
            },
            session_id=session_id,
            request_id=request_id,
            dedupe_key=f"retranscribe:{session_id}",
        )

    def request_finalize(
        self,
        session_id: str,
        *,
        request_id: str | None = None,
    ) -> dict[str, object]:
        return self._submit_task(
            action="finalize",
            label="补转写并重写",
            payload={
                "action": "finalize",
                "session_id": session_id,
            },
            session_id=session_id,
            request_id=request_id,
            dedupe_key=f"finalize:{session_id}",
        )

    def create_import_task(
        self,
        *,
        filename: str,
        title: str | None,
        kind: str,
        language: str | None,
        speaker_enabled: bool | None,
        request_id: str | None,
        file_bytes: bytes,
    ) -> dict[str, object]:
        existing = self._existing_task_for_request_id(request_id)
        if existing is not None:
            return existing
        uploaded_path = self.uploads.create_uploaded_file(
            filename=filename,
            request_id=request_id,
            file_bytes=file_bytes,
        )
        return self._submit_task(
            action="import",
            label="文件导入",
            payload={
                "action": "import",
                "uploaded_path": str(uploaded_path),
                "title": title,
                "kind": kind,
                "language": language,
                "speaker_enabled": speaker_enabled,
            },
            request_id=request_id,
            can_cancel=True,
        )

    def import_task_payload(self, task_id: str) -> dict[str, object]:
        return self.task_payload(task_id)

    def cancel_import_task(self, task_id: str) -> dict[str, object]:
        return self.cancel_task(task_id)

    def list_tasks_payload(self) -> dict[str, object]:
        records = self.runtime.tasks.list_all()
        active = [
            _task_to_payload(record, server_id=self.server_id)
            for record in sorted(
                (
                    item
                    for item in records
                    if item.status in {TaskStatus.QUEUED.value, TaskStatus.RUNNING.value}
                ),
                key=lambda item: (
                    0 if item.status == TaskStatus.RUNNING.value else 1,
                    item.updated_at,
                    item.task_id,
                ),
            )
        ]
        recent = [
            _task_to_payload(record, server_id=self.server_id)
            for record in sorted(
                (item for item in records if item.status in TERMINAL_TASK_STATUSES),
                key=lambda item: (item.updated_at, item.task_id),
                reverse=True,
            )[:50]
        ]
        return {
            "server_id": self.server_id,
            "active": active,
            "recent": recent,
        }

    def task_payload(self, task_id: str) -> dict[str, object]:
        record = self.runtime.tasks.get(task_id)
        if record is None:
            raise FileNotFoundError(f"未找到远端任务: {task_id}")
        return _task_to_payload(record, server_id=self.server_id)

    def cancel_task(self, task_id: str) -> dict[str, object]:
        current = self.runtime.tasks.get(task_id)
        if current is None:
            raise FileNotFoundError(f"未找到远端任务: {task_id}")
        if not current.can_cancel and current.status not in TERMINAL_TASK_STATUSES:
            return _task_to_payload(current, server_id=self.server_id)
        updated = self.runtime.task_supervisor.request_cancel(
            task_id,
            message=(
                "已请求取消远端任务。"
                if current.status != TaskStatus.QUEUED.value
                else "远端任务已取消。"
            ),
        )
        assert updated is not None
        if updated.status == TaskStatus.CANCELLED.value:
            self._cleanup_task_artifacts(updated)
        self._executor.signal_cancel(task_id)
        return _task_to_payload(updated, server_id=self.server_id)

    def commit_postprocess_handoff(
        self,
        session_id: str,
        *,
        speaker_enabled: bool | None = None,
        spool_path: str | None = None,
    ) -> dict[str, object]:
        handoff = self.runtime.commit_session_task_handoff(
            session_id=session_id,
            action="postprocess",
            label="后台整理",
            payload={
                "action": "postprocess",
                "session_id": session_id,
                "speaker_enabled": speaker_enabled,
            },
            dedupe_key=f"postprocess:{session_id}",
            message="已提交后台整理任务。",
            event_payload={"spool_path": spool_path} if spool_path else None,
        )
        return self.task_payload(handoff.task.task_id)

    def _record_task_progress(self, task_id: str, event: ProgressEvent) -> None:
        result_changed = event.stage in {
            "segment_transcribed",
            "publishing",
            "summarizing",
            "done",
        }
        try:
            self.runtime.task_supervisor.record_progress(
                task_id,
                event,
                result_changed=result_changed,
            )
        except FileNotFoundError:
            return

    def _safe_mark_completed(
        self,
        task_id: str,
        *,
        message: str,
        result_changed: bool = False,
    ) -> None:
        try:
            record = self.runtime.task_supervisor.finish_task(
                task_id,
                status=TaskStatus.SUCCEEDED.value,
                stage="done",
                error=None,
                message=message,
                event_kind="task_succeeded",
                result_changed=result_changed,
            )
            self._cleanup_task_artifacts(record)
        except FileNotFoundError:
            return

    def _submit_task(
        self,
        *,
        action: str,
        label: str,
        payload: dict[str, object],
        session_id: str | None = None,
        request_id: str | None = None,
        can_cancel: bool = False,
        dedupe_key: str | None = None,
        task_id: str | None = None,
    ) -> dict[str, object]:
        record = self.runtime.task_supervisor.submit(
            task_id=task_id,
            action=action,
            label=label,
            payload=payload,
            session_id=session_id,
            request_id=request_id,
            dedupe_key=dedupe_key,
            can_cancel=can_cancel,
            message="已加入远端队列。",
        )
        return _task_to_payload(record, server_id=self.server_id)

    def _existing_task_for_request_id(self, request_id: str | None) -> dict[str, object] | None:
        normalized = _optional_string(request_id)
        if normalized is None:
            return None
        record = self.runtime.tasks.find_by_request_id(normalized)
        if record is None:
            return None
        return _task_to_payload(record, server_id=self.server_id)

    def _cleanup_task_artifacts(self, task: TaskRecord) -> None:
        if task.action != "import":
            return
        uploaded_path = _optional_string(task.payload.get("uploaded_path"))
        if uploaded_path is None:
            return
        _cleanup_uploaded_file(
            Path(uploaded_path),
            uploads_root=self.config.root_dir / ".live-note" / "remote-imports",
        )


def _task_to_payload(record: TaskRecord, *, server_id: str) -> dict[str, object]:
    return {
        "task_id": record.task_id,
        "server_id": server_id,
        "action": record.action,
        "label": record.label,
        "status": record.status,
        "stage": record.stage,
        "message": record.message,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "session_id": record.session_id,
        "request_id": record.request_id,
        "current": record.current,
        "total": record.total,
        "result_version": record.result_version,
        "error": record.error,
        "can_cancel": record.can_cancel,
    }


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _cleanup_uploaded_file(uploaded_path: Path, *, uploads_root: Path) -> None:
    uploads_root = uploads_root.resolve(strict=False)
    try:
        candidate = uploaded_path.expanduser().resolve(strict=False)
    except OSError:
        return
    try:
        candidate.relative_to(uploads_root)
    except ValueError:
        return
    try:
        candidate.unlink(missing_ok=True)
    except OSError:
        return
    parent = candidate.parent
    while parent != uploads_root and parent.is_relative_to(uploads_root):
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent
