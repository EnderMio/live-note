from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace

from live_note.config import AppConfig

from ..remote.protocol import entry_from_dict, metadata_from_dict
from .remote_tasks import RemoteTaskAttachment


@dataclass(frozen=True, slots=True)
class RemoteTaskSummary:
    remote_task_id: str | None
    server_id: str | None
    action: str
    label: str
    session_id: str | None
    status: str
    stage: str
    message: str
    current: int | None
    total: int | None
    updated_at: str
    attachment_state: str
    can_cancel: bool
    result_version: int
    last_synced_result_version: int
    last_error: str | None


@dataclass(frozen=True, slots=True)
class RemoteTaskSnapshot:
    remote_available: bool
    availability_message: str | None
    tasks: list[RemoteTaskSummary]


class RemoteTaskService:
    def __init__(
        self,
        *,
        load_config: Callable[[], AppConfig],
        remote_tasks_path: Callable[[], object],
        load_remote_tasks: Callable[[object], object],
        replace_remote_task_records: Callable[[object, list[RemoteTaskAttachment]], None],
        mark_remote_task_synced: Callable[..., None],
        upsert_remote_task_payload: Callable[..., RemoteTaskAttachment],
        remote_client_factory: Callable[[object], object],
        apply_remote_artifacts: Callable[..., object],
        sync_remote_transcript_snapshot: Callable[..., object],
        optional_text: Callable[[object], str | None],
        now: Callable[[], str],
    ):
        self._load_config = load_config
        self._remote_tasks_path = remote_tasks_path
        self._load_remote_tasks = load_remote_tasks
        self._replace_remote_task_records = replace_remote_task_records
        self._mark_remote_task_synced = mark_remote_task_synced
        self._upsert_remote_task_payload = upsert_remote_task_payload
        self._remote_client_factory = remote_client_factory
        self._apply_remote_artifacts = apply_remote_artifacts
        self._sync_remote_transcript_snapshot = sync_remote_transcript_snapshot
        self._optional_text = optional_text
        self._now = now

    def list_remote_task_summaries(self) -> RemoteTaskSnapshot:
        try:
            config = self._load_config()
        except Exception as exc:
            return RemoteTaskSnapshot(
                remote_available=False, availability_message=str(exc), tasks=[]
            )

        loaded = self._load_remote_tasks(self._remote_tasks_path())
        records = list(loaded.records)
        if not config.remote.enabled:
            return RemoteTaskSnapshot(
                remote_available=False,
                availability_message="远端模式未启用。",
                tasks=_remote_task_summaries(records),
            )

        try:
            client = self._remote_client_factory(config.remote)
            payload = client.list_tasks()
        except Exception as exc:
            return RemoteTaskSnapshot(
                remote_available=False,
                availability_message=f"远端暂不可达，显示的是上次已知状态：{exc}",
                tasks=_remote_task_summaries(records),
            )

        server_id = str(payload.get("server_id") or "").strip() or None
        remote_items = [
            dict(item)
            for item in [*list(payload.get("active") or []), *list(payload.get("recent") or [])]
            if isinstance(item, dict)
        ]
        merged = _merge_remote_task_records(
            records, remote_items, server_id=server_id, now=self._now
        )
        synced = self._sync_remote_task_artifacts(config, client, merged)
        self._replace_remote_task_records(self._remote_tasks_path(), synced)
        return RemoteTaskSnapshot(
            remote_available=True, availability_message=None, tasks=_remote_task_summaries(synced)
        )

    def cancel_remote_task(self, task_id: str) -> dict[str, object]:
        config = self._load_config()
        if not config.remote.enabled:
            raise RuntimeError("远端模式未启用。")
        client = self._remote_client_factory(config.remote)
        payload = client.cancel_task(task_id)
        self._upsert_remote_task_payload(self._remote_tasks_path(), payload)
        return payload

    def sync_remote_task(self, task_id: str) -> dict[str, object]:
        config = self._load_config()
        if not config.remote.enabled:
            raise RuntimeError("远端模式未启用。")
        loaded = self._load_remote_tasks(self._remote_tasks_path())
        record = _find_remote_task_record(loaded.records, task_id)
        if record is None:
            raise FileNotFoundError(f"未找到远端任务附着记录：{task_id}")
        if record.attachment_state == "lost":
            raise RuntimeError("服务端已重置，任务无法恢复。")
        client = self._remote_client_factory(config.remote)
        payload = client.get_task(task_id)
        attachment = self._upsert_remote_task_payload(
            self._remote_tasks_path(),
            payload,
            fallback_request_id=record.request_id,
            fallback_session_id=record.session_id,
            fallback_label=record.label,
        )
        session_id = attachment.session_id
        if not session_id:
            raise RuntimeError("当前远端任务尚未关联记录，暂时无法同步。")
        try:
            artifacts = client.get_artifacts(session_id)
            metadata = metadata_from_dict(dict(artifacts["metadata"]))
            entries = [entry_from_dict(dict(item)) for item in artifacts.get("entries", [])]
            status = attachment.last_known_status
            if attachment.action == "import" and status == "running":
                self._sync_remote_transcript_snapshot(config, metadata, entries)
            else:
                self._apply_remote_artifacts(
                    config,
                    metadata,
                    entries,
                    transcript_content=self._optional_text(artifacts.get("transcript_content")),
                    structured_content=self._optional_text(artifacts.get("structured_content")),
                )
            self._mark_remote_task_synced(
                self._remote_tasks_path(),
                remote_task_id=task_id,
                result_version=attachment.result_version,
            )
            return payload
        except Exception as exc:
            _mark_remote_task_error(
                self._remote_tasks_path(),
                task_id,
                str(exc),
                load_remote_tasks=self._load_remote_tasks,
                replace_remote_task_records=self._replace_remote_task_records,
                now=self._now,
            )
            raise

    def _sync_remote_task_artifacts(
        self,
        config: AppConfig,
        client: object,
        records: list[RemoteTaskAttachment],
    ) -> list[RemoteTaskAttachment]:
        updated: list[RemoteTaskAttachment] = []
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
                artifacts = client.get_artifacts(record.session_id)
                metadata = metadata_from_dict(dict(artifacts["metadata"]))
                entries = [entry_from_dict(dict(item)) for item in artifacts.get("entries", [])]
                if record.action == "import" and record.last_known_status == "running":
                    self._sync_remote_transcript_snapshot(config, metadata, entries)
                else:
                    self._apply_remote_artifacts(
                        config,
                        metadata,
                        entries,
                        transcript_content=self._optional_text(artifacts.get("transcript_content")),
                        structured_content=self._optional_text(artifacts.get("structured_content")),
                    )
                updated.append(
                    replace(
                        record,
                        last_synced_result_version=record.result_version,
                        artifacts_synced_at=self._now(),
                        updated_at=self._now(),
                        last_error=None,
                    )
                )
            except Exception as exc:
                updated.append(replace(record, updated_at=self._now(), last_error=str(exc)))
        return updated


