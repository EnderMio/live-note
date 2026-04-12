from __future__ import annotations

from dataclasses import replace

from live_note.domain import SessionMetadata
from live_note.runtime.domain.remote_session_projection import RemoteSessionProjectionRecord
from live_note.runtime.store import ControlDb, RemoteSessionProjectionRepo
from live_note.utils import iso_now


def list_remote_session_projections(root_dir) -> list[RemoteSessionProjectionRecord]:
    return RemoteSessionProjectionRepo(ControlDb.for_root(root_dir)).list_all()


def get_remote_session_projection(
    root_dir,
    session_id: str,
) -> RemoteSessionProjectionRecord | None:
    return RemoteSessionProjectionRepo(ControlDb.for_root(root_dir)).get(session_id)


def upsert_remote_session_projection(
    root_dir,
    *,
    remote_metadata: SessionMetadata,
    local_metadata: SessionMetadata,
    runtime_status: str | None = None,
    remote_updated_at: str | None = None,
    artifacts_synced: bool = False,
) -> RemoteSessionProjectionRecord:
    repo = RemoteSessionProjectionRepo(ControlDb.for_root(root_dir))
    existing = repo.get(remote_metadata.session_id)
    now = iso_now()
    record = RemoteSessionProjectionRecord(
        session_id=remote_metadata.session_id,
        title=remote_metadata.title,
        kind=remote_metadata.kind,
        input_mode=remote_metadata.input_mode,
        source_label=remote_metadata.source_label,
        source_ref=remote_metadata.source_ref,
        language=remote_metadata.language,
        started_at=remote_metadata.started_at,
        transcript_note_path=local_metadata.transcript_note_path,
        structured_note_path=local_metadata.structured_note_path,
        session_dir=local_metadata.session_dir,
        status=remote_metadata.status,
        runtime_status=runtime_status,
        transcript_source=remote_metadata.transcript_source,
        refine_status=remote_metadata.refine_status,
        execution_target="remote",
        remote_session_id=remote_metadata.remote_session_id or remote_metadata.session_id,
        speaker_status=remote_metadata.speaker_status,
        remote_updated_at=remote_updated_at or (existing.remote_updated_at if existing else None),
        last_seen_at=now,
        artifacts_synced_at=(
            now
            if artifacts_synced
            else (existing.artifacts_synced_at if existing else None)
        ),
    )
    return repo.upsert(record)


def mark_remote_session_projection_synced(
    root_dir,
    session_id: str,
) -> RemoteSessionProjectionRecord | None:
    repo = RemoteSessionProjectionRepo(ControlDb.for_root(root_dir))
    existing = repo.get(session_id)
    if existing is None:
        return None
    now = iso_now()
    return repo.upsert(
        replace(
            existing,
            artifacts_synced_at=now,
            last_seen_at=now,
        )
    )
