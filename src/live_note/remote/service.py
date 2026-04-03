from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from fastapi import WebSocket, WebSocketDisconnect

from live_note.app.coordinator import (
    FRAME_PAUSE,
    FRAME_RESUME,
    FRAME_STOP,
    SessionCoordinator,
    _attach_console_logging,
    _emit_progress,
    _mark_session_failed,
    _open_session_audio_writer,
    _run_live_refinement,
    _runtime_whisper_config,
    create_session_metadata,
    retranscribe_session,
)
from live_note.app.events import ProgressEvent
from live_note.app.journal import SessionWorkspace, build_workspace, list_sessions
from live_note.app.session_outputs import publish_failure_outputs, publish_final_outputs
from live_note.app.task_errors import TaskCancelledError
from live_note.audio.segmentation import SpeechSegmenter
from live_note.config import AppConfig
from live_note.domain import AudioFrame, SessionMetadata, TranscriptEntry
from live_note.llm import OpenAiCompatibleClient
from live_note.obsidian.client import ObsidianClient
from live_note.obsidian.renderer import build_transcript_note
from live_note.transcribe.funasr import FunAsrMessage, FunAsrWebSocketClient
from live_note.transcribe.whisper import WhisperInferenceClient, WhisperServerProcess
from live_note.utils import compact_text, slugify_filename

from .protocol import LiveStartRequest, entry_to_dict, metadata_to_dict, progress_to_payload
from .speaker import apply_speaker_labels
from .tasks import RemoteTaskRegistry

_NO_SPACE_PUNCTUATION = frozenset("，。！？；：、】【（）《》「」『』、")


def _disabled_obsidian_client(config: AppConfig) -> ObsidianClient:
    return ObsidianClient(replace(config.obsidian, enabled=False, api_key=None))


def _server_local_only_config(config: AppConfig) -> AppConfig:
    return replace(config, obsidian=replace(config.obsidian, enabled=False, api_key=None))


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


