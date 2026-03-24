from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from live_note.config import RemoteConfig

from .protocol import websocket_url


class RemoteClientError(RuntimeError):
    pass


class RemoteLiveConnection(AbstractContextManager["RemoteLiveConnection"]):
    def __init__(self, websocket: Any):
        self._websocket = websocket

    def send_audio(self, pcm16: bytes) -> None:
        self._websocket.send(pcm16)

    def send_control(self, command: str) -> None:
        self._websocket.send(json.dumps({"type": command}))

    def iter_events(self) -> Iterator[dict[str, Any]]:
        while True:
            try:
                payload = self._websocket.recv()
            except Exception:
                return
            if payload is None:
                return
            if isinstance(payload, bytes):
                continue
            data = json.loads(payload)
            yield data
            if data.get("type") in {"completed", "error"}:
                return

    def recv_event(self) -> dict[str, Any]:
        for item in self.iter_events():
            return item
        raise RemoteClientError("远端连接已关闭。")

    def close(self) -> None:
        close = getattr(self._websocket, "close", None)
        if close is not None:
            close()

    def __exit__(self, exc_type, exc, exc_tb) -> None:
        self.close()
        return None


class RemoteClient:
    def __init__(self, config: RemoteConfig):
        self.config = config

    def health(self) -> dict[str, Any]:
        return self._request_json("GET", "/api/v1/health")

    def create_import_task(
        self,
        file_path: str,
        *,
        title: str | None,
        kind: str,
        language: str | None,
        speaker_enabled: bool | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        media_path = Path(file_path).expanduser().resolve()
        query = {
            key: value
            for key, value in {
                "filename": media_path.name,
                "title": title,
                "kind": kind,
                "language": language,
                "request_id": request_id,
            }.items()
            if value is not None
        }
        if speaker_enabled is not None:
            query["speaker_enabled"] = "1" if speaker_enabled else "0"
        payload = self._request_json(
            "POST",
            "/api/v1/imports",
            query=query,
            body=media_path.read_bytes(),
            content_type="application/octet-stream",
            timeout_seconds=self.config.upload_timeout_seconds,
        )
        if not isinstance(payload, dict):
            raise RemoteClientError("远端导入任务响应格式无效。")
        return payload

    def get_import_task(self, task_id: str) -> dict[str, Any]:
        encoded_task_id = _quote_path_segment(task_id)
        payload = self._request_json("GET", f"/api/v1/imports/{encoded_task_id}")
        if not isinstance(payload, dict):
            raise RemoteClientError("远端导入状态响应格式无效。")
        return payload

    def cancel_import_task(self, task_id: str) -> dict[str, Any]:
        encoded_task_id = _quote_path_segment(task_id)
        payload = self._request_json(
            "POST",
            f"/api/v1/imports/{encoded_task_id}/actions/cancel",
        )
        if not isinstance(payload, dict):
            raise RemoteClientError("远端导入取消响应格式无效。")
        return payload

    def list_tasks(self) -> dict[str, Any]:
        payload = self._request_json("GET", "/api/v1/tasks")
        if not isinstance(payload, dict):
            raise RemoteClientError("远端任务列表响应格式无效。")
        return payload

    def get_task(self, task_id: str) -> dict[str, Any]:
        encoded_task_id = _quote_path_segment(task_id)
        payload = self._request_json("GET", f"/api/v1/tasks/{encoded_task_id}")
        if not isinstance(payload, dict):
            raise RemoteClientError("远端任务详情响应格式无效。")
        return payload

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        encoded_task_id = _quote_path_segment(task_id)
        payload = self._request_json(
            "POST",
            f"/api/v1/tasks/{encoded_task_id}/actions/cancel",
        )
        if not isinstance(payload, dict):
            raise RemoteClientError("远端任务取消响应格式无效。")
        return payload

    def get_session(self, session_id: str) -> dict[str, Any]:
        encoded_session_id = _quote_path_segment(session_id)
        return self._request_json("GET", f"/api/v1/sessions/{encoded_session_id}")

    def list_sessions(self) -> list[dict[str, Any]]:
        payload = self._request_json("GET", "/api/v1/sessions")
        if not isinstance(payload, list):
            raise RemoteClientError("远端 sessions 响应格式无效。")
        return payload

    def get_session_artifacts(self, session_id: str) -> dict[str, Any]:
        encoded_session_id = _quote_path_segment(session_id)
        return self._request_json("GET", f"/api/v1/sessions/{encoded_session_id}/artifacts")

    def get_artifacts(self, session_id: str) -> dict[str, Any]:
        return self.get_session_artifacts(session_id)

    def refine_session(self, session_id: str, *, request_id: str | None = None) -> dict[str, Any]:
        encoded_session_id = _quote_path_segment(session_id)
        return self._request_json(
            "POST",
            f"/api/v1/sessions/{encoded_session_id}/actions/refine",
            query={"request_id": request_id} if request_id else None,
        )

    def refine(self, session_id: str, *, request_id: str | None = None) -> dict[str, Any]:
        return self.refine_session(session_id, request_id=request_id)

    def retranscribe_session(
        self,
        session_id: str,
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        encoded_session_id = _quote_path_segment(session_id)
        return self._request_json(
            "POST",
            f"/api/v1/sessions/{encoded_session_id}/actions/retranscribe",
            query={"request_id": request_id} if request_id else None,
        )

    def retranscribe(self, session_id: str, *, request_id: str | None = None) -> dict[str, Any]:
        return self.retranscribe_session(session_id, request_id=request_id)

    def open_live(self) -> RemoteLiveConnection:
        return self.connect_live({})

    def connect_live(self, payload: dict[str, Any]) -> RemoteLiveConnection:
        try:
            from websockets.sync.client import connect
        except ImportError as exc:
            raise RemoteClientError(
                "缺少 websockets 依赖，先运行 pip install -e ."
            ) from exc

        headers = {}
        if self.config.api_token:
            headers["Authorization"] = f"Bearer {self.config.api_token}"
        websocket = connect(
            websocket_url(self.config.base_url, "/api/v1/live"),
            additional_headers=headers or None,
            open_timeout=self.config.timeout_seconds,
            close_timeout=self.config.timeout_seconds,
        )
        websocket.send(json.dumps({"type": "start", **payload}))
        return RemoteLiveConnection(websocket)

    def _request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        query: dict[str, str] | None = None,
        body: bytes | None = None,
        content_type: str | None = None,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any] | list[Any]:
        if payload is not None and body is not None:
            raise ValueError("payload 和 body 不能同时传入。")
        url = f"{self.config.base_url.rstrip('/')}{path}"
        if query:
            url = f"{url}?{urlencode(query)}"
        request_body: bytes | None = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            request_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif body is not None:
            request_body = body
            if content_type:
                headers["Content-Type"] = content_type
        if self.config.api_token:
            headers["Authorization"] = f"Bearer {self.config.api_token}"
        request = Request(url, data=request_body, method=method, headers=headers)
        request_timeout = (
            self.config.timeout_seconds if timeout_seconds is None else int(timeout_seconds)
        )
        try:
            with urlopen(request, timeout=request_timeout) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RemoteClientError(f"远端请求失败: {exc.code} {detail}".strip()) from exc
        except URLError as exc:
            raise RemoteClientError(f"无法连接远端服务: {exc}") from exc
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RemoteClientError("远端返回了无效 JSON。") from exc


def _quote_path_segment(value: str) -> str:
    return quote(value, safe="")
