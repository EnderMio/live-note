from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from live_note.utils import ensure_parent, iso_now


@dataclass(frozen=True, slots=True)
class RemoteTaskAttachment:
    remote_task_id: str | None
    server_id: str | None
    action: str
    label: str
    session_id: str | None
    request_id: str | None
    last_known_status: str
    last_known_stage: str
    last_message: str
    attachment_state: str
    last_synced_result_version: int
    result_version: int
    updated_at: str
    created_at: str
    last_seen_at: str | None = None
    artifacts_synced_at: str | None = None
    last_error: str | None = None
    current: int | None = None
    total: int | None = None
    can_cancel: bool = False

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> RemoteTaskAttachment:
        required = (
            "action",
            "label",
            "last_known_status",
            "attachment_state",
            "last_synced_result_version",
            "result_version",
            "updated_at",
            "created_at",
        )
        missing = [key for key in required if key not in payload]
        if missing:
            raise ValueError(f"missing remote task fields: {', '.join(missing)}")
        return cls(
            remote_task_id=_optional_string(payload.get("remote_task_id")),
            server_id=_optional_string(payload.get("server_id")),
            action=str(payload["action"]).strip(),
            label=str(payload["label"]).strip(),
            session_id=_optional_string(payload.get("session_id")),
            request_id=_optional_string(payload.get("request_id")),
            last_known_status=str(payload["last_known_status"]).strip(),
            last_known_stage=str(
                payload.get("last_known_stage") or payload["last_known_status"]
            ).strip(),
            last_message=str(payload.get("last_message") or "").strip(),
            attachment_state=str(payload["attachment_state"]).strip(),
            last_synced_result_version=int(payload["last_synced_result_version"]),
            result_version=int(payload["result_version"]),
            updated_at=str(payload["updated_at"]).strip(),
            created_at=str(payload["created_at"]).strip(),
            last_seen_at=_optional_string(payload.get("last_seen_at")),
            artifacts_synced_at=_optional_string(payload.get("artifacts_synced_at")),
            last_error=_optional_string(payload.get("last_error")),
            current=_optional_int(payload.get("current")),
            total=_optional_int(payload.get("total")),
            can_cancel=bool(payload.get("can_cancel", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "remote_task_id": self.remote_task_id,
            "server_id": self.server_id,
            "action": self.action,
            "label": self.label,
            "session_id": self.session_id,
            "request_id": self.request_id,
            "last_known_status": self.last_known_status,
            "last_known_stage": self.last_known_stage,
            "last_message": self.last_message,
            "attachment_state": self.attachment_state,
            "last_synced_result_version": self.last_synced_result_version,
            "result_version": self.result_version,
            "updated_at": self.updated_at,
            "created_at": self.created_at,
            "last_seen_at": self.last_seen_at,
            "artifacts_synced_at": self.artifacts_synced_at,
            "last_error": self.last_error,
            "current": self.current,
            "total": self.total,
            "can_cancel": self.can_cancel,
        }


@dataclass(frozen=True, slots=True)
class RemoteTaskLoadResult:
    records: list[RemoteTaskAttachment]
    warnings: list[str]


def load_remote_tasks(path: Path) -> RemoteTaskLoadResult:
    if not path.exists():
        return RemoteTaskLoadResult(records=[], warnings=[])
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        records_payload = payload.get("records", []) if isinstance(payload, Mapping) else payload
        if not isinstance(records_payload, list):
            raise TypeError("remote task file must contain a list of records")
        records: list[RemoteTaskAttachment] = []
        warnings: list[str] = []
        for index, item in enumerate(records_payload):
            if not isinstance(item, Mapping):
                warnings.append(f"忽略损坏的远端任务记录 {path} #{index}: not an object")
                continue
            try:
                records.append(RemoteTaskAttachment.from_dict(item))
            except (TypeError, ValueError) as exc:
                warnings.append(f"忽略损坏的远端任务记录 {path} #{index}: {exc}")
        return RemoteTaskLoadResult(records=records, warnings=warnings)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return RemoteTaskLoadResult(records=[], warnings=[f"无法加载远端任务附着表 {path}: {exc}"])


def save_remote_tasks(path: Path, records: Sequence[RemoteTaskAttachment]) -> None:
    ensure_parent(path)
    payload = {
        "version": 1,
        "records": [record.to_dict() for record in records],
    }
    temp_path = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    try:
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temp_path.replace(path)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def upsert_pending_remote_task(
    path: Path,
    *,
    action: str,
    label: str,
    request_id: str,
    session_id: str | None = None,
) -> RemoteTaskAttachment:
    loaded = load_remote_tasks(path)
    now = iso_now()
    records = list(loaded.records)
    index = _find_record_index(
        records,
        request_id=request_id,
        session_id=session_id,
        action=action,
    )
    record = RemoteTaskAttachment(
        remote_task_id=None,
        server_id=None,
        action=action,
        label=label,
        session_id=session_id,
        request_id=request_id,
        last_known_status="queued",
        last_known_stage="queued",
        last_message="等待远端接受任务。",
        attachment_state="awaiting_rebind",
        last_synced_result_version=0,
        result_version=0,
        updated_at=now,
        created_at=records[index].created_at if index is not None else now,
    )
    _replace_or_append(records, index, record)
    save_remote_tasks(path, records)
    return record


def upsert_remote_task_payload(
    path: Path,
    payload: Mapping[str, Any],
    *,
    fallback_request_id: str | None = None,
    fallback_session_id: str | None = None,
    fallback_label: str | None = None,
) -> RemoteTaskAttachment:
    loaded = load_remote_tasks(path)
    records = list(loaded.records)
    action = str(payload.get("action") or "").strip()
    request_id = _optional_string(payload.get("request_id")) or _optional_string(
        fallback_request_id
    )
    session_id = _optional_string(payload.get("session_id")) or _optional_string(
        fallback_session_id
    )
    remote_task_id = _optional_string(payload.get("task_id"))
    index = _find_record_index(
        records,
        remote_task_id=remote_task_id,
        request_id=request_id,
        session_id=session_id,
        action=action,
    )
    existing = records[index] if index is not None else None
    now = iso_now()
    record = RemoteTaskAttachment(
        remote_task_id=remote_task_id,
        server_id=_optional_string(payload.get("server_id"))
        or (existing.server_id if existing else None),
        action=action or (existing.action if existing else ""),
        label=str(
            payload.get("label") or fallback_label or (existing.label if existing else "")
        ).strip(),
        session_id=session_id,
        request_id=request_id,
        last_known_status=str(
            payload.get("status") or (existing.last_known_status if existing else "queued")
        ).strip(),
        last_known_stage=str(
            payload.get("stage") or (existing.last_known_stage if existing else "queued")
        ).strip(),
        last_message=str(
            payload.get("message") or (existing.last_message if existing else "")
        ).strip(),
        attachment_state="attached" if remote_task_id else "awaiting_rebind",
        last_synced_result_version=existing.last_synced_result_version if existing else 0,
        result_version=int(
            payload.get("result_version", existing.result_version if existing else 0)
        ),
        updated_at=now,
        created_at=existing.created_at if existing else now,
        last_seen_at=now,
        artifacts_synced_at=existing.artifacts_synced_at if existing else None,
        last_error=_optional_string(payload.get("error"))
        or (existing.last_error if existing else None),
        current=_optional_int(payload.get("current")),
        total=_optional_int(payload.get("total")),
        can_cancel=bool(
            payload.get("can_cancel", existing.can_cancel if existing else False)
        ),
    )
    _replace_or_append(records, index, record)
    save_remote_tasks(path, records)
    return record


def mark_remote_task_synced(
    path: Path,
    *,
    remote_task_id: str,
    result_version: int,
    last_error: str | None = None,
) -> None:
    loaded = load_remote_tasks(path)
    records = list(loaded.records)
    index = _find_record_index(records, remote_task_id=remote_task_id)
    if index is None:
        return
    now = iso_now()
    record = replace(
        records[index],
        last_synced_result_version=result_version,
        result_version=max(records[index].result_version, result_version),
        artifacts_synced_at=now,
        updated_at=now,
        last_error=last_error,
    )
    records[index] = record
    save_remote_tasks(path, records)


def replace_remote_task_records(path: Path, records: Sequence[RemoteTaskAttachment]) -> None:
    save_remote_tasks(path, list(records))


def _replace_or_append(
    records: list[RemoteTaskAttachment],
    index: int | None,
    record: RemoteTaskAttachment,
) -> None:
    if index is None:
        records.append(record)
        return
    records[index] = record


def _find_record_index(
    records: Sequence[RemoteTaskAttachment],
    *,
    remote_task_id: str | None = None,
    request_id: str | None = None,
    session_id: str | None = None,
    action: str | None = None,
) -> int | None:
    if remote_task_id:
        for index, record in enumerate(records):
            if record.remote_task_id == remote_task_id:
                return index
    if request_id:
        for index, record in enumerate(records):
            if record.request_id == request_id:
                return index
    if session_id and action:
        for index, record in enumerate(records):
            if record.session_id == session_id and record.action == action:
                return index
    return None


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)
