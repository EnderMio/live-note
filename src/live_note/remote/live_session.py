from __future__ import annotations

import logging
import os
import queue
import threading
import time
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path

from live_note.audio.segmentation import SegmentWindow, SpeechSegmenter
from live_note.config import AppConfig
from live_note.domain import AudioFrame, PendingSegment, SessionMetadata, TranscriptEntry
from live_note.obsidian.client import ObsidianClient
from live_note.obsidian.renderer import build_transcript_note
from live_note.runtime.domain.session_state import SessionStatus
from live_note.runtime.session_mutations import (
    apply_workspace_session_command,
    require_runtime_session,
)
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
    _runtime_whisper_config,
)
from live_note.runtime.workflows.live_support import mark_live_paused, mark_live_resumed
from live_note.session_workspace import SessionWorkspace
from live_note.transcribe.funasr import FunAsrMessage, FunAsrWebSocketClient
from live_note.transcribe.whisper import WhisperInferenceClient, WhisperServerProcess
from live_note.utils import compact_text

from .live_workflow import accept_remote_live_stop, run_remote_live_session, start_remote_live_session
from .protocol import LiveStartRequest

_NO_SPACE_PUNCTUATION = frozenset("，。！？；：、】【（）《》「」『』、")


def _is_no_space_script_char(value: str) -> bool:
    codepoint = ord(value)
    return (
        0x3040 <= codepoint <= 0x30FF
        or 0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xAC00 <= codepoint <= 0xD7AF
        or 0x1100 <= codepoint <= 0x11FF
        or 0x3130 <= codepoint <= 0x318F
    )


def _is_no_space_boundary_char(value: str) -> bool:
    return _is_no_space_script_char(value) or value in _NO_SPACE_PUNCTUATION


def _merge_partial_text(current: str, cleaned: str) -> str:
    current_tail = current.rstrip()
    incoming_head = cleaned.lstrip()
    if (
        current_tail
        and incoming_head
        and " " not in current
        and " " not in cleaned
        and _is_no_space_boundary_char(current_tail[-1])
        and _is_no_space_boundary_char(incoming_head[0])
    ):
        return f"{current_tail}{incoming_head}"
    return compact_text(f"{current} {cleaned}")


class _RemoteCaptureState:
    def __init__(self) -> None:
        self._paused = False

    @property
    def is_paused(self) -> bool:
        return self._paused

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False


