from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import AbstractContextManager
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
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

    def refine_session(self, session_id: str) -> dict[str, Any]:
        encoded_session_id = _quote_path_segment(session_id)
        return self._request_json(
            "POST",
            f"/api/v1/sessions/{encoded_session_id}/actions/refine",
        )

    def refine(self, session_id: str) -> dict[str, Any]:
        return self.refine_session(session_id)

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
    ) -> dict[str, Any] | list[Any]:
        url = f"{self.config.base_url.rstrip('/')}{path}"
        body: bytes | None = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.config.api_token:
            headers["Authorization"] = f"Bearer {self.config.api_token}"
        request = Request(url, data=body, method=method, headers=headers)
        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
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