def _remote_task_summaries(records: list[RemoteTaskAttachment]) -> list[RemoteTaskSummary]:
    def sort_key(record: RemoteTaskAttachment) -> tuple[int, int, str]:
        is_lost = record.attachment_state == "lost"
        is_active = record.last_known_status in {"queued", "running", "cancelling"} and not is_lost
        active_rank = 1 if is_active else 0
        lost_rank = 0 if is_lost else 1
        return (active_rank, lost_rank, record.updated_at)

    return [
        RemoteTaskSummary(
            remote_task_id=record.remote_task_id,
            server_id=record.server_id,
            action=record.action,
            label=record.label,
            session_id=record.session_id,
            status=record.last_known_status,
            stage=record.last_known_stage,
            message=record.last_message,
            current=record.current,
            total=record.total,
            updated_at=record.updated_at,
            attachment_state=record.attachment_state,
            can_cancel=record.can_cancel,
            result_version=record.result_version,
            last_synced_result_version=record.last_synced_result_version,
            last_error=record.last_error,
        )
        for record in sorted(records, key=sort_key, reverse=True)
    ]


def _merge_remote_task_records(
    records: list[RemoteTaskAttachment],
    remote_items: list[dict[str, object]],
    *,
    server_id: str | None,
    now: Callable[[], str],
) -> list[RemoteTaskAttachment]:
    by_task_id = {
        str(item["task_id"]): item
        for item in remote_items
        if str(item.get("task_id") or "").strip()
    }
    by_request_id = {
        str(item["request_id"]): item
        for item in remote_items
        if str(item.get("request_id") or "").strip()
    }
    by_session_action = {
        (str(item.get("session_id") or "").strip(), str(item.get("action") or "").strip()): item
        for item in remote_items
        if str(item.get("session_id") or "").strip() and str(item.get("action") or "").strip()
    }
    merged: list[RemoteTaskAttachment] = []
    matched_task_ids: set[str] = set()
    for record in records:
        if record.server_id and server_id and record.server_id != server_id:
            if record.last_known_status in {"queued", "running", "cancelling"}:
                merged.append(
                    replace(
                        record,
                        attachment_state="lost",
                        updated_at=now(),
                        last_error="服务端已重置，任务无法恢复。",
                    )
                )
            else:
                merged.append(
                    replace(
                        record,
                        attachment_state="attached",
                        updated_at=now(),
                        last_error=(
                            None
                            if record.last_error == "服务端已重置，任务无法恢复。"
                            else record.last_error
                        ),
                    )
                )
            continue
        match = None
        if record.remote_task_id:
            match = by_task_id.get(record.remote_task_id)
        if match is None and record.request_id:
            match = by_request_id.get(record.request_id)
        if match is None and record.session_id:
            match = by_session_action.get((record.session_id, record.action))
        if match is None:
            if record.last_known_status in {"queued", "running", "cancelling"}:
                merged.append(
                    replace(
                        record,
                        attachment_state="lost",
                        updated_at=now(),
                        last_error="服务端已重置，任务无法恢复。",
                    )
                )
            else:
                merged.append(record)
            continue
        task_id = str(match.get("task_id") or "").strip()
        if task_id:
            matched_task_ids.add(task_id)
        merged.append(
            _record_from_task_payload(match, existing=record, server_id=server_id, now=now)
        )
    for item in remote_items:
        task_id = str(item.get("task_id") or "").strip()
        if not task_id or task_id in matched_task_ids:
            continue
        merged.append(_record_from_task_payload(item, server_id=server_id, now=now))
    return merged


