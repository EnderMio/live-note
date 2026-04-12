from __future__ import annotations

from dataclasses import replace
from typing import Any

from live_note.config import AppConfig
from live_note.remote.client import RemoteClient
from live_note.remote.protocol import entry_from_dict, metadata_from_dict
from live_note.remote_sync import apply_remote_artifacts, sync_remote_transcript_snapshot
from live_note.runtime.remote_projection_target import reconcile_remote_projection_target
from live_note.runtime.domain.remote_task_projection import RemoteTaskProjectionRecord
from live_note.runtime.remote_task_projections import (
    get_remote_task_projection_by_task_id,
    list_remote_task_projections,
    mark_remote_task_projection_error,
    mark_remote_task_projection_synced,
    upsert_remote_task_projection_from_payload,
)
from live_note.runtime.store import ControlDb, RemoteTaskProjectionRepo
from live_note.utils import iso_now


def sync_remote_task_projections(
    config: AppConfig,
    *,
    client: RemoteClient | None = None,
    now=iso_now,
) -> list[RemoteTaskProjectionRecord]:
    reconcile_remote_projection_target(config.root_dir, config.remote.base_url)
    records = list_remote_task_projections(config.root_dir)
    if not config.remote.enabled:
        return records
    resolved_client = client or RemoteClient(config.remote)
    payload = resolved_client.list_tasks()
    server_id = _optional_string(payload.get("server_id"))
    remote_items = [
        dict(item)
        for item in [*list(payload.get("active") or []), *list(payload.get("recent") or [])]
        if isinstance(item, dict)
    ]
    merged = _merge_remote_task_records(
        records,
        remote_items,
        server_id=server_id,
        now=now,
    )
    persisted = _persist_remote_task_records(config.root_dir, merged)
    return _sync_remote_task_artifacts(config, resolved_client, persisted)


def sync_single_remote_task(
    config: AppConfig,
    remote_task_id: str,
    *,
    client: RemoteClient | None = None,
) -> dict[str, object]:
    if not config.remote.enabled:
        raise RuntimeError("远端模式未启用。")
    reconcile_remote_projection_target(config.root_dir, config.remote.base_url)
    record = get_remote_task_projection_by_task_id(config.root_dir, remote_task_id)
    if record is None:
        raise FileNotFoundError(f"未找到远端任务投影：{remote_task_id}")
    if record.attachment_state == "lost":
        raise RuntimeError("服务端已重置，任务无法恢复。")
    resolved_client = client or RemoteClient(config.remote)
    payload = resolved_client.get_task(remote_task_id)
    attachment = upsert_remote_task_projection_from_payload(config.root_dir, payload)
    if not attachment.session_id:
        raise RuntimeError("当前远端任务尚未关联记录，暂时无法同步。")
    try:
        _apply_or_sync_remote_artifacts(config, resolved_client, attachment)
        mark_remote_task_projection_synced(
            config.root_dir,
            remote_task_id=remote_task_id,
            result_version=attachment.result_version,
        )
    except Exception as exc:
        mark_remote_task_projection_error(config.root_dir, remote_task_id, str(exc))
        raise
    return payload


def _sync_remote_task_artifacts(
    config: AppConfig,
    client: RemoteClient,
    records: list[RemoteTaskProjectionRecord],
) -> list[RemoteTaskProjectionRecord]:
    updated: list[RemoteTaskProjectionRecord] = []
    for record in records:
        if (
            record.remote_task_id is None
            or record.session_id is None
            or record.result_version <= record.last_synced_result_version
            or record.attachment_state == "lost"
        ):
            updated.append(record)
            continue
        try:
            _apply_or_sync_remote_artifacts(config, client, record)
            synced = mark_remote_task_projection_synced(
                config.root_dir,
                remote_task_id=record.remote_task_id,
                result_version=record.result_version,
            )
            updated.append(synced or record)
        except Exception as exc:
            errored = mark_remote_task_projection_error(
                config.root_dir,
                record.remote_task_id,
                str(exc),
            )
            updated.append(errored or replace(record, last_error=str(exc)))
    return updated


def _apply_or_sync_remote_artifacts(
    config: AppConfig,
    client: RemoteClient,
    record: RemoteTaskProjectionRecord,
) -> None:
    assert record.session_id is not None
    artifacts = client.get_artifacts(record.session_id)
    metadata = metadata_from_dict(dict(artifacts["metadata"]))
    entries = [entry_from_dict(dict(item)) for item in artifacts.get("entries", [])]
    if record.action == "import" and record.status == "running":
        sync_remote_transcript_snapshot(
            config,
            metadata,
            entries,
            remote_updated_at=_optional_string(artifacts.get("updated_at")),
        )
        return
    apply_remote_artifacts(
        config,
        metadata,
        entries,
        remote_updated_at=_optional_string(artifacts.get("updated_at")),
        transcript_content=_optional_text(artifacts.get("transcript_content")),
        structured_content=_optional_text(artifacts.get("structured_content")),
    )


