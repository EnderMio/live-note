from __future__ import annotations

from live_note.runtime.store import ControlDb

_REMOTE_PROJECTION_TARGET_KEY = "remote_projection_target"


def reconcile_remote_projection_target(root_dir, base_url: str) -> bool:
    target = _normalize_base_url(base_url)
    db = ControlDb.for_root(root_dir)
    with db.connect() as connection:
        existing_row = connection.execute(
            "SELECT value FROM schema_meta WHERE key = ?",
            (_REMOTE_PROJECTION_TARGET_KEY,),
        ).fetchone()
        existing = _normalize_base_url(existing_row["value"]) if existing_row is not None else None
        has_remote_rows = _has_remote_projection_rows(connection)
        should_reset = (
            (existing is None and has_remote_rows)
            or (existing is not None and existing != target)
        )
        if should_reset:
            connection.execute("DELETE FROM remote_task_projections")
            connection.execute("DELETE FROM remote_session_projections")
        connection.execute(
            """
            INSERT INTO schema_meta(key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (_REMOTE_PROJECTION_TARGET_KEY, target),
        )
        connection.commit()
        return should_reset


def _has_remote_projection_rows(connection) -> bool:
    task_count = connection.execute(
        "SELECT COUNT(*) AS count FROM remote_task_projections"
    ).fetchone()
    if int(task_count["count"] or 0) > 0:
        return True
    session_count = connection.execute(
        "SELECT COUNT(*) AS count FROM remote_session_projections"
    ).fetchone()
    return int(session_count["count"] or 0) > 0


def _normalize_base_url(value: str | None) -> str:
    text = str(value or "").strip().rstrip("/")
    return text
