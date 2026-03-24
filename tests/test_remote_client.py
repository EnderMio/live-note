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

    def test_create_import_task_posts_audio_bytes_and_encoded_query(self) -> None:
        config = RemoteConfig(
            enabled=True,
            base_url="https://remote.example.com",
            api_token="secret-token",
            timeout_seconds=12,
        )
        client = RemoteClient(config)

        with patch("pathlib.Path.read_bytes", return_value=b"audio-bytes"):
            with patch("live_note.remote.client.urlopen") as urlopen_mock:
                urlopen_mock.return_value = _FakeHttpResponse(
                    '{"task_id":"import-1","status":"queued"}'
                )

                payload = client.create_import_task(
                    "/tmp/第1课.mp3",
                    title="股票课 01",
                    kind="lecture",
                    language="zh",
                    speaker_enabled=True,
                    request_id="req-import-1",
                )

        request = urlopen_mock.call_args.args[0]
        self.assertEqual("POST", request.get_method())
        self.assertEqual(b"audio-bytes", request.data)
        self.assertEqual("application/octet-stream", request.headers["Content-type"])
        self.assertEqual("Bearer secret-token", request.headers["Authorization"])
        self.assertIn("/api/v1/imports?", request.full_url)
        self.assertIn("kind=lecture", request.full_url)
        self.assertIn("language=zh", request.full_url)
        self.assertIn("speaker_enabled=1", request.full_url)
        self.assertIn("request_id=req-import-1", request.full_url)
        self.assertIn("title=%E8%82%A1%E7%A5%A8%E8%AF%BE+01", request.full_url)
        self.assertIn("filename=%E7%AC%AC1%E8%AF%BE.mp3", request.full_url)
        self.assertEqual({"task_id": "import-1", "status": "queued"}, payload)

    def test_create_import_task_uses_upload_timeout_when_configured(self) -> None:
        config = RemoteConfig(
            enabled=True,
            base_url="https://remote.example.com",
            api_token="secret-token",
            timeout_seconds=12,
            upload_timeout_seconds=180,
        )
        client = RemoteClient(config)

        with patch("pathlib.Path.read_bytes", return_value=b"audio-bytes"):
            with patch("live_note.remote.client.urlopen") as urlopen_mock:
                urlopen_mock.return_value = _FakeHttpResponse(
                    '{"task_id":"import-1","status":"queued"}'
                )

                client.create_import_task(
                    "/tmp/第1课.mp3",
                    title="股票课 01",
                    kind="lecture",
                    language="zh",
                    request_id="req-import-1",
                )

        self.assertEqual(180, urlopen_mock.call_args.kwargs["timeout"])

    def test_get_import_task_quotes_non_ascii_task_id(self) -> None:
        config = RemoteConfig(
            enabled=True,
            base_url="http://remote.example.com",
            timeout_seconds=12,
        )
        client = RemoteClient(config)

        with patch("live_note.remote.client.urlopen") as urlopen_mock:
            urlopen_mock.return_value = _FakeHttpResponse('{"task_id":"ok"}')

            client.get_import_task("导入任务-01")

        request = urlopen_mock.call_args.args[0]
        self.assertEqual(
            "http://remote.example.com/api/v1/imports/%E5%AF%BC%E5%85%A5%E4%BB%BB%E5%8A%A1-01",
            request.full_url,
        )

    def test_cancel_import_task_quotes_non_ascii_task_id(self) -> None:
        config = RemoteConfig(
            enabled=True,
            base_url="http://remote.example.com",
            timeout_seconds=12,
        )
        client = RemoteClient(config)

        with patch("live_note.remote.client.urlopen") as urlopen_mock:
            urlopen_mock.return_value = _FakeHttpResponse('{"status":"cancelled"}')

            client.cancel_import_task("导入任务-01")

        request = urlopen_mock.call_args.args[0]
        self.assertEqual(
            "http://remote.example.com/api/v1/imports/%E5%AF%BC%E5%85%A5%E4%BB%BB%E5%8A%A1-01/actions/cancel",
            request.full_url,
        )

    def test_list_tasks_requests_generic_tasks_endpoint(self) -> None:
        config = RemoteConfig(
            enabled=True,
            base_url="http://remote.example.com",
            timeout_seconds=12,
        )
        client = RemoteClient(config)

        with patch("live_note.remote.client.urlopen") as urlopen_mock:
            urlopen_mock.return_value = _FakeHttpResponse(
                '{"server_id":"server-1","active":[],"recent":[]}'
            )

            payload = client.list_tasks()

        request = urlopen_mock.call_args.args[0]
        self.assertEqual("http://remote.example.com/api/v1/tasks", request.full_url)
        self.assertEqual("server-1", payload["server_id"])

    def test_get_task_quotes_non_ascii_task_id(self) -> None:
        config = RemoteConfig(
            enabled=True,
            base_url="http://remote.example.com",
            timeout_seconds=12,
        )
        client = RemoteClient(config)

        with patch("live_note.remote.client.urlopen") as urlopen_mock:
            urlopen_mock.return_value = _FakeHttpResponse('{"task_id":"ok"}')

            client.get_task("远端任务-01")

        request = urlopen_mock.call_args.args[0]
        self.assertEqual(
            "http://remote.example.com/api/v1/tasks/%E8%BF%9C%E7%AB%AF%E4%BB%BB%E5%8A%A1-01",
            request.full_url,
        )

    def test_cancel_task_quotes_non_ascii_task_id(self) -> None:
        config = RemoteConfig(
            enabled=True,
            base_url="http://remote.example.com",
            timeout_seconds=12,
        )
        client = RemoteClient(config)

        with patch("live_note.remote.client.urlopen") as urlopen_mock:
            urlopen_mock.return_value = _FakeHttpResponse('{"status":"cancelled"}')

            client.cancel_task("远端任务-01")

        request = urlopen_mock.call_args.args[0]
        self.assertEqual(
            "http://remote.example.com/api/v1/tasks/%E8%BF%9C%E7%AB%AF%E4%BB%BB%E5%8A%A1-01/actions/cancel",
            request.full_url,
        )

    def test_retranscribe_session_quotes_non_ascii_session_id(self) -> None:
        config = RemoteConfig(
            enabled=True,
            base_url="http://remote.example.com",
            timeout_seconds=12,
        )
        client = RemoteClient(config)

        with patch("live_note.remote.client.urlopen") as urlopen_mock:
            urlopen_mock.return_value = _FakeHttpResponse('{"accepted":true}')

            client.retranscribe_session("20260318-143616-远程转写测试股票课", request_id="req-rt-1")

        request = urlopen_mock.call_args.args[0]
        self.assertEqual(
            "http://remote.example.com/api/v1/sessions/20260318-143616-%E8%BF%9C%E7%A8%8B%E8%BD%AC%E5%86%99%E6%B5%8B%E8%AF%95%E8%82%A1%E7%A5%A8%E8%AF%BE/actions/retranscribe?request_id=req-rt-1",
            request.full_url,
        )
