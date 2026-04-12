from __future__ import annotations

import queue
import threading

from live_note.audio.capture import AudioCaptureError, AudioCaptureService, resolve_input_device
from live_note.audio.segmentation import SpeechSegmenter
from live_note.obsidian.client import ObsidianClient
from live_note.runtime.domain.session_state import SessionStatus
from live_note.runtime.workflow_support import (
    FRAME_STOP,
    _emit_progress,
    _enqueue_with_retry,
    _mark_session_failed,
    _runtime_whisper_config,
)
from live_note.transcribe.whisper import WhisperInferenceClient, WhisperServerProcess

from .live_support import (
    accept_live_stop,
    begin_live_ingest,
    commit_local_postprocess_handoff,
    prepare_live_session,
)


def run_local_live_runner(runner) -> int:
    device = resolve_input_device(runner.source)
    context = prepare_live_session(
        runner.config,
        title=runner.title,
        kind=runner.kind,
        language=runner.language,
        source_label=device.name,
        source_ref=str(device.index),
    )
    runner.session_id = context.metadata.session_id
    workspace = context.workspace
    logger = context.logger
    handoff_pending = False
    try:
        obsidian = ObsidianClient(runner.config.obsidian)
        whisper_config = _runtime_whisper_config(runner.config.whisper, runner.language)
        whisper_client = WhisperInferenceClient(whisper_config)
        whisper_server = WhisperServerProcess(whisper_config, workspace.logs_txt)

        metadata = begin_live_ingest(
            runner.config,
            workspace,
            context.metadata,
            obsidian=obsidian,
            logger=logger,
            on_progress=runner.on_progress,
            starting_message=f"已创建会话：{context.metadata.title}",
            listening_message=f"正在监听输入设备：{device.name}",
        )

        frame_queue: queue.Queue[object] = queue.Queue(maxsize=runner.config.audio.queue_size)
        segment_queue: queue.Queue[object] = queue.Queue(maxsize=32)
        segmenter = SpeechSegmenter(runner.config.audio)
        capture = AudioCaptureService(runner.config.audio, device, frame_queue)
        if hasattr(capture, "set_level_callback"):
            capture.set_level_callback(runner._build_input_level_callback(metadata.session_id))

        segment_thread = threading.Thread(
            target=runner._segment_loop,
            name="segmenter",
            daemon=True,
            args=(frame_queue, segment_queue, segmenter, workspace),
        )
        transcribe_thread = threading.Thread(
            target=runner._transcribe_loop,
            name="transcriber",
            daemon=True,
            args=(segment_queue, workspace, metadata, obsidian, whisper_client),
        )
        capture_finished = False
        capture_announced = False

        with whisper_server:
            segment_thread.start()
            transcribe_thread.start()
            capture.start()
            try:
                while True:
                    runner._drain_control_commands(
                        capture=capture,
                        frame_queue=frame_queue,
                        workspace=workspace,
                        metadata=metadata,
                        logger=logger,
                    )
                    if runner._stop_event.is_set():
                        logger.info("收到停止请求，开始收尾。")
                        _emit_progress(
                            runner.on_progress,
                            "stopping",
                            "正在停止录音并收尾。",
                            session_id=metadata.session_id,
                        )
                        capture_finished = True
                        break
                    runner._raise_thread_error_if_any()
                    if runner._stop_event.is_set():
                        logger.info("收到停止请求，开始收尾。")
                        _emit_progress(
                            runner.on_progress,
                            "stopping",
                            "正在停止录音并收尾。",
                            session_id=metadata.session_id,
                        )
                        capture_finished = True
                        break
                    if capture.error:
                        raise AudioCaptureError(str(capture.error))
                    if not capture.is_alive:
                        raise AudioCaptureError("音频采集线程已停止。")
                    if runner._stop_event.wait(0.25):
                        logger.info("收到停止请求，开始收尾。")
                        _emit_progress(
                            runner.on_progress,
                            "stopping",
                            "正在停止录音并收尾。",
                            session_id=metadata.session_id,
                        )
                        capture_finished = True
                        break
            except KeyboardInterrupt:
                logger.info("收到停止信号，开始收尾。")
                capture_finished = True
                _emit_progress(
                    runner.on_progress,
                    "stopping",
                    "正在停止录音并收尾。",
                    session_id=metadata.session_id,
                )
            finally:
                capture.stop()
                capture.join(timeout=5)
                if capture_finished and not capture_announced:
                    metadata = accept_live_stop(
                        runner.config,
                        workspace,
                        on_progress=runner.on_progress,
                        session_id=metadata.session_id,
                    )
                    capture_announced = True
                    handoff_pending = True
                _enqueue_with_retry(frame_queue, FRAME_STOP)
                segment_thread.join()
                transcribe_thread.join()

        runner._raise_thread_error_if_any()
        if not handoff_pending:
            raise RuntimeError("实时会话未提交后台整理任务。")
        handoff = commit_local_postprocess_handoff(
            runner.config,
            metadata.session_id,
            speaker_enabled=runner.config.speaker.enabled,
            spool_path=str(workspace.live_ingest_pcm),
        )
        metadata = handoff.session.to_metadata()
        return 0
    except BaseException as exc:
        _mark_session_failed(
            workspace=workspace,
            obsidian=obsidian,
            logger=logger,
            label="实时会话",
            exc=exc,
            on_progress=runner.on_progress,
        )
        raise
