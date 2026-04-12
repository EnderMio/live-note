from __future__ import annotations

from live_note.audio.convert import AudioImportError, convert_audio_to_wav, split_wav_file
from live_note.llm import OpenAiCompatibleClient
from live_note.obsidian.client import ObsidianClient
from live_note.runtime.domain.session_state import SessionStatus
from live_note.runtime.session_mutations import create_workspace_session
from live_note.runtime.session_outputs import publish_final_outputs, write_initial_transcript
from live_note.runtime.workflow_support import (
    _attach_console_logging,
    _emit_progress,
    _mark_session_failed,
    _process_segment,
    _runtime_whisper_config,
    create_session_metadata,
)
from live_note.task_errors import TaskCancelledError
from live_note.transcribe.whisper import WhisperInferenceClient, WhisperServerProcess


def run_local_import_runner(runner) -> int:
    metadata = create_session_metadata(
        config=runner.config,
        title=runner.title,
        kind=runner.kind,
        language=runner.language,
        input_mode="file",
        source_label=runner.file_path.name,
        source_ref=str(runner.file_path),
    )
    workspace = create_workspace_session(runner.config.root_dir, metadata)
    logger = workspace.session_logger()
    try:
        _attach_console_logging()
        runner._raise_if_cancelled(workspace, logger)
        _emit_progress(
            runner.on_progress,
            "starting",
            f"已创建导入会话：{metadata.title}",
            session_id=metadata.session_id,
        )

        obsidian = ObsidianClient(runner.config.obsidian)
        llm_client = OpenAiCompatibleClient(runner.config.llm)
        whisper_config = _runtime_whisper_config(runner.config.whisper, runner.language)
        whisper_client = WhisperInferenceClient(whisper_config)

        write_initial_transcript(
            workspace,
            metadata,
            obsidian,
            logger,
            status=SessionStatus.STARTING.value,
        )
        _emit_progress(
            runner.on_progress,
            "normalizing",
            f"正在转换媒体文件：{runner.file_path.name}",
            session_id=metadata.session_id,
        )
        runner._raise_if_cancelled(workspace, logger, metadata.session_id)

        normalized_path = workspace.root / "source.normalized.wav"
        try:
            convert_audio_to_wav(
                input_path=runner.file_path,
                output_path=normalized_path,
                sample_rate=runner.config.audio.sample_rate,
                ffmpeg_binary=runner.config.importer.ffmpeg_binary,
            )
            _emit_progress(
                runner.on_progress,
                "chunking",
                "正在切分音频片段。",
                session_id=metadata.session_id,
            )
            runner._raise_if_cancelled(workspace, logger, metadata.session_id)
            chunks = split_wav_file(
                input_path=normalized_path,
                output_dir=workspace.segments_dir,
                chunk_seconds=runner._import_chunk_seconds(),
            )
            if not chunks:
                raise AudioImportError("转换后的音频为空，无法转写。")
            for chunk in chunks:
                workspace.record_segment_created(
                    chunk.segment_id,
                    chunk.started_ms,
                    chunk.ended_ms,
                    chunk.wav_path,
                )

            with WhisperServerProcess(whisper_config, workspace.logs_txt):
                for index, chunk in enumerate(chunks, start=1):
                    runner._raise_if_cancelled(workspace, logger, metadata.session_id)
                    _emit_progress(
                        runner.on_progress,
                        "transcribing",
                        f"正在转写片段 {index}/{len(chunks)}",
                        session_id=metadata.session_id,
                        current=index,
                        total=len(chunks),
                    )
                    success = _process_segment(
                        pending=runner._chunk_to_pending_segment(chunk),
                        workspace=workspace,
                        metadata=metadata,
                        obsidian=obsidian,
                        whisper_client=whisper_client,
                        logger=logger,
                        entries=runner.entries,
                        live_status=SessionStatus.STARTING.value,
                        sample_rate=runner.config.audio.sample_rate,
                        guard_prompt_admission=True,
                    )
                    if not success:
                        _emit_progress(
                            runner.on_progress,
                            "segment_failed",
                            f"片段 {chunk.segment_id} 处理失败",
                            session_id=metadata.session_id,
                            current=index,
                            total=len(chunks),
                        )
            runner._raise_if_cancelled(workspace, logger, metadata.session_id)
            metadata = runner._apply_speaker_labels(workspace, metadata, normalized_path, logger)
            runner._raise_if_cancelled(workspace, logger, metadata.session_id)
            publish_final_outputs(
                workspace,
                metadata,
                obsidian,
                llm_client,
                logger,
                on_progress=runner.on_progress,
            )
            _emit_progress(
                runner.on_progress,
                "done",
                "导入会话已完成。",
                session_id=metadata.session_id,
            )
            return 0
        finally:
            if normalized_path.exists() and not runner.config.importer.keep_normalized_audio:
                normalized_path.unlink()
    except TaskCancelledError:
        raise
    except BaseException as exc:
        _mark_session_failed(
            workspace=workspace,
            obsidian=obsidian,
            logger=logger,
            label="导入会话",
            exc=exc,
            on_progress=runner.on_progress,
        )
        raise
