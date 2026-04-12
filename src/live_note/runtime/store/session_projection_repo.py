from __future__ import annotations

from sqlite3 import Connection, Row

from live_note.runtime.domain.session_projection import SessionProjectionRecord

from .control_db import ControlDb


class SessionProjectionRepo:
    def __init__(self, db: ControlDb):
        self.db = db

    def get(
        self,
        session_id: str,
        *,
        connection: Connection | None = None,
    ) -> SessionProjectionRecord | None:
        if connection is not None:
            row = connection.execute(
                "SELECT * FROM session_projections WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            return _row_to_record(row) if row is not None else None
        with self.db.connect() as owned:
            return self.get(session_id, connection=owned)

    def list_all(
        self,
        *,
        connection: Connection | None = None,
    ) -> list[SessionProjectionRecord]:
        if connection is not None:
            rows = connection.execute(
                """
                SELECT * FROM session_projections
                ORDER BY updated_at DESC, session_id ASC
                """
            ).fetchall()
            return [_row_to_record(row) for row in rows]
        with self.db.connect() as owned:
            return self.list_all(connection=owned)

    def upsert(
        self,
        record: SessionProjectionRecord,
        *,
        connection: Connection | None = None,
    ) -> SessionProjectionRecord:
        params = (
            record.session_id,
            record.segment_count,
            record.transcribed_count,
            record.failed_count,
            record.latest_error,
            record.updated_at,
        )
        if connection is not None:
            connection.execute(
                """
                INSERT INTO session_projections(
                    session_id, segment_count, transcribed_count, failed_count,
                    latest_error, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    segment_count = excluded.segment_count,
                    transcribed_count = excluded.transcribed_count,
                    failed_count = excluded.failed_count,
                    latest_error = excluded.latest_error,
                    updated_at = excluded.updated_at
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

    def delete_missing(
        self,
        session_ids: set[str],
        *,
        connection: Connection | None = None,
    ) -> None:
        if connection is not None:
            if session_ids:
                placeholders = ",".join("?" for _ in session_ids)
                connection.execute(
                    f"DELETE FROM session_projections WHERE session_id NOT IN ({placeholders})",
                    tuple(sorted(session_ids)),
                )
                return
            connection.execute("DELETE FROM session_projections")
            return
        with self.db.connect() as owned:
            self.delete_missing(session_ids, connection=owned)
            owned.commit()


def _row_to_record(row: Row) -> SessionProjectionRecord:
    return SessionProjectionRecord(
        session_id=row["session_id"],
        segment_count=int(row["segment_count"] or 0),
        transcribed_count=int(row["transcribed_count"] or 0),
        failed_count=int(row["failed_count"] or 0),
        latest_error=row["latest_error"],
        updated_at=row["updated_at"],
    )
