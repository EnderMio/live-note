from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable, Iterable
from pathlib import Path

from live_note.audio.capture import (
    AudioCaptureService,
    InputLevel,
    describe_input_level,
)
from live_note.audio.segmentation import SegmentWindow, SpeechSegmenter
from live_note.config import AppConfig, with_refine_auto_after_live
from live_note.domain import AudioFrame, PendingSegment, SessionMetadata, TranscriptEntry
from live_note.obsidian.client import ObsidianClient
from live_note.runtime import append_audio_frame
from live_note.runtime.domain.session_state import SessionCommandKind, SessionStatus
from live_note.runtime.session_mutations import (
    apply_workspace_session_command,
    update_workspace_session,
)
from live_note.runtime.types import ProgressCallback
from live_note.runtime.workflow_support import (
    FRAME_PAUSE,
    FRAME_RESUME,
    FRAME_STOP,
    SEGMENT_CONTEXT_RESET,
    SEGMENT_STOP,
    _emit_progress,
    _enqueue_with_retry,
    _open_session_audio_writer,
    _persist_live_segment,
    _process_segment,
)
from live_note.runtime.workflows import run_local_import_runner, run_local_live_runner
from live_note.session_workspace import SessionWorkspace, workspace_root_dir
from live_note.task_errors import TaskCancelledError
from live_note.transcribe.whisper import WhisperInferenceClient, WhisperServerProcess


def apply_speaker_labels(*args, **kwargs):
    from live_note.remote.speaker import apply_speaker_labels as _apply_speaker_labels

    return _apply_speaker_labels(*args, **kwargs)


def _import_chunk_seconds(config: AppConfig) -> int:
    if config.speaker.enabled:
        return min(config.importer.chunk_seconds, 15)
    return config.importer.chunk_seconds


class LocalLiveRunner:
    def __init__(
        self,
        config: AppConfig,
        title: str,
        source: str,
        kind: str,
        language: str | None = None,
        on_progress: ProgressCallback | None = None,
        auto_refine_after_live: bool | None = None,
    ):
        self.config = with_refine_auto_after_live(config, auto_refine_after_live)
        self.title = title
        self.source = source
        self.kind = kind
        self.language = language or self.config.whisper.language
        self.on_progress = on_progress
        self.entries: list[TranscriptEntry] = []
        self._thread_errors: queue.Queue[BaseException] = queue.Queue()
        self._stop_event = threading.Event()
        self._control_commands: queue.Queue[str] = queue.Queue()
        self._pause_requested = False
        self.session_id: str | None = None

    def request_stop(self) -> None:
        self._stop_event.set()

    def request_pause(self) -> None:
        if self._pause_requested:
            return
        self._pause_requested = True
        self._control_commands.put("pause")

    def request_resume(self) -> None:
        if not self._pause_requested:
            return
        self._pause_requested = False
        self._control_commands.put("resume")

    @property
    def is_paused(self) -> bool:
        return self._pause_requested

    def _build_input_level_callback(self, session_id: str) -> Callable[[InputLevel], None]:
        def callback(level: InputLevel) -> None:
            _emit_progress(
                self.on_progress,
                "input_level",
                describe_input_level(level),
                session_id=session_id,
                current=max(0, min(100, round(level.normalized * 100))),
                total=100,
            )

        return callback

    def run(self) -> int:
        return run_local_live_runner(self)

    def _drain_control_commands(
        self,
        *,
        capture: AudioCaptureService,
        frame_queue: queue.Queue[AudioFrame | object],
        workspace: SessionWorkspace,
        metadata: SessionMetadata,
        logger: logging.Logger,
    ) -> None:
        while True:
            try:
                command = self._control_commands.get_nowait()
            except queue.Empty:
                return

            if command == "pause":
                if capture.is_paused:
                    continue
                capture.pause()
                _enqueue_with_retry(frame_queue, FRAME_PAUSE)
                from live_note.runtime.workflows.live_support import mark_live_paused

                mark_live_paused(
                    self.config,
                    workspace,
                    logger=logger,
                    on_progress=self.on_progress,
                    session_id=metadata.session_id,
                )
                continue

            if command == "resume":
                if not capture.is_paused:
                    continue
                _enqueue_with_retry(frame_queue, FRAME_RESUME)
                capture.resume()
                from live_note.runtime.workflows.live_support import mark_live_resumed

                mark_live_resumed(
                    self.config,
                    workspace,
                    logger=logger,
                    on_progress=self.on_progress,
                    session_id=metadata.session_id,
                )

    def _segment_loop(
        self,
        frame_queue: queue.Queue[AudioFrame | object],
        segment_queue: queue.Queue[PendingSegment | object],
        segmenter: SpeechSegmenter,
        workspace: SessionWorkspace,
    ) -> None:
        try:
            with _open_session_audio_writer(
                workspace.session_live_wav,
                self.config.audio.sample_rate,
                enabled=self.config.audio.save_session_wav,
            ) as session_audio:
                counter = 0

                def emit_segments(segments: Iterable[SegmentWindow]) -> None:
                    nonlocal counter
                    for segment in segments:
                        counter += 1
                        pending = _persist_live_segment(
                            counter=counter,
                            segment=segment,
                            workspace=workspace,
                            sample_rate=self.config.audio.sample_rate,
                        )
                        _emit_progress(
                            self.on_progress,
                            "segment_created",
                            f"已切出片段 {pending.segment_id}",
                            current=counter,
                        )
                        _enqueue_with_retry(segment_queue, pending)

                while True:
                    item = frame_queue.get()
                    if item is FRAME_STOP:
                        break
                    if item is FRAME_PAUSE:
                        emit_segments(segmenter.flush())
                        _enqueue_with_retry(segment_queue, SEGMENT_CONTEXT_RESET)
                        continue
                    if item is FRAME_RESUME:
                        continue
                    assert isinstance(item, AudioFrame)
                    append_audio_frame(workspace.root, item)
                    if session_audio is not None:
                        session_audio.write(item.pcm16)
                    emit_segments(segmenter.feed(item))
                emit_segments(segmenter.flush())
        except BaseException as exc:
            self._thread_errors.put(exc)
            _emit_progress(
                self.on_progress,
                "error",
                f"分段线程失败：{exc}",
                error=str(exc),
            )
        finally:
            _enqueue_with_retry(segment_queue, SEGMENT_STOP)

    def _transcribe_loop(
        self,
        segment_queue: queue.Queue[PendingSegment | object],
        workspace: SessionWorkspace,
        metadata: SessionMetadata,
        obsidian: ObsidianClient,
        whisper_client: WhisperInferenceClient,
    ) -> None:
        logger = workspace.session_logger()
        prompt_entries: list[TranscriptEntry] = []
        try:
            while True:
                item = segment_queue.get()
                if item is SEGMENT_STOP:
                    break
                if item is SEGMENT_CONTEXT_RESET:
                    prompt_entries.clear()
                    continue
                assert isinstance(item, PendingSegment)
                success = _process_segment(
                    pending=item,
                    workspace=workspace,
                    metadata=metadata,
                    obsidian=obsidian,
                    whisper_client=whisper_client,
                    logger=logger,
                    entries=self.entries,
                    context_entries=prompt_entries,
                    live_status=SessionStatus.INGESTING.value,
                    sample_rate=self.config.audio.sample_rate,
                )
                if success:
                    _emit_progress(
                        self.on_progress,
                        "segment_transcribed",
                        f"片段 {item.segment_id} 已转写",
                        session_id=metadata.session_id,
                        current=len(self.entries),
                    )
                else:
                    _emit_progress(
                        self.on_progress,
                        "segment_failed",
                        f"片段 {item.segment_id} 处理失败",
                        session_id=metadata.session_id,
                    )
        except BaseException as exc:
            self._thread_errors.put(exc)
            _emit_progress(
                self.on_progress,
                "error",
                f"转写线程失败：{exc}",
                session_id=metadata.session_id,
                error=str(exc),
            )

    def _raise_thread_error_if_any(self) -> None:
        try:
            error = self._thread_errors.get_nowait()
        except queue.Empty:
            return
        raise RuntimeError(str(error)) from error