class _FunAsrDraftTracker:
    def __init__(self) -> None:
        self._next_index = 1
        self._current_segment_id: str | None = None
        self._current_started_ms = 0
        self._current_ended_ms = 0
        self._current_text = ""
        self._last_finalized_ms = 0
        self._stream_base_ms = 0

    @property
    def has_open_segment(self) -> bool:
        return self._current_segment_id is not None and bool(self._current_text)

    def start_stream(self, base_ms: int) -> None:
        self._stream_base_ms = max(0, int(base_ms))
        if self._current_segment_id is None:
            self._current_started_ms = max(self._last_finalized_ms, self._stream_base_ms)
            self._current_ended_ms = self._current_started_ms

    def build_partial_payload(
        self,
        text: str,
        *,
        current_ms: int,
        bounds_ms: tuple[int, int] | None = None,
    ) -> dict[str, object] | None:
        cleaned = text.strip()
        if not cleaned:
            return None
        started_ms, ended_ms = self._resolve_bounds(current_ms=current_ms, bounds_ms=bounds_ms)
        self._ensure_current_segment(started_ms=started_ms)
        current = self._current_text
        normalized_incoming = compact_text(cleaned)
        normalized_current = compact_text(current)
        if not current:
            merged = cleaned
        elif normalized_incoming.startswith(normalized_current):
            merged = cleaned
        elif normalized_current == normalized_incoming or normalized_current.endswith(
            normalized_incoming
        ):
            merged = current
        else:
            merged = _merge_partial_text(current, cleaned)
        previous_ended_ms = self._current_ended_ms
        self._current_started_ms = min(self._current_started_ms, started_ms)
        self._current_ended_ms = max(self._current_ended_ms, ended_ms)
        self._current_text = merged
        if merged == current:
            self._current_ended_ms = max(previous_ended_ms, ended_ms)
            return None
        return {
            "type": "segment_partial",
            "segment_id": self._current_segment_id,
            "started_ms": self._current_started_ms,
            "ended_ms": self._current_ended_ms,
            "text": merged,
            "speaker_label": None,
        }

    def build_final_entry(
        self,
        text: str,
        *,
        current_ms: int,
        bounds_ms: tuple[int, int] | None = None,
    ) -> TranscriptEntry | None:
        if not text.strip():
            return None
        started_ms, ended_ms = self._resolve_bounds(current_ms=current_ms, bounds_ms=bounds_ms)
        self._ensure_current_segment(started_ms=started_ms)
        self._current_started_ms = min(self._current_started_ms, started_ms)
        self._current_ended_ms = max(self._current_ended_ms, ended_ms)
        entry = TranscriptEntry(
            segment_id=str(self._current_segment_id),
            started_ms=self._current_started_ms,
            ended_ms=self._current_ended_ms,
            text=text,
        )
        self._reset_open_segment(finalized_ms=self._current_ended_ms)
        return entry

    def force_finalize(self) -> TranscriptEntry | None:
        cleaned = self._current_text.strip()
        if self._current_segment_id is None or not cleaned:
            return None
        ended_ms = max(self._current_ended_ms, self._current_started_ms + 1)
        entry = TranscriptEntry(
            segment_id=str(self._current_segment_id),
            started_ms=self._current_started_ms,
            ended_ms=ended_ms,
            text=cleaned,
        )
        self._reset_open_segment(finalized_ms=ended_ms)
        return entry

    def _reset_open_segment(self, *, finalized_ms: int) -> None:
        self._last_finalized_ms = finalized_ms
        self._current_segment_id = None
        self._current_started_ms = finalized_ms
        self._current_ended_ms = finalized_ms
        self._current_text = ""

    def _ensure_current_segment(self, *, started_ms: int | None = None) -> None:
        if self._current_segment_id is not None:
            return
        self._current_segment_id = f"seg-{self._next_index:05d}"
        self._next_index += 1
        resolved_started_ms = max(
            self._last_finalized_ms,
            self._stream_base_ms,
            started_ms if started_ms is not None else self._last_finalized_ms,
        )
        self._current_started_ms = resolved_started_ms
        self._current_ended_ms = resolved_started_ms

    def _resolve_bounds(
        self,
        *,
        current_ms: int,
        bounds_ms: tuple[int, int] | None,
    ) -> tuple[int, int]:
        if bounds_ms is not None:
            start = self._stream_base_ms + max(0, int(bounds_ms[0]))
            end = self._stream_base_ms + max(0, int(bounds_ms[1]))
            start_ms = min(start, end)
            end_ms = max(start, end)
            return (start_ms, max(end_ms, start_ms + 1))
        started_ms = (
            self._current_started_ms
            if self._current_segment_id is not None
            else max(self._last_finalized_ms, self._stream_base_ms)
        )
        ended_ms = max(self._current_ended_ms, current_ms, started_ms + 1)
        return (started_ms, ended_ms)


class _FunAsrAudioBatcher:
    def __init__(self, chunk_ms: int) -> None:
        self._chunk_ms = max(1, chunk_ms)
        self._buffer = bytearray()
        self._duration_ms = 0

    def push(self, frame: AudioFrame) -> bytes | None:
        self._buffer.extend(frame.pcm16)
        self._duration_ms += max(1, frame.ended_ms - frame.started_ms)
        if self._duration_ms < self._chunk_ms:
            return None
        return self.flush()

    def flush(self) -> bytes | None:
        if not self._buffer:
            return None
        payload = bytes(self._buffer)
        self._buffer.clear()
        self._duration_ms = 0
        return payload


def _is_funasr_final_message(message: FunAsrMessage) -> bool:
    mode = message.mode.strip().lower().replace("_", "-")
    if mode in {"offline", "2pass-offline"}:
        return True
    if mode in {"online", "2pass-online"}:
        return False
    return message.is_final


