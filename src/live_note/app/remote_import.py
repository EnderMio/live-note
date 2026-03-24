from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from live_note.config import AppConfig
from live_note.remote.client import RemoteClient, RemoteClientError
from live_note.remote.protocol import entry_from_dict, metadata_from_dict

from .events import ProgressCallback, ProgressEvent
from .remote_sync import apply_remote_artifacts, sync_remote_transcript_snapshot
from .remote_tasks import (
    mark_remote_task_synced,
    upsert_pending_remote_task,
    upsert_remote_task_payload,
)
from .task_errors import TaskCancelledError

DEFAULT_REMOTE_IMPORT_POLL_SECONDS = 0.8


class RemoteImportCoordinator:
    def __init__(
        self,
        config: AppConfig,
        file_path: str,
        title: str | None,
        kind: str,
        language: str | None = None,
        on_progress: ProgressCallback | None = None,
        *,
        client: RemoteClient | None = None,
        poll_interval_seconds: float = DEFAULT_REMOTE_IMPORT_POLL_SECONDS,
        cancel_event: threading.Event | None = None,
    ):
        self.config = config
        self.file_path = Path(file_path).expanduser().resolve()
        self.title = title or self.file_path.stem
        self.kind = kind
        self.language = language or config.whisper.language
        self.on_progress = on_progress
        self.client = client or RemoteClient(config.remote)
        self.poll_interval_seconds = max(0.0, poll_interval_seconds)
        self.cancel_event = cancel_event
        self._last_progress_signature: tuple[Any, ...] | None = None
        self._last_snapshot_signature: tuple[Any, ...] | None = None

    def run(self) -> int:
        self._raise_if_cancelled()
        self._ensure_remote_supports_imports()
        request_id = f"import-{uuid4().hex[:12]}"
        upsert_pending_remote_task(
            self._remote_tasks_path(),
            action="import",
            label="文件导入",
            request_id=request_id,
        )
        self._emit_progress("uploading", f"正在上传音频到远端：{self.file_path.name}")
        task = self.client.create_import_task(
            str(self.file_path),
            title=self.title,
            kind=self.kind,
            language=self.language,
            speaker_enabled=self.config.speaker.enabled,
            request_id=request_id,
        )
        upsert_remote_task_payload(
            self._remote_tasks_path(),
            task,
            fallback_request_id=request_id,
            fallback_label="文件导入",
        )
        self._emit_remote_state(task)

        task_id = str(task.get("task_id") or "").strip()
        if not task_id:
            raise RemoteClientError("远端未返回导入任务 ID。")

        final_state = self._wait_until_complete(task_id)
        upsert_remote_task_payload(
            self._remote_tasks_path(),
            final_state,
            fallback_request_id=request_id,
        )
        session_id = str(final_state.get("session_id") or "").strip()
        if not session_id:
            raise RemoteClientError("远端导入完成但未返回 session_id。")

        artifacts = self.client.get_artifacts(session_id)
        metadata = metadata_from_dict(dict(artifacts["metadata"]))
        entries = [entry_from_dict(dict(item)) for item in artifacts.get("entries", [])]
        apply_remote_artifacts(
            self.config,
            metadata,
            entries,
            transcript_content=_optional_text(artifacts.get("transcript_content")),
            structured_content=_optional_text(artifacts.get("structured_content")),
            on_progress=self.on_progress,
        )
        mark_remote_task_synced(
            self._remote_tasks_path(),
            remote_task_id=task_id,
            result_version=int(final_state.get("result_version") or 0),
        )
        self._emit_progress(
            "done",
            "远端导入已同步到本机。",
            session_id=session_id,
            task_id=task_id,
        )
        return 0

    def _ensure_remote_supports_imports(self) -> None:
        self._raise_if_cancelled()
        health = self.client.health()
        if bool(health.get("supports_imports")):
            return
        raise RemoteClientError(
            "远端服务版本过旧，不支持远端导入。"
            "先在本机重新执行 make deploy-remote ARGS='--host ...'，再重试导入。"
        )

    def _wait_until_complete(self, task_id: str) -> dict[str, Any]:
        while True:
            self._raise_if_cancelled(task_id)
            state = self.client.get_task(task_id)
            upsert_remote_task_payload(self._remote_tasks_path(), state)
            self._emit_remote_state(state)
            self._sync_snapshot_if_needed(state)
            status = str(state.get("status", "")).strip().lower()
            if status == "completed":
                return state
            if status == "cancelled":
                raise TaskCancelledError(str(state.get("message") or "导入任务已取消。"))
            if status == "failed":
                error = str(state.get("error") or state.get("message") or "远端导入失败。")
                raise RemoteClientError(error)
            if self.poll_interval_seconds:
                time.sleep(self.poll_interval_seconds)

    def _emit_remote_state(self, state: dict[str, Any]) -> None:
        signature = (
            state.get("status"),
            state.get("stage"),
            state.get("message"),
            state.get("session_id"),
            state.get("current"),
            state.get("total"),
            state.get("error"),
        )
        if signature == self._last_progress_signature:
            return
        self._last_progress_signature = signature
        self._emit_progress(
            str(state.get("stage") or state.get("status") or "remote_import"),
            str(state.get("message") or "远端导入处理中。"),
            session_id=_optional_string(state.get("session_id")),
            current=_optional_int(state.get("current")),
            total=_optional_int(state.get("total")),
            error=_optional_string(state.get("error")),
            task_id=_optional_string(state.get("task_id")),
        )

    def _sync_snapshot_if_needed(self, state: dict[str, Any]) -> None:
        session_id = _optional_string(state.get("session_id"))
        status = str(state.get("status") or "").strip().lower()
        if session_id is None or status != "running":
            return
        artifacts = self.client.get_artifacts(session_id)
        metadata = metadata_from_dict(dict(artifacts["metadata"]))
        entries = [entry_from_dict(dict(item)) for item in artifacts.get("entries", [])]
        signature = (
            metadata.session_id,
            metadata.status,
            len(entries),
            entries[-1].segment_id if entries else None,
            entries[-1].ended_ms if entries else None,
        )
        if signature == self._last_snapshot_signature:
            return
        self._last_snapshot_signature = signature
        sync_remote_transcript_snapshot(self.config, metadata, entries)

    def _raise_if_cancelled(self, task_id: str | None = None) -> None:
        if self.cancel_event is None or not self.cancel_event.is_set():
            return
        if task_id:
            try:
                self.client.cancel_task(task_id)
            except Exception:
                pass
        raise TaskCancelledError("导入任务已取消。")

    def _remote_tasks_path(self) -> Path:
        return (self.config.root_dir / ".live-note" / "remote_tasks.json").resolve()

    def _emit_progress(
        self,
        stage: str,
        message: str,
        *,
        session_id: str | None = None,
        current: int | None = None,
        total: int | None = None,
        error: str | None = None,
        task_id: str | None = None,
    ) -> None:
        if self.on_progress is None:
            return
        self.on_progress(
            ProgressEvent(
                stage=stage,
                message=message,
                session_id=session_id,
                current=current,
                total=total,
                error=error,
                task_id=task_id,
            )
        )


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)