class RemoteSessionService:
    def __init__(self, config: AppConfig):
        self.config = config
        self._request_upload_locks: dict[str, threading.Lock] = {}
        self._request_upload_locks_lock = threading.Lock()
        self.tasks = RemoteTaskRegistry(config, recover_runner=self._build_recovered_runner)
        self._recover_stale_remote_live_sessions()

    @property
    def api_token(self) -> str | None:
        return self.config.serve.api_token or self.config.remote.api_token

    def health_payload(self) -> dict[str, object]:
        return {
            "status": "ok",
            "service": "live-note-remote",
            "speaker_enabled": self.config.speaker.enabled,
            "funasr_enabled": self.config.funasr.enabled,
            "supports_imports": True,
            "supports_tasks": True,
            "server_id": self.tasks.server_id,
            "realtime_backend": "funasr" if self.config.funasr.enabled else "whisper_cpp",
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

    def _recover_stale_remote_live_sessions(self) -> None:
        stale_statuses = {"starting", "live", "paused", "finalizing"}
        disabled_obsidian = _disabled_obsidian_client(self.config)
        for root in list_sessions(self.config.root_dir):
            try:
                workspace = SessionWorkspace.load(root)
                metadata = workspace.read_session()
            except Exception:
                continue
            if metadata.execution_target != "remote" or metadata.status not in stale_statuses:
                continue
            logger = workspace.session_logger()
            publish_failure_outputs(
                workspace=workspace,
                metadata=metadata,
                obsidian=disabled_obsidian,
                logger=logger,
                reason="远端服务重启导致实时会话中断，请重试。",
            )

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
            "transcript_content": (
                workspace.transcript_md.read_text(encoding="utf-8")
                if workspace.transcript_md.exists()
                else ""
            ),
            "structured_content": (
                workspace.structured_md.read_text(encoding="utf-8")
                if workspace.structured_md.exists()
                else ""
            ),
        }

    def request_refine(
        self,
        session_id: str,
        *,
        request_id: str | None = None,
    ) -> dict[str, object]:
        return self.tasks.create_task(
            action="refine",
            label="离线精修并重写",
            session_id=session_id,
            request_id=request_id,
            task_spec={
                "action": "refine",
                "session_id": session_id,
            },
            build_runner=lambda task_id, _cancel_event: self._build_refine_runner(
                task_id,
                session_id,
            ),
        )

    def request_retranscribe(
        self,
        session_id: str,
        *,
        request_id: str | None = None,
    ) -> dict[str, object]:
        return self.tasks.create_task(
            action="retranscribe",
            label="重转写并重写",
            session_id=session_id,
            request_id=request_id,
            task_spec={
                "action": "retranscribe",
                "session_id": session_id,
            },
            build_runner=lambda task_id, _cancel_event: self._build_retranscribe_runner(
                task_id,
                session_id,
            ),
        )

    def create_import_task(
        self,
        *,
        filename: str,
        title: str | None,
        kind: str,
        language: str | None,
        speaker_enabled: bool | None,
        request_id: str | None,
        file_bytes: bytes,
    ) -> dict[str, object]:
        normalized_name = slugify_filename(Path(filename).name.strip())
        if not normalized_name:
            raise ValueError("上传文件名不能为空。")
        if not file_bytes:
            raise ValueError("上传文件为空。")
        normalized_request_id = str(request_id).strip() if request_id is not None else ""
        request_lock = (
            self._request_upload_lock(normalized_request_id) if normalized_request_id else None
        )
        if request_lock is not None:
            with request_lock:
                return self._create_import_task_locked(
                    normalized_name=normalized_name,
                    title=title,
                    kind=kind,
                    language=language,
                    speaker_enabled=speaker_enabled,
                    request_id=request_id,
                    normalized_request_id=normalized_request_id,
                    file_bytes=file_bytes,
                )
        return self._create_import_task_locked(
            normalized_name=normalized_name,
            title=title,
            kind=kind,
            language=language,
            speaker_enabled=speaker_enabled,
            request_id=request_id,
            normalized_request_id=normalized_request_id,
            file_bytes=file_bytes,
        )

    def _create_import_task_locked(
        self,
        *,
        normalized_name: str,
        title: str | None,
        kind: str,
        language: str | None,
        speaker_enabled: bool | None,
        request_id: str | None,
        normalized_request_id: str,
        file_bytes: bytes,
    ) -> dict[str, object]:
        existing = self.tasks.existing_task_for_request_id(request_id)
        if existing is not None:
            return existing
        upload_name = "upload.bin" if normalized_request_id else normalized_name
        uploaded_path = self._uploads_dir(request_id=normalized_request_id or None) / upload_name
        self._write_uploaded_file(uploaded_path, file_bytes)
        return self.tasks.create_task(
            action="import",
            label="文件导入",
            request_id=request_id,
            can_cancel=True,
            task_spec={
                "action": "import",
                "uploaded_path": str(uploaded_path),
                "title": title,
                "kind": kind,
                "language": language,
                "speaker_enabled": speaker_enabled,
            },
            build_runner=lambda task_id, cancel_event: self._build_import_runner(
                task_id=task_id,
                uploaded_path=uploaded_path,
                title=title,
                kind=kind,
                language=language,
                speaker_enabled=speaker_enabled,
                cancel_event=cancel_event,
            ),
        )

    def import_task_payload(self, task_id: str) -> dict[str, object]:
        return self.tasks.task_payload(task_id)

    def cancel_import_task(self, task_id: str) -> dict[str, object]:
        return self.tasks.cancel_task(task_id)

    def list_tasks_payload(self) -> dict[str, object]:
        return self.tasks.list_tasks()

    def task_payload(self, task_id: str) -> dict[str, object]:
        return self.tasks.task_payload(task_id)

    def cancel_task(self, task_id: str) -> dict[str, object]:
        return self.tasks.cancel_task(task_id)

    def _build_import_runner(
        self,
        *,
        task_id: str,
        uploaded_path: Path,
        title: str | None,
        kind: str,
        language: str | None,
        speaker_enabled: bool | None,
        cancel_event: threading.Event | None,
    ):
        def run() -> None:
            from live_note.app.coordinator import FileImportCoordinator

            config = _server_local_only_config(self.config)
            if speaker_enabled is not None:
                config = replace(
                    config,
                    speaker=replace(config.speaker, enabled=bool(speaker_enabled)),
                )
            runner = FileImportCoordinator(
                config=config,
                file_path=str(uploaded_path),
                title=title,
                kind=kind,
                language=language,
                on_progress=lambda event: self._record_task_progress(task_id, event),
                cancel_event=cancel_event,
            )
            try:
                exit_code = runner.run()
                if exit_code != 0:
                    raise RuntimeError(f"远端导入返回非零退出码: {exit_code}")
            finally:
                self._cleanup_uploaded_file(uploaded_path)

        return run

    def _build_refine_runner(self, task_id: str, session_id: str):
        def run() -> None:
            workspace = build_workspace(self.config.root_dir, session_id)
            metadata = workspace.read_session()
            logger = workspace.session_logger()
            disabled_obsidian = _disabled_obsidian_client(self.config)

            def on_progress(event: ProgressEvent) -> None:
                self._record_task_progress(task_id, event)

            previous_source = metadata.transcript_source
            metadata = workspace.update_session(status="refining", refine_status="refining")
            try:
                metadata = _run_live_refinement(
                    config=self.config,
                    workspace=workspace,
                    metadata=metadata,
                    logger=logger,
                    on_progress=on_progress,
                )
            except Exception as exc:
                metadata = workspace.update_session(
                    transcript_source=previous_source,
                    refine_status="failed",
                )
                self.tasks.record_progress(
                    task_id,
                    ProgressEvent(
                        stage="error",
                        message=f"远端离线精修失败：{exc}",
                        session_id=session_id,
                        error=str(exc),
                    ),
                )
            metadata = apply_speaker_labels(
                self.config,
                workspace,
                metadata,
                on_progress=on_progress,
            )
            publish_final_outputs(
                workspace=workspace,
                metadata=metadata,
                obsidian=disabled_obsidian,
                llm_client=OpenAiCompatibleClient(self.config.llm),
                logger=logger,
                on_progress=on_progress,
            )
            self.tasks.mark_completed(
                task_id,
                message="远端离线精修已完成。",
                result_changed=True,
            )

        return run

    def _build_retranscribe_runner(self, task_id: str, session_id: str):
        def run() -> None:
            exit_code = retranscribe_session(
                _server_local_only_config(self.config),
                session_id,
                on_progress=lambda event: self._record_task_progress(task_id, event),
            )
            if exit_code != 0:
                raise RuntimeError(f"远端重转写返回非零退出码: {exit_code}")
            self.tasks.mark_completed(
                task_id,
                message="远端重转写已完成。",
                result_changed=True,
            )

        return run

    def _create_postprocess_task(
        self,
        session_id: str,
        *,
        speaker_enabled: bool | None = None,
        start_event: threading.Event | None = None,
    ) -> dict[str, object]:
        return self.tasks.create_task(
            action="postprocess",
            label="后台整理",
            session_id=session_id,
            task_spec={
                "action": "postprocess",
                "session_id": session_id,
                "speaker_enabled": speaker_enabled,
            },
            build_runner=lambda task_id, cancel_event: self._build_postprocess_runner(
                task_id,
                session_id,
                speaker_enabled=speaker_enabled,
                start_event=start_event,
                cancel_event=cancel_event,
            ),
        )

    def _build_recovered_runner(
        self,
        task_id: str,
        task_spec: dict[str, object] | None,
        cancel_event: threading.Event | None,
    ):
        if not isinstance(task_spec, dict):
            raise ValueError("缺少 task_spec，无法恢复任务。")
        action = str(task_spec.get("action") or "").strip().lower()
        if action == "import":
            uploaded_path = self._validated_uploaded_path(task_spec.get("uploaded_path"))
            if not uploaded_path.exists():
                raise FileNotFoundError(f"import 文件不存在：{uploaded_path}")
            title = task_spec.get("title")
            kind = str(task_spec.get("kind") or "generic")
            language = task_spec.get("language")
            speaker_enabled = task_spec.get("speaker_enabled")
            return self._build_import_runner(
                task_id=task_id,
                uploaded_path=uploaded_path,
                title=str(title) if title is not None else None,
                kind=kind,
                language=str(language) if language is not None else None,
                speaker_enabled=bool(speaker_enabled) if speaker_enabled is not None else None,
                cancel_event=cancel_event,
            )
        if action == "refine":
            session_id = str(task_spec.get("session_id") or "").strip()
            if not session_id:
                raise ValueError("refine 任务缺少 session_id。")
            return self._build_refine_runner(task_id, session_id)
        if action == "retranscribe":
            session_id = str(task_spec.get("session_id") or "").strip()
            if not session_id:
                raise ValueError("retranscribe 任务缺少 session_id。")
            return self._build_retranscribe_runner(task_id, session_id)
        if action == "postprocess":
            raise RuntimeError("postprocess 任务暂不支持重启恢复，请手动重试。")
        raise ValueError(f"未知任务恢复动作：{action or '<empty>'}")

    def _build_postprocess_runner(
        self,
        task_id: str,
        session_id: str,
        *,
        speaker_enabled: bool | None = None,
        start_event: threading.Event | None = None,
        cancel_event: threading.Event | None = None,
    ):
        def run() -> None:
            if start_event is not None:
                while not start_event.wait(0.1):
                    if cancel_event is not None and cancel_event.is_set():
                        raise TaskCancelledError("远端后台整理已取消。")
            workspace = build_workspace(self.config.root_dir, session_id)
            config = self.config
            if speaker_enabled is not None:
                config = replace(
                    config,
                    speaker=replace(config.speaker, enabled=bool(speaker_enabled)),
                )
            logger = workspace.session_logger()
            disabled_obsidian = _disabled_obsidian_client(config)
            if workspace.read_session().status == "failed":
                raise RuntimeError("远端实时会话失败，后台整理未启动。")
            try:
                _run_remote_postprocess(
                    config,
                    workspace,
                    workspace.read_session(),
                    logger=logger,
                    on_progress=lambda event: self._record_task_progress(task_id, event),
                )
            except Exception as exc:
                publish_failure_outputs(
                    workspace=workspace,
                    metadata=workspace.read_session(),
                    obsidian=disabled_obsidian,
                    logger=logger,
                    reason=str(exc),
                )
                self.tasks.record_progress(
                    task_id,
                    ProgressEvent(
                        stage="error",
                        message=f"远端后台整理失败：{exc}",
                        session_id=session_id,
                        error=str(exc),
                    ),
                    result_changed=True,
                )
                raise
            self.tasks.mark_completed(
                task_id,
                message="远端后台整理已完成。",
                result_changed=True,
            )

        return run

    def _record_task_progress(self, task_id: str, event: ProgressEvent) -> None:
        result_changed = event.stage in {
            "segment_transcribed",
            "publishing",
            "summarizing",
            "done",
        }
        self.tasks.record_progress(task_id, event, result_changed=result_changed)

    def _uploads_dir(self, *, request_id: str | None = None) -> Path:
        root = self._uploads_root()
        normalized_request_id = str(request_id).strip() if request_id is not None else ""
        if normalized_request_id:
            return root / uuid5(NAMESPACE_URL, normalized_request_id).hex
        return root / uuid4().hex

    def _uploads_root(self) -> Path:
        return self.config.root_dir / ".live-note" / "remote-imports"

    def _request_upload_lock(self, request_id: str) -> threading.Lock:
        with self._request_upload_locks_lock:
            existing = self._request_upload_locks.get(request_id)
            if existing is not None:
                return existing
            created = threading.Lock()
            self._request_upload_locks[request_id] = created
            return created

    def _write_uploaded_file(self, uploaded_path: Path, file_bytes: bytes) -> None:
        uploaded_path.parent.mkdir(parents=True, exist_ok=True)
        uploaded_path.write_bytes(file_bytes)

    def _validated_uploaded_path(self, uploaded_path: object) -> Path:
        uploaded_path_text = str(uploaded_path).strip() if uploaded_path is not None else ""
        if not uploaded_path_text:
            raise ValueError("import 任务缺少 uploaded_path。")
        try:
            candidate = Path(uploaded_path_text).expanduser().resolve(strict=False)
        except OSError as exc:
            raise ValueError(f"import uploaded_path 非法：{uploaded_path_text}") from exc
        uploads_root = self._uploads_root().resolve(strict=False)
        try:
            candidate.relative_to(uploads_root)
        except ValueError as exc:
            raise ValueError(f"import uploaded_path 超出 uploads root：{candidate}") from exc
        if candidate == uploads_root:
            raise ValueError("import uploaded_path 不能指向 uploads 根目录。")
        return candidate

    def _cleanup_uploaded_file(self, uploaded_path: Path) -> None:
        try:
            uploaded_path = self._validated_uploaded_path(uploaded_path)
        except ValueError:
            return
        try:
            uploaded_path.unlink(missing_ok=True)
        except OSError:
            return
        uploads_root = self._uploads_root().resolve(strict=False)
        parent = uploaded_path.parent
        while parent != uploads_root and parent.is_relative_to(uploads_root):
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

    async def live_session(self, websocket: WebSocket) -> None:
        await websocket.accept()
        start_payload = await websocket.receive_json()
        request = LiveStartRequest.from_payload(start_payload)
        event_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        runner = RemoteLiveSessionRunner(
            config=self.config,
            request=request,
            on_progress=lambda event: event_queue.put(progress_to_payload(event)),
            on_event=event_queue.put,
            create_postprocess_task=lambda session_id,
            start_event=None: self._create_postprocess_task(
                session_id,
                speaker_enabled=request.speaker_enabled,
                start_event=start_event,
            ),
        )
        try:
            metadata = await asyncio.to_thread(runner.start)
        except Exception as exc:
            runner.request_stop()
            runner.join(timeout=5)
            message = str(exc).strip() or "远端实时后端启动失败。"
            if not message.startswith("远端实时后端启动"):
                message = f"远端实时后端启动失败：{message}"
            payload: dict[str, object] = {
                "type": "error",
                "error": message,
            }
            if runner.session_id:
                payload["session_id"] = runner.session_id
            await self._send_live_payload(websocket, payload)
            await self._close_live_websocket(websocket)
            return
        client_connected = await self._send_live_payload(
            websocket,
            {
                "type": "session_started",
                "metadata": metadata_to_dict(metadata),
            },
        )
        if not client_connected:
            runner.request_stop()

        receiver = asyncio.create_task(self._receive_live_messages(websocket, runner))
        emitted_entries = 0
        try:
            while receiver.done() is False or runner.is_alive or not event_queue.empty():
                while emitted_entries < len(runner.entries):
                    entry = runner.entries[emitted_entries]
                    emitted_entries += 1
                    if client_connected:
                        client_connected = await self._send_live_payload(
                            websocket,
                            {
                                "type": "segment_final",
                                "session_id": runner.session_id,
                                "segment_id": entry.segment_id,
                                "started_ms": entry.started_ms,
                                "ended_ms": entry.ended_ms,
                                "text": entry.text,
                                "speaker_label": entry.speaker_label,
                            },
                        )
                        if not client_connected:
                            runner.request_stop()
                try:
                    payload = await asyncio.to_thread(event_queue.get, True, 0.2)
                except queue.Empty:
                    continue
                if client_connected:
                    client_connected = await self._send_live_payload(websocket, payload)
                    if not client_connected:
                        runner.request_stop()
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
        if client_connected:
            await self._send_live_payload(websocket, terminal)
        await self._close_live_websocket(websocket)

    async def _send_live_payload(self, websocket: WebSocket, payload: dict[str, object]) -> bool:
        try:
            await websocket.send_json(payload)
        except (RuntimeError, WebSocketDisconnect):
            return False
        return True

    async def _close_live_websocket(self, websocket: WebSocket) -> None:
        try:
            await websocket.close()
        except (RuntimeError, WebSocketDisconnect):
            return

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
                    accepted = await asyncio.to_thread(runner.enqueue_audio_bytes, message["bytes"])
                    if not accepted:
                        return
                    continue
                payload = json.loads(message["text"]) if message.get("text") else {}
                action = str(payload.get("type", "")).strip().lower()
                if action == "pause":
                    runner.request_pause()
                elif action == "resume":
                    runner.request_resume()
                elif action == "stop":
                    postprocess_task = runner.ensure_postprocess_task_payload()
                    if runner.on_event is not None:
                        payload = {
                            "type": "stop_received",
                            "session_id": runner.session_id,
                            "message": "远端已确认停止，后台整理任务已创建。",
                        }
                        if postprocess_task is not None:
                            payload["postprocess_task"] = postprocess_task
                        runner.on_event(payload)
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


