from __future__ import annotations

import logging
import queue
import shutil
import threading
import wave
from collections.abc import Iterable
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from live_note.audio.capture import (
    AudioCaptureError,
    AudioCaptureService,
    resolve_input_device,
)
from live_note.audio.convert import (
    AudioImportError,
    convert_audio_to_wav,
    split_wav_file,
)
from live_note.audio.segmentation import SegmentWindow, SpeechSegmenter
from live_note.config import AppConfig, with_refine_auto_after_live
from live_note.domain import (
    AudioFrame,
    PendingSegment,
    SegmentState,
    SessionMetadata,
    TranscriptEntry,
)
from live_note.llm import OpenAiCompatibleClient
from live_note.obsidian.client import ObsidianClient
from live_note.obsidian.renderer import build_transcript_note
from live_note.transcribe.text import build_transcription_prompt, normalize_transcript_text
from live_note.transcribe.whisper import (
    WhisperError,
    WhisperInferenceClient,
    WhisperServerProcess,
    with_language_override,
    with_runtime_port,
)
from live_note.utils import iso_now, slugify_filename

from .events import ProgressCallback, ProgressEvent
from .journal import SessionWorkspace, build_workspace, session_root
from .session_outputs import (
    publish_final_outputs,
    try_sync_note,
    write_initial_transcript,
)
from .task_errors import TaskCancelledError

FRAME_STOP = object()
FRAME_PAUSE = object()
FRAME_RESUME = object()
SEGMENT_STOP = object()
SEGMENT_CONTEXT_RESET = object()


def apply_speaker_labels(*args, **kwargs):
    from live_note.remote.speaker import apply_speaker_labels as _apply_speaker_labels

    return _apply_speaker_labels(*args, **kwargs)


def _import_chunk_seconds(config: AppConfig) -> int:
    if config.speaker.enabled:
        return min(config.importer.chunk_seconds, 15)
    return config.importer.chunk_seconds


@dataclass(frozen=True, slots=True)
class MergeSourceSession:
    workspace: SessionWorkspace
    metadata: SessionMetadata
    states: list[SegmentState]
    duration_ms: int


class SessionAudioWriter:
    def __init__(self, path: Path, sample_rate: int):
        self.path = path
        self.sample_rate = sample_rate
        self._handle: wave.Wave_write | None = None

    def __enter__(self) -> SessionAudioWriter:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = wave.open(str(self.path), "wb")
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(self.sample_rate)
        self._handle = handle
        return self

    def write(self, pcm16: bytes) -> None:
        if self._handle is None:
            raise RuntimeError("SessionAudioWriter 尚未打开。")
        self._handle.writeframes(pcm16)

    def close(self) -> None:
        if self._handle is None:
            return
        self._handle.close()
        self._handle = None

    def __exit__(self, exc_type, exc, exc_tb) -> None:
        self.close()
        return None


def create_session_metadata(
    config: AppConfig,
    title: str,
    kind: str,
    language: str,
    input_mode: str,
    source_label: str,
    source_ref: str,
) -> SessionMetadata:
    now = datetime.now(UTC)
    session_id = f"{now.strftime('%Y%m%d-%H%M%S')}-{slugify_filename(title)}"
    date_fragment = now.strftime("%Y-%m-%d")
    file_stem = f"{slugify_filename(title)}-{now.strftime('%H%M%S')}"
    workspace_dir = session_root(config.root_dir) / session_id
    transcript_note_path = f"{config.obsidian.transcript_dir}/{date_fragment}/{file_stem}.md"
    structured_note_path = f"{config.obsidian.structured_dir}/{date_fragment}/{file_stem}.md"
    if input_mode == "live":
        transcript_source = "live"
        refine_status = "pending" if config.refine.enabled else "disabled"
    else:
        transcript_source = "refined"
        refine_status = "disabled"
    return SessionMetadata(
        session_id=session_id,
        title=title,
        kind=kind,
        input_mode=input_mode,
        source_label=source_label,
        source_ref=source_ref,
        language=language,
        started_at=iso_now(),
        transcript_note_path=transcript_note_path,
        structured_note_path=structured_note_path,
        session_dir=str(workspace_dir),
        status="starting",
        transcript_source=transcript_source,
        refine_status=refine_status,
    )


