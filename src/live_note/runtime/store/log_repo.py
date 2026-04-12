from __future__ import annotations

import json
from dataclasses import replace
from sqlite3 import Connection, Row

from live_note.runtime.domain.commands import CommandRecord
from live_note.runtime.domain.events import EventRecord

from .control_db import ControlDb


class LogRepo:
    def __init__(self, db: ControlDb):
        self.db = db

    def append_command(
        self,
        record: CommandRecord,
        *,
        connection: Connection | None = None,
    ) -> CommandRecord:
        params = (
            record.command_id,
            record.kind,
            record.session_id,
            record.task_id,
            record.created_at,
            json.dumps(record.payload, ensure_ascii=False, sort_keys=True),
        )
        if connection is not None:
            cursor = connection.execute(
                """
                INSERT INTO commands(
                    command_id,
                    kind,
                    session_id,
                    task_id,
                    created_at,
                    payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                params,
            )
            return replace(record, sequence=int(cursor.lastrowid))
        with self.db.connect() as owned:
            persisted = self.append_command(record, connection=owned)
            owned.commit()
            return persisted

    def append_event(
        self,
        record: EventRecord,
        *,
        connection: Connection | None = None,
    ) -> EventRecord:
        params = (
            record.event_id,
            record.kind,
            record.session_id,
            record.task_id,
            record.created_at,
            json.dumps(record.payload, ensure_ascii=False, sort_keys=True),
        )
        if connection is not None:
            cursor = connection.execute(
                """
                INSERT INTO events(
                    event_id,
                    kind,
                    session_id,
                    task_id,
                    created_at,
                    payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                params,
            )
            return replace(record, sequence=int(cursor.lastrowid))
        with self.db.connect() as owned:
            persisted = self.append_event(record, connection=owned)
            owned.commit()
            return persisted

    def list_commands(
        self,
        *,
        session_id: str | None = None,
        task_id: str | None = None,
        connection: Connection | None = None,
    ) -> list[CommandRecord]:
        sql = "SELECT * FROM commands"
        params: list[object] = []
        sql, params = _append_filters(sql, params, session_id=session_id, task_id=task_id)
        sql += " ORDER BY sequence ASC"
        if connection is not None:
            rows = connection.execute(sql, tuple(params)).fetchall()
            return [_row_to_command_record(row) for row in rows]
        with self.db.connect() as owned:
            return self.list_commands(
                session_id=session_id,
                task_id=task_id,
                connection=owned,
            )

    def list_events(
        self,
        *,
        session_id: str | None = None,
        task_id: str | None = None,
        connection: Connection | None = None,
    ) -> list[EventRecord]:
        sql = "SELECT * FROM events"
        params: list[object] = []
        sql, params = _append_filters(sql, params, session_id=session_id, task_id=task_id)
        sql += " ORDER BY sequence ASC"
        if connection is not None:
            rows = connection.execute(sql, tuple(params)).fetchall()
            return [_row_to_event_record(row) for row in rows]
        with self.db.connect() as owned:
            return self.list_events(
                session_id=session_id,
                task_id=task_id,
                connection=owned,
            )


def _append_filters(
    sql: str,
    params: list[object],
    *,
    session_id: str | None,
    task_id: str | None,
) -> tuple[str, list[object]]:
    filters: list[str] = []
    if session_id is not None:
        filters.append("session_id = ?")
        params.append(session_id)
    if task_id is not None:
        filters.append("task_id = ?")
        params.append(task_id)
    if filters:
        sql += " WHERE " + " AND ".join(filters)
    return sql, params


def _row_to_command_record(row: Row) -> CommandRecord:
    return CommandRecord(
        command_id=row["command_id"],
        kind=row["kind"],
        session_id=row["session_id"],
        task_id=row["task_id"],
        created_at=row["created_at"],
        payload=dict(json.loads(row["payload_json"] or "{}")),
        sequence=int(row["sequence"]),
    )


def _row_to_event_record(row: Row) -> EventRecord:
    return EventRecord(
        event_id=row["event_id"],
        kind=row["kind"],
        session_id=row["session_id"],
        task_id=row["task_id"],
        created_at=row["created_at"],
        payload=dict(json.loads(row["payload_json"] or "{}")),
        sequence=int(row["sequence"]),
    )
