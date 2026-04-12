from __future__ import annotations

from dataclasses import replace
from sqlite3 import Connection
from uuid import uuid4

from live_note.domain import SessionMetadata
from live_note.runtime.domain.commands import CommandRecord
from live_note.runtime.domain.events import EventRecord
from live_note.runtime.domain.session_state import (
    SessionCommandKind,
    SessionRecord,
    SessionStatus,
    reduce_session_command,
)
from live_note.runtime.store import ControlDb, LogRepo, SessionRepo
from live_note.utils import iso_now


class SessionSupervisor:
    def __init__(self, db: ControlDb, *, now=None):
        self.db = db
        self.sessions = SessionRepo(db)
        self.logs = LogRepo(db)
        self._now = now or iso_now

    def create_or_load(
        self,
        metadata: SessionMetadata,
    ) -> SessionRecord:
        existing = self.sessions.get(metadata.session_id)
        if existing is not None:
            return existing
        created_at = metadata.started_at
        record = SessionRecord.from_metadata(
            metadata,
            updated_at=created_at,
        )
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            persisted = self.sessions.upsert(record, connection=connection)
            self.logs.append_event(
                EventRecord(
                    event_id=f"session_created:{persisted.session_id}:{created_at}",
                    kind="session_created",
                    session_id=persisted.session_id,
                    created_at=created_at,
                    payload={
                        "status": persisted.status,
                        "runtime_status": persisted.runtime_status,
                    },
                ),
                connection=connection,
            )
            connection.commit()
        return persisted

    def apply_metadata_changes(
        self,
        session_id: str,
        changes: dict[str, object],
        *,
        event_kind: str,
        event_payload: dict[str, object] | None = None,
        changed_at: str | None = None,
        connection: Connection | None = None,
    ) -> SessionRecord:
        normalized_changes = _normalize_metadata_changes(changes)
        return self._apply_changes(
            session_id,
            normalized_changes,
            event_kind=event_kind,
            event_payload=event_payload,
            changed_at=changed_at,
            connection=connection,
        )

    def apply_command(
        self,
        session_id: str,
        command_kind: str,
        *,
        payload: dict[str, object] | None = None,
        changed_at: str | None = None,
        connection: Connection | None = None,
    ) -> SessionRecord:
        applied_at = changed_at or self._now()
        if connection is not None:
            return self._apply_command_in_transaction(
                session_id,
                command_kind,
                payload=dict(payload or {}),
                changed_at=applied_at,
                connection=connection,
            )
        with self.db.connect() as owned:
            owned.execute("BEGIN IMMEDIATE")
            persisted = self._apply_command_in_transaction(
                session_id,
                command_kind,
                payload=dict(payload or {}),
                changed_at=applied_at,
                connection=owned,
            )
            owned.commit()
        return persisted

    def begin_ingest(
        self,
        session_id: str,
        *,
        connection: Connection | None = None,
        changed_at: str | None = None,
    ) -> SessionRecord:
        return self.apply_command(
            session_id,
            SessionCommandKind.BEGIN_INGEST.value,
            connection=connection,
            changed_at=changed_at,
        )

    def pause_ingest(
        self,
        session_id: str,
        *,
        connection: Connection | None = None,
        changed_at: str | None = None,
    ) -> SessionRecord:
        return self.apply_command(
            session_id,
            SessionCommandKind.PAUSE_INGEST.value,
            connection=connection,
            changed_at=changed_at,
        )

    def resume_ingest(
        self,
        session_id: str,
        *,
        connection: Connection | None = None,
        changed_at: str | None = None,
    ) -> SessionRecord:
        return self.apply_command(
            session_id,
            SessionCommandKind.RESUME_INGEST.value,
            connection=connection,
            changed_at=changed_at,
        )

    def accept_stop(
        self,
        session_id: str,
        *,
        payload: dict[str, object] | None = None,
        connection: Connection | None = None,
        changed_at: str | None = None,
    ) -> SessionRecord:
        return self.apply_command(
            session_id,
            SessionCommandKind.ACCEPT_STOP.value,
            payload=payload,
            connection=connection,
            changed_at=changed_at,
        )

    def commit_handoff(
        self,
        session_id: str,
        *,
        payload: dict[str, object] | None = None,
        connection: Connection | None = None,
        changed_at: str | None = None,
    ) -> SessionRecord:
        return self.apply_command(
            session_id,
            SessionCommandKind.COMMIT_HANDOFF.value,
            payload=payload,
            connection=connection,
            changed_at=changed_at,
        )

    def complete_session(
        self,
        session_id: str,
        *,
        display_status: str,
        payload: dict[str, object] | None = None,
        connection: Connection | None = None,
        changed_at: str | None = None,
    ) -> SessionRecord:
        resolved_payload = dict(payload or {})
        resolved_payload["display_status"] = display_status
        return self.apply_command(
            session_id,
            SessionCommandKind.COMPLETE.value,
            payload=resolved_payload,
            connection=connection,
            changed_at=changed_at,
        )

    def fail_session(
        self,
        session_id: str,
        *,
        reason: str | None = None,
        payload: dict[str, object] | None = None,
        connection: Connection | None = None,
        changed_at: str | None = None,
    ) -> SessionRecord:
        resolved_payload = dict(payload or {})
        if reason:
            resolved_payload["reason"] = reason
        return self.apply_command(
            session_id,
            SessionCommandKind.FAIL.value,
            payload=resolved_payload,
            connection=connection,
            changed_at=changed_at,
        )

    def abandon_session(
        self,
        session_id: str,
        *,
        reason: str | None = None,
        payload: dict[str, object] | None = None,
        connection: Connection | None = None,
        changed_at: str | None = None,
    ) -> SessionRecord:
        resolved_payload = dict(payload or {})
        if reason:
            resolved_payload["reason"] = reason
        return self.apply_command(
            session_id,
            SessionCommandKind.ABANDON.value,
            payload=resolved_payload,
            connection=connection,
            changed_at=changed_at,
        )

    def _apply_changes(
        self,
        session_id: str,
        changes: dict[str, object],
        *,
        event_kind: str,
        event_payload: dict[str, object] | None = None,
        changed_at: str | None = None,
        connection: Connection | None = None,
    ) -> SessionRecord:
        applied_at = changed_at or self._now()
        if connection is not None:
            return self._apply_changes_in_transaction(
                session_id,
                changes,
                event_kind=event_kind,
                event_payload=event_payload,
                changed_at=applied_at,
                connection=connection,
            )
        with self.db.connect() as owned:
            owned.execute("BEGIN IMMEDIATE")
            persisted = self._apply_changes_in_transaction(
                session_id,
                changes,
                event_kind=event_kind,
                event_payload=event_payload,
                changed_at=applied_at,
                connection=owned,
            )
            owned.commit()
        return persisted

    def _apply_changes_in_transaction(
        self,
        session_id: str,
        changes: dict[str, object],
        *,
        event_kind: str,
        event_payload: dict[str, object] | None,
        changed_at: str,
        connection: Connection,
    ) -> SessionRecord:
        current = self.sessions.get(session_id, connection=connection)
        if current is None:
            raise FileNotFoundError(f"session not found: {session_id}")
        updated = _apply_changes(current, changes, now=changed_at)
        persisted = self.sessions.upsert(updated, connection=connection)
        payload = {
            "changes": {
                str(key): value
                for key, value in changes.items()
            }
        }
        if event_payload:
            payload.update(event_payload)
        self.logs.append_event(
            EventRecord(
                event_id=f"{event_kind}:{persisted.session_id}:{persisted.updated_at}",
                kind=event_kind,
                session_id=persisted.session_id,
                created_at=persisted.updated_at or changed_at,
                payload=payload,
            ),
            connection=connection,
        )
        return persisted

    def _apply_command_in_transaction(
        self,
        session_id: str,
        command_kind: str,
        *,
        payload: dict[str, object],
        changed_at: str,
        connection: Connection,
    ) -> SessionRecord:
        current = self.sessions.get(session_id, connection=connection)
        if current is None:
            raise FileNotFoundError(f"session not found: {session_id}")
        command = self.logs.append_command(
            CommandRecord(
                command_id=_session_command_id(command_kind, session_id),
                kind=command_kind,
                session_id=session_id,
                created_at=changed_at,
                payload=payload,
            ),
            connection=connection,
        )
        transition = reduce_session_command(
            current,
            command_kind,
            payload=payload,
            now=changed_at,
        )
        persisted = self.sessions.upsert(transition.record, connection=connection)
        event_payload = dict(transition.event_payload)
        event_payload.update(
            {
                "command_id": command.command_id,
                "command_sequence": command.sequence,
            }
        )
        self.logs.append_event(
            EventRecord(
                event_id=f"{transition.event_kind}:{persisted.session_id}:{command.command_id}",
                kind=transition.event_kind,
                session_id=persisted.session_id,
                created_at=changed_at,
                payload=event_payload,
            ),
            connection=connection,
        )
        return persisted


