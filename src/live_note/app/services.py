from __future__ import annotations

import importlib
import shutil
import subprocess
import threading
from pathlib import Path

from live_note.audio.capture import (
    InputDevice,
)
from live_note.audio.capture import (
    list_input_devices as capture_list_input_devices,
)
from live_note.config import AppConfig, load_config
from live_note.obsidian.client import ObsidianClient
from live_note.utils import iso_now

from ..remote.client import RemoteClient
from .coordinator import (
    FileImportCoordinator,
    SessionCoordinator,
    finalize_session,
    merge_sessions,
    refine_session,
    republish_session,
    retranscribe_session,
    sync_session_notes,
)
from .coordinator_factory_service import CoordinatorFactoryService
from .doctor_service import DoctorCheck, DoctorService
from .events import ProgressCallback
from .input_device_service import InputDeviceService
from .journal import SessionWorkspace
from .journal import list_sessions as iter_session_roots
from .path_opener_service import PathOpenerService
from .remote_coordinator import RemoteLiveCoordinator
from .remote_import import RemoteImportCoordinator
from .remote_sync import apply_remote_artifacts, sync_remote_transcript_snapshot
from .remote_task_service import RemoteTaskService, RemoteTaskSnapshot, RemoteTaskSummary
from .remote_tasks import (
    load_remote_tasks,
    mark_remote_task_synced,
    replace_remote_task_records,
    upsert_pending_remote_task,
    upsert_remote_task_payload,
)
from .session_action_service import SessionActionService
from .session_query_service import SessionQueryService, SessionSummary
from .settings_service import (
    SettingsDraft,
    SettingsService,
    _default_config,
    _whisper_model_sort_key,
)
from .task_dispatch_service import TaskDispatchService
from .task_queue import QueuedTaskRecord

__all__ = [
    "AppService",
    "DoctorCheck",
    "RemoteTaskSnapshot",
    "RemoteTaskSummary",
    "SessionSummary",
    "SettingsDraft",
    "_default_config",
    "_whisper_model_sort_key",
]


