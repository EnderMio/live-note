from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from live_note.remote_sync import (
    apply_remote_artifacts,
    ensure_remote_workspace,
    sync_remote_transcript_snapshot,
)
from live_note.audio.capture import (
    AudioCaptureError,
    AudioCaptureService,
    InputLevel,
    describe_input_level,
    resolve_input_device,
)
from live_note.config import AppConfig, with_refine_auto_after_live
from live_note.domain import AudioFrame
from live_note.obsidian.client import ObsidianClient
from live_note.obsidian.renderer import (
    build_structured_failure_note,
    build_transcript_failure_note,
)
from live_note.remote.client import RemoteClient, RemoteClientError
from live_note.remote.protocol import entry_from_dict, metadata_from_dict
from live_note.runtime.session_mutations import require_runtime_session
from live_note.runtime.session_outputs import try_sync_note
from live_note.runtime.types import ProgressCallback, ProgressEvent
from live_note.session_workspace import SessionWorkspace

DEFAULT_REMOTE_LIVE_SNAPSHOT_POLL_SECONDS = 0.8


@dataclass
class _RemoteAudioBatcher:
    chunk_ms: int
    _buffer: bytearray = field(default_factory=bytearray)
    _duration_ms: int = 0

    def push(self, frame: AudioFrame) -> bytes | None:
        self._buffer.extend(frame.pcm16)
        self._duration_ms += max(1, frame.ended_ms - frame.started_ms)
        if self._duration_ms < max(1, self.chunk_ms):
            return None
        return self.flush()

    def flush(self) -> bytes | None:
        if not self._buffer:
            return None
        payload = bytes(self._buffer)
        self._buffer.clear()
        self._duration_ms = 0
        return payload


