from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

from live_note.runtime.domain.remote_task_projection import RemoteTaskProjectionRecord
from live_note.runtime.store import ControlDb, RemoteTaskProjectionRepo
from live_note.utils import iso_now


def list_remote_task_projections(root_dir) -> list[RemoteTaskProjectionRecord]:
    return RemoteTaskProjectionRepo(ControlDb.for_root(root_dir)).list_all()


def get_remote_task_projection_by_task_id(
    root_dir,
    remote_task_id: str,
) -> RemoteTaskProjectionRecord | None:
    return RemoteTaskProjectionRepo(
        ControlDb.for_root(root_dir)
    ).get_by_remote_task_id(remote_task_id)


def upsert_remote_task_projection_from_payload(
    root_dir,
    payload: dict[str, object],
) -> RemoteTaskProjectionRecord:
    db = ControlDb.for_root(root_dir)
    repo = RemoteTaskProjectionRepo(db)
    now = iso_now()
    remote_task_id = _optional_string(payload.get("task_id"))
    request_id = _optional_string(payload.get("request_id"))
    session_id = _optional_string(payload.get("session_id"))
    if remote_task_id is None and request_id is None:
        raise ValueError("remote task projection requires task_id or request_id")
    existing = None
    if remote_task_id is not None:
        existing = repo.get_by_remote_task_id(remote_task_id)
    if existing is None and request_id is not None:
        existing = repo.get_by_request_id(request_id)
    projection_id = (
        existing.projection_id
        if existing is not None
        else f"remote-proj-{uuid4().hex[:12]}"
    )
    record = RemoteTaskProjectionRecord(
        projection_id=projection_id,
        remote_task_id=remote_task_id,
        server_id=(
            _optional_string(payload.get("server_id"))
            or (existing.server_id if existing else None)
        ),
        action=str(payload.get("action") or (existing.action if existing else "")).strip(),
        label=str(payload.get("label") or (existing.label if existing else "")).strip(),
        session_id=session_id,
        request_id=request_id,
        status=str(payload.get("status") or (existing.status if existing else "queued")).strip(),
        stage=str(payload.get("stage") or (existing.stage if existing else "queued")).strip(),
        message=str(payload.get("message") or (existing.message if existing else "")).strip(),
        updated_at=(
            str(payload.get("updated_at") or (existing.updated_at if existing else now)).strip()
            or now
        ),
        created_at=existing.created_at if existing else now,
        attachment_state=(
            "attached"
            if remote_task_id
            else (existing.attachment_state if existing else "awaiting_rebind")
        ),
        last_synced_result_version=existing.last_synced_result_version if existing else 0,
        result_version=int(
            payload.get("result_version", existing.result_version if existing else 0)
        ),
        last_seen_at=now,
        artifacts_synced_at=existing.artifacts_synced_at if existing else None,
        last_error=(
            _optional_string(payload.get("error"))
            or (existing.last_error if existing else None)
        ),
        current=_optional_int(payload.get("current")),
        total=_optional_int(payload.get("total")),
        can_cancel=bool(payload.get("can_cancel", existing.can_cancel if existing else False)),
    )
    return repo.upsert(record)


def mark_remote_task_projection_synced(
    root_dir,
    *,
    remote_task_id: str,
    result_version: int,
) -> RemoteTaskProjectionRecord | None:
    db = ControlDb.for_root(root_dir)
    repo = RemoteTaskProjectionRepo(db)
    existing = repo.get_by_remote_task_id(remote_task_id)
    if existing is None:
        return None
    now = iso_now()
    updated = replace(
        existing,
        last_synced_result_version=result_version,
        result_version=max(existing.result_version, result_version),
        artifacts_synced_at=now,
        last_error=None,
    )
    return repo.upsert(updated)


def mark_remote_task_projection_error(
    root_dir,
    remote_task_id: str,
    error: str,
) -> RemoteTaskProjectionRecord | None:
    db = ControlDb.for_root(root_dir)
    repo = RemoteTaskProjectionRepo(db)
    existing = repo.get_by_remote_task_id(remote_task_id)
    if existing is None:
        return None
    return repo.upsert(replace(existing, last_error=error))


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)