def _persist_remote_task_records(
    root_dir,
    records: list[RemoteTaskProjectionRecord],
) -> list[RemoteTaskProjectionRecord]:
    repo = RemoteTaskProjectionRepo(ControlDb.for_root(root_dir))
    return [repo.upsert(record) for record in records]


def _merge_remote_task_records(
    records: list[RemoteTaskProjectionRecord],
    remote_items: list[dict[str, object]],
    *,
    server_id: str | None,
    now,
) -> list[RemoteTaskProjectionRecord]:
    by_task_id = {
        str(item["task_id"]): item
        for item in remote_items
        if _optional_string(item.get("task_id"))
    }
    by_request_id = {
        str(item["request_id"]): item
        for item in remote_items
        if _optional_string(item.get("request_id"))
    }
    merged: list[RemoteTaskProjectionRecord] = []
    matched_task_ids: set[str] = set()
    for record in records:
        if record.server_id and server_id and record.server_id != server_id:
            if record.status in {"queued", "running"}:
                merged.append(
                    replace(
                        record,
                        attachment_state="lost",
                        last_error="服务端已重置，任务无法恢复。",
                        last_seen_at=now(),
                    )
                )
            else:
                merged.append(
                    replace(
                        record,
                        attachment_state="attached",
                        last_error=(
                            None
                            if record.last_error == "服务端已重置，任务无法恢复。"
                            else record.last_error
                        ),
                        last_seen_at=now(),
                    )
                )
            continue
        match = None
        if record.remote_task_id:
            match = by_task_id.get(record.remote_task_id)
        if match is None and record.request_id:
            match = by_request_id.get(record.request_id)
        if match is None:
            if record.status in {"queued", "running"}:
                merged.append(
                    replace(
                        record,
                        attachment_state="lost",
                        last_error="服务端已重置，任务无法恢复。",
                        last_seen_at=now(),
                    )
                )
            else:
                merged.append(replace(record, last_seen_at=now()))
            continue
        task_id = _optional_string(match.get("task_id"))
        if task_id:
            matched_task_ids.add(task_id)
        merged.append(
            _record_from_task_payload(
                match,
                existing=record,
                server_id=server_id,
                now=now,
            )
        )
    for item in remote_items:
        task_id = _optional_string(item.get("task_id"))
        if task_id is None or task_id in matched_task_ids:
            continue
        merged.append(
            _record_from_task_payload(
                item,
                existing=None,
                server_id=server_id,
                now=now,
            )
        )
    return merged


def _record_from_task_payload(
    payload: dict[str, object],
    *,
    existing: RemoteTaskProjectionRecord | None,
    server_id: str | None,
    now,
) -> RemoteTaskProjectionRecord:
    now_value = now()
    remote_updated_at = (
        str(payload.get("updated_at") or (existing.updated_at if existing else now_value)).strip()
        or now_value
    )
    return RemoteTaskProjectionRecord(
        projection_id=(
            existing.projection_id
            if existing is not None
            else f"remote-proj-{(payload.get('task_id') or payload.get('request_id') or now_value)}"
        ),
        remote_task_id=_optional_string(payload.get("task_id")),
        server_id=(
            _optional_string(payload.get("server_id"))
            or server_id
            or (existing.server_id if existing else None)
        ),
        action=str(payload.get("action") or (existing.action if existing else "")).strip(),
        label=str(payload.get("label") or (existing.label if existing else "")).strip(),
        session_id=(
            _optional_string(payload.get("session_id"))
            or (existing.session_id if existing else None)
        ),
        request_id=(
            _optional_string(payload.get("request_id"))
            or (existing.request_id if existing else None)
        ),
        status=str(payload.get("status") or (existing.status if existing else "queued")).strip(),
        stage=str(payload.get("stage") or (existing.stage if existing else "queued")).strip(),
        message=str(payload.get("message") or (existing.message if existing else "")).strip(),
        updated_at=remote_updated_at,
        created_at=existing.created_at if existing else now_value,
        attachment_state=(
            "attached"
            if _optional_string(payload.get("task_id"))
            else (existing.attachment_state if existing else "awaiting_rebind")
        ),
        last_synced_result_version=existing.last_synced_result_version if existing else 0,
        result_version=int(
            payload.get("result_version", existing.result_version if existing else 0)
        ),
        last_seen_at=now_value,
        artifacts_synced_at=existing.artifacts_synced_at if existing else None,
        last_error=(
            _optional_string(payload.get("error"))
            or (existing.last_error if existing else None)
        ),
        current=_optional_int(payload.get("current")),
        total=_optional_int(payload.get("total")),
        can_cancel=bool(payload.get("can_cancel", existing.can_cancel if existing else False)),
    )


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)
