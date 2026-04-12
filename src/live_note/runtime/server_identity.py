from __future__ import annotations

from uuid import uuid4

from live_note.runtime.store import ControlDb


def load_or_create_server_id(db: ControlDb) -> str:
    with db.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'server_id'"
        ).fetchone()
        if row is not None and str(row["value"]).strip():
            server_id = str(row["value"]).strip()
            connection.commit()
            return server_id
        server_id = f"server-{uuid4().hex[:12]}"
        connection.execute(
            """
            INSERT INTO schema_meta(key, value)
            VALUES ('server_id', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (server_id,),
        )
        connection.commit()
        return server_id
