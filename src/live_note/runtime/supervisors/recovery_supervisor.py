from __future__ import annotations

from pathlib import Path

from live_note.runtime.domain.session_state import SessionStatus
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
        for session in self._sessions.sessions.list_all():
            if not self._should_recover_session(session):
                continue
            try:
                if self._recover_session(session):
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
        if session.input_mode != "live":
            return False
        return session.runtime_status in _RECOVERABLE_LIVE_SESSION_STATUSES

    def _recover_session(self, session) -> bool:
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


def _speaker_enabled_for_session(speaker_status: str) -> bool:
    return str(speaker_status).strip() != "disabled"
