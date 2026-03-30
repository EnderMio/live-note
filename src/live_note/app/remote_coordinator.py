from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from live_note.audio.capture import (
    AudioCaptureError,
    AudioCaptureService,
    InputLevel,
    describe_input_level,
    resolve_input_device,
)
from live_note.config import AppConfig, with_refine_auto_after_live
from live_note.domain import AudioFrame, TranscriptEntry
from live_note.obsidian.client import ObsidianClient
from live_note.obsidian.renderer import build_transcript_note
from live_note.remote.client import RemoteClient, RemoteClientError
from live_note.remote.protocol import entry_from_dict, metadata_from_dict
from live_note.utils import compact_text

from .events import ProgressCallback, ProgressEvent
from .journal import SessionWorkspace
from .remote_sync import apply_remote_artifacts, ensure_remote_workspace
from .remote_tasks import upsert_remote_task_payload
from .session_outputs import publish_failure_outputs

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


def _merge_partial_segment_append(current: str, cleaned: str) -> str:
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


class RemoteLiveCoordinator:
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
        self._live_entries: list[TranscriptEntry] = []
        self._finalized_segment_ids: set[str] = set()
        self._explicit_remote_failure_reason: str | None = None

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
                        self._raise_if_error(error_queue)
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
                            if stopping_capture and not capture.is_alive and not stop_sent:
                                self._flush_audio_batch(connection, batcher)
                                connection.send_control("stop")
                                stop_sent = True
                            elif not stop_sent:
                                self._flush_audio_batch(connection, batcher)
                            self._drain_remote_events(done_event)
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
                    self._flush_audio_batch(connection, batcher)
                    reader.join(timeout=max(self.config.remote.timeout_seconds, 10))
                    self._raise_if_error(error_queue)
                finally:
                    capture.stop()
                    capture.join(timeout=5)
            return 0
        except BaseException as exc:
            should_publish_failure = self._explicit_remote_failure_reason is not None or isinstance(
                exc, AudioCaptureError
            )
            if self.workspace is not None and should_publish_failure:
                publish_failure_outputs(
                    workspace=self.workspace,
                    metadata=self.workspace.read_session(),
                    obsidian=ObsidianClient(self.config.obsidian),
                    logger=self.workspace.session_logger(),
                    reason=str(exc),
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
            if event_type == "segment_final":
                self._append_live_segment(payload)
                continue
            if event_type == "segment_partial":
                self._append_live_segment(payload, emit_final_progress=False)
                continue
            if event_type == "capture_finished":
                self._emit(
                    "capture_finished",
                    str(payload.get("message") or "录音已停止，后台继续转写、精修和整理。"),
                    session_id=self.session_id,
                )
                continue
            if event_type == "completed":
                postprocess_task = payload.get("postprocess_task")
                if isinstance(postprocess_task, dict):
                    task_payload = dict(postprocess_task)
                    upsert_remote_task_payload(
                        self._remote_tasks_path(),
                        task_payload,
                        fallback_session_id=str(payload.get("session_id") or self.session_id or ""),
                        fallback_label="后台整理",
                    )
                    self._emit(
                        "postprocess_attached",
                        "录音已结束，后台整理已转为远端任务。",
                        session_id=self.session_id,
                        task_id=str(task_payload.get("task_id") or "") or None,
                    )
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
        if isinstance(metadata_payload, dict):
            metadata = metadata_from_dict(dict(metadata_payload))
        else:
            metadata = metadata_from_dict(
                {
                    "session_id": str(payload["session_id"]),
                    "title": str(payload.get("title") or self.title),
                    "kind": str(payload.get("kind") or self.kind),
                    "input_mode": "live",
                    "source_label": str(payload.get("source_label") or "remote-audio"),
                    "source_ref": str(payload.get("source_ref") or "remote"),
                    "language": str(payload.get("language") or self.language),
                    "started_at": str(payload["started_at"]),
                    "transcript_note_path": "",
                    "structured_note_path": "",
                    "session_dir": "",
                    "status": "live",
                    "transcript_source": "live",
                    "refine_status": "pending" if self.config.refine.enabled else "disabled",
                    "execution_target": "remote",
                    "remote_session_id": str(payload["session_id"]),
                    "speaker_status": "disabled",
                }
            )
        self.workspace = ensure_remote_workspace(self.config, metadata)
        self.session_id = metadata.session_id
        self._live_entries = self.workspace.transcript_entries()
        self._finalized_segment_ids = {entry.segment_id for entry in self._live_entries}
        self._session_started_event.set()

    def _append_live_segment(
        self,
        payload: dict[str, Any],
        *,
        emit_final_progress: bool = True,
    ) -> None:
        if self.workspace is None:
            return
        metadata = self.workspace.read_session()
        segment_id = str(payload["segment_id"])
        is_partial = not emit_final_progress
        if is_partial and segment_id in self._finalized_segment_ids:
            return
        started_ms = int(payload["started_ms"])
        ended_ms = int(payload["ended_ms"])
        text = str(payload["text"])
        current_entry = self._entry_for_segment(segment_id)
        if is_partial:
            merged_text = _merge_partial_segment_text(
                current_entry.text if current_entry is not None else "",
                text,
            )
            if merged_text is None:
                return
            text = merged_text
            if current_entry is not None:
                started_ms = min(started_ms, current_entry.started_ms)
                ended_ms = max(ended_ms, current_entry.ended_ms)
        speaker_label = (
            str(payload["speaker_label"]) if payload.get("speaker_label") is not None else None
        )
        self.workspace.record_segment_text(
            segment_id,
            started_ms,
            ended_ms,
            text,
            speaker_label=speaker_label,
        )
        entry = TranscriptEntry(
            segment_id=segment_id,
            started_ms=started_ms,
            ended_ms=ended_ms,
            text=text,
            speaker_label=speaker_label,
        )
        self._upsert_live_entry(entry)
        content = build_transcript_note(metadata, list(self._live_entries), status="live")
        self.workspace.write_transcript(content)
        if not is_partial:
            self._finalized_segment_ids.add(segment_id)
        if emit_final_progress:
            self._emit(
                "segment_transcribed",
                f"片段 {segment_id} 已转写",
                session_id=self.session_id,
            )

    def _sync_remote_artifacts(self, payload: dict[str, Any]) -> None:
        metadata = metadata_from_dict(dict(payload["metadata"]))
        entries = [entry_from_dict(dict(item)) for item in payload.get("entries", [])]
        local_metadata = apply_remote_artifacts(
            self.config,
            metadata,
            entries,
            transcript_content=_optional_text(payload.get("transcript_content")),
            structured_content=_optional_text(payload.get("structured_content")),
            on_progress=self.on_progress,
        )
        self.workspace = SessionWorkspace.load(Path(local_metadata.session_dir))
        self.session_id = local_metadata.session_id
        self._live_entries = self.workspace.transcript_entries()
        self._finalized_segment_ids = {entry.segment_id for entry in self._live_entries}

    def _upsert_live_entry(self, entry: TranscriptEntry) -> None:
        for index, current in enumerate(self._live_entries):
            if current.segment_id != entry.segment_id:
                continue
            self._live_entries[index] = entry
            break
        else:
            self._live_entries.append(entry)
        self._live_entries.sort(key=lambda item: (item.started_ms, item.segment_id))

    def _entry_for_segment(self, segment_id: str) -> TranscriptEntry | None:
        for entry in self._live_entries:
            if entry.segment_id == segment_id:
                return entry
        return None

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

    def _remote_tasks_path(self) -> Path:
        return (self.config.root_dir / ".live-note" / "remote_tasks.json").resolve()

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


def _merge_partial_segment_text(current: str, incoming: str) -> str | None:
    cleaned = incoming.strip()
    if not cleaned:
        return None
    if not current:
        return cleaned
    normalized_current = compact_text(current)
    normalized_incoming = compact_text(cleaned)
    if normalized_incoming.startswith(normalized_current):
        return cleaned
    if normalized_current == normalized_incoming or normalized_current.endswith(
        normalized_incoming
    ):
        return current
    return _merge_partial_segment_append(current, cleaned)