class SessionCoordinator:
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

    def run(self) -> int:
        device = resolve_input_device(self.source)
        metadata = create_session_metadata(
            config=self.config,
            title=self.title,
            kind=self.kind,
            language=self.language,
            input_mode="live",
            source_label=device.name,
            source_ref=str(device.index),
        )
        self.session_id = metadata.session_id
        workspace = SessionWorkspace.create(Path(metadata.session_dir), metadata)
        logger = workspace.session_logger()
        try:
            _attach_console_logging()
            _emit_progress(
                self.on_progress,
                "starting",
                f"已创建会话：{metadata.title}",
                session_id=metadata.session_id,
            )

            obsidian = ObsidianClient(self.config.obsidian)
            llm_client = OpenAiCompatibleClient(self.config.llm)
            whisper_config = _runtime_whisper_config(self.config.whisper, self.language)
            whisper_client = WhisperInferenceClient(whisper_config)
            whisper_server = WhisperServerProcess(whisper_config, workspace.logs_txt)

            write_initial_transcript(workspace, metadata, obsidian, logger, status="live")
            metadata = workspace.update_status("live")
            _emit_progress(
                self.on_progress,
                "listening",
                f"正在监听输入设备：{device.name}",
                session_id=metadata.session_id,
            )

            frame_queue: queue.Queue[AudioFrame | object] = queue.Queue(
                maxsize=self.config.audio.queue_size
            )
            segment_queue: queue.Queue[PendingSegment | object] = queue.Queue(maxsize=32)
            segmenter = SpeechSegmenter(self.config.audio)
            capture = AudioCaptureService(self.config.audio, device, frame_queue)

            segment_thread = threading.Thread(
                target=self._segment_loop,
                name="segmenter",
                daemon=True,
                args=(frame_queue, segment_queue, segmenter, workspace),
            )
            transcribe_thread = threading.Thread(
                target=self._transcribe_loop,
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
                        self._drain_control_commands(
                            capture=capture,
                            frame_queue=frame_queue,
                            workspace=workspace,
                            metadata=metadata,
                            logger=logger,
                        )
                        if self._stop_event.is_set():
                            logger.info("收到停止请求，开始收尾。")
                            _emit_progress(
                                self.on_progress,
                                "stopping",
                                "正在停止录音并收尾。",
                                session_id=metadata.session_id,
                            )
                            capture_finished = True
                            break
                        self._raise_thread_error_if_any()
                        if self._stop_event.is_set():
                            logger.info("收到停止请求，开始收尾。")
                            _emit_progress(
                                self.on_progress,
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
                        if self._stop_event.wait(0.25):
                            logger.info("收到停止请求，开始收尾。")
                            _emit_progress(
                                self.on_progress,
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
                        self.on_progress,
                        "stopping",
                        "正在停止录音并收尾。",
                        session_id=metadata.session_id,
                    )
                finally:
                    capture.stop()
                    capture.join(timeout=5)
                    if capture_finished and not capture_announced:
                        metadata = workspace.update_status("finalizing")
                        _emit_progress(
                            self.on_progress,
                            "capture_finished",
                            "录音已停止，后台继续转写、精修和整理。",
                            session_id=metadata.session_id,
                        )
                        capture_announced = True
                    _enqueue_with_retry(frame_queue, FRAME_STOP)
                    segment_thread.join()
                    transcribe_thread.join()

            self._raise_thread_error_if_any()
            if self.config.refine.enabled and self.config.refine.auto_after_live:
                previous_source = metadata.transcript_source
                try:
                    metadata = _run_live_refinement(
                        config=self.config,
                        workspace=workspace,
                        metadata=workspace.update_session(
                            status="refining",
                            refine_status="refining",
                        ),
                        logger=logger,
                        on_progress=self.on_progress,
                    )
                except Exception as exc:
                    logger.error("自动离线精修失败，将保留实时草稿: %s", exc)
                    _emit_progress(
                        self.on_progress,
                        "error",
                        f"自动离线精修失败：{exc}",
                        session_id=metadata.session_id,
                        error=str(exc),
                    )
                    metadata = workspace.update_session(
                        transcript_source=previous_source,
                        refine_status="failed",
                    )
            if self.config.speaker.enabled:
                metadata = apply_speaker_labels(
                    self.config,
                    workspace,
                    metadata,
                    on_progress=self.on_progress,
                )
            publish_final_outputs(
                workspace,
                metadata,
                obsidian,
                llm_client,
                logger,
                on_progress=self.on_progress,
            )
            _emit_progress(
                self.on_progress,
                "done",
                "会话已完成。",
                session_id=metadata.session_id,
            )
            return 0
        except BaseException as exc:
            _mark_session_failed(
                workspace=workspace,
                logger=logger,
                label="实时会话",
                exc=exc,
                on_progress=self.on_progress,
            )
            raise

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
                workspace.update_status("paused")
                logger.info("录音已暂停。")
                _emit_progress(
                    self.on_progress,
                    "paused",
                    "录音已暂停。",
                    session_id=metadata.session_id,
                )
                continue

            if command == "resume":
                if not capture.is_paused:
                    continue
                _enqueue_with_retry(frame_queue, FRAME_RESUME)
                capture.resume()
                workspace.update_status("live")
                logger.info("录音已继续。")
                _emit_progress(
                    self.on_progress,
                    "listening",
                    "已继续录音。",
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
                    live_status="live",
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


class FileImportCoordinator:
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
        metadata = create_session_metadata(
            config=self.config,
            title=self.title,
            kind=self.kind,
            language=self.language,
            input_mode="file",
            source_label=self.file_path.name,
            source_ref=str(self.file_path),
        )
        workspace = SessionWorkspace.create(Path(metadata.session_dir), metadata)
        logger = workspace.session_logger()
        try:
            _attach_console_logging()
            self._raise_if_cancelled(workspace, logger)
            _emit_progress(
                self.on_progress,
                "starting",
                f"已创建导入会话：{metadata.title}",
                session_id=metadata.session_id,
            )

            obsidian = ObsidianClient(self.config.obsidian)
            llm_client = OpenAiCompatibleClient(self.config.llm)
            whisper_config = _runtime_whisper_config(self.config.whisper, self.language)
            whisper_client = WhisperInferenceClient(whisper_config)

            write_initial_transcript(workspace, metadata, obsidian, logger, status="importing")
            metadata = workspace.update_status("importing")
            _emit_progress(
                self.on_progress,
                "normalizing",
                f"正在转换媒体文件：{self.file_path.name}",
                session_id=metadata.session_id,
            )
            self._raise_if_cancelled(workspace, logger, metadata.session_id)

            normalized_path = workspace.root / "source.normalized.wav"
            try:
                convert_audio_to_wav(
                    input_path=self.file_path,
                    output_path=normalized_path,
                    sample_rate=self.config.audio.sample_rate,
                    ffmpeg_binary=self.config.importer.ffmpeg_binary,
                )
                _emit_progress(
                    self.on_progress,
                    "chunking",
                    "正在切分音频片段。",
                    session_id=metadata.session_id,
                )
                self._raise_if_cancelled(workspace, logger, metadata.session_id)
                chunks = split_wav_file(
                    input_path=normalized_path,
                    output_dir=workspace.segments_dir,
                    chunk_seconds=_import_chunk_seconds(self.config),
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
                        self._raise_if_cancelled(workspace, logger, metadata.session_id)
                        _emit_progress(
                            self.on_progress,
                            "transcribing",
                            f"正在转写片段 {index}/{len(chunks)}",
                            session_id=metadata.session_id,
                            current=index,
                            total=len(chunks),
                        )
                        success = _process_segment(
                            pending=PendingSegment(
                                segment_id=chunk.segment_id,
                                started_ms=chunk.started_ms,
                                ended_ms=chunk.ended_ms,
                                pcm16=None,
                                wav_path=chunk.wav_path,
                            ),
                            workspace=workspace,
                            metadata=metadata,
                            obsidian=obsidian,
                            whisper_client=whisper_client,
                            logger=logger,
                            entries=self.entries,
                            live_status="importing",
                            sample_rate=self.config.audio.sample_rate,
                        )
                        if not success:
                            _emit_progress(
                                self.on_progress,
                                "segment_failed",
                                f"片段 {chunk.segment_id} 处理失败",
                                session_id=metadata.session_id,
                                current=index,
                                total=len(chunks),
                            )
                self._raise_if_cancelled(workspace, logger, metadata.session_id)
                metadata = apply_speaker_labels(
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
                self._raise_if_cancelled(workspace, logger, metadata.session_id)
                publish_final_outputs(
                    workspace,
                    metadata,
                    obsidian,
                    llm_client,
                    logger,
                    on_progress=self.on_progress,
                )
                _emit_progress(
                    self.on_progress,
                    "done",
                    "导入会话已完成。",
                    session_id=metadata.session_id,
                )
                return 0
            finally:
                if normalized_path.exists() and not self.config.importer.keep_normalized_audio:
                    normalized_path.unlink()
        except TaskCancelledError:
            raise
        except BaseException as exc:
            _mark_session_failed(
                workspace=workspace,
                logger=logger,
                label="导入会话",
                exc=exc,
                on_progress=self.on_progress,
            )
            raise

    def _raise_if_cancelled(
        self,
        workspace: SessionWorkspace,
        logger: logging.Logger,
        session_id: str | None = None,
    ) -> None:
        if self.cancel_event is None or not self.cancel_event.is_set():
            return
        workspace.update_status("cancelled")
        logger.info("导入会话已取消。")
        _emit_progress(
            self.on_progress,
            "cancelled",
            "导入任务已取消。",
            session_id=session_id,
        )
        raise TaskCancelledError("导入任务已取消。")


def finalize_session(
    config: AppConfig,
    session_id: str,
    on_progress: ProgressCallback | None = None,
) -> int:
    workspace = build_workspace(config.root_dir, session_id)
    metadata = workspace.read_session()
    logger = workspace.session_logger()
    _attach_console_logging()
    _emit_progress(
        on_progress,
        "recovering",
        f"正在补全会话 {session_id}",
        session_id=metadata.session_id,
    )

    obsidian = ObsidianClient(config.obsidian)
    llm_client = OpenAiCompatibleClient(config.llm)
    metadata = workspace.update_status("finalizing")

    missing = [
        state for state in workspace.rebuild_segment_states() if state.wav_path and not state.text
    ]
    if missing:
        whisper_config = _runtime_whisper_config(config.whisper, metadata.language)
        _recover_session_segments(
            workspace=workspace,
            metadata=metadata,
            whisper_config=whisper_config,
            logger=logger,
            states=missing,
            on_progress=on_progress,
            verb="补转写",
            status="transcribing",
            seed_entries=workspace.transcript_entries(),
        )

    publish_final_outputs(
        workspace,
        metadata,
        obsidian,
        llm_client,
        logger,
        on_progress=on_progress,
    )
    _emit_progress(
        on_progress,
        "done",
        "会话补写完成。",
        session_id=metadata.session_id,
    )
    return 0


def retranscribe_session(
    config: AppConfig,
    session_id: str,
    on_progress: ProgressCallback | None = None,
) -> int:
    workspace = build_workspace(config.root_dir, session_id)
    metadata = workspace.read_session()
    logger = workspace.session_logger()
    _attach_console_logging()
    _emit_progress(
        on_progress,
        "recovering",
        f"正在重转写会话 {session_id}",
        session_id=metadata.session_id,
    )

    obsidian = ObsidianClient(config.obsidian)
    llm_client = OpenAiCompatibleClient(config.llm)
    whisper_config = _runtime_whisper_config(config.whisper, metadata.language)
    metadata = workspace.update_status("retranscribing")
    states = [state for state in workspace.rebuild_segment_states() if state.wav_path]
    _recover_session_segments(
        workspace=workspace,
        metadata=metadata,
        whisper_config=whisper_config,
        logger=logger,
        states=states,
        on_progress=on_progress,
        verb="重转写",
        status="retranscribing",
        seed_entries=[],
    )

    publish_final_outputs(
        workspace,
        metadata,
        obsidian,
        llm_client,
        logger,
        on_progress=on_progress,
    )
    _emit_progress(
        on_progress,
        "done",
        "会话重转写完成。",
        session_id=metadata.session_id,
    )
    return 0


def refine_session(
    config: AppConfig,
    session_id: str,
    on_progress: ProgressCallback | None = None,
) -> int:
    workspace = build_workspace(config.root_dir, session_id)
    metadata = workspace.read_session()
    if metadata.input_mode != "live":
        raise RuntimeError("只有实时录音会话支持离线精修。")
    logger = workspace.session_logger()
    if not workspace.session_live_wav.exists():
        _emit_progress(
            on_progress,
            "refining",
            "未找到整场录音，正在尝试用分段音频回拼。",
            session_id=metadata.session_id,
        )
        if not reconstruct_session_live_audio(workspace):
            raise RuntimeError(
                "当前会话没有 session.live.wav，且无法从分段音频回拼整场录音，无法执行离线精修。"
            )
        logger.info("已从分段音频回拼 session.live.wav。")

    _attach_console_logging()
    _emit_progress(
        on_progress,
        "refining",
        f"正在离线精修会话 {session_id}",
        session_id=metadata.session_id,
    )

    obsidian = ObsidianClient(config.obsidian)
    llm_client = OpenAiCompatibleClient(config.llm)
    previous_source = metadata.transcript_source
    metadata = workspace.update_session(status="refining", refine_status="refining")
    try:
        metadata = _run_live_refinement(
            config=config,
            workspace=workspace,
            metadata=metadata,
            logger=logger,
            on_progress=on_progress,
        )
    except Exception as exc:
        logger.error("离线精修失败: %s", exc)
        _emit_progress(
            on_progress,
            "error",
            f"离线精修失败：{exc}",
            session_id=metadata.session_id,
            error=str(exc),
        )
        metadata = workspace.update_session(
            transcript_source=previous_source,
            refine_status="failed",
        )

    publish_final_outputs(
        workspace,
        metadata,
        obsidian,
        llm_client,
        logger,
        on_progress=on_progress,
    )
    _emit_progress(
        on_progress,
        "done",
        "离线精修已完成。",
        session_id=metadata.session_id,
    )
    return 0


def merge_sessions(
    config: AppConfig,
    session_ids: list[str],
    *,
    title: str | None = None,
    on_progress: ProgressCallback | None = None,
) -> int:
    normalized_ids = _normalize_merge_session_ids(session_ids)
    if len(normalized_ids) < 2:
        raise RuntimeError("至少需要两条不同的会话才能执行合并。")

    sources = sorted(
        (_load_merge_source(config.root_dir, session_id) for session_id in normalized_ids),
        key=lambda item: (item.metadata.started_at, item.metadata.session_id),
    )
    metadata = _build_merged_session_metadata(config, sources, title=title)
    workspace = SessionWorkspace.create(Path(metadata.session_dir), metadata)
    logger = workspace.session_logger()
    _attach_console_logging()
    _emit_progress(
        on_progress,
        "merging",
        f"正在合并 {len(sources)} 条会话。",
        session_id=metadata.session_id,
    )

    _merge_source_sessions(
        workspace=workspace,
        metadata=metadata,
        sources=sources,
        logger=logger,
        on_progress=on_progress,
    )

    if _can_merge_live_audio(sources):
        try:
            _merge_session_live_audio(sources, workspace.session_live_wav)
        except AudioImportError as exc:
            logger.warning("跳过合并后的 session.live.wav: %s", exc)
            if metadata.refine_status == "pending":
                metadata = workspace.update_session(refine_status="disabled")
        else:
            logger.info("已生成合并后的 session.live.wav。")

    obsidian = ObsidianClient(config.obsidian)
    llm_client = OpenAiCompatibleClient(config.llm)
    publish_final_outputs(
        workspace,
        metadata,
        obsidian,
        llm_client,
        logger,
        on_progress=on_progress,
    )
    _emit_progress(
        on_progress,
        "done",
        f"已生成合并会话：{metadata.title}",
        session_id=metadata.session_id,
    )
    return 0


def republish_session(
    config: AppConfig,
    session_id: str,
    on_progress: ProgressCallback | None = None,
) -> int:
    workspace = build_workspace(config.root_dir, session_id)
    metadata = workspace.read_session()
    logger = workspace.session_logger()
    _attach_console_logging()
    _emit_progress(
        on_progress,
        "publishing",
        f"正在重新生成会话 {session_id} 的输出",
        session_id=metadata.session_id,
    )
    obsidian = ObsidianClient(config.obsidian)
    llm_client = OpenAiCompatibleClient(config.llm)
    publish_final_outputs(
        workspace,
        metadata,
        obsidian,
        llm_client,
        logger,
        on_progress=on_progress,
    )
    _emit_progress(
        on_progress,
        "done",
        "已重新生成原文与整理稿。",
        session_id=metadata.session_id,
    )
    return 0


def sync_session_notes(
    config: AppConfig,
    session_id: str,
    on_progress: ProgressCallback | None = None,
) -> int:
    workspace = build_workspace(config.root_dir, session_id)
    metadata = workspace.read_session()
    logger = workspace.session_logger()
    _attach_console_logging()
    obsidian = ObsidianClient(config.obsidian)
    if not obsidian.is_enabled():
        _emit_progress(
            on_progress,
            "done",
            "Obsidian 同步已关闭，跳过重新同步。",
            session_id=metadata.session_id,
        )
        return 0
    _emit_progress(
        on_progress,
        "syncing",
        f"正在重新同步会话 {session_id}",
        session_id=metadata.session_id,
    )
    if workspace.transcript_md.exists():
        try_sync_note(
            obsidian,
            metadata.transcript_note_path,
            workspace.transcript_md.read_text(encoding="utf-8"),
            logger,
            "原文笔记",
        )
    if workspace.structured_md.exists():
        try_sync_note(
            obsidian,
            metadata.structured_note_path,
            workspace.structured_md.read_text(encoding="utf-8"),
            logger,
            "整理笔记",
        )
    _emit_progress(
        on_progress,
        "done",
        "会话笔记已重新同步。",
        session_id=metadata.session_id,
    )
    return 0


def _mark_session_failed(
    *,
    workspace: SessionWorkspace,
    logger: logging.Logger,
    label: str,
    exc: BaseException,
    on_progress: ProgressCallback | None,
) -> None:
    try:
        metadata = workspace.update_session(status="failed")
    except Exception:
        metadata = workspace.read_session()
    logger.exception("%s失败: %s", label, exc)
    _emit_progress(
        on_progress,
        "error",
        f"{label}失败：{exc}",
        session_id=metadata.session_id,
        error=str(exc),
    )


def _normalize_merge_session_ids(session_ids: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for session_id in session_ids:
        trimmed = session_id.strip()
        if not trimmed or trimmed in seen:
            continue
        normalized.append(trimmed)
        seen.add(trimmed)
    return normalized


def _load_merge_source(config_root: Path, session_id: str) -> MergeSourceSession:
    workspace = build_workspace(config_root, session_id)
    metadata = workspace.read_session()
    states = workspace.rebuild_segment_states()
    return MergeSourceSession(
        workspace=workspace,
        metadata=metadata,
        states=states,
        duration_ms=_session_duration_ms(workspace, states),
    )


def _build_merged_session_metadata(
    config: AppConfig,
    sources: list[MergeSourceSession],
    *,
    title: str | None,
) -> SessionMetadata:
    resolved_title = title.strip() if title and title.strip() else _default_merged_title(sources)
    input_mode = _shared_value([item.metadata.input_mode for item in sources], default="merged")
    kind = _shared_value([item.metadata.kind for item in sources], default="generic")
    language = _shared_value([item.metadata.language for item in sources], default="auto")
    transcript_source = _resolve_merged_transcript_source(sources)
    refine_status = _resolve_merged_refine_status(config, sources, input_mode=input_mode)
    base = create_session_metadata(
        config=config,
        title=resolved_title,
        kind=kind,
        language=language,
        input_mode=input_mode,
        source_label=f"合并自 {len(sources)} 条会话",
        source_ref=",".join(item.metadata.session_id for item in sources),
    )
    return replace(
        base,
        started_at=sources[0].metadata.started_at,
        status="merged",
        transcript_source=transcript_source,
        refine_status=refine_status,
    )


def _default_merged_title(sources: list[MergeSourceSession]) -> str:
    titles = _dedupe_preserving_order(item.metadata.title for item in sources)
    if len(titles) == 1:
        base = titles[0]
    elif len(titles) == 2:
        base = " + ".join(titles)
    else:
        base = f"{titles[0]} 等 {len(titles)} 段"
    return f"{base}（合并）"


def _dedupe_preserving_order(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        result.append(value)
        seen.add(value)
    return result


def _shared_value(values: list[str], *, default: str) -> str:
    unique = {value for value in values if value}
    if len(unique) == 1:
        return unique.pop()
    return default


def _resolve_merged_transcript_source(sources: list[MergeSourceSession]) -> str:
    if all(item.metadata.transcript_source == "refined" for item in sources):
        return "refined"
    return "live"


def _resolve_merged_refine_status(
    config: AppConfig,
    sources: list[MergeSourceSession],
    *,
    input_mode: str,
) -> str:
    if all(item.metadata.refine_status == "done" for item in sources):
        return "done"
    if input_mode == "live" and _can_merge_live_audio(sources):
        return "pending" if config.refine.enabled else "disabled"
    return "disabled"


def _merge_source_sessions(
    workspace: SessionWorkspace,
    metadata: SessionMetadata,
    sources: list[MergeSourceSession],
    logger: logging.Logger,
    on_progress: ProgressCallback | None,
) -> None:
    offset_ms = 0
    counter = 0
    for index, source in enumerate(sources, start=1):
        _emit_progress(
            on_progress,
            "merging",
            f"正在合并会话 {index}/{len(sources)}：{source.metadata.title}",
            session_id=metadata.session_id,
            current=index,
            total=len(sources),
        )
        for state in source.states:
            counter += 1
            segment_id = f"seg-{counter:05d}"
            started_ms = state.started_ms + offset_ms
            ended_ms = state.ended_ms + offset_ms
            copied_wav = _copy_segment_wav_if_present(
                source=source,
                state=state,
                target_path=workspace.next_wav_path(segment_id),
                logger=logger,
            )
            if copied_wav is not None:
                workspace.record_segment_created(segment_id, started_ms, ended_ms, copied_wav)
            if state.text:
                workspace.record_segment_text(segment_id, started_ms, ended_ms, state.text)
            elif state.error:
                workspace.record_segment_error(segment_id, started_ms, ended_ms, state.error)
        offset_ms += source.duration_ms


def _copy_segment_wav_if_present(
    *,
    source: MergeSourceSession,
    state: SegmentState,
    target_path: Path,
    logger: logging.Logger,
) -> Path | None:
    if state.wav_path is None:
        return None
    if not state.wav_path.exists():
        logger.warning(
            "合并会话 %s 时找不到片段音频 %s，将仅保留文本。",
            source.metadata.session_id,
            state.wav_path,
        )
        return None
    target_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(state.wav_path, target_path)
    return target_path


def _session_duration_ms(workspace: SessionWorkspace, states: list[SegmentState]) -> int:
    if states:
        return max(state.ended_ms for state in states)
    if workspace.session_live_wav.exists():
        try:
            return _wav_duration_ms(workspace.session_live_wav)
        except AudioImportError:
            return 0
    return 0


def _wav_duration_ms(path: Path) -> int:
    try:
        with wave.open(str(path), "rb") as handle:
            sample_rate = handle.getframerate()
            frame_count = handle.getnframes()
    except (EOFError, OSError, wave.Error) as exc:
        raise AudioImportError(f"WAV 文件损坏或不可读: {path}") from exc
    if sample_rate <= 0:
        return 0
    return round(frame_count * 1000 / sample_rate)


def _wav_sample_rate(path: Path) -> int:
    try:
        with wave.open(str(path), "rb") as handle:
            sample_rate = handle.getframerate()
    except (EOFError, OSError, wave.Error) as exc:
        raise AudioImportError(f"WAV 文件损坏或不可读: {path}") from exc
    if sample_rate <= 0:
        raise AudioImportError(f"WAV 采样率不合法: {path}")
    return sample_rate


def _can_merge_live_audio(sources: list[MergeSourceSession]) -> bool:
    if not sources:
        return False
    sample_rate: int | None = None
    for item in sources:
        wav_path = item.workspace.session_live_wav
        if item.metadata.input_mode != "live" or not wav_path.exists():
            return False
        try:
            current_rate = _wav_sample_rate(wav_path)
        except AudioImportError:
            return False
        if sample_rate is None:
            sample_rate = current_rate
        elif current_rate != sample_rate:
            return False
    return True


def can_reconstruct_session_live_audio(workspace: SessionWorkspace) -> bool:
    try:
        states = workspace.rebuild_segment_states()
    except Exception:
        return False
    if not states:
        return False
    sample_rate: int | None = None
    for state in states:
        if state.wav_path is None or not state.wav_path.exists():
            return False
        try:
            current_rate = _wav_sample_rate(state.wav_path)
        except AudioImportError:
            return False
        if sample_rate is None:
            sample_rate = current_rate
        elif current_rate != sample_rate:
            return False
    return True


def reconstruct_session_live_audio(workspace: SessionWorkspace) -> bool:
    if workspace.session_live_wav.exists():
        return True
    if not can_reconstruct_session_live_audio(workspace):
        return False

    states = workspace.rebuild_segment_states()
    parts: list[bytes] = []
    sample_rate: int | None = None
    current_frame = 0
    final_frame = 0

    for state in states:
        if state.wav_path is None:
            return False
        pcm16, current_rate = _read_wav_pcm16(state.wav_path)
        if sample_rate is None:
            sample_rate = current_rate
        elif current_rate != sample_rate:
            return False

        start_frame = round(state.started_ms * current_rate / 1000)
        end_frame = round(state.ended_ms * current_rate / 1000)
        if start_frame > current_frame:
            parts.append(b"\x00\x00" * (start_frame - current_frame))
            current_frame = start_frame
        parts.append(pcm16)
        current_frame += len(pcm16) // 2
        final_frame = max(final_frame, end_frame)

    if sample_rate is None:
        return False
    if final_frame > current_frame:
        parts.append(b"\x00\x00" * (final_frame - current_frame))

    _write_wav(workspace.session_live_wav, sample_rate, b"".join(parts))
    return True


def _merge_session_live_audio(
    sources: list[MergeSourceSession],
    output_path: Path,
) -> None:
    parts: list[bytes] = []
    sample_rate: int | None = None
    for source in sources:
        pcm16, current_rate = _read_wav_pcm16(source.workspace.session_live_wav)
        if sample_rate is None:
            sample_rate = current_rate
        elif current_rate != sample_rate:
            raise AudioImportError("无法合并采样率不同的整场录音。")
        parts.append(pcm16)
    if sample_rate is None:
        return
    _write_wav(output_path, sample_rate, b"".join(parts))


def _persist_live_segment(
    counter: int,
    segment: SegmentWindow,
    workspace: SessionWorkspace,
    sample_rate: int,
) -> PendingSegment:
    segment_id = f"seg-{counter:05d}"
    wav_path = workspace.next_wav_path(segment_id)
    _write_wav(wav_path, sample_rate, segment.pcm16)
    workspace.record_segment_created(segment_id, segment.started_ms, segment.ended_ms, wav_path)
    return PendingSegment(
        segment_id=segment_id,
        started_ms=segment.started_ms,
        ended_ms=segment.ended_ms,
        pcm16=segment.pcm16,
        wav_path=wav_path,
    )


def _run_live_refinement(
    config: AppConfig,
    workspace: SessionWorkspace,
    metadata: SessionMetadata,
    logger: logging.Logger,
    on_progress: ProgressCallback | None = None,
) -> SessionMetadata:
    if metadata.input_mode != "live":
        raise RuntimeError("只有实时录音会话支持离线精修。")
    if not workspace.session_live_wav.exists():
        raise FileNotFoundError("找不到 session.live.wav，无法离线精修。")

    whisper_config = _runtime_whisper_config(config.whisper, metadata.language)
    whisper_client = WhisperInferenceClient(whisper_config)

    if workspace.refined_dir.exists():
        shutil.rmtree(workspace.refined_dir)
    workspace.refined_dir.mkdir(parents=True, exist_ok=True)
    if workspace.refined_segments_tmp_jsonl.exists():
        workspace.refined_segments_tmp_jsonl.unlink()

    try:
        chunks = split_wav_file(
            input_path=workspace.session_live_wav,
            output_dir=workspace.refined_dir,
            chunk_seconds=config.importer.chunk_seconds,
        )
        if not chunks:
            raise AudioImportError("整场录音为空，无法离线精修。")

        entries: list[TranscriptEntry] = []
        with WhisperServerProcess(whisper_config, workspace.logs_txt):
            for index, chunk in enumerate(chunks, start=1):
                _emit_progress(
                    on_progress,
                    "refining",
                    f"正在离线精修片段 {index}/{len(chunks)}",
                    session_id=metadata.session_id,
                    current=index,
                    total=len(chunks),
                )
                workspace.record_segment_created(
                    chunk.segment_id,
                    chunk.started_ms,
                    chunk.ended_ms,
                    chunk.wav_path,
                    journal_path=workspace.refined_segments_tmp_jsonl,
                )
                text = _transcribe_segment_text(
                    whisper_client=whisper_client,
                    wav_path=chunk.wav_path,
                    language=metadata.language,
                    entries=entries,
                )
                if not text:
                    workspace.record_segment_error(
                        chunk.segment_id,
                        chunk.started_ms,
                        chunk.ended_ms,
                        "离线精修结果为空。",
                        journal_path=workspace.refined_segments_tmp_jsonl,
                    )
                    logger.info("离线精修跳过空片段 %s", chunk.segment_id)
                    continue
                workspace.record_segment_text(
                    chunk.segment_id,
                    chunk.started_ms,
                    chunk.ended_ms,
                    text,
                    journal_path=workspace.refined_segments_tmp_jsonl,
                )
                entries.append(
                    TranscriptEntry(
                        segment_id=chunk.segment_id,
                        started_ms=chunk.started_ms,
                        ended_ms=chunk.ended_ms,
                        text=text,
                    )
                )
        if not entries:
            raise WhisperError("离线精修结果为空。")
    except Exception:
        logger.exception("离线精修失败")
        raise
    else:
        workspace.replace_canonical_journal(workspace.refined_segments_tmp_jsonl)
        return workspace.update_session(
            transcript_source="refined",
            refine_status="done",
        )
    finally:
        if workspace.refined_segments_tmp_jsonl.exists():
            workspace.refined_segments_tmp_jsonl.unlink()


def _process_segment(
    pending: PendingSegment,
    workspace: SessionWorkspace,
    metadata: SessionMetadata,
    obsidian: ObsidianClient,
    whisper_client: WhisperInferenceClient,
    logger: logging.Logger,
    entries: list[TranscriptEntry],
    live_status: str,
    context_entries: list[TranscriptEntry] | None = None,
    sample_rate: int | None = None,
) -> bool:
    try:
        prompt_entries = context_entries if context_entries is not None else entries
        text = _transcribe_segment_text(
            whisper_client=whisper_client,
            wav_path=pending.wav_path,
            language=metadata.language,
            entries=prompt_entries,
            pcm16=pending.pcm16,
            sample_rate=sample_rate,
        )
        if not text:
            raise WhisperError("whisper-server 返回空转写结果。")
        workspace.record_segment_text(
            pending.segment_id,
            pending.started_ms,
            pending.ended_ms,
            text,
        )
        entry = TranscriptEntry(
            segment_id=pending.segment_id,
            started_ms=pending.started_ms,
            ended_ms=pending.ended_ms,
            text=text,
        )
        entries.append(entry)
        if context_entries is not None:
            context_entries.append(entry)
        content = build_transcript_note(metadata, list(entries), status=live_status)
        workspace.write_transcript(content)
        try_sync_note(
            obsidian,
            metadata.transcript_note_path,
            content,
            logger,
            f"原文片段 {pending.segment_id}",
        )
        return True
    except Exception as exc:
        workspace.record_segment_error(
            pending.segment_id,
            pending.started_ms,
            pending.ended_ms,
            str(exc),
        )
        logger.error("片段 %s 处理失败: %s", pending.segment_id, exc)
        return False


def _recover_session_segments(
    workspace: SessionWorkspace,
    metadata: SessionMetadata,
    whisper_config: Any,
    logger: logging.Logger,
    states: list[SegmentState],
    on_progress: ProgressCallback | None,
    verb: str,
    status: str,
    seed_entries: list[TranscriptEntry],
) -> None:
    if not states:
        return

    whisper_client = WhisperInferenceClient(whisper_config)
    entries = list(seed_entries)
    with WhisperServerProcess(whisper_config, workspace.logs_txt):
        for index, state in enumerate(states, start=1):
            assert state.wav_path is not None
            try:
                _emit_progress(
                    on_progress,
                    status,
                    f"正在{verb}片段 {index}/{len(states)}",
                    session_id=metadata.session_id,
                    current=index,
                    total=len(states),
                )
                text = _transcribe_segment_text(
                    whisper_client=whisper_client,
                    wav_path=state.wav_path,
                    language=metadata.language,
                    entries=entries,
                )
                if not text:
                    raise WhisperError(f"{verb}结果为空。")
                workspace.record_segment_text(
                    state.segment_id,
                    state.started_ms,
                    state.ended_ms,
                    text,
                )
                entries.append(
                    TranscriptEntry(
                        segment_id=state.segment_id,
                        started_ms=state.started_ms,
                        ended_ms=state.ended_ms,
                        text=text,
                    )
                )
            except Exception as exc:
                workspace.record_segment_error(
                    state.segment_id,
                    state.started_ms,
                    state.ended_ms,
                    str(exc),
                )
                logger.error("%s失败 %s: %s", verb, state.segment_id, exc)
                _emit_progress(
                    on_progress,
                    "segment_failed",
                    f"{verb}失败：{state.segment_id}",
                    session_id=metadata.session_id,
                    current=index,
                    total=len(states),
                    error=str(exc),
                )


def _transcribe_segment_text(
    whisper_client: WhisperInferenceClient,
    wav_path: Path,
    language: str,
    entries: list[TranscriptEntry],
    *,
    pcm16: bytes | None = None,
    sample_rate: int | None = None,
) -> str:
    prompt = build_transcription_prompt(language, entries)
    raw_text = whisper_client.transcribe(wav_path, prompt=prompt)
    if not raw_text.strip():
        return ""

    resolved_pcm16 = pcm16
    resolved_sample_rate = sample_rate
    if resolved_pcm16 is None or resolved_sample_rate is None:
        resolved_pcm16, resolved_sample_rate = _read_wav_pcm16(wav_path)

    return normalize_transcript_text(
        raw_text,
        language,
        pcm16=resolved_pcm16,
        sample_rate=resolved_sample_rate,
    )


def _read_wav_pcm16(path: Path) -> tuple[bytes, int]:
    try:
        with wave.open(str(path), "rb") as handle:
            channels = handle.getnchannels()
            sample_width = handle.getsampwidth()
            sample_rate = handle.getframerate()
            if channels != 1 or sample_width != 2:
                raise AudioImportError(f"WAV 格式不受支持: {path}")
            return handle.readframes(handle.getnframes()), sample_rate
    except AudioImportError:
        raise
    except (EOFError, OSError, wave.Error) as exc:
        raise AudioImportError(f"WAV 文件损坏或不可读: {path}") from exc


def _write_wav(path: Path, sample_rate: int, pcm16: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm16)


def _runtime_whisper_config(config: Any, language: str | None) -> Any:
    return with_runtime_port(with_language_override(config, language))


def _open_session_audio_writer(
    path: Path,
    sample_rate: int,
    *,
    enabled: bool,
):
    if not enabled:
        return nullcontext(None)
    return SessionAudioWriter(path, sample_rate)


def _enqueue_with_retry(target_queue: queue.Queue[Any], item: Any) -> None:
    while True:
        try:
            target_queue.put(item, timeout=0.5)
            return
        except queue.Full:
            continue


def _attach_console_logging() -> None:
    logger = logging.getLogger("live_note")
    logger.setLevel(logging.INFO)
    if any(getattr(handler, "_live_note_console", False) for handler in logger.handlers):
        return
    handler = _console_handler()
    setattr(handler, "_live_note_console", True)
    logger.addHandler(handler)


def _console_handler() -> logging.Handler:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    return handler


def _emit_progress(
    callback: ProgressCallback | None,
    stage: str,
    message: str,
    *,
    session_id: str | None = None,
    current: int | None = None,
    total: int | None = None,
    error: str | None = None,
) -> None:
    if callback is None:
        return
    callback(
        ProgressEvent(
            stage=stage,
            message=message,
            session_id=session_id,
            current=current,
            total=total,
            error=error,
        )
    )
