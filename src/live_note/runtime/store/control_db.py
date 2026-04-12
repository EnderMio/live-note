from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

_INIT_LOCKS_GUARD = threading.Lock()
_INIT_LOCKS: dict[Path, threading.Lock] = {}


def control_db_path(root_dir: Path) -> Path:
    return (root_dir / ".live-note" / "runtime" / "control.db").resolve()


class ControlDb:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _init_lock_for_path(self.path):
            self._initialize()

    @classmethod
    def for_root(cls, root_dir: Path) -> ControlDb:
        return cls(control_db_path(root_dir))

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=30.0,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    input_mode TEXT NOT NULL,
                    source_label TEXT NOT NULL,
                    source_ref TEXT NOT NULL,
                    language TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    transcript_note_path TEXT NOT NULL,
                    structured_note_path TEXT NOT NULL,
                    session_dir TEXT NOT NULL,
                    display_status TEXT NOT NULL,
                    runtime_status TEXT NOT NULL,
                    transcript_source TEXT NOT NULL,
                    refine_status TEXT NOT NULL,
                    execution_target TEXT NOT NULL,
                    remote_session_id TEXT,
                    speaker_status TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_started_at
                    ON sessions(started_at DESC, session_id ASC);

                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    session_id TEXT,
                    action TEXT NOT NULL,
                    label TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    request_id TEXT UNIQUE,
                    dedupe_key TEXT,
                    resource_keys_json TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    can_cancel INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT,
                    completed_at TEXT,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    message TEXT NOT NULL DEFAULT '',
                    current INTEGER,
                    total INTEGER,
                    result_version INTEGER NOT NULL DEFAULT 0,
                    cancel_requested INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_status
                    ON tasks(status, updated_at DESC, task_id ASC);
                CREATE INDEX IF NOT EXISTS idx_tasks_session_id
                    ON tasks(session_id, created_at DESC, task_id ASC);
                CREATE INDEX IF NOT EXISTS idx_tasks_dedupe_key
                    ON tasks(dedupe_key);

                CREATE TABLE IF NOT EXISTS commands (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    command_id TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    session_id TEXT,
                    task_id TEXT,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_commands_session_id
                    ON commands(session_id, sequence ASC);
                CREATE INDEX IF NOT EXISTS idx_commands_task_id
                    ON commands(task_id, sequence ASC);

                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    session_id TEXT,
                    task_id TEXT,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_session_id
                    ON events(session_id, sequence ASC);
                CREATE INDEX IF NOT EXISTS idx_events_task_id
                    ON events(task_id, sequence ASC);

                CREATE TABLE IF NOT EXISTS remote_task_projections (
                    projection_id TEXT PRIMARY KEY,
                    remote_task_id TEXT UNIQUE,
                    server_id TEXT,
                    action TEXT NOT NULL,
                    label TEXT NOT NULL,
                    session_id TEXT,
                    request_id TEXT,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    message TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    attachment_state TEXT NOT NULL,
                    last_synced_result_version INTEGER NOT NULL DEFAULT 0,
                    result_version INTEGER NOT NULL DEFAULT 0,
                    last_seen_at TEXT,
                    artifacts_synced_at TEXT,
                    last_error TEXT,
                    current INTEGER,
                    total INTEGER,
                    can_cancel INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_remote_task_projections_remote_task_id
                    ON remote_task_projections(remote_task_id);
                CREATE INDEX IF NOT EXISTS idx_remote_task_projections_request_id
                    ON remote_task_projections(request_id);
                CREATE INDEX IF NOT EXISTS idx_remote_task_projections_session_id
                    ON remote_task_projections(session_id, created_at DESC, projection_id ASC);

                CREATE TABLE IF NOT EXISTS remote_session_projections (
                    session_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    input_mode TEXT NOT NULL,
                    source_label TEXT NOT NULL,
                    source_ref TEXT NOT NULL,
                    language TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    transcript_note_path TEXT NOT NULL,
                    structured_note_path TEXT NOT NULL,
                    session_dir TEXT NOT NULL,
                    status TEXT NOT NULL,
                    runtime_status TEXT,
                    transcript_source TEXT NOT NULL,
                    refine_status TEXT NOT NULL,
                    execution_target TEXT NOT NULL,
                    remote_session_id TEXT,
                    speaker_status TEXT NOT NULL,
                    remote_updated_at TEXT,
                    last_seen_at TEXT,
                    artifacts_synced_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_remote_session_projections_started_at
                    ON remote_session_projections(started_at DESC, session_id ASC);

                CREATE TABLE IF NOT EXISTS session_projections (
                    session_id TEXT PRIMARY KEY,
                    segment_count INTEGER NOT NULL DEFAULT 0,
                    transcribed_count INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0,
                    latest_error TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_session_projections_updated_at
                    ON session_projections(updated_at DESC, session_id ASC);
                """
            )
            connection.execute(
                """
                INSERT INTO schema_meta(key, value)
                VALUES ('schema_version', '5')
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """
            )
            _ensure_task_column(connection, "message", "TEXT NOT NULL DEFAULT ''")
            _ensure_task_column(connection, "current", "INTEGER")
            _ensure_task_column(connection, "total", "INTEGER")
            _ensure_task_column(connection, "result_version", "INTEGER NOT NULL DEFAULT 0")
            _ensure_task_column(connection, "cancel_requested", "INTEGER NOT NULL DEFAULT 0")


def _init_lock_for_path(path: Path) -> threading.Lock:
    normalized = path.resolve()
    with _INIT_LOCKS_GUARD:
        existing = _INIT_LOCKS.get(normalized)
        if existing is not None:
            return existing
        created = threading.Lock()
        _INIT_LOCKS[normalized] = created
        return created


def _ensure_task_column(connection: sqlite3.Connection, name: str, ddl: str) -> None:
    columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
    }
    if name in columns:
        return
    connection.execute(f"ALTER TABLE tasks ADD COLUMN {name} {ddl}")