class AppService:
    def __init__(self, config_path: Path | None = None):
        self.config_path = (config_path or Path("config.toml")).resolve()
        self.env_path = (self.config_path.parent / ".env").resolve()

    def task_queue_path(self) -> Path:
        return (self.config_path.parent / ".live-note" / "task_queue.json").resolve()

    def remote_tasks_path(self) -> Path:
        return (self.config_path.parent / ".live-note" / "remote_tasks.json").resolve()

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

    def _session_query_service(self) -> SessionQueryService:
        return SessionQueryService(
            load_config=self.load_config,
            iter_session_roots=iter_session_roots,
            workspace_loader=SessionWorkspace,
        )

    def _coordinator_factory_service(self) -> CoordinatorFactoryService:
        return CoordinatorFactoryService(
            load_config=self.load_config,
            local_live_factory=SessionCoordinator,
            remote_live_factory=RemoteLiveCoordinator,
            local_import_factory=FileImportCoordinator,
            remote_import_factory=RemoteImportCoordinator,
        )

    def _remote_task_service(self) -> RemoteTaskService:
        return RemoteTaskService(
            load_config=self.load_config,
            remote_tasks_path=self.remote_tasks_path,
            load_remote_tasks=load_remote_tasks,
            replace_remote_task_records=replace_remote_task_records,
            mark_remote_task_synced=mark_remote_task_synced,
            upsert_remote_task_payload=upsert_remote_task_payload,
            remote_client_factory=RemoteClient,
            apply_remote_artifacts=apply_remote_artifacts,
            sync_remote_transcript_snapshot=sync_remote_transcript_snapshot,
            optional_text=_optional_text,
            now=_now,
        )

    def _session_action_service(self) -> SessionActionService:
        return SessionActionService(
            load_config=self.load_config,
            remote_tasks_path=self.remote_tasks_path,
            workspace_loader=SessionWorkspace,
            remote_client_factory=RemoteClient,
            upsert_pending_remote_task=upsert_pending_remote_task,
            upsert_remote_task_payload=upsert_remote_task_payload,
            merge_sessions=merge_sessions,
            republish_session=republish_session,
            sync_session_notes=sync_session_notes,
            retranscribe_session=retranscribe_session,
            refine_session=refine_session,
            finalize_session=finalize_session,
        )

    def _task_dispatch_service(self) -> TaskDispatchService:
        return TaskDispatchService(
            create_import_coordinator=self.create_import_coordinator,
            merge=self.merge,
            retranscribe=self.retranscribe,
            refine=self.refine,
            republish=self.republish,
            resync_notes=self.resync_notes,
        )

    def load_settings_draft(self) -> SettingsDraft:
        return self._settings_service().load_settings_draft()

    def detect_settings_draft(self) -> SettingsDraft:
        return self._settings_service().detect_settings_draft()

    def validate_settings(self, draft: SettingsDraft) -> list[str]:
        return self._settings_service().validate_settings(draft)

    def save_settings(self, draft: SettingsDraft) -> AppConfig:
        return self._settings_service().save_settings(draft)

    def doctor_checks(self) -> list[DoctorCheck]:
        return self._doctor_service().doctor_checks()

    def list_input_devices(self) -> list[InputDevice]:
        return self._input_device_service().list_input_devices()

    def list_session_summaries(self) -> list[SessionSummary]:
        return self._session_query_service().list_session_summaries()

    def list_remote_task_summaries(self) -> RemoteTaskSnapshot:
        return self._remote_task_service().list_remote_task_summaries()

    def create_live_coordinator(
        self,
        title: str,
        source: str,
        kind: str,
        language: str | None = None,
        on_progress: ProgressCallback | None = None,
        auto_refine_after_live: bool | None = None,
        speaker_enabled: bool | None = None,
    ) -> SessionCoordinator:
        return self._coordinator_factory_service().create_live_coordinator(
            title=title,
            source=source,
            kind=kind,
            language=language,
            on_progress=on_progress,
            auto_refine_after_live=auto_refine_after_live,
            speaker_enabled=speaker_enabled,
        )

    def create_import_coordinator(
        self,
        file_path: str,
        title: str | None,
        kind: str,
        language: str | None = None,
        on_progress: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
        speaker_enabled: bool | None = None,
    ) -> FileImportCoordinator | RemoteImportCoordinator:
        return self._coordinator_factory_service().create_import_coordinator(
            file_path=file_path,
            title=title,
            kind=kind,
            language=language,
            on_progress=on_progress,
            cancel_event=cancel_event,
            speaker_enabled=speaker_enabled,
        )

    def finalize(
        self,
        session_id: str,
        on_progress: ProgressCallback | None = None,
    ) -> int:
        return self._session_action_service().finalize(session_id, on_progress=on_progress)

    def retranscribe(
        self,
        session_id: str,
        on_progress: ProgressCallback | None = None,
    ) -> int:
        return self._session_action_service().retranscribe(session_id, on_progress=on_progress)

    def refine(
        self,
        session_id: str,
        on_progress: ProgressCallback | None = None,
    ) -> int:
        return self._session_action_service().refine(session_id, on_progress=on_progress)

    def merge(
        self,
        session_ids: list[str],
        title: str | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> int:
        return self._session_action_service().merge(
            session_ids,
            title=title,
            on_progress=on_progress,
        )

    def republish(
        self,
        session_id: str,
        on_progress: ProgressCallback | None = None,
    ) -> int:
        return self._session_action_service().republish(session_id, on_progress=on_progress)

    def resync_notes(
        self,
        session_id: str,
        on_progress: ProgressCallback | None = None,
    ) -> int:
        return self._session_action_service().resync_notes(session_id, on_progress=on_progress)

    def run_queue_task(
        self,
        record: QueuedTaskRecord,
        *,
        on_progress: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
    ) -> int:
        return self._task_dispatch_service().run_queue_task(
            record,
            on_progress=on_progress,
            cancel_event=cancel_event,
        )

    def open_path(self, path: Path) -> None:
        self._path_opener_service().open_path(path)

    def cancel_remote_task(self, task_id: str) -> dict[str, object]:
        return self._remote_task_service().cancel_remote_task(task_id)

    def sync_remote_task(self, task_id: str) -> dict[str, object]:
        return self._remote_task_service().sync_remote_task(task_id)


def _binary_exists(value: str) -> bool:
    if not value:
        return False
    return bool(shutil.which(value) or Path(value).expanduser().exists())


def _module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _now() -> str:
    return iso_now()


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    return str(value)