class RemoteLiveSessionRunner(SessionCoordinator):
    def __init__(
        self,
        *,
        config: AppConfig,
        request: LiveStartRequest,
        on_progress,
        on_event=None,
        create_postprocess_task=None,
    ) -> None:
        if request.speaker_enabled is not None:
            config = replace(
                config,
                speaker=replace(config.speaker, enabled=bool(request.speaker_enabled)),
            )
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
        self._create_postprocess_task = create_postprocess_task
        self._postprocess_ready_event = threading.Event()
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
    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> SessionMetadata:
        self._backend_ready_event.clear()
        self._startup_error = None
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
        startup_timeout = max(int(self.config.remote.timeout_seconds), 1)
        if not self._backend_ready_event.wait(timeout=startup_timeout):
            self.request_stop()
            self.join(timeout=1)
            message = f"远端实时后端启动超时（{startup_timeout}s）。"
            self.failure_message = message
            raise RuntimeError(message)
        if self._startup_error:
            self.request_stop()
            self.join(timeout=1)
            self.failure_message = self._startup_error
            raise RuntimeError(self._startup_error)
        return metadata

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def request_stop(self) -> None:
        super().request_stop()
        self._seal_ingest_spool()

    def ensure_postprocess_task_payload(self) -> dict[str, object] | None:
        if self.postprocess_task_payload is not None:
            return self.postprocess_task_payload
        if self._create_postprocess_task is None or self.session_id is None:
            return None
        self.postprocess_task_payload = self._create_postprocess_task(
            self.session_id,
            start_event=self._postprocess_ready_event,
        )
        return self.postprocess_task_payload

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
        assert self.workspace is not None
        assert self.metadata is not None
        workspace = self.workspace
        metadata = self.metadata
        logger = workspace.session_logger()
        self._spool_logger = logger
        disabled_obsidian = _disabled_obsidian_client(self.config)
        try:
            _attach_console_logging()
            self._initialize_ingest_spool()
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
            if self.config.funasr.enabled:
                self._run_funasr_live_backend(workspace, metadata, logger)
            else:
                self._run_whisper_live_backend(
                    workspace,
                    metadata,
                    logger,
                    disabled_obsidian,
                )

            self._raise_thread_error_if_any()
            if self._create_postprocess_task is not None:
                self.ensure_postprocess_task_payload()
                self._postprocess_ready_event.set()
                _emit_progress(
                    self.on_progress,
                    "postprocess_queued",
                    "后台整理已转为远端任务。",
                    session_id=metadata.session_id,
                )
                return 0
            metadata = _run_remote_postprocess(
                self.config,
                workspace,
                workspace.read_session(),
                logger=logger,
                on_progress=self.on_progress,
            )
            _emit_progress(
                self.on_progress,
                "done",
                "远端会话已完成。",
                session_id=metadata.session_id,
            )
            return 0
        except BaseException as exc:
            self._mark_backend_startup_failed(exc)
            self.failure_message = str(exc)
            _mark_session_failed(
                workspace=workspace,
                obsidian=disabled_obsidian,
                logger=logger,
                label="远端会话",
                exc=exc,
                on_progress=self.on_progress,
            )
            if self.postprocess_task_payload is not None:
                self._postprocess_ready_event.set()
            raise
        finally:
            self._seal_ingest_spool()
            self._close_ingest_spool()
            self._maybe_log_spool_stats(force=True)

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
                workspace.update_status("finalizing")
                _emit_progress(
                    self.on_progress,
                    "capture_finished",
                    "录音已停止，后台继续转写、精修和整理。",
                    session_id=metadata.session_id,
                )
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

            workspace.update_status("finalizing")
            _emit_progress(
                self.on_progress,
                "capture_finished",
                "录音已停止，后台继续转写、精修和整理。",
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
        if self.on_event is not None:
            self.on_event(dict(payload))

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
        content = build_transcript_note(
            metadata,
            workspace.transcript_entries(),
            status="live",
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
    publish_final_outputs(
        workspace=workspace,
        metadata=current,
        obsidian=_disabled_obsidian_client(config),
        llm_client=OpenAiCompatibleClient(config.llm),
        logger=logger,
        on_progress=on_progress,
    )
    return workspace.read_session()