def _apply_changes(record: SessionRecord, changes: dict[str, object], *, now: str) -> SessionRecord:
    illegal = {"status", "display_status", "runtime_status"} & set(changes)
    if illegal:
        joined = ", ".join(sorted(illegal))
        raise ValueError(
            f"session lifecycle status must be changed via session commands, not metadata changes: {joined}"
        )
    updated = replace(record, updated_at=now)
    if "title" in changes:
        updated = replace(updated, title=str(changes["title"]))
    if "kind" in changes:
        updated = replace(updated, kind=str(changes["kind"]))
    if "input_mode" in changes:
        updated = replace(updated, input_mode=str(changes["input_mode"]))
    if "source_label" in changes:
        updated = replace(updated, source_label=str(changes["source_label"]))
    if "source_ref" in changes:
        updated = replace(updated, source_ref=str(changes["source_ref"]))
    if "language" in changes:
        updated = replace(updated, language=str(changes["language"]))
    if "started_at" in changes:
        updated = replace(updated, started_at=str(changes["started_at"]))
    if "transcript_note_path" in changes:
        updated = replace(updated, transcript_note_path=str(changes["transcript_note_path"]))
    if "structured_note_path" in changes:
        updated = replace(updated, structured_note_path=str(changes["structured_note_path"]))
    if "session_dir" in changes:
        updated = replace(updated, session_dir=str(changes["session_dir"]))
    if "transcript_source" in changes:
        updated = replace(updated, transcript_source=str(changes["transcript_source"]))
    if "refine_status" in changes:
        updated = replace(updated, refine_status=str(changes["refine_status"]))
    if "execution_target" in changes:
        updated = replace(updated, execution_target=str(changes["execution_target"]))
    if "remote_session_id" in changes:
        value = changes["remote_session_id"]
        updated = replace(updated, remote_session_id=None if value in (None, "") else str(value))
    if "speaker_status" in changes:
        updated = replace(updated, speaker_status=str(changes["speaker_status"]))
    return updated


def _normalize_metadata_changes(changes: dict[str, object]) -> dict[str, object]:
    normalized = dict(changes)
    illegal = {"status", "display_status", "runtime_status"} & set(normalized)
    if illegal:
        joined = ", ".join(sorted(illegal))
        raise ValueError(
            f"session lifecycle status must be changed via session commands, not metadata changes: {joined}"
        )
    return normalized
def _session_command_id(command_kind: str, session_id: str) -> str:
    return f"{command_kind}:{session_id}:{uuid4().hex[:12]}"
