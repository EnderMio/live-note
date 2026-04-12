from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from live_note.audio.capture import (
    InputDevice,
)
from live_note.audio.capture import (
    list_input_devices as capture_list_input_devices,
)
from live_note.config import AppConfig, load_config
from live_note.obsidian.client import ObsidianClient
from live_note.runtime import (
    LOCAL_RECOVERABLE_ACTIONS,
    RuntimeHost,
)
from live_note.runtime import (
    control_db_path as runtime_control_db_path,
)
from live_note.runtime.domain.remote_task_projection import RemoteTaskProjectionRecord
from live_note.runtime.remote_projection_target import reconcile_remote_projection_target
from live_note.runtime.domain.task_state import TaskRecord, TaskStatus
from live_note.runtime.remote_task_projections import (
    list_remote_task_projections,
    upsert_remote_task_projection_from_payload,
)
from live_note.runtime.remote_projection_sync import sync_single_remote_task
from live_note.runtime.session_mutations import require_runtime_session
from live_note.runtime.live_control import (
    LIVE_TASK_PAUSE_REQUESTED,
    LIVE_TASK_RESUME_REQUESTED,
    LIVE_TASK_STOP_REQUESTED,
    append_live_control_command,
)
from live_note.runtime.read_model import (
    SessionSummary,
    get_active_live_task as load_active_live_task,
)
from live_note.runtime.read_model import (
    get_live_task_control,
    list_session_summaries as load_session_summaries,
)
from live_note.runtime.store import LogRepo
from live_note.task_errors import TaskCancelledError

from ..remote.client import RemoteClient
from .doctor_service import DoctorCheck, DoctorService
from .input_device_service import InputDeviceService
from .path_opener_service import PathOpenerService
from .settings_service import (
    SettingsDraft,
    SettingsService,
    _default_config,
    _whisper_model_sort_key,
)

__all__ = [
    "AppService",
    "DoctorCheck",
    "LiveTaskSnapshot",
    "RemoteTaskSnapshot",
    "RemoteTaskSummary",
    "SessionSummary",
    "SettingsDraft",
    "_default_config",
    "_whisper_model_sort_key",
]

_LOCAL_SESSION_TASK_ACTIONS = frozenset(
    {
        "finalize",
        "retranscribe",
        "refine",
        "republish",
        "resync_notes",
    }
)
_REMOTE_SESSION_TASK_ACTIONS = frozenset(
    {
        "finalize",
        "retranscribe",
        "refine",
        "republish",
    }
)


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
    last_seen_at: str | None
    artifacts_synced_at: str | None


@dataclass(frozen=True, slots=True)
class RemoteTaskSnapshot:
    remote_available: bool
    availability_message: str | None
    tasks: list[RemoteTaskSummary]


@dataclass(frozen=True, slots=True)
class LiveTaskSnapshot:
    task_id: str
    label: str
    status: str
    stage: str
    message: str
    current: int | None
    total: int | None
    session_id: str | None
    is_paused: bool
    stop_requested: bool


