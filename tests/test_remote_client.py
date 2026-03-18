from __future__ import annotations

import json
import sys
import types
import unittest
from unittest.mock import patch

from live_note.config import RemoteConfig
from live_note.remote.client import RemoteClient


class _FakeHttpResponse:
    def __init__(self, payload: str) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload.encode("utf-8")

    def __enter__(self) -> _FakeHttpResponse:
        return self

    def __exit__(self, exc_type, exc, exc_tb) -> None:
        return None


class _FakeWebSocket:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.sent: list[str | bytes] = []
        self.closed = False

    def send(self, payload: str | bytes) -> None:
        self.sent.append(payload)

    def recv(self) -> str | bytes | None:
        if not self.responses:
            return None
        return self.responses.pop(0)

    def close(self) -> None:
        self.closed = True


class RemoteClientTests(unittest.TestCase):
    def test_health_includes_bearer_token_header(self) -> None:
        config = RemoteConfig(
            enabled=True,
            base_url="https://remote.example.com",
            api_token="secret-token",
            timeout_seconds=12,
        )
        client = RemoteClient(config)

        with patch("live_note.remote.client.urlopen") as urlopen_mock:
            urlopen_mock.return_value = _FakeHttpResponse('{"status":"ok"}')

            payload = client.health()

        request = urlopen_mock.call_args.args[0]
        timeout = urlopen_mock.call_args.kwargs["timeout"]
        self.assertEqual("https://remote.example.com/api/v1/health", request.full_url)
        self.assertEqual("Bearer secret-token", request.headers["Authorization"])
        self.assertEqual(12, timeout)
        self.assertEqual({"status": "ok"}, payload)

    def test_connect_live_sends_start_and_control_frames_expected_by_server(self) -> None:
        fake_socket = _FakeWebSocket(
            [
                json.dumps({"type": "session_started", "session_id": "remote-1"}),
                json.dumps({"type": "completed", "session_id": "remote-1"}),
            ]
        )
        connect_calls: list[dict[str, object]] = []
        client_module = types.ModuleType("websockets.sync.client")

        def fake_connect(url: str, **kwargs):
            connect_calls.append({"url": url, **kwargs})
            return fake_socket

        client_module.connect = fake_connect
        config = RemoteConfig(
            enabled=True,
            base_url="https://remote.example.com",
            api_token="secret-token",
            timeout_seconds=9,
        )
        client = RemoteClient(config)

        with patch.dict(sys.modules, {"websockets.sync.client": client_module}):
            with client.connect_live({"title": "产品周会"}) as connection:
                started = connection.recv_event()
                connection.send_control("pause")

        self.assertEqual("session_started", started["type"])
        self.assertEqual("wss://remote.example.com/api/v1/live", connect_calls[0]["url"])
        self.assertEqual(
            {"Authorization": "Bearer secret-token"},
            connect_calls[0]["additional_headers"],
        )
        self.assertEqual(
            {"type": "start", "title": "产品周会"},
            json.loads(str(fake_socket.sent[0])),
        )
        self.assertEqual({"type": "pause"}, json.loads(str(fake_socket.sent[1])))
        self.assertTrue(fake_socket.closed)

    def test_get_session_artifacts_quotes_non_ascii_session_id(self) -> None:
        config = RemoteConfig(
            enabled=True,
            base_url="http://remote.example.com",
            timeout_seconds=12,
        )
        client = RemoteClient(config)

        with patch("live_note.remote.client.urlopen") as urlopen_mock:
            urlopen_mock.return_value = _FakeHttpResponse('{"session_id":"ok"}')

            client.get_session_artifacts("20260318-143616-远程转写测试股票课")

        request = urlopen_mock.call_args.args[0]
        self.assertEqual(
            "http://remote.example.com/api/v1/sessions/20260318-143616-%E8%BF%9C%E7%A8%8B%E8%BD%AC%E5%86%99%E6%B5%8B%E8%AF%95%E8%82%A1%E7%A5%A8%E8%AF%BE/artifacts",
            request.full_url,
        )

    def test_refine_session_quotes_non_ascii_session_id(self) -> None:
        config = RemoteConfig(
            enabled=True,
            base_url="http://remote.example.com",
            timeout_seconds=12,
        )
        client = RemoteClient(config)

        with patch("live_note.remote.client.urlopen") as urlopen_mock:
            urlopen_mock.return_value = _FakeHttpResponse('{"accepted":true}')

            client.refine_session("20260318-143616-远程转写测试股票课")

        request = urlopen_mock.call_args.args[0]
        self.assertEqual(
            "http://remote.example.com/api/v1/sessions/20260318-143616-%E8%BF%9C%E7%A8%8B%E8%BD%AC%E5%86%99%E6%B5%8B%E8%AF%95%E8%82%A1%E7%A5%A8%E8%AF%BE/actions/refine",
            request.full_url,
        )
