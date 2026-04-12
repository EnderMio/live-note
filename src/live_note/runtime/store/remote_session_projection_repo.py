from __future__ import annotations

from sqlite3 import Connection, Row

from live_note.runtime.domain.remote_session_projection import RemoteSessionProjectionRecord

from .control_db import ControlDb


class RemoteSessionProjectionRepo:
    def __init__(self, db: ControlDb):
        self.db = db

    def get(
        self,
        session_id: str,
        *,
        connection: Connection | None = None,
    ) -> RemoteSessionProjectionRecord | None:
        if connection is not None:
            row = connection.execute(
                "SELECT * FROM remote_session_projections WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            return _row_to_record(row) if row is not None else None
        with self.db.connect() as owned:
            return self.get(session_id, connection=owned)

    def list_all(
        self,
        *,
        connection: Connection | None = None,
    ) -> list[RemoteSessionProjectionRecord]:
        if connection is not None:
            rows = connection.execute(
                """
                SELECT * FROM remote_session_projections
                ORDER BY started_at DESC, session_id ASC
                """
            ).fetchall()
            return [_row_to_record(row) for row in rows]
        with self.db.connect() as owned:
            return self.list_all(connection=owned)

    def upsert(
        self,
        record: RemoteSessionProjectionRecord,
        *,
        connection: Connection | None = None,
    ) -> RemoteSessionProjectionRecord:
        params = (
            record.session_id,
            record.title,
            record.kind,
            record.input_mode,
            record.source_label,
            record.source_ref,
            record.language,
            record.started_at,
            record.transcript_note_path,
            record.structured_note_path,
            record.session_dir,
            record.status,
            record.runtime_status,
            record.transcript_source,
            record.refine_status,
            record.execution_target,
            record.remote_session_id,
            record.speaker_status,
            record.remote_updated_at,
            record.last_seen_at,
            record.artifacts_synced_at,
        )
        if connection is not None:
            connection.execute(
                """
                INSERT INTO remote_session_projections(
                    session_id, title, kind, input_mode, source_label, source_ref, language,
                    started_at, transcript_note_path, structured_note_path, session_dir,
                    status, runtime_status, transcript_source, refine_status,
                    execution_target, remote_session_id, speaker_status, remote_updated_at,
                    last_seen_at, artifacts_synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    title = excluded.title,
                    kind = excluded.kind,
                    input_mode = excluded.input_mode,
                    source_label = excluded.source_label,
                    source_ref = excluded.source_ref,
                    language = excluded.language,
                    started_at = excluded.started_at,
                    transcript_note_path = excluded.transcript_note_path,
                    structured_note_path = excluded.structured_note_path,
                    session_dir = excluded.session_dir,
                    status = excluded.status,
                    runtime_status = excluded.runtime_status,
                    transcript_source = excluded.transcript_source,
                    refine_status = excluded.refine_status,
                    execution_target = excluded.execution_target,
                    remote_session_id = excluded.remote_session_id,
                    speaker_status = excluded.speaker_status,
                    remote_updated_at = excluded.remote_updated_at,
                    last_seen_at = excluded.last_seen_at,
                    artifacts_synced_at = excluded.artifacts_synced_at
                """,
                params,
            )
            persisted = self.get(record.session_id, connection=connection)
            assert persisted is not None
            return persisted
        with self.db.connect() as owned:
            persisted = self.upsert(record, connection=owned)
            owned.commit()
            return persisted


def _row_to_record(row: Row) -> RemoteSessionProjectionRecord:
    return RemoteSessionProjectionRecord(
        session_id=row["session_id"],
        title=row["title"],
        kind=row["kind"],
        input_mode=row["input_mode"],
        source_label=row["source_label"],
        source_ref=row["source_ref"],
        language=row["language"],
        started_at=row["started_at"],
        transcript_note_path=row["transcript_note_path"],
        structured_note_path=row["structured_note_path"],
        session_dir=row["session_dir"],
        status=row["status"],
        runtime_status=row["runtime_status"],
        transcript_source=row["transcript_source"],
        refine_status=row["refine_status"],
        execution_target=row["execution_target"],
        remote_session_id=row["remote_session_id"],
        speaker_status=row["speaker_status"],
        remote_updated_at=row["remote_updated_at"],
        last_seen_at=row["last_seen_at"],
        artifacts_synced_at=row["artifacts_synced_at"],
    )
