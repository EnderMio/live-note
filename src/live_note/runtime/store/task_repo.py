from __future__ import annotations

import json
from sqlite3 import Connection, Row

from live_note.runtime.domain.task_state import TaskRecord, normalize_resource_keys

from .control_db import ControlDb


class TaskRepo:
    def __init__(self, db: ControlDb):
        self.db = db

    def get(self, task_id: str, *, connection: Connection | None = None) -> TaskRecord | None:
        if connection is not None:
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            return _row_to_task_record(row) if row is not None else None
        with self.db.connect() as owned:
            return self.get(task_id, connection=owned)

    def list_all(self, *, connection: Connection | None = None) -> list[TaskRecord]:
        if connection is not None:
            rows = connection.execute(
                """
                SELECT * FROM tasks
                ORDER BY created_at ASC, task_id ASC
                """
            ).fetchall()
            return [_row_to_task_record(row) for row in rows]
        with self.db.connect() as owned:
            return self.list_all(connection=owned)

    def list_by_status(
        self,
        *statuses: str,
        connection: Connection | None = None,
    ) -> list[TaskRecord]:
        if not statuses:
            return []
        placeholders = ", ".join("?" for _ in statuses)
        sql = (
            "SELECT * FROM tasks WHERE status IN ("
            f"{placeholders}) ORDER BY updated_at DESC, task_id ASC"
        )
        if connection is not None:
            rows = connection.execute(sql, tuple(statuses)).fetchall()
            return [_row_to_task_record(row) for row in rows]
        with self.db.connect() as owned:
            return self.list_by_status(*statuses, connection=owned)

    def find_by_request_id(
        self,
        request_id: str,
        *,
        connection: Connection | None = None,
    ) -> TaskRecord | None:
        if connection is not None:
            row = connection.execute(
                "SELECT * FROM tasks WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            return _row_to_task_record(row) if row is not None else None
        with self.db.connect() as owned:
            return self.find_by_request_id(request_id, connection=owned)

    def find_active_by_dedupe_key(
        self,
        dedupe_key: str,
        *,
        connection: Connection | None = None,
    ) -> TaskRecord | None:
        if connection is not None:
            row = connection.execute(
                """
                SELECT * FROM tasks
                WHERE dedupe_key = ?
                  AND status IN ('queued', 'running')
                ORDER BY created_at ASC, task_id ASC
                LIMIT 1
                """,
                (dedupe_key,),
            ).fetchone()
            return _row_to_task_record(row) if row is not None else None
        with self.db.connect() as owned:
            return self.find_active_by_dedupe_key(dedupe_key, connection=owned)

    def find_running_resource_conflict(
        self,
        resource_keys: tuple[str, ...],
        *,
        exclude_task_id: str | None = None,
        connection: Connection | None = None,
    ) -> TaskRecord | None:
        normalized = normalize_resource_keys(resource_keys)
        if not normalized:
            return None
        if connection is not None:
            rows = connection.execute(
                "SELECT * FROM tasks WHERE status = 'running' ORDER BY started_at ASC, task_id ASC"
            ).fetchall()
            for row in rows:
                if exclude_task_id is not None and row["task_id"] == exclude_task_id:
                    continue
                record = _row_to_task_record(row)
                if set(record.resource_keys) & set(normalized):
                    return record
            return None
        with self.db.connect() as owned:
            return self.find_running_resource_conflict(
                normalized,
                exclude_task_id=exclude_task_id,
                connection=owned,
            )

    def upsert(
        self,
        record: TaskRecord,
        *,
        connection: Connection | None = None,
    ) -> TaskRecord:
        params = (
            record.task_id,
            record.session_id,
            record.action,
            record.label,
            record.status,
            record.stage,
            record.created_at,
            record.updated_at,
            record.request_id,
            record.dedupe_key,
            json.dumps(normalize_resource_keys(record.resource_keys), ensure_ascii=False),
            json.dumps(record.payload, ensure_ascii=False, sort_keys=True),
            1 if record.can_cancel else 0,
            record.started_at,
            record.completed_at,
            record.attempt,
            record.error,
            record.message,
            record.current,
            record.total,
            record.result_version,
            1 if record.cancel_requested else 0,
        )
        if connection is not None:
            connection.execute(
                """
                INSERT INTO tasks(
                    task_id, session_id, action, label, status, stage, created_at, updated_at,
                    request_id, dedupe_key, resource_keys_json, payload_json, can_cancel,
                    started_at, completed_at, attempt, error, message, current, total,
                    result_version, cancel_requested
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    session_id = excluded.session_id,
                    action = excluded.action,
                    label = excluded.label,
                    status = excluded.status,
                    stage = excluded.stage,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    request_id = excluded.request_id,
                    dedupe_key = excluded.dedupe_key,
                    resource_keys_json = excluded.resource_keys_json,
                    payload_json = excluded.payload_json,
                    can_cancel = excluded.can_cancel,
                    started_at = excluded.started_at,
                    completed_at = excluded.completed_at,
                    attempt = excluded.attempt,
                    error = excluded.error,
                    message = excluded.message,
                    current = excluded.current,
                    total = excluded.total,
                    result_version = excluded.result_version,
                    cancel_requested = excluded.cancel_requested
                """,
                params,
            )
            persisted = self.get(record.task_id, connection=connection)
            assert persisted is not None
            return persisted
        with self.db.connect() as owned:
            persisted = self.upsert(record, connection=owned)
            owned.commit()
            return persisted


def _row_to_task_record(row: Row) -> TaskRecord:
    return TaskRecord(
        task_id=row["task_id"],
        session_id=row["session_id"],
        action=row["action"],
        label=row["label"],
        status=row["status"],
        stage=row["stage"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        request_id=row["request_id"],
        dedupe_key=row["dedupe_key"],
        resource_keys=tuple(json.loads(row["resource_keys_json"] or "[]")),
        payload=dict(json.loads(row["payload_json"] or "{}")),
        can_cancel=bool(row["can_cancel"]),
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        attempt=int(row["attempt"] or 0),
        error=row["error"],
        message=row["message"] or "",
        current=int(row["current"]) if row["current"] is not None else None,
        total=int(row["total"]) if row["total"] is not None else None,
        result_version=int(row["result_version"] or 0),
        cancel_requested=bool(row["cancel_requested"]),
    )
