from __future__ import annotations

from pathlib import Path

from live_note.runtime.domain.session_state import SessionStatus
from live_note.runtime.domain.task_state import ACTIVE_TASK_STATUSES, TaskStatus
from live_note.runtime.ingest import audio_spool_path
from live_note.runtime.store import ControlDb
from live_note.utils import iso_now

from .session_supervisor import SessionSupervisor
from .task_supervisor import RecoveryReport, TaskSupervisor

_RECOVERABLE_LIVE_SESSION_STATUSES = {
    SessionStatus.STARTING.value,
    SessionStatus.INGESTING.value,
    SessionStatus.PAUSED.value,
    SessionStatus.STOP_REQUESTED.value,
}


class RecoverySupervisor:
    def __init__(
        self,
        db: ControlDb,
        *,
        now=None,
        session_supervisor: SessionSupervisor,
        task_supervisor: TaskSupervisor,
    ) -> None:
        self.db = db
        self._now = now or iso_now
        self._sessions = session_supervisor
        self._tasks = task_supervisor

    def recover_sessions(self) -> RecoveryReport:
        recovered: list[str] = []
        failed: list[str] = []
        tasks_by_session_id: dict[str, list] = {}
        for task in self._tasks.tasks.list_all():
            if task.session_id is None:
                continue
            tasks_by_session_id.setdefault(task.session_id, []).append(task)
        for session in self._sessions.sessions.list_all():
            if not self._should_recover_session(session):
                continue
            try:
                session_tasks = tasks_by_session_id.get(session.session_id, [])
                if self._recover_session(session, session_tasks=session_tasks):
                    recovered.append(session.session_id)
            except Exception:
                self._sessions.fail_session(
                    session.session_id,
                    reason="runtime recovery failed",
                )
                failed.append(session.session_id)
        return RecoveryReport(
            recovered_session_ids=tuple(recovered),
            failed_session_ids=tuple(failed),
        )

    def _should_recover_session(self, session) -> bool:
        if session.execution_target not in {"local", "remote"}:
            return False
        if session.input_mode == "live":
            return session.runtime_status in _RECOVERABLE_LIVE_SESSION_STATUSES
        return session.runtime_status == SessionStatus.STARTING.value

    def _recover_session(self, session, *, session_tasks: list) -> bool:
        if session.input_mode != "live":
            return self._recover_starting_file_session(session, session_tasks=session_tasks)
        return self._recover_live_session(session)

    def _recover_live_session(self, session) -> bool:
        spool_path = audio_spool_path(Path(session.session_dir))
        if not spool_path.exists() or spool_path.stat().st_size <= 0:
            raise RuntimeError(f"missing ingest spool for recovery: {spool_path}")
        changed_at = self._now()
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._sessions.sessions.get(session.session_id, connection=connection)
            if current is None:
                raise FileNotFoundError(f"session not found: {session.session_id}")
            status = current.runtime_status
            if status in {
                SessionStatus.STARTING.value,
                SessionStatus.INGESTING.value,
                SessionStatus.PAUSED.value,
            }:
                current = self._sessions.accept_stop(
                    current.session_id,
                    payload={"recovered": True, "status": "stop_requested"},
                    connection=connection,
                    changed_at=changed_at,
                )
            task = self._tasks.submit(
                action="postprocess",
                label="后台整理",
                payload={
                    "action": "postprocess",
                    "session_id": current.session_id,
                    "speaker_enabled": _speaker_enabled_for_session(current.speaker_status),
                    "recover_from_spool": True,
                },
                session_id=current.session_id,
                dedupe_key=f"postprocess:{current.session_id}",
                message="已提交后台整理任务。",
                created_at=changed_at,
                connection=connection,
            )
            self._sessions.commit_handoff(
                current.session_id,
                payload={
                    "task_id": task.task_id,
                    "spool_path": str(spool_path),
                    "recovered": True,
                },
                connection=connection,
                changed_at=changed_at,
            )
            connection.commit()
        return True

    def _recover_starting_file_session(
        self,
        session,
        *,
        session_tasks: list,
    ) -> bool:
        if any(task.status in ACTIVE_TASK_STATUSES for task in session_tasks):
            return False
        latest_task = _latest_task(session_tasks)
        if latest_task is None:
            self._sessions.fail_session(
                session.session_id,
                reason="orphaned starting session without task",
            )
            return True
        if latest_task.status == TaskStatus.CANCELLED.value:
            self._sessions.abandon_session(
                session.session_id,
                reason="starting session abandoned after task cancellation",
                payload={"task_id": latest_task.task_id},
            )
            return True
        self._sessions.fail_session(
            session.session_id,
            reason=(
                f"starting session left behind by task {latest_task.task_id} "
                f"({latest_task.status})"
            ),
            payload={"task_id": latest_task.task_id},
        )
        return True


def _speaker_enabled_for_session(speaker_status: str) -> bool:
    return str(speaker_status).strip() != "disabled"


def _latest_task(tasks: list) -> object | None:
    if not tasks:
        return None
    return max(
        tasks,
        key=lambda item: (
            item.updated_at,
            item.created_at,
            item.task_id,
        ),
    )