class RemoteLiveRunner:
    def __init__(
        self,
        config: AppConfig,
        title: str,
        source: str,
        kind: str,
        language: str | None = None,
        on_progress: ProgressCallback | None = None,
        auto_refine_after_live: bool | None = None,
        *,
        client: RemoteClient | None = None,
    ) -> None:
        self.config = with_refine_auto_after_live(config, auto_refine_after_live)
        self.title = title
        self.source = source
        self.kind = kind
        self.language = language or self.config.whisper.language
        self.on_progress = on_progress
        self.client = client or RemoteClient(self.config.remote)
        self._stop_event = threading.Event()
        self._pause_requested = False
        self._control_commands: queue.Queue[str] = queue.Queue()
        self._event_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._session_started_event = threading.Event()
        self.session_id: str | None = None
        self.workspace: SessionWorkspace | None = None
        self._explicit_remote_failure_reason: str | None = None
        self._remote_stop_acknowledged = False
        self._last_live_snapshot_signature: tuple[object, ...] | None = None
        self._last_live_snapshot_at_monotonic = 0.0

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
        frame_queue: queue.Queue[object] = queue.Queue(maxsize=self.config.audio.queue_size)
        capture = AudioCaptureService(self.config.audio, device, frame_queue)
        done_event = threading.Event()
        error_queue: queue.Queue[BaseException] = queue.Queue()
        batcher = _RemoteAudioBatcher(chunk_ms=self.config.remote.live_chunk_ms)
        self._session_started_event.clear()
        self._explicit_remote_failure_reason = None
        self._remote_stop_acknowledged = False
        try:
            with self.client.connect_live(self._live_start_payload(device)) as connection:
                reader = threading.Thread(
                    target=self._reader_loop,
                    args=(connection, done_event, error_queue),
                    daemon=True,
                )
                reader.start()
                self._wait_for_session_started(done_event, error_queue)
                if hasattr(capture, "set_level_callback"):
                    capture.set_level_callback(self._build_input_level_callback())
                capture.start()
                stop_sent = False
                stopping_capture = False
                try:
                    while not done_event.is_set():
                        self._drain_control_commands(capture, connection, batcher)
                        try:
                            self._raise_if_error(error_queue)
                        except RuntimeError as exc:
                            if not self._handle_post_stop_disconnect(exc):
                                raise
                            done_event.set()
                            continue
                        if capture.error:
                            raise AudioCaptureError(str(capture.error))
                        if self._stop_event.is_set() and not stopping_capture:
                            capture.stop()
                            stopping_capture = True
                        if not capture.is_alive and not stop_sent and not stopping_capture:
                            raise AudioCaptureError("音频采集线程已停止。")

                        try:
                            item = frame_queue.get(timeout=0.1)
                        except queue.Empty:
                            if stopping_capture and not stop_sent:
                                self._flush_audio_batch(connection, batcher)
                                connection.send_control("stop")
                                stop_sent = True
                            elif not stop_sent:
                                self._flush_audio_batch(connection, batcher)
                            self._drain_remote_events(done_event)
                            self._maybe_sync_live_snapshot()
                            continue
                        if isinstance(item, AudioFrame) and stop_sent:
                            self._drain_remote_events(done_event)
                            self._maybe_sync_live_snapshot()
                            continue
                        if isinstance(item, AudioFrame) and not capture.is_paused:
                            payload = batcher.push(item)
                            if payload:
                                connection.send_audio(payload)
                        if (
                            stopping_capture
                            and not capture.is_alive
                            and frame_queue.empty()
                            and not stop_sent
                        ):
                            self._flush_audio_batch(connection, batcher)
                            connection.send_control("stop")
                            stop_sent = True
                        self._drain_remote_events(done_event)
                        self._maybe_sync_live_snapshot()
                    self._drain_remote_events(done_event)
                    self._flush_audio_batch(connection, batcher)
                    reader.join(timeout=max(self.config.remote.timeout_seconds, 10))
                    self._drain_remote_events(done_event)
                    try:
                        self._raise_if_error(error_queue)
                    except RuntimeError as exc:
                        if not self._handle_post_stop_disconnect(exc):
                            raise
                finally:
                    capture.stop()
                    capture.join(timeout=5)
            return 0
        except BaseException as exc:
            should_publish_failure = self._explicit_remote_failure_reason is not None or isinstance(
                exc, AudioCaptureError
            )
            if self.workspace is not None and should_publish_failure:
                session_id = self.session_id or self.workspace.root.name
                metadata = require_runtime_session(self.config.root_dir, session_id)
                logger = self.workspace.session_logger()
                obsidian = ObsidianClient(self.config.obsidian)
                transcript = build_transcript_failure_note(metadata, str(exc))
                structured = build_structured_failure_note(
                    metadata,
                    transcript_note_path=metadata.transcript_note_path,
                    reason=str(exc),
                )
                self.workspace.write_transcript(transcript)
                self.workspace.write_structured(structured)
                try_sync_note(
                    obsidian,
                    metadata.transcript_note_path,
                    transcript,
                    logger,
                    "远端原文失败笔记",
                )
                try_sync_note(
                    obsidian,
                    metadata.structured_note_path,
                    structured,
                    logger,
                    "远端整理失败笔记",
                )
            raise

    def _live_start_payload(self, device) -> dict[str, Any]:
        return {
            "title": self.title,
            "kind": self.kind,
            "language": self.language,
            "source_label": device.name,
            "source_ref": str(device.index),
            "auto_refine_after_live": self.config.refine.auto_after_live,
            "speaker_enabled": self.config.speaker.enabled,
        }

    def _reader_loop(
        self,
        connection,
        done_event: threading.Event,
        error_queue: queue.Queue[BaseException],
    ) -> None:
        try:
            for payload in connection.iter_events():
                self._event_queue.put(payload)
                if payload.get("type") in {"completed", "error"}:
                    done_event.set()
                    return
            error_queue.put(
                RemoteClientError(
                    "远端连接在会话就绪前已关闭。"
                    if not self._session_started_event.is_set()
                    else "远端连接已断开。"
                )
            )
            done_event.set()
        except BaseException as exc:
            error_queue.put(exc)
            done_event.set()

    def _wait_for_session_started(
        self,
        done_event: threading.Event,
        error_queue: queue.Queue[BaseException],
    ) -> None:
        timeout_seconds = max(int(self.config.remote.timeout_seconds), 1)
        deadline = time.monotonic() + timeout_seconds
        while not self._session_started_event.is_set():
            self._raise_if_error(error_queue)
            self._drain_remote_events(done_event)
            if self._session_started_event.is_set():
                return
            if done_event.is_set():
                raise RemoteClientError("远端会话在就绪前已结束。")
            if time.monotonic() >= deadline:
                raise RemoteClientError(f"等待远端会话就绪超时（{timeout_seconds}s）。")
            done_event.wait(0.05)

    def _drain_control_commands(
        self,
        capture: AudioCaptureService,
        connection,
        batcher: _RemoteAudioBatcher,
    ) -> None:
        while True:
            try:
                command = self._control_commands.get_nowait()
            except queue.Empty:
                return
            if command == "pause":
                self._flush_audio_batch(connection, batcher)
                capture.pause()
                connection.send_control("pause")
            elif command == "resume":
                capture.resume()
                connection.send_control("resume")

    def _flush_audio_batch(self, connection, batcher: _RemoteAudioBatcher) -> None:
        payload = batcher.flush()
        if payload:
            connection.send_audio(payload)

    def _build_input_level_callback(self) -> Callable[[InputLevel], None]:
        def callback(level: InputLevel) -> None:
            self._emit(
                "input_level",
                describe_input_level(level),
                session_id=self.session_id,
                current=max(0, min(100, round(level.normalized * 100))),
                total=100,
            )

        return callback

    def _drain_remote_events(self, done_event: threading.Event) -> None:
        while True:
            try:
                payload = self._event_queue.get_nowait()
            except queue.Empty:
                return
            event_type = str(payload.get("type", "")).strip().lower()
            if event_type == "progress":
                self._emit_progress_payload(payload)
                continue
            if event_type == "session_started":
                self._apply_session_started(payload)
                self._emit(
                    "listening",
                    "已连接远端录音服务。",
                    session_id=self.session_id,
                )
                continue
            if event_type == "capture_finished":
                self._emit(
                    "capture_finished",
                    str(payload.get("message") or "录音已停止，后台继续转写、精修和整理。"),
                    session_id=self.session_id,
                )
                continue
            if event_type == "stop_accepted":
                self._remote_stop_acknowledged = True
                self._maybe_sync_live_snapshot(force=True)
                self._emit(
                    "stopping",
                    str(payload.get("message") or "远端已接受停止请求，正在收尾当前片段。"),
                    session_id=self.session_id,
                )
                continue
            if event_type == "handoff_committed":
                self._maybe_sync_live_snapshot(force=True)
                self._emit(
                    "stopping",
                    str(payload.get("message") or "后台整理任务已完成 durable handoff。"),
                    session_id=self.session_id,
                )
                continue
            if event_type == "completed":
                if self._remote_stop_acknowledged:
                    done_event.set()
                    continue
                self._sync_remote_artifacts(
                    self.client.get_session_artifacts(str(payload["session_id"]))
                )
                self._emit("done", "远端会话已完成。", session_id=self.session_id)
                done_event.set()
                continue
            if event_type == "error":
                self._explicit_remote_failure_reason = str(payload.get("error") or "远端会话失败。")
                raise RemoteClientError(self._explicit_remote_failure_reason)

    def _apply_session_started(self, payload: dict[str, Any]) -> None:
        metadata_payload = payload.get("metadata")
        if not isinstance(metadata_payload, dict):
            raise RemoteClientError("远端 session_started 缺少 metadata。")
        metadata = metadata_from_dict(dict(metadata_payload))
        self.workspace = ensure_remote_workspace(
            self.config,
            metadata,
            runtime_status=_optional_runtime_status(payload.get("runtime_status")),
        )
        self.session_id = metadata.session_id
        self._last_live_snapshot_signature = None
        self._last_live_snapshot_at_monotonic = 0.0
        self._session_started_event.set()

    def _sync_remote_artifacts(self, payload: dict[str, Any]) -> None:
        metadata = metadata_from_dict(dict(payload["metadata"]))
        entries = [entry_from_dict(dict(item)) for item in payload.get("entries", [])]
        local_metadata = apply_remote_artifacts(
            self.config,
            metadata,
            entries,
            runtime_status=_optional_runtime_status(payload.get("runtime_status")),
            remote_updated_at=_optional_text(payload.get("updated_at")),
            transcript_content=_optional_text(payload.get("transcript_content")),
            structured_content=_optional_text(payload.get("structured_content")),
            on_progress=self.on_progress,
        )
        self.workspace = SessionWorkspace.load(Path(local_metadata.session_dir))
        self.session_id = local_metadata.session_id
        self._last_live_snapshot_signature = _snapshot_signature(metadata, entries)
        self._last_live_snapshot_at_monotonic = time.monotonic()

    def _maybe_sync_live_snapshot(self, *, force: bool = False) -> bool:
        if self.session_id is None or self.workspace is None:
            return False
        now = time.monotonic()
        if (
            not force
            and now - self._last_live_snapshot_at_monotonic
            < DEFAULT_REMOTE_LIVE_SNAPSHOT_POLL_SECONDS
        ):
            return False
        self._last_live_snapshot_at_monotonic = now
        try:
            payload = self.client.get_session_artifacts(self.session_id)
        except Exception:
            return False
        metadata = metadata_from_dict(dict(payload["metadata"]))
        entries = [entry_from_dict(dict(item)) for item in payload.get("entries", [])]
        signature = _snapshot_signature(metadata, entries)
        if signature == self._last_live_snapshot_signature:
            return False
        local_metadata = sync_remote_transcript_snapshot(
            self.config,
            metadata,
            entries,
            runtime_status=_optional_runtime_status(payload.get("runtime_status")),
            remote_updated_at=_optional_text(payload.get("updated_at")),
        )
        self.workspace = SessionWorkspace.load(Path(local_metadata.session_dir))
        self.session_id = local_metadata.session_id
        self._last_live_snapshot_signature = signature
        return True

    def _emit_progress_payload(self, payload: dict[str, Any]) -> None:
        stage = str(payload.get("stage") or "progress")
        if stage == "segment_transcribed":
            return
        session_id = payload.get("session_id") or self.session_id
        self._emit(
            stage,
            str(payload.get("message") or ""),
            session_id=str(session_id) if session_id else None,
            current=int(payload["current"]) if payload.get("current") is not None else None,
            total=int(payload["total"]) if payload.get("total") is not None else None,
            error=str(payload["error"]) if payload.get("error") is not None else None,
        )

    def _emit(
        self,
        stage: str,
        message: str,
        *,
        session_id: str | None = None,
        current: int | None = None,
        total: int | None = None,
        error: str | None = None,
        task_id: str | None = None,
    ) -> None:
        if self.on_progress is None:
            return
        self.on_progress(
            ProgressEvent(
                stage=stage,
                message=message,
                session_id=session_id,
                current=current,
                total=total,
                error=error,
                task_id=task_id,
            )
        )

    def _handle_post_stop_disconnect(self, exc: RuntimeError) -> bool:
        if not self._remote_stop_acknowledged:
            return False
        if str(exc) != "远端连接已断开。":
            return False
        self._emit(
            "stopping",
            "远端已确认停止，实时连接已关闭，后台整理将由任务投影反映。",
            session_id=self.session_id,
        )
        self._maybe_sync_live_snapshot(force=True)
        return True

    def _raise_if_error(self, error_queue: queue.Queue[BaseException]) -> None:
        try:
            error = error_queue.get_nowait()
        except queue.Empty:
            return
        raise RuntimeError(str(error)) from error


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_runtime_status(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _snapshot_signature(metadata, entries) -> tuple[object, ...]:
    last_entry = entries[-1] if entries else None
    return (
        metadata.session_id,
        metadata.status,
        metadata.transcript_source,
        metadata.refine_status,
        metadata.speaker_status,
        len(entries),
        last_entry.segment_id if last_entry is not None else None,
        last_entry.ended_ms if last_entry is not None else None,
    )
