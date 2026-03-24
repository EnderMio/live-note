from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from live_note.config import FunAsrConfig
from live_note.transcribe.funasr import FunAsrLiveConnection, FunAsrWebSocketClient


class _FakeWebSocket:
    def __init__(self, responses: list[object] | None = None) -> None:
        self.sent: list[object] = []
        self._responses = list(responses or [])

    def send(self, payload: object) -> None:
        self.sent.append(payload)

    def recv(self, timeout: float | None = None) -> object:
        if not self._responses:
            raise TimeoutError(timeout)
        return self._responses.pop(0)

    def close(self, code: int = 1000, reason: str = "") -> None:
        return None


class FunAsrConnectionTests(unittest.TestCase):
    def test_connect_live_requests_binary_subprotocol(self) -> None:
        websocket = _FakeWebSocket()
        client = FunAsrWebSocketClient(
            FunAsrConfig(enabled=True, base_url="ws://127.0.0.1:10095"),
        )

        with patch("websockets.sync.client.connect", return_value=websocket) as connect_mock:
            connection = client.connect_live()

        connect_mock.assert_called_once()
        _, kwargs = connect_mock.call_args
        self.assertEqual(["binary"], kwargs["subprotocols"])
        connection.close()

    def test_start_stream_sends_official_handshake_payload(self) -> None:
        websocket = _FakeWebSocket()
        connection = FunAsrLiveConnection(
            websocket,
            FunAsrConfig(
                enabled=True,
                base_url="ws://127.0.0.1:10095",
                mode="2pass",
                use_itn=True,
            ),
        )

        connection.start_stream(wav_name="session-1", sample_rate=16000)

        payload = json.loads(websocket.sent[0])
        self.assertEqual("2pass", payload["mode"])
        self.assertEqual("session-1", payload["wav_name"])
        self.assertEqual(16000, payload["audio_fs"])
        self.assertEqual([5, 10, 5], payload["chunk_size"])
        self.assertEqual(10, payload["chunk_interval"])
        self.assertTrue(payload["is_speaking"])
        self.assertTrue(payload["itn"])

    def test_send_stop_marks_stream_as_not_speaking(self) -> None:
        websocket = _FakeWebSocket()
        connection = FunAsrLiveConnection(
            websocket,
            FunAsrConfig(enabled=True, base_url="ws://127.0.0.1:10095"),
        )

        connection.send_stop()

        self.assertEqual({"is_speaking": False}, json.loads(websocket.sent[0]))

    def test_recv_message_parses_text_mode_and_final_flag(self) -> None:
        websocket = _FakeWebSocket(
            [
                json.dumps(
                    {
                        "text": "大家好，开始吧。",
                        "mode": "2pass",
                        "wav_name": "session-1",
                        "is_final": True,
                    },
                    ensure_ascii=False,
                )
            ]
        )
        connection = FunAsrLiveConnection(
            websocket,
            FunAsrConfig(enabled=True, base_url="ws://127.0.0.1:10095"),
        )

        message = connection.recv_message(timeout=0.1)

        self.assertEqual("大家好，开始吧。", message.text)
        self.assertEqual("2pass", message.mode)
        self.assertTrue(message.is_final)
        self.assertEqual("session-1", message.wav_name)

    def test_recv_message_parses_realtime_protocol_modes_and_timestamp_bounds(self) -> None:
        websocket = _FakeWebSocket(
            [
                json.dumps(
                    {
                        "text": "正 是 因 为",
                        "mode": "2pass-offline",
                        "wav_name": "session-2",
                        "is_final": False,
                        "timestamp": "[[430,670],[670,810],[810,1030],[1030,1130]]",
                        "stamp_sents": [
                            {
                                "text_seg": "正 是 因 为",
                                "punc": ",",
                                "start": 430,
                                "end": 1130,
                            }
                        ],
                    },
                    ensure_ascii=False,
                )
            ]
        )
        connection = FunAsrLiveConnection(
            websocket,
            FunAsrConfig(enabled=True, base_url="ws://127.0.0.1:10095"),
        )

        message = connection.recv_message(timeout=0.1)

        self.assertEqual("2pass-offline", message.mode)
        self.assertFalse(message.is_final)
        self.assertEqual(((430, 670), (670, 810), (810, 1030), (1030, 1130)), message.timestamp_ms)
        self.assertEqual(((430, 1130),), message.sentence_spans_ms)
        self.assertEqual((430, 1130), message.bounds_ms)
