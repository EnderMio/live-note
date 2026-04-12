from __future__ import annotations

import asyncio
import json
import queue
from collections.abc import Callable
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from live_note.config import AppConfig
from live_note.runtime.supervisors.runtime_host import RuntimeHost

from .live_session import RemoteLiveSessionRunner
from .protocol import LiveStartRequest, metadata_to_dict, progress_to_payload


class RemoteLiveGateway:
    def __init__(
        self,
        config: AppConfig,
        *,
        commit_postprocess_handoff: Callable[[str, bool | None, str | None], dict[str, object]],
    ) -> None:
        self.config = config
        self._commit_postprocess_handoff = commit_postprocess_handoff

    async def handle(self, websocket: WebSocket) -> None:
        await websocket.accept()
        start_payload = await websocket.receive_json()
        request = LiveStartRequest.from_payload(start_payload)
        event_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        runner = RemoteLiveSessionRunner(
            config=self.config,
            request=request,
            on_progress=lambda event: event_queue.put(progress_to_payload(event)),
            on_event=event_queue.put,
            commit_postprocess_handoff=lambda session_id, spool_path: (
                self._commit_postprocess_handoff(
                    session_id,
                    request.speaker_enabled,
                    spool_path,
                )
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
            await self._send_payload(websocket, payload)
            await self._close(websocket)
            return
        client_connected = await self._send_payload(
                websocket,
                {
                    "type": "session_started",
                    "metadata": metadata_to_dict(metadata),
                    "runtime_status": _session_runtime_status(self.config, metadata.session_id),
                },
            )
        if not client_connected:
            runner.request_stop()

        receiver = asyncio.create_task(self._receive_messages(websocket, runner))
        try:
            while receiver.done() is False or runner.is_alive or not event_queue.empty():
                try:
                    payload = await asyncio.to_thread(event_queue.get, True, 0.2)
                except queue.Empty:
                    continue
                if client_connected:
                    client_connected = await self._send_payload(websocket, payload)
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
            await self._send_payload(websocket, terminal)
        await self._close(websocket)

    async def _send_payload(self, websocket: WebSocket, payload: dict[str, object]) -> bool:
        try:
            await websocket.send_json(payload)
        except (RuntimeError, WebSocketDisconnect):
            return False
        return True

    async def _close(self, websocket: WebSocket) -> None:
        try:
            await websocket.close()
        except (RuntimeError, WebSocketDisconnect):
            return

    async def _receive_messages(
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
                    runner.request_stop()
                    return
        except WebSocketDisconnect:
            runner.request_stop()


def _session_runtime_status(config: AppConfig, session_id: str) -> str | None:
    record = RuntimeHost.for_root(config.root_dir).sessions.get(session_id)
    return record.runtime_status if record is not None else None