class AppService:
    def __init__(self, config_path: Path | None = None):
        self.config_path = (config_path or Path("config.toml")).resolve()
        self.env_path = (self.config_path.parent / ".env").resolve()
        self._runtime_host_instance: RuntimeHost | None = None

    def runtime_control_db_path(self) -> Path:
        return runtime_control_db_path(self.config_path.parent)

    def runtime_host(self) -> RuntimeHost:
        host = self._runtime_host_instance
        if host is None:
            host = RuntimeHost.for_root(
                self.config_path.parent,
                cancelled_exceptions=(TaskCancelledError,),
                recoverable_actions=set(LOCAL_RECOVERABLE_ACTIONS),
            )
            self._runtime_host_instance = host
        return host

    def start_runtime(self) -> None:
        self.ensure_runtime_daemon()

    def get_active_live_task(self) -> LiveTaskSnapshot | None:
        task = load_active_live_task(self.runtime_host().db)
        if task is None:
            return None
        control = get_live_task_control(self.runtime_host().db, task.task_id)
        return LiveTaskSnapshot(
            task_id=task.task_id,
            label=task.label,
            status=task.status,
            stage=task.stage,
            message=task.message,
            current=task.current,
            total=task.total,
            session_id=task.session_id,
            is_paused=control.is_paused,
            stop_requested=control.stop_requested,
        )

    def request_live_task_stop(self, task_id: str) -> bool:
        return self._append_live_task_command(task_id, LIVE_TASK_STOP_REQUESTED)

    def request_live_task_pause(self, task_id: str) -> bool:
        return self._append_live_task_command(task_id, LIVE_TASK_PAUSE_REQUESTED)

    def request_live_task_resume(self, task_id: str) -> bool:
        return self._append_live_task_command(task_id, LIVE_TASK_RESUME_REQUESTED)

    def _append_live_task_command(self, task_id: str, kind: str) -> bool:
        state = get_live_task_control(self.runtime_host().db, task_id)
        if state.last_sequence <= 0:
            return False
        if kind == LIVE_TASK_STOP_REQUESTED:
            if state.stop_requested:
                return False
        elif kind == LIVE_TASK_PAUSE_REQUESTED:
            if state.stop_requested or state.is_paused:
                return False
        elif kind == LIVE_TASK_RESUME_REQUESTED:
            if state.stop_requested or not state.is_paused:
                return False
        append_live_control_command(
            LogRepo(self.runtime_host().db),
            task_id=task_id,
            kind=kind,
        )
        return True

    def _ensure_runtime_started(self) -> None:
        self.ensure_runtime_daemon()
        return None

    def ensure_runtime_daemon(self) -> None:
        if not self.config_exists():
            return
        pythonpath = os.environ.get("PYTHONPATH", "")
        src_path = str((self.config_path.parent / "src").resolve())
        merged_pythonpath = src_path if not pythonpath else f"{src_path}{os.pathsep}{pythonpath}"
        subprocess.Popen(
            [
                shutil.which("python3") or "python3",
                "-m",
                "live_note.runtime_daemon_main",
                "--config",
                str(self.config_path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(self.config_path.parent),
            start_new_session=True,
            env={**os.environ, "PYTHONPATH": merged_pythonpath},
        )

    def config_exists(self) -> bool:
        return self.config_path.exists()

    def load_config(self) -> AppConfig:
        return load_config(self.config_path, self.env_path)

    def _settings_service(self) -> SettingsService:
        return SettingsService(self.config_path, self.env_path)

    def _doctor_service(self) -> DoctorService:
        return DoctorService(
            self.config_path,
            self.env_path,
            settings_service=self._settings_service(),
            obsidian_client_factory=ObsidianClient,
            remote_client_factory=RemoteClient,
            module_available=_module_available,
            binary_exists=_binary_exists,
        )

    def _input_device_service(self) -> InputDeviceService:
        return InputDeviceService(list_input_devices=capture_list_input_devices)

    def _path_opener_service(self) -> PathOpenerService:
        return PathOpenerService(run=subprocess.run)

    def load_settings_draft(self) -> SettingsDraft:
        return self._settings_service().load_settings_draft()

    def detect_settings_draft(self) -> SettingsDraft:
        return self._settings_service().detect_settings_draft()

    def validate_settings(self, draft: SettingsDraft) -> list[str]:
        return self._settings_service().validate_settings(draft)

    def save_settings(self, draft: SettingsDraft) -> AppConfig:
        config = self._settings_service().save_settings(draft)
        self.ensure_runtime_daemon()
        return config

    def doctor_checks(self) -> list[DoctorCheck]:
        return self._doctor_service().doctor_checks()

    def list_input_devices(self) -> list[InputDevice]:
        return self._input_device_service().list_input_devices()

    def list_session_summaries(self) -> list[SessionSummary]:
        try:
            config = self.load_config()
        except Exception:
            return []
        if config.remote.enabled:
            reconcile_remote_projection_target(config.root_dir, config.remote.base_url)
        return load_session_summaries(config.root_dir)

    def list_remote_task_summaries(self) -> RemoteTaskSnapshot:
        try:
            config = self.load_config()
        except Exception as exc:
            return RemoteTaskSnapshot(
                remote_available=False,
                availability_message=str(exc),
                tasks=[],
            )

        if not config.remote.enabled:
            return RemoteTaskSnapshot(
                remote_available=False,
                availability_message="远端模式未启用。",
                tasks=_remote_task_summaries(list_remote_task_projections(config.root_dir)),
            )
        reconcile_remote_projection_target(config.root_dir, config.remote.base_url)
        synced = list_remote_task_projections(config.root_dir)
        return RemoteTaskSnapshot(
            remote_available=True,
            availability_message=None,
            tasks=_remote_task_summaries(synced),
        )

    def start_live_session(
        self,
        *,
        title: str,
        source: str,
        kind: str,
        language: str | None = None,
        auto_refine_after_live: bool | None = None,
        speaker_enabled: bool | None = None,
    ) -> str:
        task_id = _queue_task_id()
        self.start_live_task(
            task_id=task_id,
            label="实时录音",
            title=title,
            source=source,
            kind=kind,
            language=language,
            auto_refine_after_live=auto_refine_after_live,
            speaker_enabled=speaker_enabled,
        )
        return task_id

    def start_live_task(
        self,
        *,
        task_id: str,
        label: str,
        title: str,
        source: str,
        kind: str,
        language: str | None = None,
        auto_refine_after_live: bool | None = None,
        speaker_enabled: bool | None = None,
    ) -> None:
        self._ensure_runtime_started()
        record = self.runtime_host().task_supervisor.submit(
            task_id=task_id,
            action="live",
            label=label,
            payload={
                "title": title,
                "source": source,
                "kind": kind,
                "language": language,
                "auto_refine_after_live": auto_refine_after_live,
                "speaker_enabled": speaker_enabled,
            },
            dedupe_key="live",
            resource_keys=("live",),
            message="实时录音准备中。",
        )
        if record.task_id != task_id:
            raise RuntimeError("已有正在进行的实时录音任务。")

    def import_audio_file(
        self,
        *,
        file_path: str,
        title: str | None,
        kind: str,
        language: str | None = None,
        speaker_enabled: bool | None = None,
    ) -> str:
        config = self.load_config()
        if config.remote.enabled:
            self._ensure_runtime_started()
            return self._request_remote_import_task(
                config=config,
                file_path=file_path,
                title=title,
                kind=kind,
                language=language,
                speaker_enabled=speaker_enabled,
            )
        record = self.enqueue_queue_task(
            label="文件导入",
            action="import",
            payload={
                "file_path": file_path,
                "title": title,
                "kind": kind,
                "language": language,
                "speaker_enabled": speaker_enabled,
            },
        )
        if record is None:
            raise RuntimeError("相同文件导入任务已在队列中。")
        return record.task_id

        

    def finalize(
        self,
        session_id: str,
    ) -> str:
        return self._run_session_task(
            label="补转写并重写",
            action="finalize",
            session_id=session_id,
        )

    def retranscribe(
        self,
        session_id: str,
    ) -> str:
        return self._run_session_task(
            label="重转写并重写",
            action="retranscribe",
            session_id=session_id,
        )

    def refine(
        self,
        session_id: str,
    ) -> str:
        return self._run_session_task(
            label="离线精修并重写",
            action="refine",
            session_id=session_id,
        )

    def merge(
        self,
        session_ids: list[str],
        title: str | None = None,
    ) -> str:
        record, _created = self._submit_queue_task(
            label="合并会话",
            action="merge",
            payload={
                "session_ids": session_ids,
                "title": title,
            },
        )
        return record.task_id

    def republish(
        self,
        session_id: str,
    ) -> str:
        return self._run_session_task(
            label="重新生成整理",
            action="republish",
            session_id=session_id,
        )

    def resync_notes(
        self,
        session_id: str,
    ) -> str:
        return self._run_session_task(
            label="重新同步 Obsidian",
            action="resync_notes",
            session_id=session_id,
        )

    def list_queue_tasks(self) -> list[TaskRecord]:
        self._ensure_runtime_started()
        tasks = self.runtime_host().tasks.list_by_status(
            TaskStatus.QUEUED.value,
            TaskStatus.RUNNING.value,
        )
        return sorted(
            [item for item in tasks if item.action != "live"],
            key=lambda item: (
                0 if item.status == TaskStatus.RUNNING.value else 1,
                item.created_at,
                item.task_id,
            ),
        )

    def get_queue_task(self, task_id: str) -> TaskRecord | None:
        self._ensure_runtime_started()
        record = self.runtime_host().tasks.get(task_id)
        if record is None or record.action == "live":
            return None
        return record

    def enqueue_queue_task(
        self,
        *,
        label: str,
        action: str,
        payload: dict[str, object],
    ) -> TaskRecord | None:
        record, created = self._submit_queue_task(
            label=label,
            action=action,
            payload=payload,
        )
        if not created:
            return None
        return record

    def request_running_queue_import_cancel(self, task_id: str) -> bool:
        record = self.runtime_host().tasks.get(task_id)
        if record is None or record.action != "import":
            return False
        if record.status != TaskStatus.RUNNING.value:
            return False
        updated = self.runtime_host().task_supervisor.request_cancel(
            task_id,
            message="已请求取消任务。",
        )
        return updated is not None

    def cancel_queued_tasks(self, task_ids: set[str]) -> int:
        if not task_ids:
            return 0
        self._ensure_runtime_started()
        host = self.runtime_host()
        cancelled = 0
        for task_id in task_ids:
            record = host.task_supervisor.cancel_task(task_id)
            if record is not None and record.status == "cancelled":
                cancelled += 1
        return cancelled

    def open_path(self, path: Path) -> None:
        self._path_opener_service().open_path(path)

    def cancel_remote_task(self, task_id: str) -> dict[str, object]:
        config = self.load_config()
        if not config.remote.enabled:
            raise RuntimeError("远端模式未启用。")
        client = RemoteClient(config.remote)
        payload = client.cancel_task(task_id)
        upsert_remote_task_projection_from_payload(config.root_dir, payload)
        return payload

    def sync_remote_task(self, task_id: str) -> dict[str, object]:
        config = self.load_config()
        return sync_single_remote_task(config, task_id)

    def _run_session_task(
        self,
        *,
        label: str,
        action: str,
        session_id: str,
    ) -> str:
        config = self.load_config()
        metadata = require_runtime_session(config.root_dir, session_id)
        if metadata.execution_target == "remote" and action in _REMOTE_SESSION_TASK_ACTIONS:
            return self._request_remote_session_task(
                config=config,
                action=action,
                session_id=metadata.remote_session_id or metadata.session_id,
            )
        record, _created = self._submit_queue_task(
            label=label,
            action=action,
            payload={
                "session_id": session_id,
            },
        )
        return record.task_id

    def _request_remote_session_task(
        self,
        *,
        config: AppConfig,
        action: str,
        session_id: str,
    ) -> str:
        if not config.remote.enabled:
            raise RuntimeError("远端模式未启用。")
        client = RemoteClient(config.remote)
        request_id = f"{action}-{uuid4().hex[:12]}"
        if action == "finalize":
            payload = client.finalize_session(session_id, request_id=request_id)
        elif action == "retranscribe":
            payload = client.retranscribe_session(session_id, request_id=request_id)
        elif action == "refine":
            payload = client.refine_session(session_id, request_id=request_id)
        elif action == "republish":
            payload = client.republish_session(session_id, request_id=request_id)
        else:
            raise RuntimeError(f"未知远端会话任务：{action}")
        record = upsert_remote_task_projection_from_payload(config.root_dir, payload)
        return record.remote_task_id or record.request_id or request_id

    def _request_remote_import_task(
        self,
        *,
        config: AppConfig,
        file_path: str,
        title: str | None,
        kind: str,
        language: str | None,
        speaker_enabled: bool | None,
    ) -> str:
        if not config.remote.enabled:
            raise RuntimeError("远端模式未启用。")
        client = RemoteClient(config.remote)
        request_id = f"import-{uuid4().hex[:12]}"
        payload = client.create_import_task(
            file_path,
            title=title,
            kind=kind,
            language=language,
            speaker_enabled=(
                config.speaker.enabled if speaker_enabled is None else bool(speaker_enabled)
            ),
            request_id=request_id,
        )
        record = upsert_remote_task_projection_from_payload(config.root_dir, payload)
        return record.remote_task_id or record.request_id or request_id

    def _submit_queue_task(
        self,
        *,
        label: str,
        action: str,
        payload: dict[str, object],
    ) -> tuple[TaskRecord, bool]:
        if action == "import" and self.load_config().remote.enabled:
            raise RuntimeError("remote import 已改为直接远端任务，不能进入本地 queue。")
        self._ensure_runtime_started()
        host = self.runtime_host()
        dedupe_key = _queue_task_dedupe_key(action, payload)
        task_id = _queue_task_id()
        record = host.task_supervisor.submit(
            session_id=_queue_task_session_id(action, payload),
            action=action,
            label=label,
            payload=payload,
            dedupe_key=dedupe_key,
            can_cancel=action == "import",
            task_id=task_id,
        )
        return record, record.task_id == task_id


def _binary_exists(value: str) -> bool:
    if not value:
        return False
    return bool(shutil.which(value) or Path(value).expanduser().exists())
def _remote_task_summaries(records: list[RemoteTaskProjectionRecord]) -> list[RemoteTaskSummary]:
    def sort_key(record: RemoteTaskProjectionRecord) -> tuple[int, int, str]:
        is_lost = record.attachment_state == "lost"
        is_active = record.status in {"queued", "running"} and not is_lost
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
            status=record.status,
            stage=record.stage,
            message=record.message,
            current=record.current,
            total=record.total,
            updated_at=record.updated_at,
            attachment_state=record.attachment_state,
            can_cancel=record.can_cancel,
            result_version=record.result_version,
            last_synced_result_version=record.last_synced_result_version,
            last_error=record.last_error,
            last_seen_at=record.last_seen_at,
            artifacts_synced_at=record.artifacts_synced_at,
        )
        for record in sorted(records, key=sort_key, reverse=True)
    ]


