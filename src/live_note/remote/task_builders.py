from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from live_note.config import AppConfig
from live_note.domain import SessionMetadata
from live_note.llm import OpenAiCompatibleClient
from live_note.obsidian.client import ObsidianClient
from live_note.runtime.domain.task_state import TaskRecord
from live_note.runtime.session_mutations import require_runtime_session, update_workspace_session
from live_note.runtime.session_outputs import publish_failure_outputs, publish_final_outputs
from live_note.runtime.session_workflows import (
    finalize_session,
    postprocess_session,
    republish_session,
    retranscribe_session,
)
from live_note.runtime.task_runners import build_local_import_runner
from live_note.runtime.types import ProgressEvent
from live_note.runtime.workflow_support import _run_live_refinement
from live_note.session_workspace import SessionWorkspace, build_workspace

from .speaker import apply_speaker_labels


class TaskProgressRecorder(Protocol):
    def __call__(self, task_id: str, event: ProgressEvent) -> None: ...


class TaskCompletionRecorder(Protocol):
    def __call__(self, task_id: str, *, message: str, result_changed: bool = False) -> None: ...


def server_local_only_config(config: AppConfig) -> AppConfig:
    return replace(config, obsidian=replace(config.obsidian, enabled=False, api_key=None))


def _server_local_only_obsidian_client(config: AppConfig) -> ObsidianClient:
    return ObsidianClient(server_local_only_config(config).obsidian)


