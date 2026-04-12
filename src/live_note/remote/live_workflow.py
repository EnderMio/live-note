from __future__ import annotations

import threading

from live_note.runtime.session_mutations import require_runtime_session
from live_note.runtime.workflow_support import _emit_progress, _mark_session_failed
from live_note.runtime.workflows.live_support import (
    accept_live_stop,
    begin_live_ingest,
    prepare_live_session,
)


def start_remote_live_session(runner):
    runner._backend_ready_event.clear()
    runner._startup_error = None
    context = prepare_live_session(
        runner.config,
        title=runner.request.title,
        kind=runner.request.kind,
        language=runner.language,
        source_label=runner.request.source_label,
        source_ref=runner.request.source_ref,
        execution_target="remote",
    )
    runner.session_id = context.metadata.session_id
    runner.metadata = context.metadata
    runner.workspace = context.workspace
    runner._thread = threading.Thread(
        target=runner.run,
        name=f"remote-live-{context.metadata.session_id}",
    )
    runner._thread.start()
    startup_timeout = max(int(runner.config.remote.timeout_seconds), 1)
    if not runner._backend_ready_event.wait(timeout=startup_timeout):
        runner.request_stop()
        runner.join(timeout=1)
        message = f"远端实时后端启动超时（{startup_timeout}s）。"
        runner.failure_message = message
        raise RuntimeError(message)
    if runner._startup_error:
        runner.request_stop()
        runner.join(timeout=1)
        runner.failure_message = runner._startup_error
        raise RuntimeError(runner._startup_error)
    return require_runtime_session(runner.config.root_dir, context.metadata.session_id)


def run_remote_live_session(runner) -> int:
    assert runner.workspace is not None
    assert runner.metadata is not None
    workspace = runner.workspace
    metadata = runner.metadata
    logger = workspace.session_logger()
    runner._spool_logger = logger
    disabled_obsidian = runner._disabled_obsidian_client()
    try:
        runner._initialize_ingest_spool()
        metadata = begin_live_ingest(
            runner.config,
            workspace,
            metadata,
            obsidian=disabled_obsidian,
            logger=logger,
            on_progress=runner.on_progress,
            starting_message=f"已连接远端会话：{metadata.title}",
            listening_message=f"正在接收远端音频：{metadata.source_label}",
        )
        if runner.config.funasr.enabled:
            runner._run_funasr_live_backend(workspace, metadata, logger)
        else:
            runner._run_whisper_live_backend(
                workspace,
                metadata,
                logger,
                disabled_obsidian,
            )

        runner._raise_thread_error_if_any()
        handoff_payload = runner.commit_postprocess_handoff()
        _emit_progress(
            runner.on_progress,
            "postprocess_queued",
            "后台整理已转为远端任务。",
            session_id=metadata.session_id,
        )
        if runner.on_event is not None:
            runner.on_event(
                {
                    "type": "handoff_committed",
                    "session_id": metadata.session_id,
                    "task_id": handoff_payload.get("task_id"),
                    "message": "后台整理任务已完成 durable handoff。",
                }
            )
        _emit_progress(
            runner.on_progress,
            "done",
            "远端实时 ingest 已完成，后台整理改由任务运行时继续。",
            session_id=metadata.session_id,
        )
        return 0
    except BaseException as exc:
        runner._mark_backend_startup_failed(exc)
        runner.failure_message = str(exc)
        _mark_session_failed(
            workspace=workspace,
            obsidian=disabled_obsidian,
            logger=logger,
            label="远端实时会话",
            exc=exc,
            on_progress=runner.on_progress,
        )
        raise
    finally:
        runner._seal_ingest_spool()
        runner._close_ingest_spool()
        runner._maybe_log_spool_stats(force=True)


def accept_remote_live_stop(runner, workspace, metadata) -> None:
    accept_live_stop(
        runner.config,
        workspace,
        on_progress=runner.on_progress,
        session_id=metadata.session_id,
    )
    if runner.on_event is not None:
        runner.on_event(
            {
                "type": "stop_accepted",
                "session_id": metadata.session_id,
                "message": "远端已接受停止请求，正在收尾当前片段。",
            }
        )
