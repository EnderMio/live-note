from __future__ import annotations

from dataclasses import dataclass, replace
from sqlite3 import Connection
from typing import Any
from uuid import uuid4

from live_note.runtime.domain.events import EventRecord
from live_note.runtime.domain.task_state import (
    TERMINAL_TASK_STATUSES,
    TaskRecord,
    TaskStatus,
    normalize_resource_keys,
)
from live_note.runtime.store import ControlDb, LogRepo, TaskRepo
from live_note.runtime.types import ProgressEvent
from live_note.utils import iso_now


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    requeued_task_ids: tuple[str, ...] = ()
    interrupted_task_ids: tuple[str, ...] = ()
    recovered_session_ids: tuple[str, ...] = ()
    failed_session_ids: tuple[str, ...] = ()


class TaskSupervisor:
    def __init__(
        self,
        db: ControlDb,
        *,
        now=None,
        cancelled_exceptions: tuple[type[BaseException], ...] = (),
        recoverable_actions: set[str] | None = None,
    ):
        self.db = db
        self.tasks = TaskRepo(db)
        self.logs = LogRepo(db)
        self._now = now or iso_now
        self._cancelled_exceptions = cancelled_exceptions
        self._recoverable_actions = set(recoverable_actions or ())

    def submit(
        self,
        *,
        action: str,
        label: str,
        payload: dict[str, object],
        session_id: str | None = None,
        request_id: str | None = None,
        dedupe_key: str | None = None,
        resource_keys: tuple[str, ...] | list[str] | None = None,
        can_cancel: bool = False,
        task_id: str | None = None,
        message: str | None = None,
        connection: Connection | None = None,
        created_at: str | None = None,
    ) -> TaskRecord:
        queued_at = created_at or self._now()
        resolved_task_id = task_id or f"task-{uuid4().hex[:12]}"
        resolved_session_id = session_id or _infer_session_id(action, payload)
        resolved_resource_keys = normalize_resource_keys(
            resource_keys or _infer_resource_keys(action, resolved_session_id, payload)
        )
        candidate = TaskRecord(
            task_id=resolved_task_id,
            session_id=resolved_session_id,
            action=action,
            label=label,
            status=TaskStatus.QUEUED.value,
            stage="queued",
            created_at=queued_at,
            updated_at=queued_at,
            request_id=request_id,
            dedupe_key=dedupe_key,
            resource_keys=resolved_resource_keys,
            payload=dict(payload),
            can_cancel=can_cancel,
            message=message or "已加入任务队列。",
        )
        if connection is not None:
            return self._submit_in_transaction(
                candidate,
                connection=connection,
                request_id=request_id,
                dedupe_key=dedupe_key,
            )
        with self.db.connect() as owned:
            owned.execute("BEGIN IMMEDIATE")
            persisted = self._submit_in_transaction(
                candidate,
                connection=owned,
                request_id=request_id,
                dedupe_key=dedupe_key,
            )
            owned.commit()
        return persisted

    def _submit_in_transaction(
        self,
        candidate: TaskRecord,
        *,
        connection: Connection,
        request_id: str | None,
        dedupe_key: str | None,
    ) -> TaskRecord:
        if request_id:
            existing = self.tasks.find_by_request_id(request_id, connection=connection)
            if existing is not None:
                return existing
        if dedupe_key:
            existing = self.tasks.find_active_by_dedupe_key(dedupe_key, connection=connection)
            if existing is not None:
                return existing
        persisted = self.tasks.upsert(candidate, connection=connection)
        self.logs.append_event(
            EventRecord(
                event_id=f"task_queued:{persisted.task_id}:{candidate.created_at}",
                kind="task_queued",
                session_id=persisted.session_id,
                task_id=persisted.task_id,
                created_at=candidate.created_at,
                payload={"action": persisted.action},
            ),
            connection=connection,
        )
        return persisted

    def claim_task(self, task_id: str, *, claimer: str | None = None) -> TaskRecord:
        del claimer
        record = self.tasks.get(task_id)
        if record is None:
            raise FileNotFoundError(f"task not found: {task_id}")
        return record

    def start_task(self, task_id: str) -> TaskRecord:
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self.tasks.get(task_id, connection=connection)
            if current is None:
                raise FileNotFoundError(f"task not found: {task_id}")
            if current.status == TaskStatus.RUNNING.value:
                connection.commit()
                return current
            transition_at = self._now()
            if current.status != TaskStatus.QUEUED.value:
                raise RuntimeError(f"task is not queued: {task_id} ({current.status})")
            conflict = self.tasks.find_running_resource_conflict(
                current.resource_keys,
                exclude_task_id=current.task_id,
                connection=connection,
            )
            if conflict is not None:
                raise RuntimeError(
                    f"task resource conflict: {current.task_id} vs {conflict.task_id}"
                )
            started_at = transition_at
            started = replace(
                current,
                status=TaskStatus.RUNNING.value,
                stage="running",
                updated_at=started_at,
                started_at=started_at,
                completed_at=None,
                attempt=current.attempt + 1,
                error=None,
                message="任务已开始运行。",
            )
            persisted = self.tasks.upsert(started, connection=connection)
            self.logs.append_event(
                EventRecord(
                    event_id=f"task_started:{persisted.task_id}:{started_at}",
                    kind="task_started",
                    session_id=persisted.session_id,
                    task_id=persisted.task_id,
                    created_at=started_at,
                    payload={"attempt": persisted.attempt},
                ),
                connection=connection,
            )
            connection.commit()
        return persisted

    def run_task(self, task_id: str, *, runner, **kwargs: Any) -> int:
        current = self.tasks.get(task_id)
        if current is None:
            raise FileNotFoundError(f"task not found: {task_id}")
        if current.status == TaskStatus.QUEUED.value:
            current = self.start_task(task_id)
        if current.status != TaskStatus.RUNNING.value:
            raise RuntimeError(f"task is not runnable: {task_id} ({current.status})")
        return self.run_started_task(task_id, runner=runner, **kwargs)

    def run_started_task(self, task_id: str, *, runner, **kwargs: Any) -> int:
        current = self.tasks.get(task_id)
        if current is None:
            raise FileNotFoundError(f"task not found: {task_id}")
        if current.status != TaskStatus.RUNNING.value:
            raise RuntimeError(f"task is not started: {task_id} ({current.status})")
        try:
            result = runner(current, **kwargs)
        except self._cancelled_exceptions as exc:
            self.finish_task(
                current.task_id,
                status=TaskStatus.CANCELLED.value,
                stage="cancelled",
                error=str(exc) or "cancelled",
                message=str(exc) or "任务已取消。",
                event_kind="task_cancelled",
            )
            raise
        except BaseException as exc:
            self.finish_task(
                current.task_id,
                status=TaskStatus.FAILED.value,
                stage="failed",
                error=str(exc),
                message=str(exc) or "任务失败。",
                event_kind="task_failed",
            )
            raise
        refreshed = self.tasks.get(task_id)
        if refreshed is None:
            raise FileNotFoundError(f"task not found after run: {task_id}")
        if refreshed.status in TERMINAL_TASK_STATUSES:
            return int(result)
        if refreshed.cancel_requested:
            self.finish_task(
                current.task_id,
                status=TaskStatus.CANCELLED.value,
                stage="cancelled",
                error="cancelled",
                message="任务已取消。",
                event_kind="task_cancelled",
            )
            return int(result)
        self.finish_task(
            current.task_id,
            status=TaskStatus.SUCCEEDED.value,
            stage="done",
            error=None,
            message="任务已完成。",
            event_kind="task_succeeded",
        )
        return int(result)

    def record_progress(
        self,
        task_id: str,
        event: ProgressEvent,
        *,
        result_changed: bool = False,
    ) -> TaskRecord:
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self.tasks.get(task_id, connection=connection)
            if current is None:
                raise FileNotFoundError(f"task not found: {task_id}")
            if current.status in TERMINAL_TASK_STATUSES:
                connection.commit()
                return current
            updated_at = self._now()
            updated_session_id = event.session_id or current.session_id
            updated_resource_keys = current.resource_keys
            if updated_session_id:
                updated_resource_keys = normalize_resource_keys(
                    (*current.resource_keys, f"session:{updated_session_id}")
                )
            updated = replace(
                current,
                stage=str(event.stage or current.stage),
                updated_at=updated_at,
                session_id=updated_session_id,
                resource_keys=updated_resource_keys,
                error=str(event.error) if event.error is not None else current.error,
                message=event.message or current.message,
                current=event.current if event.current is not None else current.current,
                total=event.total if event.total is not None else current.total,
                result_version=current.result_version + (1 if result_changed else 0),
            )
            persisted = self.tasks.upsert(updated, connection=connection)
            self.logs.append_event(
                EventRecord(
                    event_id=f"task_progressed:{persisted.task_id}:{updated_at}",
                    kind="task_progressed",
                    session_id=persisted.session_id,
                    task_id=persisted.task_id,
                    created_at=updated_at,
                    payload={
                        "stage": persisted.stage,
                        "message": persisted.message,
                        "current": persisted.current,
                        "total": persisted.total,
                        "result_version": persisted.result_version,
                        "error": persisted.error,
                    },
                ),
                connection=connection,
            )
            connection.commit()
        return persisted

    def bump_result_version(self, task_id: str) -> TaskRecord:
        current = self.tasks.get(task_id)
        if current is None:
            raise FileNotFoundError(f"task not found: {task_id}")
        return self._update_task(
            task_id,
            result_version=current.result_version + 1,
        )

    def request_cancel(self, task_id: str, *, message: str | None = None) -> TaskRecord | None:
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self.tasks.get(task_id, connection=connection)
            if current is None:
                return None
            if current.status in TERMINAL_TASK_STATUSES:
                connection.commit()
                return current
            if current.cancel_requested:
                connection.commit()
                return current
            updated_at = self._now()
            if current.status == TaskStatus.QUEUED.value:
                cancelled = replace(
                    current,
                    status=TaskStatus.CANCELLED.value,
                    stage="cancelled",
                    updated_at=updated_at,
                    completed_at=updated_at,
                    cancel_requested=True,
                    error="cancelled",
                    message=message or "任务已取消。",
                )
                persisted = self.tasks.upsert(cancelled, connection=connection)
                event_kind = "task_cancelled"
            else:
                persisted = self.tasks.upsert(
                    replace(
                        current,
                        updated_at=updated_at,
                        stage="cancel_requested",
                        cancel_requested=True,
                        message=message or "已请求取消任务。",
                    ),
                    connection=connection,
                )
                event_kind = "task_cancel_requested"
            self.logs.append_event(
                EventRecord(
                    event_id=f"{event_kind}:{persisted.task_id}:{updated_at}",
                    kind=event_kind,
                    session_id=persisted.session_id,
                    task_id=persisted.task_id,
                    created_at=updated_at,
                    payload={},
                ),
                connection=connection,
            )
            connection.commit()
        return persisted

    def cancel_task(self, task_id: str) -> TaskRecord | None:
        return self.request_cancel(task_id)

    def finish_task(
        self,
        task_id: str,
        *,
        status: str,
        stage: str,
        error: str | None,
        message: str,
        event_kind: str,
        result_changed: bool = False,
    ) -> TaskRecord:
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self.tasks.get(task_id, connection=connection)
            if current is None:
                raise FileNotFoundError(f"task not found: {task_id}")
            if current.status in TERMINAL_TASK_STATUSES:
                connection.commit()
                return current
            now = self._now()
            finished = replace(
                current,
                status=status,
                stage=stage,
                updated_at=now,
                completed_at=now,
                error=error,
                message=message,
                result_version=current.result_version + (1 if result_changed else 0),
            )
            persisted = self.tasks.upsert(finished, connection=connection)
            self.logs.append_event(
                EventRecord(
                    event_id=f"{event_kind}:{persisted.task_id}:{now}",
                    kind=event_kind,
                    session_id=persisted.session_id,
                    task_id=persisted.task_id,
                    created_at=now,
                    payload={
                        "stage": persisted.stage,
                        "message": persisted.message,
                        "error": persisted.error,
                        "result_version": persisted.result_version,
                    },
                ),
                connection=connection,
            )
            connection.commit()
        return persisted

    def recover_incomplete_tasks(self) -> RecoveryReport:
        requeued: list[str] = []
        interrupted: list[str] = []
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            running = self.tasks.list_by_status(
                TaskStatus.RUNNING.value,
                connection=connection,
            )
            for record in running:
                now = self._now()
                if record.action in self._recoverable_actions:
                    recovered = replace(
                        record,
                        status=TaskStatus.QUEUED.value,
                        stage="recovered",
                        updated_at=now,
                        started_at=None,
                        completed_at=None,
                        error=None,
                        message="runtime host 重启后已重新入队。",
                    )
                    self.tasks.upsert(recovered, connection=connection)
                    self.logs.append_event(
                        EventRecord(
                            event_id=f"task_requeued:{record.task_id}:{now}",
                            kind="task_requeued",
                            session_id=record.session_id,
                            task_id=record.task_id,
                            created_at=now,
                            payload={},
                        ),
                        connection=connection,
                    )
                    requeued.append(record.task_id)
                    continue
                interrupted_record = replace(
                    record,
                    status=TaskStatus.INTERRUPTED.value,
                    stage="interrupted",
                    updated_at=now,
                    completed_at=now,
                    error="runtime host restarted before task finished",
                    message="runtime host restarted before task finished",
                )
                self.tasks.upsert(interrupted_record, connection=connection)
                self.logs.append_event(
                    EventRecord(
                        event_id=f"task_interrupted:{record.task_id}:{now}",
                        kind="task_interrupted",
                        session_id=record.session_id,
                        task_id=record.task_id,
                        created_at=now,
                        payload={},
                    ),
                    connection=connection,
                )
                interrupted.append(record.task_id)
            connection.commit()
        return RecoveryReport(
            requeued_task_ids=tuple(requeued),
            interrupted_task_ids=tuple(interrupted),
        )

    def _update_task(self, task_id: str, **changes: Any) -> TaskRecord:
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self.tasks.get(task_id, connection=connection)
            if current is None:
                raise FileNotFoundError(f"task not found: {task_id}")
            updated = self.tasks.upsert(
                replace(
                    current,
                    updated_at=changes.pop("updated_at", self._now()),
                    **changes,
                ),
                connection=connection,
            )
            connection.commit()
        return updated

def _infer_session_id(action: str, payload: dict[str, object]) -> str | None:
    value = payload.get("session_id")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _infer_resource_keys(
    action: str,
    session_id: str | None,
    payload: dict[str, object],
) -> tuple[str, ...]:
    if action == "merge":
        raw_session_ids = payload.get("session_ids", [])
        if isinstance(raw_session_ids, list):
            return tuple(
                f"session:{session_id}"
                for session_id in sorted(
                    {
                        str(item).strip()
                        for item in raw_session_ids
                        if str(item).strip()
                    }
                )
            )
        return ()
    if session_id:
        return (f"session:{session_id}",)
    return ()