class LocalImportRunner:
    def __init__(
        self,
        config: AppConfig,
        file_path: str,
        title: str | None,
        kind: str,
        language: str | None = None,
        on_progress: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
    ):
        self.config = config
        self.file_path = Path(file_path).expanduser().resolve()
        self.title = title or self.file_path.stem
        self.kind = kind
        self.language = language or config.whisper.language
        self.on_progress = on_progress
        self.cancel_event = cancel_event
        self.entries: list[TranscriptEntry] = []

    def run(self) -> int:
        return run_local_import_runner(self)

    def _import_chunk_seconds(self) -> int:
        return _import_chunk_seconds(self.config)

    def _chunk_to_pending_segment(self, chunk) -> PendingSegment:
        return PendingSegment(
            segment_id=chunk.segment_id,
            started_ms=chunk.started_ms,
            ended_ms=chunk.ended_ms,
            pcm16=None,
            wav_path=chunk.wav_path,
        )

    def _apply_speaker_labels(
        self,
        workspace: SessionWorkspace,
        metadata: SessionMetadata,
        normalized_path: Path,
        logger: logging.Logger,
    ) -> SessionMetadata:
        return apply_speaker_labels(
            self.config,
            workspace,
            metadata,
            audio_path=normalized_path,
            on_progress=self.on_progress,
            cancel_callback=lambda: self._raise_if_cancelled(
                workspace,
                logger,
                metadata.session_id,
            ),
        )

    def _raise_if_cancelled(
        self,
        workspace: SessionWorkspace,
        logger: logging.Logger,
        session_id: str | None = None,
    ) -> None:
        if self.cancel_event is None or not self.cancel_event.is_set():
            return
        apply_workspace_session_command(
            self.config.root_dir,
            workspace,
            SessionCommandKind.ABANDON.value,
            payload={"reason": "导入任务已取消。"},
        )
        logger.info("导入会话已取消。")
        _emit_progress(
            self.on_progress,
            "cancelled",
            "导入任务已取消。",
            session_id=session_id,
        )
        raise TaskCancelledError("导入任务已取消。")
