from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from live_note.domain import SessionMetadata


class SessionStatus(StrEnum):
    STARTING = "starting"
    INGESTING = "ingesting"
    PAUSED = "paused"
    STOP_REQUESTED = "stop_requested"
    HANDOFF_COMMITTED = "handoff_committed"
    COMPLETED = "completed"
    FAILED = "failed"
    ABANDONED = "abandoned"


class SessionCommandKind(StrEnum):
    BEGIN_INGEST = "session_begin_ingest"
    PAUSE_INGEST = "session_pause_ingest"
    RESUME_INGEST = "session_resume_ingest"
    ACCEPT_STOP = "session_accept_stop"
    COMMIT_HANDOFF = "session_commit_handoff"
    COMPLETE = "session_complete"
    FAIL = "session_fail"
    ABANDON = "session_abandon"


_RUNTIME_SESSION_STATUSES = {status.value for status in SessionStatus}
_COMPLETED_DISPLAY_STATUSES = {
    "finalized",
    "merged",
    "structured_failed",
    "transcript_only",
}

_SESSION_EVENT_KINDS = {
    SessionCommandKind.BEGIN_INGEST: "ingest_started",
    SessionCommandKind.PAUSE_INGEST: "ingest_paused",
    SessionCommandKind.RESUME_INGEST: "ingest_resumed",
    SessionCommandKind.ACCEPT_STOP: "stop_accepted",
    SessionCommandKind.COMMIT_HANDOFF: "handoff_committed",
    SessionCommandKind.COMPLETE: "session_completed",
    SessionCommandKind.FAIL: "session_failed",
    SessionCommandKind.ABANDON: "session_abandoned",
}

_SESSION_ALLOWED_STATUSES = {
    SessionCommandKind.BEGIN_INGEST: {
        SessionStatus.STARTING,
        SessionStatus.INGESTING,
    },
    SessionCommandKind.PAUSE_INGEST: {
        SessionStatus.INGESTING,
        SessionStatus.PAUSED,
    },
    SessionCommandKind.RESUME_INGEST: {
        SessionStatus.PAUSED,
        SessionStatus.INGESTING,
    },
    SessionCommandKind.ACCEPT_STOP: {
        SessionStatus.STARTING,
        SessionStatus.INGESTING,
        SessionStatus.PAUSED,
        SessionStatus.STOP_REQUESTED,
    },
    SessionCommandKind.COMMIT_HANDOFF: {
        SessionStatus.STOP_REQUESTED,
        SessionStatus.HANDOFF_COMMITTED,
    },
    SessionCommandKind.COMPLETE: {
        SessionStatus.COMPLETED,
    },
    SessionCommandKind.FAIL: {
        SessionStatus.FAILED,
    },
    SessionCommandKind.ABANDON: {
        SessionStatus.ABANDONED,
    },
}

_SESSION_TARGET_STATUSES = {
    SessionCommandKind.BEGIN_INGEST: SessionStatus.INGESTING,
    SessionCommandKind.PAUSE_INGEST: SessionStatus.PAUSED,
    SessionCommandKind.RESUME_INGEST: SessionStatus.INGESTING,
    SessionCommandKind.ACCEPT_STOP: SessionStatus.STOP_REQUESTED,
    SessionCommandKind.COMMIT_HANDOFF: SessionStatus.HANDOFF_COMMITTED,
    SessionCommandKind.COMPLETE: SessionStatus.COMPLETED,
    SessionCommandKind.FAIL: SessionStatus.FAILED,
    SessionCommandKind.ABANDON: SessionStatus.ABANDONED,
}


@dataclass(frozen=True, slots=True)
class SessionTransition:
    record: SessionRecord
    event_kind: str
    event_payload: dict[str, object]


def is_runtime_session_status(value: str) -> bool:
    return value.strip() in _RUNTIME_SESSION_STATUSES


def is_session_command_kind(value: str) -> bool:
    try:
        SessionCommandKind(value.strip())
    except ValueError:
        return False
    return True


def infer_runtime_session_status(value: str, *, input_mode: str) -> SessionStatus:
    normalized = value.strip()
    if normalized in _RUNTIME_SESSION_STATUSES:
        return SessionStatus(normalized)
    if normalized in _COMPLETED_DISPLAY_STATUSES:
        return SessionStatus.COMPLETED
    raise ValueError(
        f"cannot infer runtime session status from display status: {value!r} "
        f"(input_mode={input_mode!r})"
    )


