from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from live_note.app.coordinator import (
    FRAME_STOP,
    SessionCoordinator,
    _attach_console_logging,
    _emit_progress,
    _mark_session_failed,
    _run_live_refinement,
    _runtime_whisper_config,
    create_session_metadata,
)
from live_note.app.journal import SessionWorkspace, build_workspace, list_sessions
from live_note.audio.segmentation import SpeechSegmenter
from live_note.config import AppConfig
from live_note.domain import AudioFrame, SessionMetadata
from live_note.obsidian.client import ObsidianClient
from live_note.obsidian.renderer import build_transcript_note
from live_note.transcribe.whisper import WhisperInferenceClient, WhisperServerProcess

from .protocol import LiveStartRequest, entry_to_dict, metadata_to_dict, progress_to_payload
from .speaker import apply_speaker_labels


class RemoteSessionService:
    def __init__(self, config: AppConfig):
        self.config = config

    @property
    def api_token(self) -> str | None:
        return self.config.serve.api_token or self.config.remote.api_token

    def health_payload(self) -> dict[str, object]:
        return {
            "status": "ok",
            "service": "live-note-remote",
            "speaker_enabled": self.config.speaker.enabled,
            "remote_enabled": self.config.remote.enabled,
        }

    def list_sessions_payload(self) -> list[dict[str, object]]:
        items: list[dict[str, object]] = []
        for root in list_sessions(self.config.root_dir):
            try:
                workspace = SessionWorkspace.load(root)
                metadata = workspace.read_session()
                entries = workspace.transcript_entries()
            except Exception:
                continue
            items.append(
                {
                    "session_id": metadata.session_id,
                    "title": metadata.title,
                    "kind": metadata.kind,
                    "status": metadata.status,
                    "started_at": metadata.started_at,
                    "execution_target": metadata.execution_target,
                    "speaker_status": metadata.speaker_status,
                    "entry_count": len(entries),
                }
            )
        return sorted(items, key=lambda item: str(item["started_at"]), reverse=True)

    def session_payload(self, session_id: str) -> dict[str, object]:
        workspace = build_workspace(self.config.root_dir, session_id)
        return metadata_to_dict(workspace.read_session())

    def artifacts_payload(self, session_id: str) -> dict[str, object]:
        workspace = build_workspace(self.config.root_dir, session_id)
        metadata = workspace.read_session()
        return {
            "session_id": session_id,
            "metadata": metadata_to_dict(metadata),
            "entries": [entry_to_dict(item) for item in workspace.transcript_entries()],
            "has_session_audio": workspace.session_live_wav.exists(),
        }

    def request_refine(self, session_id: str) -> dict[str, object]:
        workspace = build_workspace(self.config.root_dir, session_id)
        metadata = workspace.read_session()
        logger = workspace.session_logger()
        previous_source = metadata.transcript_source
        metadata = workspace.update_session(status="refining", refine_status="refining")
        try:
            metadata = _run_live_refinement(
                config=self.config,
                workspace=workspace,
                metadata=metadata,
                logger=logger,
            )
        except Exception:
            metadata = workspace.update_session(
                transcript_source=previous_source,
                refine_status="failed",
                status="failed",
            )
            raise
        metadata = apply_speaker_labels(self.config, workspace, metadata)
        metadata = workspace.update_session(status="completed")
        return {
            "session_id": session_id,
            "accepted": True,
            "status": metadata.status,
            "speaker_status": metadata.speaker_status,
            "refine_status": metadata.refine_status,
        }

    async def live_session(self, websocket: WebSocket) -> None:
        await websocket.accept()
        start_payload = await websocket.receive_json()
        request = LiveStartRequest.from_payload(start_payload)
        event_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        runner = RemoteLiveSessionRunner(
            config=self.config,
            request=request,
            on_progress=lambda event: event_queue.put(progress_to_payload(event)),
        )
        metadata = runner.start()
        await websocket.send_json(
            {
                "type": "session_started",
                "metadata": metadata_to_dict(metadata),
            }
        )

        receiver = asyncio.create_task(self._receive_live_messages(websocket, runner))
        emitted_entries = 0
        try:
            while receiver.done() is False or runner.is_alive or not event_queue.empty():
                while emitted_entries < len(runner.entries):
                    entry = runner.entries[emitted_entries]
                    emitted_entries += 1
                    await websocket.send_json(
                        {
                            "type": "segment_final",
                            "session_id": runner.session_id,
                            "segment_id": entry.segment_id,
                            "started_ms": entry.started_ms,
                            "ended_ms": entry.ended_ms,
                            "text": entry.text,
                            "speaker_label": entry.speaker_label,
                        }
                    )
                try:
                    payload = await asyncio.to_thread(event_queue.get, True, 0.2)
                except queue.Empty:
                    continue
                await websocket.send_json(payload)
        finally:
            if not receiver.done():
                receiver.cancel()
            runner.request_stop()
            runner.join(timeout=5)

        terminal = (
            {
                "type": "error",
                "session_id": runner.session_id,
                "error": runner.failure_message,
            }
            if runner.failure_message
            else {
                "type": "completed",
                "session_id": runner.session_id,
            }
        )
        await websocket.send_json(terminal)

    async def _receive_live_messages(
        self,
        websocket: WebSocket,
        runner: RemoteLiveSessionRunner,
    ) -> None:
        try:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    runner.request_stop()
                    return
                if message.get("bytes") is not None:
                    runner.feed_audio(message["bytes"])
                    continue
                payload = json.loads(message["text"]) if message.get("text") else {}
                action = str(payload.get("type", "")).strip().lower()
                if action == "pause":
                    runner.request_pause()
                elif action == "resume":
                    runner.request_resume()
                elif action == "stop":
                    runner.request_stop()
                    return
        except WebSocketDisconnect:
            runner.request_stop()


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


