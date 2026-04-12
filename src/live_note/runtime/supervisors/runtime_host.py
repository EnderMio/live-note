from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from live_note.runtime.domain.session_state import SessionRecord
from live_note.runtime.domain.task_state import TaskRecord
from live_note.runtime.store import ControlDb, SessionRepo, TaskRepo
from live_note.utils import iso_now

from .recovery_supervisor import RecoverySupervisor
from .session_supervisor import SessionSupervisor
from .task_supervisor import RecoveryReport, TaskSupervisor

if TYPE_CHECKING:
    pass


@dataclass(frozen=True, slots=True)
class SessionTaskHandoff:
    session: SessionRecord
    task: TaskRecord


class RuntimeHost:
    def __init__(
        self,
        db: ControlDb,
        *,
        now=None,
        recoverable_actions: set[str] | None = None,
        cancelled_exceptions: tuple[type[BaseException], ...] = (),
    ):
        self.db = db
        self._now = now or iso_now
        self.sessions = SessionRepo(db)
        self.tasks = TaskRepo(db)
        self.session_supervisor = SessionSupervisor(db, now=self._now)
        self.task_supervisor = TaskSupervisor(
            db,
            now=self._now,
            cancelled_exceptions=cancelled_exceptions,
            recoverable_actions=recoverable_actions,
        )

    @classmethod
    def for_root(
        cls,
        root_dir: Path,
        *,
        now=None,
        recoverable_actions: set[str] | None = None,
        cancelled_exceptions: tuple[type[BaseException], ...] = (),
    ) -> RuntimeHost:
        return cls(
            ControlDb.for_root(root_dir),
            now=now,
            recoverable_actions=recoverable_actions,
            cancelled_exceptions=cancelled_exceptions,
        )

    def start(self) -> RecoveryReport:
        task_report = self.task_supervisor.recover_incomplete_tasks()
        session_report = RecoverySupervisor(
            self.db,
            now=self._now,
            session_supervisor=self.session_supervisor,
            task_supervisor=self.task_supervisor,
        ).recover_sessions()
        return replace(
            task_report,
            recovered_session_ids=task_report.recovered_session_ids
            + session_report.recovered_session_ids,
            failed_session_ids=task_report.failed_session_ids + session_report.failed_session_ids,
        )

    def shutdown(self) -> None:
        return

    def commit_session_task_handoff(
        self,
        *,
        session_id: str,
        action: str,
        label: str,
        payload: dict[str, object],
        event_kind: str = "handoff_committed",
        event_payload: dict[str, object] | None = None,
        request_id: str | None = None,
        dedupe_key: str | None = None,
        resource_keys: tuple[str, ...] | list[str] | None = None,
        can_cancel: bool = False,
        task_id: str | None = None,
        message: str | None = None,
    ) -> SessionTaskHandoff:
        changed_at = self._now()
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = self.task_supervisor.submit(
                task_id=task_id,
                action=action,
                label=label,
                payload=payload,
                session_id=session_id,
                request_id=request_id,
                dedupe_key=dedupe_key,
                resource_keys=resource_keys,
                can_cancel=can_cancel,
                message=message,
                created_at=changed_at,
                connection=connection,
            )
            combined_event_payload = {
                "task_id": task.task_id,
            }
            if event_payload:
                combined_event_payload.update(event_payload)
            session = self.session_supervisor.commit_handoff(
                session_id,
                payload=combined_event_payload | {"event_kind": event_kind},
                changed_at=changed_at,
                connection=connection,
            )
            connection.commit()
        return SessionTaskHandoff(session=session, task=task)