class RemoteLiveSessionRunner:
    def __init__(
        self,
        *,
        config: AppConfig,
        request: LiveStartRequest,
        on_progress,
        on_event=None,
        commit_postprocess_handoff=None,
    ) -> None:
        if request.speaker_enabled is not None:
            config = replace(
                config,
                speaker=replace(config.speaker, enabled=bool(request.speaker_enabled)),
            )
        self.config = config
        self.title = request.title
        self.source = request.source_ref
        self.kind = request.kind
        self.language = request.language or self.config.whisper.language
        self.on_progress = on_progress
        self.entries: list[TranscriptEntry] = []
        self._thread_errors: queue.Queue[BaseException] = queue.Queue()
        self._stop_event = threading.Event()
        self._control_commands: queue.Queue[str] = queue.Queue()
        self._pause_requested = False
        self.session_id: str | None = None
        self.request = request
        self.on_event = on_event
        self.frame_queue: queue.Queue[AudioFrame | object] = queue.Queue(
            maxsize=self.config.audio.queue_size
        )
        self.segment_queue: queue.Queue[object] = queue.Queue(maxsize=32)
        self._audio_offset_ms = 0
        self._pcm_buffer = bytearray()
        self._capture = _RemoteCaptureState()
        self._thread: threading.Thread | None = None
        self._spool_logger: logging.Logger | None = None
        self.workspace: SessionWorkspace | None = None
        self.metadata: SessionMetadata | None = None
        self.failure_message: str | None = None
        self.postprocess_task_payload: dict[str, object] | None = None
        self._commit_postprocess_handoff = commit_postprocess_handoff
        self._backend_ready_event = threading.Event()
        self._startup_error: str | None = None
        self._spool_lock = threading.Condition()
        self._spool_path: Path | None = None
        self._spool_writer = None
        self._spool_read_offset = 0
        self._spool_write_offset = 0
        self._spool_sealed = False
        self._spool_stats_lock = threading.Lock()
        self._spool_enqueue_count = 0
        self._spool_enqueue_wait_total_ms = 0.0
        self._spool_enqueue_wait_max_ms = 0.0
        self._spool_enqueue_wait_slow_count = 0
        self._spool_processed_bytes = 0
        self._frame_max_depth = 0
        self._spool_last_stats_logged_at = 0.0

    @property
    def is_paused(self) -> bool:
        return self._pause_requested

    @property
    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> SessionMetadata:
        return start_remote_live_session(self)

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def request_stop(self) -> None:
        self._stop_event.set()
        self._seal_ingest_spool()

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

    def _drain_control_commands(
        self,
        *,
        capture: _RemoteCaptureState,
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

    def commit_postprocess_handoff(self) -> dict[str, object]:
        if self.postprocess_task_payload is not None:
            return self.postprocess_task_payload
        if (
            self._commit_postprocess_handoff is None
            or self.session_id is None
            or self.workspace is None
        ):
            raise RuntimeError("远端实时会话缺少 durable handoff 提交器。")
        self.postprocess_task_payload = self._commit_postprocess_handoff(
            self.session_id,
            str(self.workspace.live_ingest_pcm),
        )
        return self.postprocess_task_payload

    def recover_transcript_from_spool(self) -> SessionMetadata:
        assert self.workspace is not None
        assert self.metadata is not None
        workspace = self.workspace
        metadata = self.metadata
        logger = workspace.session_logger()
        self._spool_logger = logger
        self.entries.clear()
        self._thread_errors = queue.Queue()
        self.frame_queue = queue.Queue(maxsize=self.config.audio.queue_size)
        self.segment_queue = queue.Queue(maxsize=32)
        self._audio_offset_ms = 0
        self._pcm_buffer.clear()
        self._reset_recovery_workspace(workspace, metadata)
        self._load_existing_ingest_spool()
        _emit_progress(
            self.on_progress,
            "recovering",
            f"正在从 ingest journal 恢复会话 {metadata.session_id}",
            session_id=metadata.session_id,
        )
        try:
            if self.config.funasr.enabled:
                self._recover_funasr_from_spool(workspace, metadata, logger)
            else:
                self._recover_whisper_from_spool(
                    workspace,
                    metadata,
                    logger,
                    self._disabled_obsidian_client(),
                )
            self._raise_thread_error_if_any()
            return require_runtime_session(self.config.root_dir, metadata.session_id)
        finally:
            self._seal_ingest_spool()
            self._close_ingest_spool()
            self._maybe_log_spool_stats(force=True)

    def _reset_recovery_workspace(
        self,
        workspace: SessionWorkspace,
        metadata: SessionMetadata,
    ) -> None:
        workspace.segments_jsonl.write_text("", encoding="utf-8")
        workspace.segments_live_jsonl.write_text("", encoding="utf-8")
        workspace.transcript_md.write_text(
            build_transcript_note(metadata, [], status=SessionStatus.INGESTING.value),
            encoding="utf-8",
        )
        workspace.structured_md.unlink(missing_ok=True)
        workspace.session_live_wav.unlink(missing_ok=True)
        for wav_path in workspace.segments_dir.glob("*.wav"):
            wav_path.unlink(missing_ok=True)

    def _load_existing_ingest_spool(self) -> None:
        assert self.workspace is not None
        path = self.workspace.live_ingest_pcm
        size = path.stat().st_size if path.exists() else 0
        with self._spool_lock:
            self._spool_path = path
            self._spool_writer = None
            self._spool_read_offset = 0
            self._spool_write_offset = size
            self._spool_processed_bytes = 0
            self._spool_sealed = True

    def _recover_whisper_from_spool(
        self,
        workspace: SessionWorkspace,
        metadata: SessionMetadata,
        logger: logging.Logger,
        disabled_obsidian: ObsidianClient,
    ) -> None:
        whisper_config = _runtime_whisper_config(self.config.whisper, self.language)
        whisper_client = WhisperInferenceClient(whisper_config)
        whisper_server = WhisperServerProcess(whisper_config, workspace.logs_txt)
        segmenter = SpeechSegmenter(self.config.audio)
        segment_thread = threading.Thread(
            target=self._segment_loop,
            name="remote-recovery-segmenter",
            daemon=True,
            args=(self.frame_queue, self.segment_queue, segmenter, workspace),
        )
        transcribe_thread = threading.Thread(
            target=self._transcribe_loop,
            name="remote-recovery-transcriber",
            daemon=True,
            args=(self.segment_queue, workspace, metadata, disabled_obsidian, whisper_client),
        )
        with whisper_server:
            segment_thread.start()
            transcribe_thread.start()
            while True:
                self._raise_thread_error_if_any()
                drained = self._drain_spool_to_frames(wait_timeout=0.0)
                if not drained and self._spool_drained_after_stop():
                    break
            _emit_progress(
                self.on_progress,
                "capture_finished",
                "已从 ingest journal 回放实时音频，开始后台整理。",
                session_id=metadata.session_id,
            )
            self.frame_queue.put(FRAME_STOP)
            segment_thread.join()
            transcribe_thread.join()

    def _recover_funasr_from_spool(
        self,
        workspace: SessionWorkspace,
        metadata: SessionMetadata,
        logger: logging.Logger,
    ) -> None:
        tracker = _FunAsrDraftTracker()
        chunker = _FunAsrAudioBatcher(chunk_ms=60)
        current_ms = 0
        tracker.start_stream(current_ms)
        connection = self._open_funasr_connection(metadata.session_id)
        with _open_session_audio_writer(
            workspace.session_live_wav,
            self.config.audio.sample_rate,
            enabled=self.config.audio.save_session_wav,
        ) as session_audio:
            while True:
                self._drain_spool_to_frames(wait_timeout=0.0)
                self._drain_funasr_messages(connection, tracker, workspace, metadata, current_ms)
                try:
                    item = self.frame_queue.get(timeout=0.05)
                except queue.Empty:
                    if self._spool_drained_after_stop() and self.frame_queue.empty():
                        break
                    continue
                if item is FRAME_STOP:
                    break
                if item in {FRAME_PAUSE, FRAME_RESUME}:
                    continue
                assert isinstance(item, AudioFrame)
                current_ms = item.ended_ms
                if session_audio is not None:
                    session_audio.write(item.pcm16)
                payload = chunker.push(item)
                if payload and connection is not None:
                    connection.send_audio(payload)
                    self._drain_funasr_messages(
                        connection,
                        tracker,
                        workspace,
                        metadata,
                        current_ms,
                    )
            _emit_progress(
                self.on_progress,
                "capture_finished",
                "已从 ingest journal 回放实时音频，开始后台整理。",
                session_id=metadata.session_id,
            )
            self._flush_funasr_stream(
                connection,
                chunker,
                tracker,
                workspace,
                metadata,
                current_ms,
            )
            if connection is not None:
                connection.close()

    def feed_audio(self, pcm16: bytes) -> None:
        if not pcm16 or self._capture.is_paused:
            return
        self._pcm_buffer.extend(pcm16)
        frame_bytes = max(
            2,
            int(self.config.audio.sample_rate * self.config.audio.frame_duration_ms / 1000) * 2,
        )
        frame_duration_ms = max(1, self.config.audio.frame_duration_ms)
        while len(self._pcm_buffer) >= frame_bytes:
            frame_pcm16 = bytes(self._pcm_buffer[:frame_bytes])
            del self._pcm_buffer[:frame_bytes]
            started_ms = self._audio_offset_ms
            ended_ms = started_ms + frame_duration_ms
            self._audio_offset_ms = ended_ms
            self.frame_queue.put(
                AudioFrame(
                    started_ms=started_ms,
                    ended_ms=ended_ms,
                    pcm16=frame_pcm16,
                )
            )

    def enqueue_audio_bytes(self, pcm16: bytes) -> bool:
        if not pcm16:
            return True
        enqueue_started_at = time.monotonic()
        if not self._append_to_ingest_spool(pcm16):
            return False
        wait_ms = (time.monotonic() - enqueue_started_at) * 1000.0
        self._record_spool_enqueue(wait_ms=wait_ms)
        self._maybe_log_spool_stats()
        return True

    def ingress_diagnostics(self) -> dict[str, float | int]:
        with self._spool_stats_lock:
            avg_wait_ms = (
                self._spool_enqueue_wait_total_ms / self._spool_enqueue_count
                if self._spool_enqueue_count
                else 0.0
            )
            captured_bytes = self._spool_write_offset
            processed_bytes = self._spool_processed_bytes
            backlog_bytes = max(0, captured_bytes - processed_bytes)
            captured_ms = int(captured_bytes / 2 / max(1, self.config.audio.sample_rate) * 1000)
            processed_ms = int(processed_bytes / 2 / max(1, self.config.audio.sample_rate) * 1000)
            backlog_ms = max(0, captured_ms - processed_ms)
            return {
                "frame_depth": self.frame_queue.qsize(),
                "frame_max_depth": self._frame_max_depth,
                "enqueue_count": self._spool_enqueue_count,
                "enqueue_wait_avg_ms": round(avg_wait_ms, 3),
                "enqueue_wait_max_ms": round(self._spool_enqueue_wait_max_ms, 3),
                "enqueue_wait_slow_count": self._spool_enqueue_wait_slow_count,
                "captured_bytes": captured_bytes,
                "processed_bytes": processed_bytes,
                "backlog_bytes": backlog_bytes,
                "captured_ms": captured_ms,
                "processed_ms": processed_ms,
                "backlog_ms": backlog_ms,
                "spool_sealed": int(self._spool_sealed),
            }

    def _initialize_ingest_spool(self) -> None:
        if self.workspace is None:
            raise RuntimeError("会话工作区尚未初始化，无法创建实时音频缓冲。")
        with self._spool_lock:
            self._spool_path = self.workspace.root / "live.ingest.pcm"
            self._spool_path.parent.mkdir(parents=True, exist_ok=True)
            self._spool_path.write_bytes(b"")
            self._spool_writer = self._spool_path.open("ab")
            self._spool_read_offset = 0
            self._spool_write_offset = 0
            self._spool_processed_bytes = 0
            self._spool_sealed = False
        if self._stop_event.is_set():
            self._seal_ingest_spool()

    def _append_to_ingest_spool(self, pcm16: bytes) -> bool:
        with self._spool_lock:
            if self._spool_sealed or self._spool_writer is None:
                return False
            self._spool_writer.write(pcm16)
            self._spool_writer.flush()
            os.fsync(self._spool_writer.fileno())
            self._spool_write_offset += len(pcm16)
            self._spool_lock.notify_all()
            return True

    def _seal_ingest_spool(self) -> None:
        with self._spool_lock:
            if self._spool_sealed:
                return
            self._spool_sealed = True
            if self._spool_writer is not None:
                self._spool_writer.flush()
                os.fsync(self._spool_writer.fileno())
                self._spool_writer.close()
                self._spool_writer = None
            self._spool_lock.notify_all()

    def _close_ingest_spool(self) -> None:
        with self._spool_lock:
            if self._spool_writer is not None:
                self._spool_writer.close()
                self._spool_writer = None

    def _drain_spool_to_frames(self, *, wait_timeout: float) -> bool:
        read_path: Path | None = None
        read_offset = 0
        read_size = 0
        with self._spool_lock:
            if self._spool_path is None:
                return False
            if self._spool_read_offset >= self._spool_write_offset:
                if self._spool_sealed:
                    return False
                self._spool_lock.wait(timeout=max(wait_timeout, 0.0))
                if self._spool_read_offset >= self._spool_write_offset:
                    return False
            read_path = self._spool_path
            read_offset = self._spool_read_offset
            read_size = self._spool_write_offset - self._spool_read_offset
        if read_path is None or read_size <= 0:
            return False
        with read_path.open("rb") as handle:
            handle.seek(read_offset)
            payload = handle.read(read_size)
        if not payload:
            return False
        self.feed_audio(payload)
        with self._spool_lock:
            self._spool_read_offset += len(payload)
        self._record_spool_processed(len(payload))
        self._track_frame_depth()
        self._maybe_log_spool_stats()
        return True

    def _spool_drained_after_stop(self) -> bool:
        with self._spool_lock:
            if self._spool_path is None:
                return True
            return self._spool_sealed and self._spool_read_offset >= self._spool_write_offset

    def _track_frame_depth(self) -> None:
        frame_depth = self.frame_queue.qsize()
        with self._spool_stats_lock:
            self._frame_max_depth = max(self._frame_max_depth, frame_depth)

    def _record_spool_enqueue(self, *, wait_ms: float) -> None:
        slow_threshold_ms = 60.0
        with self._spool_stats_lock:
            self._spool_enqueue_count += 1
            self._spool_enqueue_wait_total_ms += wait_ms
            self._spool_enqueue_wait_max_ms = max(self._spool_enqueue_wait_max_ms, wait_ms)
            if wait_ms >= slow_threshold_ms:
                self._spool_enqueue_wait_slow_count += 1

    def _record_spool_processed(self, consumed_bytes: int) -> None:
        with self._spool_stats_lock:
            self._spool_processed_bytes += max(0, consumed_bytes)

    def _maybe_log_spool_stats(self, *, force: bool = False) -> None:
        now = time.monotonic()
        with self._spool_stats_lock:
            if not force and now - self._spool_last_stats_logged_at < 2.0:
                return
            self._spool_last_stats_logged_at = now
        diagnostics = self.ingress_diagnostics()
        logger = self._spool_logger or logging.getLogger(__name__)
        logger.info(
            (
                "remote spool stats session=%s captured=%sB(%sms) processed=%sB(%sms) "
                "backlog=%sB(%sms) frame=%s/%s sealed=%s enq=%s wait_avg=%.2fms "
                "wait_max=%.2fms slow=%s"
            ),
            self.session_id,
            diagnostics["captured_bytes"],
            diagnostics["captured_ms"],
            diagnostics["processed_bytes"],
            diagnostics["processed_ms"],
            diagnostics["backlog_bytes"],
            diagnostics["backlog_ms"],
            diagnostics["frame_depth"],
            diagnostics["frame_max_depth"],
            diagnostics["spool_sealed"],
            diagnostics["enqueue_count"],
            diagnostics["enqueue_wait_avg_ms"],
            diagnostics["enqueue_wait_max_ms"],
            diagnostics["enqueue_wait_slow_count"],
        )

    def run(self) -> int:
        return run_remote_live_session(self)

    def _disabled_obsidian_client(self) -> ObsidianClient:
        return ObsidianClient(replace(self.config.obsidian, enabled=False, api_key=None))

    def _mark_backend_ready(self) -> None:
        self._backend_ready_event.set()

    def _mark_backend_startup_failed(self, exc: BaseException) -> None:
        if self._backend_ready_event.is_set():
            return
        message = str(exc).strip() or "未知错误"
        self._startup_error = f"远端实时后端启动失败：{message}"
        self._backend_ready_event.set()

    def _run_whisper_live_backend(
        self,
        workspace: SessionWorkspace,
        metadata: SessionMetadata,
        logger: logging.Logger,
        disabled_obsidian: ObsidianClient,
    ) -> None:
        whisper_config = _runtime_whisper_config(self.config.whisper, self.language)
        whisper_client = WhisperInferenceClient(whisper_config)
        whisper_server = WhisperServerProcess(whisper_config, workspace.logs_txt)
        segmenter = SpeechSegmenter(self.config.audio)

        segment_thread = threading.Thread(
            target=self._segment_loop,
            name="remote-segmenter",
            daemon=True,
            args=(self.frame_queue, self.segment_queue, segmenter, workspace),
        )
        transcribe_thread = threading.Thread(
            target=self._transcribe_loop,
            name="remote-transcriber",
            daemon=True,
            args=(self.segment_queue, workspace, metadata, disabled_obsidian, whisper_client),
        )
        capture_finished = False
        capture_announced = False

        with whisper_server:
            segment_thread.start()
            transcribe_thread.start()
            self._mark_backend_ready()
            while True:
                self._drain_control_commands(
                    capture=self._capture,
                    frame_queue=self.frame_queue,
                    workspace=workspace,
                    metadata=metadata,
                    logger=logger,
                )
                self._raise_thread_error_if_any()
                self._drain_spool_to_frames(wait_timeout=0.1)
                if self._stop_event.is_set() and self._spool_drained_after_stop():
                    capture_finished = True
                    break
            if capture_finished and not capture_announced:
                accept_remote_live_stop(self, workspace, metadata)
            self.frame_queue.put(FRAME_STOP)
            segment_thread.join()
            transcribe_thread.join()

    def _run_funasr_live_backend(
        self,
        workspace: SessionWorkspace,
        metadata: SessionMetadata,
        logger: logging.Logger,
    ) -> None:
        tracker = _FunAsrDraftTracker()
        chunker = _FunAsrAudioBatcher(chunk_ms=60)
        current_ms = 0
        tracker.start_stream(current_ms)
        connection = self._open_funasr_connection(metadata.session_id)
        self._mark_backend_ready()

        with _open_session_audio_writer(
            workspace.session_live_wav,
            self.config.audio.sample_rate,
            enabled=self.config.audio.save_session_wav,
        ) as session_audio:
            while True:
                self._drain_control_commands(
                    capture=self._capture,
                    frame_queue=self.frame_queue,
                    workspace=workspace,
                    metadata=metadata,
                    logger=logger,
                )
                self._drain_spool_to_frames(wait_timeout=0.05)
                self._drain_funasr_messages(connection, tracker, workspace, metadata, current_ms)
                try:
                    item = self.frame_queue.get(timeout=0.1)
                except queue.Empty:
                    if (
                        self._stop_event.is_set()
                        and self._spool_drained_after_stop()
                        and self.frame_queue.empty()
                    ):
                        break
                    continue
                if item is FRAME_PAUSE:
                    self._flush_funasr_stream(
                        connection,
                        chunker,
                        tracker,
                        workspace,
                        metadata,
                        current_ms,
                    )
                    if connection is not None:
                        connection.close()
                    connection = None
                    continue
                if item is FRAME_RESUME:
                    if connection is None:
                        tracker.start_stream(current_ms)
                        connection = self._open_funasr_connection(metadata.session_id)
                    continue
                if item is FRAME_STOP:
                    break
                assert isinstance(item, AudioFrame)
                current_ms = item.ended_ms
                if session_audio is not None:
                    session_audio.write(item.pcm16)
                if connection is None:
                    tracker.start_stream(current_ms)
                    connection = self._open_funasr_connection(metadata.session_id)
                payload = chunker.push(item)
                if payload and connection is not None:
                    connection.send_audio(payload)
                    self._drain_funasr_messages(
                        connection,
                        tracker,
                        workspace,
                        metadata,
                        current_ms,
                    )

            accept_remote_live_stop(self, workspace, metadata)
            self._flush_funasr_stream(
                connection,
                chunker,
                tracker,
                workspace,
                metadata,
                current_ms,
            )
            if connection is not None:
                connection.close()

    def _open_funasr_connection(self, wav_name: str):
        connection = FunAsrWebSocketClient(self.config.funasr).connect_live()
        connection.start_stream(
            wav_name=wav_name,
            sample_rate=self.config.audio.sample_rate,
        )
        return connection

    def _flush_funasr_stream(
        self,
        connection,
        chunker: _FunAsrAudioBatcher,
        tracker: _FunAsrDraftTracker,
        workspace: SessionWorkspace,
        metadata: SessionMetadata,
        current_ms: int,
    ) -> None:
        if connection is None:
            return
        payload = chunker.flush()
        awaiting_result = bool(payload) or tracker.has_open_segment
        if payload:
            connection.send_audio(payload)
            self._drain_funasr_messages(connection, tracker, workspace, metadata, current_ms)
            awaiting_result = tracker.has_open_segment
        connection.send_stop()
        deadline = time.monotonic() + min(max(self.config.remote.timeout_seconds, 1), 8)
        while time.monotonic() < deadline:
            if self._stop_event.is_set() and awaiting_result:
                break
            try:
                message = connection.recv_message(timeout=0.2)
            except TimeoutError:
                if self._stop_event.is_set() and awaiting_result:
                    break
                if not awaiting_result:
                    return
                continue
            self._handle_funasr_message(message, tracker, workspace, metadata, current_ms)
            awaiting_result = tracker.has_open_segment
            if not awaiting_result:
                return
        forced_entry = tracker.force_finalize()
        if forced_entry is not None:
            self._commit_funasr_final_entry(forced_entry, workspace, metadata)

    def _drain_funasr_messages(
        self,
        connection,
        tracker: _FunAsrDraftTracker,
        workspace: SessionWorkspace,
        metadata: SessionMetadata,
        current_ms: int,
    ) -> None:
        if connection is None:
            return
        while True:
            try:
                message = connection.recv_message(timeout=0.01)
            except TimeoutError:
                return
            self._handle_funasr_message(message, tracker, workspace, metadata, current_ms)

    def _handle_funasr_message(
        self,
        message: FunAsrMessage,
        tracker: _FunAsrDraftTracker,
        workspace: SessionWorkspace,
        metadata: SessionMetadata,
        current_ms: int,
    ) -> None:
        text = message.text
        if not text.strip():
            return
        if _is_funasr_final_message(message):
            entry = tracker.build_final_entry(
                text,
                current_ms=current_ms,
                bounds_ms=message.bounds_ms,
            )
            if entry is None:
                return
            self._commit_funasr_final_entry(entry, workspace, metadata)
            return
        payload = tracker.build_partial_payload(
            text,
            current_ms=current_ms,
            bounds_ms=message.bounds_ms,
        )
        if payload is None:
            return
        self._persist_live_entry(
            workspace,
            metadata,
            TranscriptEntry(
                segment_id=str(payload["segment_id"]),
                started_ms=int(payload["started_ms"]),
                ended_ms=int(payload["ended_ms"]),
                text=str(payload["text"]),
            ),
        )

    def _commit_funasr_final_entry(
        self,
        entry: TranscriptEntry,
        workspace: SessionWorkspace,
        metadata: SessionMetadata,
    ) -> None:
        self._upsert_final_entry(entry)
        self._persist_live_entry(workspace, metadata, entry)
        _emit_progress(
            self.on_progress,
            "segment_transcribed",
            f"片段 {entry.segment_id} 已转写",
            session_id=metadata.session_id,
            current=len(self.entries),
        )

    def _persist_live_entry(
        self,
        workspace: SessionWorkspace,
        metadata: SessionMetadata,
        entry: TranscriptEntry,
    ) -> None:
        workspace.record_segment_text(
            entry.segment_id,
            entry.started_ms,
            entry.ended_ms,
            entry.text,
            speaker_label=entry.speaker_label,
        )
        current_metadata = require_runtime_session(self.config.root_dir, workspace.root.name)
        content = build_transcript_note(
            current_metadata,
            workspace.transcript_entries(),
            status=current_metadata.status,
        )
        workspace.write_transcript(content)

    def _upsert_final_entry(self, entry: TranscriptEntry) -> None:
        for index, current in enumerate(self.entries):
            if current.segment_id != entry.segment_id:
                continue
            self.entries[index] = entry
            break
        else:
            self.entries.append(entry)
        self.entries.sort(key=lambda item: (item.started_ms, item.segment_id))