class RemoteTaskRunnerFactory:
    def __init__(
        self,
        config: AppConfig,
        *,
        record_progress: TaskProgressRecorder,
        mark_completed: TaskCompletionRecorder,
    ) -> None:
        self.config = config
        self._record_progress = record_progress
        self._mark_completed = mark_completed

    def build_task_runner(
        self,
        record,
        cancel_event: threading.Event | None,
    ):
        payload = dict(record.payload)
        action = str(payload.get("action") or record.action or "").strip().lower()
        if action == "import":
            uploaded_path = self._validated_uploaded_path(payload.get("uploaded_path"))
            if not uploaded_path.exists():
                raise FileNotFoundError(f"import 文件不存在：{uploaded_path}")
            title = payload.get("title")
            kind = str(payload.get("kind") or "generic")
            language = payload.get("language")
            speaker_enabled = payload.get("speaker_enabled")
            return self._build_import_runner(
                task_id=record.task_id,
                uploaded_path=uploaded_path,
                title=str(title) if title is not None else None,
                kind=kind,
                language=str(language) if language is not None else None,
                speaker_enabled=bool(speaker_enabled) if speaker_enabled is not None else None,
                cancel_event=cancel_event,
            )
        if action == "refine":
            session_id = str(payload.get("session_id") or record.session_id or "").strip()
            if not session_id:
                raise ValueError("refine 任务缺少 session_id。")
            return self._build_refine_runner(record.task_id, session_id)
        if action == "republish":
            session_id = str(payload.get("session_id") or record.session_id or "").strip()
            if not session_id:
                raise ValueError("republish 任务缺少 session_id。")
            return self._build_republish_runner(record.task_id, session_id)
        if action == "retranscribe":
            session_id = str(payload.get("session_id") or record.session_id or "").strip()
            if not session_id:
                raise ValueError("retranscribe 任务缺少 session_id。")
            return self._build_retranscribe_runner(record.task_id, session_id)
        if action == "finalize":
            session_id = str(payload.get("session_id") or record.session_id or "").strip()
            if not session_id:
                raise ValueError("finalize 任务缺少 session_id。")
            return self._build_finalize_runner(record.task_id, session_id)
        if action == "postprocess":
            session_id = str(payload.get("session_id") or record.session_id or "").strip()
            if not session_id:
                raise ValueError("postprocess 任务缺少 session_id。")
            speaker_enabled = payload.get("speaker_enabled")
            return self._build_postprocess_runner(
                record.task_id,
                session_id,
                speaker_enabled=bool(speaker_enabled) if speaker_enabled is not None else None,
            )
        raise ValueError(f"未知任务动作：{action or '<empty>'}")

    def run_task_record(
        self,
        record: TaskRecord,
        cancel_event: threading.Event | None,
    ) -> int:
        runner = self.build_task_runner(record, cancel_event)
        result = runner()
        if result is None:
            return 0
        return int(result)

    def _build_import_runner(
        self,
        *,
        task_id: str,
        uploaded_path: Path,
        title: str | None,
        kind: str,
        language: str | None,
        speaker_enabled: bool | None,
        cancel_event: threading.Event | None,
    ):
        def run() -> None:
            config = server_local_only_config(self.config)
            if speaker_enabled is not None:
                config = replace(
                    config,
                    speaker=replace(config.speaker, enabled=bool(speaker_enabled)),
                )
            runner = build_local_import_runner(
                config=config,
                file_path=str(uploaded_path),
                title=title,
                kind=kind,
                language=language,
                on_progress=lambda event: self._record_progress(task_id, event),
                cancel_event=cancel_event,
            )
            exit_code = runner.run()
            if exit_code != 0:
                raise RuntimeError(f"远端导入返回非零退出码: {exit_code}")

        return run

    def _build_refine_runner(self, task_id: str, session_id: str):
        def run() -> None:
            workspace = build_workspace(self.config.root_dir, session_id)
            metadata = require_runtime_session(self.config.root_dir, session_id)
            logger = workspace.session_logger()
            disabled_obsidian = _server_local_only_obsidian_client(self.config)

            def on_progress(event: ProgressEvent) -> None:
                self._record_progress(task_id, event)

            previous_source = metadata.transcript_source
            metadata = update_workspace_session(
                self.config.root_dir,
                workspace,
                event_kind="refine_started",
                refine_status="refining",
            )
            try:
                metadata = _run_live_refinement(
                    config=self.config,
                    workspace=workspace,
                    metadata=metadata,
                    logger=logger,
                    on_progress=on_progress,
                )
            except Exception as exc:
                update_workspace_session(
                    self.config.root_dir,
                    workspace,
                    event_kind="refine_failed",
                    transcript_source=previous_source,
                    refine_status="failed",
                )
                self._record_progress(
                    task_id,
                    ProgressEvent(
                        stage="error",
                        message=f"远端离线精修失败：{exc}",
                        session_id=session_id,
                        error=str(exc),
                    ),
                )
                raise
            metadata = apply_speaker_labels(
                self.config,
                workspace,
                metadata,
                on_progress=on_progress,
            )
            publish_final_outputs(
                workspace=workspace,
                metadata=metadata,
                obsidian=disabled_obsidian,
                llm_client=OpenAiCompatibleClient(self.config.llm),
                logger=logger,
                on_progress=on_progress,
            )
            self._mark_completed(
                task_id,
                message="远端离线精修已完成。",
                result_changed=True,
            )

        return run

    def _build_retranscribe_runner(self, task_id: str, session_id: str):
        def run() -> None:
            exit_code = retranscribe_session(
                server_local_only_config(self.config),
                session_id,
                on_progress=lambda event: self._record_progress(task_id, event),
            )
            if exit_code != 0:
                raise RuntimeError(f"远端重转写返回非零退出码: {exit_code}")
            self._mark_completed(
                task_id,
                message="远端重转写已完成。",
                result_changed=True,
            )

        return run

    def _build_republish_runner(self, task_id: str, session_id: str):
        def run() -> None:
            exit_code = republish_session(
                server_local_only_config(self.config),
                session_id,
                on_progress=lambda event: self._record_progress(task_id, event),
            )
            if exit_code != 0:
                raise RuntimeError(f"远端重新生成整理返回非零退出码: {exit_code}")
            self._mark_completed(
                task_id,
                message="远端重新生成整理已完成。",
                result_changed=True,
            )

        return run

    def _build_finalize_runner(self, task_id: str, session_id: str):
        def run() -> None:
            exit_code = finalize_session(
                server_local_only_config(self.config),
                session_id,
                on_progress=lambda event: self._record_progress(task_id, event),
            )
            if exit_code != 0:
                raise RuntimeError(f"远端补转写返回非零退出码: {exit_code}")
            self._mark_completed(
                task_id,
                message="远端补转写已完成。",
                result_changed=True,
            )

        return run

    def _build_postprocess_runner(
        self,
        task_id: str,
        session_id: str,
        *,
        speaker_enabled: bool | None = None,
    ):
        def run() -> None:
            workspace = build_workspace(self.config.root_dir, session_id)
            config = self.config
            if speaker_enabled is not None:
                config = replace(
                    config,
                    speaker=replace(config.speaker, enabled=bool(speaker_enabled)),
                )
            logger = workspace.session_logger()
            workflow_config = server_local_only_config(config)
            disabled_obsidian = ObsidianClient(workflow_config.obsidian)
            metadata = require_runtime_session(self.config.root_dir, session_id)
            if metadata.status == "failed":
                raise RuntimeError("远端实时会话失败，后台整理未启动。")
            recover_from_spool = self._should_recover_postprocess_from_spool(workspace, metadata)
            try:
                exit_code = postprocess_session(
                    workflow_config,
                    session_id,
                    on_progress=lambda event: self._record_progress(task_id, event),
                    speaker_enabled=speaker_enabled,
                    recover_from_spool=recover_from_spool,
                )
                if exit_code != 0:
                    raise RuntimeError(f"远端后台整理返回非零退出码: {exit_code}")
            except Exception as exc:
                publish_failure_outputs(
                    workspace=workspace,
                    metadata=metadata,
                    obsidian=disabled_obsidian,
                    logger=logger,
                    reason=str(exc),
                )
                self._record_progress(
                    task_id,
                    ProgressEvent(
                        stage="error",
                        message=f"远端后台整理失败：{exc}",
                        session_id=session_id,
                        error=str(exc),
                    ),
                )
                raise
            self._mark_completed(
                task_id,
                message="远端后台整理已完成。",
                result_changed=True,
            )

        return run

    def _should_recover_postprocess_from_spool(
        self,
        workspace: SessionWorkspace,
        metadata: SessionMetadata,
    ) -> bool:
        if metadata.input_mode != "live":
            return False
        if metadata.execution_target != "remote":
            return False
        return workspace.live_ingest_pcm.exists()

    def _validated_uploaded_path(self, uploaded_path: object) -> Path:
        uploaded_path_text = str(uploaded_path).strip() if uploaded_path is not None else ""
        if not uploaded_path_text:
            raise ValueError("import 任务缺少 uploaded_path。")
        try:
            candidate = Path(uploaded_path_text).expanduser().resolve(strict=False)
        except OSError as exc:
            raise ValueError(f"import uploaded_path 非法：{uploaded_path_text}") from exc
        uploads_root = self._uploads_root().resolve(strict=False)
        try:
            candidate.relative_to(uploads_root)
        except ValueError as exc:
            raise ValueError(f"import uploaded_path 超出 uploads root：{candidate}") from exc
        if candidate == uploads_root:
            raise ValueError("import uploaded_path 不能指向 uploads 根目录。")
        return candidate

    def _uploads_root(self) -> Path:
        return self.config.root_dir / ".live-note" / "remote-imports"
