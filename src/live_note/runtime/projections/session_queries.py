from __future__ import annotations

from live_note.runtime.domain.session_state import SessionRecord
from live_note.runtime.store import ControlDb, SessionRepo


def get_session(db: ControlDb, session_id: str) -> SessionRecord | None:
    return SessionRepo(db).get(session_id)


def list_session_history(db: ControlDb) -> list[SessionRecord]:
    return SessionRepo(db).list_all()