@dataclass(frozen=True, slots=True)
class SessionRecord:
    session_id: str
    title: str
    kind: str
    input_mode: str
    source_label: str
    source_ref: str
    language: str
    started_at: str
    transcript_note_path: str
    structured_note_path: str
    session_dir: str
    display_status: str
    runtime_status: str
    transcript_source: str = "live"
    refine_status: str = "disabled"
    execution_target: str = "local"
    remote_session_id: str | None = None
    speaker_status: str = "disabled"
    updated_at: str | None = None

    @property
    def status(self) -> str:
        return self.runtime_status

    @classmethod
    def from_metadata(
        cls,
        metadata: SessionMetadata,
        *,
        updated_at: str | None = None,
    ) -> SessionRecord:
        resolved_runtime_status = infer_runtime_session_status(
            metadata.status,
            input_mode=metadata.input_mode,
        ).value
        return cls(
            session_id=metadata.session_id,
            title=metadata.title,
            kind=metadata.kind,
            input_mode=metadata.input_mode,
            source_label=metadata.source_label,
            source_ref=metadata.source_ref,
            language=metadata.language,
            started_at=metadata.started_at,
            transcript_note_path=metadata.transcript_note_path,
            structured_note_path=metadata.structured_note_path,
            session_dir=metadata.session_dir,
            display_status=metadata.status,
            runtime_status=resolved_runtime_status,
            transcript_source=metadata.transcript_source,
            refine_status=metadata.refine_status,
            execution_target=metadata.execution_target,
            remote_session_id=metadata.remote_session_id,
            speaker_status=metadata.speaker_status,
            updated_at=updated_at or metadata.started_at,
        )

    def to_metadata(self) -> SessionMetadata:
        return SessionMetadata(
            session_id=self.session_id,
            title=self.title,
            kind=self.kind,
            input_mode=self.input_mode,
            source_label=self.source_label,
            source_ref=self.source_ref,
            language=self.language,
            started_at=self.started_at,
            transcript_note_path=self.transcript_note_path,
            structured_note_path=self.structured_note_path,
            session_dir=self.session_dir,
            status=self.display_status,
            transcript_source=self.transcript_source,
            refine_status=self.refine_status,
            execution_target=self.execution_target,
            remote_session_id=self.remote_session_id,
            speaker_status=self.speaker_status,
        )


def reduce_session_command(
    record: SessionRecord,
    command_kind: str,
    *,
    payload: dict[str, object] | None = None,
    now: str,
) -> SessionTransition:
    command = SessionCommandKind(command_kind.strip())
    current_status = SessionStatus(record.runtime_status)
    normalized_payload = dict(payload or {})
    allowed = _allowed_session_statuses(
        record,
        command,
        payload=normalized_payload,
    )
    if current_status not in allowed:
        raise RuntimeError(
            f"invalid session transition: {record.session_id} "
            f"{current_status.value} -> {command.value}"
        )
    target_status = _SESSION_TARGET_STATUSES[command]
    display_status = _resolved_display_status(
        record,
        command,
        payload=normalized_payload,
    )
    updated = SessionRecord(
        session_id=record.session_id,
        title=record.title,
        kind=record.kind,
        input_mode=record.input_mode,
        source_label=record.source_label,
        source_ref=record.source_ref,
        language=record.language,
        started_at=record.started_at,
        transcript_note_path=record.transcript_note_path,
        structured_note_path=record.structured_note_path,
        session_dir=record.session_dir,
        display_status=display_status,
        runtime_status=target_status.value,
        transcript_source=record.transcript_source,
        refine_status=record.refine_status,
        execution_target=record.execution_target,
        remote_session_id=record.remote_session_id,
        speaker_status=record.speaker_status,
        updated_at=now,
    )
    event_payload = dict(normalized_payload)
    event_payload.update(
        {
            "previous_runtime_status": record.runtime_status,
            "runtime_status": updated.runtime_status,
            "display_status": updated.display_status,
        }
    )
    return SessionTransition(
        record=updated,
        event_kind=_SESSION_EVENT_KINDS[command],
        event_payload=event_payload,
    )


def _allowed_session_statuses(
    record: SessionRecord,
    command: SessionCommandKind,
    *,
    payload: dict[str, object],
) -> set[SessionStatus]:
    del payload
    allowed = set(_SESSION_ALLOWED_STATUSES[command])
    if command is SessionCommandKind.COMPLETE:
        if record.input_mode == "live":
            allowed.add(SessionStatus.HANDOFF_COMMITTED)
        else:
            allowed.add(SessionStatus.STARTING)
        return allowed
    if command is SessionCommandKind.FAIL:
        allowed.update(
            {
                SessionStatus.STARTING,
                SessionStatus.INGESTING,
                SessionStatus.PAUSED,
                SessionStatus.STOP_REQUESTED,
                SessionStatus.HANDOFF_COMMITTED,
            }
        )
        return allowed
    if command is SessionCommandKind.ABANDON:
        allowed.update(
            {
                SessionStatus.STARTING,
                SessionStatus.INGESTING,
                SessionStatus.PAUSED,
                SessionStatus.STOP_REQUESTED,
            }
        )
        return allowed
    return allowed


def _resolved_display_status(
    record: SessionRecord,
    command: SessionCommandKind,
    *,
    payload: dict[str, object],
) -> str:
    raw_display = payload.get("display_status")
    if isinstance(raw_display, str) and raw_display.strip():
        return raw_display.strip()
    if command is SessionCommandKind.COMPLETE:
        if record.display_status in _COMPLETED_DISPLAY_STATUSES:
            return record.display_status
        return "finalized"
    if command is SessionCommandKind.COMMIT_HANDOFF:
        return SessionStatus.HANDOFF_COMMITTED.value
    if command is SessionCommandKind.FAIL:
        return SessionStatus.FAILED.value
    if command is SessionCommandKind.ABANDON:
        return SessionStatus.ABANDONED.value
    return _SESSION_TARGET_STATUSES[command].value
