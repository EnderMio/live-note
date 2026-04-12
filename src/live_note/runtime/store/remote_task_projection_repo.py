from __future__ import annotations

from sqlite3 import Connection, Row

from live_note.runtime.domain.remote_task_projection import RemoteTaskProjectionRecord

from .control_db import ControlDb


class RemoteTaskProjectionRepo:
    def __init__(self, db: ControlDb):
        self.db = db

    def get(
        self,
        projection_id: str,
        *,
        connection: Connection | None = None,
    ) -> RemoteTaskProjectionRecord | None:
        if connection is not None:
            row = connection.execute(
                "SELECT * FROM remote_task_projections WHERE projection_id = ?",
                (projection_id,),
            ).fetchone()
            return _row_to_record(row) if row is not None else None
        with self.db.connect() as owned:
            return self.get(projection_id, connection=owned)

    def get_by_remote_task_id(
        self,
        remote_task_id: str,
        *,
        connection: Connection | None = None,
    ) -> RemoteTaskProjectionRecord | None:
        if connection is not None:
            row = connection.execute(
                "SELECT * FROM remote_task_projections WHERE remote_task_id = ?",
                (remote_task_id,),
            ).fetchone()
            return _row_to_record(row) if row is not None else None
        with self.db.connect() as owned:
            return self.get_by_remote_task_id(remote_task_id, connection=owned)

    def list_all(
        self,
        *,
        connection: Connection | None = None,
    ) -> list[RemoteTaskProjectionRecord]:
        if connection is not None:
            rows = connection.execute(
                """
                SELECT * FROM remote_task_projections
                ORDER BY created_at ASC, projection_id ASC
                """
            ).fetchall()
            return [_row_to_record(row) for row in rows]
        with self.db.connect() as owned:
            return self.list_all(connection=owned)

    def get_by_request_id(
        self,
        request_id: str,
        *,
        connection: Connection | None = None,
    ) -> RemoteTaskProjectionRecord | None:
        if connection is not None:
            row = connection.execute(
                "SELECT * FROM remote_task_projections WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            return _row_to_record(row) if row is not None else None
        with self.db.connect() as owned:
            return self.get_by_request_id(request_id, connection=owned)

    def upsert(
        self,
        record: RemoteTaskProjectionRecord,
        *,
        connection: Connection | None = None,
    ) -> RemoteTaskProjectionRecord:
        params = (
            record.projection_id,
            record.remote_task_id,
            record.server_id,
            record.action,
            record.label,
            record.session_id,
            record.request_id,
            record.status,
            record.stage,
            record.message,
            record.updated_at,
            record.created_at,
            record.attachment_state,
            record.last_synced_result_version,
            record.result_version,
            record.last_seen_at,
            record.artifacts_synced_at,
            record.last_error,
            record.current,
            record.total,
            1 if record.can_cancel else 0,
        )
        if connection is not None:
            connection.execute(
                """
                INSERT INTO remote_task_projections(
                    projection_id, remote_task_id, server_id, action, label, session_id,
                    request_id, status, stage, message, updated_at, created_at,
                    attachment_state, last_synced_result_version, result_version,
                    last_seen_at, artifacts_synced_at, last_error, current, total,
                    can_cancel
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(projection_id) DO UPDATE SET
                    remote_task_id = excluded.remote_task_id,
                    server_id = excluded.server_id,
                    action = excluded.action,
                    label = excluded.label,
                    session_id = excluded.session_id,
                    request_id = excluded.request_id,
                    status = excluded.status,
                    stage = excluded.stage,
                    message = excluded.message,
                    updated_at = excluded.updated_at,
                    created_at = excluded.created_at,
                    attachment_state = excluded.attachment_state,
                    last_synced_result_version = excluded.last_synced_result_version,
                    result_version = excluded.result_version,
                    last_seen_at = excluded.last_seen_at,
                    artifacts_synced_at = excluded.artifacts_synced_at,
                    last_error = excluded.last_error,
                    current = excluded.current,
                    total = excluded.total,
                    can_cancel = excluded.can_cancel
                """,
                params,
            )
            persisted = self.get(record.projection_id, connection=connection)
            assert persisted is not None
            return persisted
        with self.db.connect() as owned:
            persisted = self.upsert(record, connection=owned)
            owned.commit()
            return persisted


def _row_to_record(row: Row) -> RemoteTaskProjectionRecord:
    return RemoteTaskProjectionRecord(
        projection_id=row["projection_id"],
        remote_task_id=row["remote_task_id"],
        server_id=row["server_id"],
        action=row["action"],
        label=row["label"],
        session_id=row["session_id"],
        request_id=row["request_id"],
        status=row["status"],
        stage=row["stage"],
        message=row["message"],
        updated_at=row["updated_at"],
        created_at=row["created_at"],
        attachment_state=row["attachment_state"],
        last_synced_result_version=int(row["last_synced_result_version"] or 0),
        result_version=int(row["result_version"] or 0),
        last_seen_at=row["last_seen_at"],
        artifacts_synced_at=row["artifacts_synced_at"],
        last_error=row["last_error"],
        current=row["current"],
        total=row["total"],
        can_cancel=bool(row["can_cancel"]),
    )