class RemoteLiveSessionRunner(SessionCoordinator):
    def __init__(
        self,
        *,
        config: AppConfig,
        request: LiveStartRequest,
        on_progress,
    ) -> None:
        super().__init__(
            config=config,
            title=request.title,
            source=request.source_ref,
            kind=request.kind,
            language=request.language,
            on_progress=on_progress,
            auto_refine_after_live=request.auto_refine_after_live,
        )
        self.request = request
        self.frame_queue: queue.Queue[AudioFrame | object] = queue.Queue(
            maxsize=self.config.audio.queue_size
        )
        self.segment_queue: queue.Queue[object] = queue.Queue(maxsize=32)
        self._audio_offset_ms = 0
        self._pcm_buffer = bytearray()
        self._capture = _RemoteCaptureState()
        self._thread: threading.Thread | None = None
        self.workspace: SessionWorkspace | None = None
        self.metadata: SessionMetadata | None = None
        self.failure_message: str | None = None

    @property
    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> SessionMetadata:
        metadata = create_session_metadata(
            config=self.config,
            title=self.request.title,
            kind=self.request.kind,
            language=self.language,
            input_mode="live",
            source_label=self.request.source_label,
            source_ref=self.request.source_ref,
        )
        metadata = replace(
            metadata,
            execution_target="remote",
            remote_session_id=metadata.session_id,
            speaker_status="pending" if self.config.speaker.enabled else "disabled",
        )
        self.session_id = metadata.session_id
        self.metadata = metadata
        self.workspace = SessionWorkspace.create(Path(metadata.session_dir), metadata)
        self._thread = threading.Thread(target=self.run, name=f"remote-live-{metadata.session_id}")
        self._thread.start()
        return metadata

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def feed_audio(self, pcm16: bytes) -> None:
        if not pcm16 or self._capture.is_paused:
            return
        self._pcm_buffer.extend(pcm16)
        frame_bytes = max(
            2,
            int(
                self.config.audio.sample_rate
                * self.config.audio.frame_duration_ms
                / 1000
            )
            * 2,
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
        

    def run(self) -> int:
        assert self.workspace is not None
        assert self.metadata is not None
        workspace = self.workspace
        metadata = self.metadata
        logger = workspace.session_logger()
        disabled_obsidian = ObsidianClient(
            replace(self.config.obsidian, enabled=False, api_key=None)
        )
        try:
            _attach_console_logging()
            workspace.write_transcript(build_transcript_note(metadata, [], status="live"))
            metadata = workspace.update_status("live")
            _emit_progress(
                self.on_progress,
                "starting",
                f"已连接远端会话：{metadata.title}",
                session_id=metadata.session_id,
            )
            _emit_progress(
                self.on_progress,
                "listening",
                f"正在接收远端音频：{metadata.source_label}",
                session_id=metadata.session_id,
            )
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
                while True:
                    self._drain_control_commands(
                        capture=self._capture,
                        frame_queue=self.frame_queue,
                        workspace=workspace,
                        metadata=metadata,
                        logger=logger,
                    )
                    self._raise_thread_error_if_any()
                    if self._stop_event.wait(0.1):
                        capture_finished = True
                        break
                if capture_finished and not capture_announced:
                    metadata = workspace.update_status("finalizing")
                    _emit_progress(
                        self.on_progress,
                        "capture_finished",
                        "录音已停止，后台继续转写、精修和整理。",
                        session_id=metadata.session_id,
                    )
                    capture_announced = True
                self.frame_queue.put(FRAME_STOP)
                segment_thread.join()
                transcribe_thread.join()

            self._raise_thread_error_if_any()
            metadata = _run_remote_postprocess(
                self.config,
                workspace,
                workspace.read_session(),
                logger=logger,
                on_progress=self.on_progress,
            )
            workspace.update_session(status="completed")
            _emit_progress(
                self.on_progress,
                "done",
                "远端会话已完成。",
                session_id=metadata.session_id,
            )
            return 0
        except BaseException as exc:
            self.failure_message = str(exc)
            _mark_session_failed(
                workspace=workspace,
                logger=logger,
                label="远端会话",
                exc=exc,
                on_progress=self.on_progress,
            )
            raise


def _run_remote_postprocess(
    config: AppConfig,
    workspace: SessionWorkspace,
    metadata: SessionMetadata,
    *,
    logger: logging.Logger,
    on_progress,
) -> SessionMetadata:
    current = metadata
    if config.refine.enabled and config.refine.auto_after_live:
        previous_source = current.transcript_source
        try:
            current = _run_live_refinement(
                config=config,
                workspace=workspace,
                metadata=workspace.update_session(status="refining", refine_status="refining"),
                logger=logger,
                on_progress=on_progress,
            )
        except Exception as exc:
            logger.error("远端自动离线精修失败，将保留实时草稿: %s", exc)
            current = workspace.update_session(
                transcript_source=previous_source,
                refine_status="failed",
            )
            _emit_progress(
                on_progress,
                "error",
                f"远端自动离线精修失败：{exc}",
                session_id=current.session_id,
                error=str(exc),
            )
    current = apply_speaker_labels(config, workspace, current, on_progress=on_progress)
    content = build_transcript_note(
        current,
        workspace.transcript_entries(),
        status=workspace.read_session().status,
        session_audio_path="session.live.wav" if workspace.session_live_wav.exists() else None,
    )
    workspace.write_transcript(content)
    return current
