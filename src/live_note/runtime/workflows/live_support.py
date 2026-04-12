from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from live_note.config import AppConfig
from live_note.obsidian.client import ObsidianClient
from live_note.runtime.domain.session_state import SessionCommandKind, SessionStatus
from live_note.runtime.session_mutations import apply_workspace_session_command, create_workspace_session
from live_note.runtime.session_outputs import write_initial_transcript
from live_note.runtime.supervisors.runtime_host import RuntimeHost
from live_note.runtime.types import ProgressCallback
from live_note.runtime.workflow_support import _attach_console_logging, _emit_progress, create_session_metadata
from live_note.session_workspace import SessionWorkspace
from live_note.task_errors import TaskCancelledError

if TYPE_CHECKING:
    from live_note.domain import SessionMetadata


@dataclass(frozen=True, slots=True)
class LiveSessionContext:
    workspace: SessionWorkspace
    metadata: SessionMetadata
    logger: logging.Logger


def prepare_live_session(
    config: AppConfig,
    *,
    title: str,
    kind: str,
    language: str,
    source_label: str,
    source_ref: str,
    execution_target: str = "local",
) -> LiveSessionContext:
    metadata = create_session_metadata(
        config=config,
        title=title,
        kind=kind,
        language=language,
        input_mode="live",
        source_label=source_label,
        source_ref=source_ref,
    )
    if execution_target == "remote":
        metadata = replace(
            metadata,
            execution_target="remote",
            remote_session_id=metadata.session_id,
            speaker_status="pending" if config.speaker.enabled else "disabled",
        )
    workspace = create_workspace_session(config.root_dir, metadata)
    return LiveSessionContext(
        workspace=workspace,
        metadata=metadata,
        logger=workspace.session_logger(),
    )


def begin_live_ingest(
    config: AppConfig,
    workspace: SessionWorkspace,
    metadata: SessionMetadata,
    *,
    obsidian: ObsidianClient,
    logger: logging.Logger,
    on_progress: ProgressCallback | None,
    starting_message: str,
    listening_message: str,
) -> SessionMetadata:
    _attach_console_logging()
    write_initial_transcript(
        workspace,
        metadata,
        obsidian,
        logger,
        status=SessionStatus.INGESTING.value,
    )
    updated = apply_workspace_session_command(
        config.root_dir,
        workspace,
        SessionCommandKind.BEGIN_INGEST.value,
    )
    _emit_progress(
        on_progress,
        "starting",
        starting_message,
        session_id=updated.session_id,
    )
    _emit_progress(
        on_progress,
        "listening",
        listening_message,
        session_id=updated.session_id,
    )
    return updated


def mark_live_paused(
    config: AppConfig,
    workspace: SessionWorkspace,
    *,
    logger: logging.Logger,
    on_progress: ProgressCallback | None,
    session_id: str,
) -> None:
    apply_workspace_session_command(
        config.root_dir,
        workspace,
        SessionCommandKind.PAUSE_INGEST.value,
    )
    logger.info("录音已暂停。")
    _emit_progress(
        on_progress,
        "paused",
        "录音已暂停。",
        session_id=session_id,
    )


def mark_live_resumed(
    config: AppConfig,
    workspace: SessionWorkspace,
    *,
    logger: logging.Logger,
    on_progress: ProgressCallback | None,
    session_id: str,
) -> None:
    apply_workspace_session_command(
        config.root_dir,
        workspace,
        SessionCommandKind.RESUME_INGEST.value,
    )
    logger.info("录音已继续。")
    _emit_progress(
        on_progress,
        "listening",
        "已继续录音。",
        session_id=session_id,
    )


def accept_live_stop(
    config: AppConfig,
    workspace: SessionWorkspace,
    *,
    on_progress: ProgressCallback | None,
    session_id: str,
    message: str = "录音已停止，后台继续转写、精修和整理。",
) -> SessionMetadata:
    updated = apply_workspace_session_command(
        config.root_dir,
        workspace,
        SessionCommandKind.ACCEPT_STOP.value,
        payload={"status": "stop_requested"},
    )
    _emit_progress(
        on_progress,
        "capture_finished",
        message,
        session_id=session_id,
    )
    return updated


def commit_local_postprocess_handoff(
    config: AppConfig,
    session_id: str,
    *,
    speaker_enabled: bool,
    spool_path: str,
):
    return RuntimeHost.for_root(
        config.root_dir,
        cancelled_exceptions=(TaskCancelledError,),
        recoverable_actions={"postprocess"},
    ).commit_session_task_handoff(
        session_id=session_id,
        action="postprocess",
        label="后台整理",
        payload={
            "action": "postprocess",
            "session_id": session_id,
            "speaker_enabled": speaker_enabled,
            "recover_from_spool": False,
        },
        dedupe_key=f"postprocess:{session_id}",
        message="已提交后台整理任务。",
        event_payload={"spool_path": spool_path},
    )