def _record_from_task_payload(
    payload: Mapping[str, object],
    *,
    existing: RemoteTaskAttachment | None = None,
    server_id: str | None = None,
    now: Callable[[], str],
) -> RemoteTaskAttachment:
    now_value = now()
    return RemoteTaskAttachment(
        remote_task_id=str(payload.get("task_id") or "").strip() or None,
        server_id=str(payload.get("server_id") or server_id or "").strip() or None,
        action=str(payload.get("action") or (existing.action if existing else "")).strip(),
        label=str(payload.get("label") or (existing.label if existing else "")).strip(),
        session_id=str(
            payload.get("session_id") or (existing.session_id if existing else "")
        ).strip()
        or None,
        request_id=str(
            payload.get("request_id") or (existing.request_id if existing else "")
        ).strip()
        or None,
        last_known_status=str(
            payload.get("status") or (existing.last_known_status if existing else "queued")
        ).strip(),
        last_known_stage=str(
            payload.get("stage") or (existing.last_known_stage if existing else "queued")
        ).strip(),
        last_message=str(
            payload.get("message") or (existing.last_message if existing else "")
        ).strip(),
        attachment_state="attached"
        if str(payload.get("task_id") or "").strip()
        else (existing.attachment_state if existing else "awaiting_rebind"),
        last_synced_result_version=existing.last_synced_result_version if existing else 0,
        result_version=int(
            payload.get("result_version", existing.result_version if existing else 0)
        ),
        updated_at=now_value,
        created_at=existing.created_at if existing else now_value,
        last_seen_at=now_value,
        artifacts_synced_at=existing.artifacts_synced_at if existing else None,
        last_error=str(payload.get("error") or (existing.last_error if existing else "")).strip()
        or None,
        current=int(payload["current"]) if payload.get("current") is not None else None,
        total=int(payload["total"]) if payload.get("total") is not None else None,
        can_cancel=bool(payload.get("can_cancel", existing.can_cancel if existing else False)),
    )


def _find_remote_task_record(
    records: list[RemoteTaskAttachment],
    task_id: str,
) -> RemoteTaskAttachment | None:
    for record in records:
        if record.remote_task_id == task_id:
            return record
    return None


def _mark_remote_task_error(
    path: object,
    task_id: str,
    error: str,
    *,
    load_remote_tasks: Callable[[object], object],
    replace_remote_task_records: Callable[[object, list[RemoteTaskAttachment]], None],
    now: Callable[[], str],
) -> None:
    loaded = load_remote_tasks(path)
    records: list[RemoteTaskAttachment] = []
    for record in loaded.records:
        if record.remote_task_id == task_id:
            records.append(replace(record, updated_at=now(), last_error=error))
            continue
        records.append(record)
    replace_remote_task_records(path, records)
