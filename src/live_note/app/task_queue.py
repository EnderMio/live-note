from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from live_note.utils import ensure_parent

TaskStatus = Literal["queued", "running"]

_VALID_STATUSES = {"queued", "running"}


@dataclass(frozen=True, slots=True)
class TaskQueueRecord:
    task_id: str
    action: str
    label: str
    payload: dict[str, Any]
    fingerprint: str
    status: TaskStatus
    created_at: str
    started_at: str | None = None

    def __post_init__(self) -> None:
        if self.status not in _VALID_STATUSES:
            raise ValueError(f"unsupported task queue status: {self.status}")
        _coerce_json_object(self.payload)
        if self.started_at is not None and not isinstance(self.started_at, str):
            raise TypeError("task queue started_at must be a string or null")

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> TaskQueueRecord:
        required_fields = (
            "task_id",
            "action",
            "label",
            "payload",
            "fingerprint",
            "status",
            "created_at",
        )
        missing = [field for field in required_fields if field not in payload]
        if missing:
            raise ValueError(f"missing task queue fields: {', '.join(missing)}")
        status = payload["status"]
        if status not in _VALID_STATUSES:
            raise ValueError(f"unsupported task queue status: {status}")
        raw_task_payload = payload["payload"]
        if not isinstance(raw_task_payload, Mapping):
            raise TypeError("task queue payload must be a JSON object")
        started_at = payload.get("started_at")
        if started_at is not None and not isinstance(started_at, str):
            raise TypeError("task queue started_at must be a string or null")
        return cls(
            task_id=str(payload["task_id"]),
            action=str(payload["action"]),
            label=str(payload["label"]),
            payload=_coerce_json_object(raw_task_payload),
            fingerprint=str(payload["fingerprint"]),
            status=status,
            created_at=str(payload["created_at"]),
            started_at=started_at,
        )

    def to_dict(self) -> dict[str, Any]:
        record = {
            "task_id": self.task_id,
            "action": self.action,
            "label": self.label,
            "payload": _coerce_json_object(self.payload),
            "fingerprint": self.fingerprint,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
        }
        return record


@dataclass(frozen=True, slots=True)
class QueueLoadResult:
    active: list[TaskQueueRecord]
    interrupted: list[TaskQueueRecord]
    warnings: list[str]

    @property
    def active_records(self) -> list[TaskQueueRecord]:
        return self.active

    @property
    def interrupted_records(self) -> list[TaskQueueRecord]:
        return self.interrupted


def load_task_queue(path: Path) -> QueueLoadResult:
    if not path.exists():
        return QueueLoadResult(active=[], interrupted=[], warnings=[])
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
        records_payload = _extract_records_payload(parsed)
        active: list[TaskQueueRecord] = []
        interrupted: list[TaskQueueRecord] = []
        warnings: list[str] = []
        for index, item in enumerate(records_payload):
            try:
                record = TaskQueueRecord.from_dict(item)
            except (TypeError, ValueError) as exc:
                warnings.append(
                    f"忽略损坏的队列记录 {path} #{index}: {exc}"
                )
                continue
            if record.status == "running":
                interrupted.append(record)
            else:
                active.append(record)
        return QueueLoadResult(active=active, interrupted=interrupted, warnings=warnings)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return QueueLoadResult(
            active=[],
            interrupted=[],
            warnings=[f"无法加载任务队列 {path}: {exc}"],
        )


def save_task_queue(path: Path, records: Sequence[TaskQueueRecord]) -> None:
    ensure_parent(path)
    serialized = {
        "version": 1,
        "records": [record.to_dict() for record in records],
    }
    temp_path = _temporary_queue_path(path)
    temp_path.write_text(
        json.dumps(serialized, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temp_path.replace(path)


def normalize_task_payload(action: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    normalized = _coerce_json_object(payload)
    normalized_action = action.strip()
    if normalized_action == "import" and "file_path" in normalized:
        file_path = normalized["file_path"]
        if not isinstance(file_path, str):
            raise TypeError("import payload file_path must be a string")
        normalized["file_path"] = str(Path(file_path).expanduser().resolve())
    if normalized_action == "session_action":
        if "session_id" in normalized:
            normalized["session_id"] = _normalize_string_field(
                normalized["session_id"],
                field_name="session_id",
            )
        if "action" in normalized:
            normalized["action"] = _normalize_string_field(
                normalized["action"],
                field_name="action",
            )
        if "operation" in normalized:
            normalized["operation"] = _normalize_string_field(
                normalized["operation"],
                field_name="operation",
            )
    if normalized_action == "merge":
        session_ids = normalized.get("session_ids", [])
        normalized["session_ids"] = _normalize_merge_session_ids(session_ids)
    return normalized


def task_fingerprint(action: str, payload: Mapping[str, Any]) -> str:
    normalized_action = action.strip()
    normalized_payload = normalize_task_payload(normalized_action, payload)
    return json.dumps(
        {"action": normalized_action, "payload": normalized_payload},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _extract_records_payload(parsed: Any) -> list[Mapping[str, Any]]:
    if isinstance(parsed, list):
        records = parsed
    elif isinstance(parsed, Mapping):
        records = parsed.get("records", parsed.get("active", []))
    else:
        raise TypeError("task queue file must contain an object or list")
    if not isinstance(records, list):
        raise TypeError("task queue active records must be a list")
    validated_records: list[Mapping[str, Any]] = []
    for item in records:
        if not isinstance(item, Mapping):
            raise TypeError("task queue record must be an object")
        validated_records.append(item)
    return validated_records


def _coerce_json_object(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): _coerce_json_value(value) for key, value in payload.items()}


def _coerce_json_value(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return _coerce_json_object(value)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_coerce_json_value(item) for item in value]
    raise TypeError(f"value is not JSON serializable: {type(value).__name__}")


def _normalize_merge_session_ids(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        raise TypeError("merge payload session_ids must be a list")
    normalized = sorted(
        {
            str(session_id).strip()
            for session_id in value
            if str(session_id).strip()
        }
    )
    return normalized


def _normalize_string_field(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    return value.strip()


def _temporary_queue_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.tmp")


QueuedTaskRecord = TaskQueueRecord


def build_task_record(
    *,
    task_id: str,
    action: str,
    label: str,
    payload: Mapping[str, Any],
    created_at: str,
    status: TaskStatus = "queued",
    started_at: str | None = None,
) -> TaskQueueRecord:
    normalized_payload = normalize_task_payload(action, payload)
    return TaskQueueRecord(
        task_id=task_id,
        action=action.strip(),
        label=label,
        payload=normalized_payload,
        fingerprint=task_fingerprint(action, normalized_payload),
        status=status,
        created_at=created_at,
        started_at=started_at,
    )


class TaskQueueStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> QueueLoadResult:
        return load_task_queue(self.path)

    def save(self, records: Sequence[TaskQueueRecord]) -> None:
        save_task_queue(self.path, records)