def _module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None
def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)


def _queue_task_session_id(action: str, payload: dict[str, object]) -> str | None:
    if action not in _LOCAL_SESSION_TASK_ACTIONS and action != "postprocess":
        return None
    value = payload.get("session_id")
    return str(value) if value is not None else None


def _queue_task_dedupe_key(action: str, payload: dict[str, object]) -> str:
    normalized_action = action.strip()
    normalized_payload = _normalize_queue_payload(normalized_action, payload)
    return json.dumps(
        {"action": normalized_action, "payload": normalized_payload},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _normalize_queue_payload(action: str, payload: dict[str, object]) -> dict[str, Any]:
    normalized = {
        str(key): _normalize_queue_value(value)
        for key, value in payload.items()
    }
    if action == "import" and "file_path" in normalized:
        file_path = normalized["file_path"]
        if not isinstance(file_path, str):
            raise TypeError("import payload file_path must be a string")
        normalized["file_path"] = str(Path(file_path).expanduser().resolve())
    if action in _LOCAL_SESSION_TASK_ACTIONS or action == "postprocess":
        if "session_id" in normalized:
            value = normalized["session_id"]
            if not isinstance(value, str):
                raise TypeError("session_id must be a string")
            normalized["session_id"] = value.strip()
    if action == "merge":
        session_ids = normalized.get("session_ids", [])
        if not isinstance(session_ids, list):
            raise TypeError("merge payload session_ids must be a list")
        normalized["session_ids"] = sorted(
            {
                str(session_id).strip()
                for session_id in session_ids
                if str(session_id).strip()
            }
        )
    return normalized


def _normalize_queue_value(value: object) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _normalize_queue_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [_normalize_queue_value(item) for item in value]
    return str(value)


def _queue_task_id() -> str:
    return f"task-{uuid4().hex[:12]}"
