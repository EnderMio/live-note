from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from live_note.config import LlmConfig
from live_note.domain import SessionMetadata, TranscriptEntry
from live_note.llm import LlmError, OpenAiCompatibleClient


class FakeJsonResponse:
    def __init__(self, body: bytes):
        self.body = body

    def read(self) -> bytes:
        return self.body

    def __enter__(self) -> FakeJsonResponse:
        return self

    def __exit__(self, exc_type, exc, exc_tb) -> None:
        return None


class FakeStreamResponse:
    def __init__(self, lines: list[bytes]):
        self.lines = lines

    def __iter__(self):
        return iter(self.lines)

    def __enter__(self) -> FakeStreamResponse:
        return self

    def __exit__(self, exc_type, exc, exc_tb) -> None:
        return None


def sample_metadata() -> SessionMetadata:
    return SessionMetadata(
        session_id="20260315-210500-周会",
        title="产品周会",
        kind="meeting",
        input_mode="file",
        source_label="meeting.mp3",
        source_ref="/tmp/meeting.mp3",
        language="zh",
        started_at="2026-03-15T13:05:00+00:00",
        transcript_note_path="Sessions/Transcripts/2026-03-15/产品周会-210500.md",
        structured_note_path="Sessions/Summaries/2026-03-15/产品周会-210500.md",
        session_dir="/tmp/session",
        status="importing",
    )


class OpenAiCompatibleClientTests(unittest.TestCase):
    def test_generate_structured_note_supports_json_response(self) -> None:
        captured: dict[str, object] = {}

        def fake_urlopen(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeJsonResponse(
                json.dumps(
                    {"choices": [{"message": {"content": "## 摘要\n\n正常返回"}}]},
                    ensure_ascii=False,
                ).encode("utf-8")
            )

        client = OpenAiCompatibleClient(
            LlmConfig(
                base_url="https://llm.example.com/v1",
                model="demo-model",
                enabled=True,
                stream=False,
                timeout_seconds=12,
                api_key="demo-token",
            )
        )

        with patch("live_note.llm.urlopen", side_effect=fake_urlopen):
            content = client.generate_structured_note(
                sample_metadata(),
                [TranscriptEntry("seg-00001", 0, 2000, "今天讨论发布节奏。")],
            )

        payload = json.loads(captured["request"].data.decode("utf-8"))
        self.assertEqual("## 摘要\n\n正常返回", content)
        self.assertEqual(12, captured["timeout"])
        self.assertEqual(
            "https://llm.example.com/v1/chat/completions",
            captured["request"].full_url,
        )
        self.assertEqual("demo-model", payload["model"])
        self.assertNotIn("stream", payload)
        self.assertEqual("live-note/0.1", captured["request"].headers["User-agent"])

    def test_generate_structured_note_supports_stream_response(self) -> None:
        captured: dict[str, object] = {}

        def fake_urlopen(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            first_chunk = 'data: {"choices":[{"delta":{"content":"## 摘要\\n\\n"}}]}\n'.encode()
            second_chunk = 'data: {"choices":[{"delta":{"content":"流式聚合成功"}}]}\n'.encode()
            return FakeStreamResponse(
                [
                    first_chunk,
                    b"\n",
                    second_chunk,
                    b"\n",
                    b"data: [DONE]\n",
                    b"\n",
                ]
            )

        client = OpenAiCompatibleClient(
            LlmConfig(
                base_url="https://llm.example.com/v1",
                model="demo-model",
                enabled=True,
                stream=True,
                timeout_seconds=8,
                api_key="demo-token",
            )
        )

        with patch("live_note.llm.urlopen", side_effect=fake_urlopen):
            content = client.generate_structured_note(
                sample_metadata(),
                [TranscriptEntry("seg-00001", 0, 2000, "今天讨论发布节奏。")],
            )

        payload = json.loads(captured["request"].data.decode("utf-8"))
        self.assertEqual("## 摘要\n\n流式聚合成功", content)
        self.assertEqual(8, captured["timeout"])
        self.assertTrue(payload["stream"])
        self.assertEqual("text/event-stream", captured["request"].headers["Accept"])

    def test_generate_structured_note_supports_responses_json_response(self) -> None:
        captured: dict[str, object] = {}

        def fake_urlopen(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeJsonResponse(
                json.dumps(
                    {
                        "output": [
                            {
                                "content": [
                                    {"type": "output_text", "text": "## 摘要\n\nResponses 成功"}
                                ]
                            }
                        ]
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
            )

        client = OpenAiCompatibleClient(
            LlmConfig(
                base_url="https://llm.example.com/v1",
                model="demo-model",
                enabled=True,
                stream=False,
                wire_api="responses",
                timeout_seconds=18,
                api_key="demo-token",
            )
        )

        with patch("live_note.llm.urlopen", side_effect=fake_urlopen):
            content = client.generate_structured_note(
                sample_metadata(),
                [TranscriptEntry("seg-00001", 0, 2000, "今天讨论发布节奏。")],
            )

        payload = json.loads(captured["request"].data.decode("utf-8"))
        self.assertEqual("## 摘要\n\nResponses 成功", content)
        self.assertEqual(18, captured["timeout"])
        self.assertEqual("https://llm.example.com/v1/responses", captured["request"].full_url)
        self.assertEqual("demo-model", payload["model"])
        self.assertEqual("你是音频内容整理助手。", payload["instructions"].splitlines()[0])
        self.assertIn("原始转写:", payload["input"])
        self.assertNotIn("messages", payload)
        self.assertEqual("live-note/0.1", captured["request"].headers["User-agent"])

    def test_generate_structured_note_supports_responses_stream_response(self) -> None:
        captured: dict[str, object] = {}

        def fake_urlopen(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            first_chunk = (
                b'data: {"type":"response.output_text.delta","delta":"## \\u6458\\u8981\\n\\n"}\n'
            )
            second_chunk = (
                b'data: {"type":"response.output_text.delta","delta":"Responses '
                b'\\u6d41\\u5f0f\\u6210\\u529f"}\n'
            )
            completion = b'data: {"type":"response.completed"}\n'
            return FakeStreamResponse(
                [
                    first_chunk,
                    b"\n",
                    second_chunk,
                    b"\n",
                    completion,
                    b"\n",
                    b"data: [DONE]\n",
                    b"\n",
                ]
            )

        client = OpenAiCompatibleClient(
            LlmConfig(
                base_url="https://llm.example.com/v1",
                model="demo-model",
                enabled=True,
                stream=True,
                wire_api="responses",
                timeout_seconds=9,
                api_key="demo-token",
            )
        )

        with patch("live_note.llm.urlopen", side_effect=fake_urlopen):
            content = client.generate_structured_note(
                sample_metadata(),
                [TranscriptEntry("seg-00001", 0, 2000, "今天讨论发布节奏。")],
            )

        payload = json.loads(captured["request"].data.decode("utf-8"))
        self.assertEqual("## 摘要\n\nResponses 流式成功", content)
        self.assertEqual(9, captured["timeout"])
        self.assertTrue(payload["stream"])
        self.assertEqual("https://llm.example.com/v1/responses", captured["request"].full_url)
        self.assertEqual("text/event-stream", captured["request"].headers["Accept"])

    def test_generate_structured_note_rejects_empty_json_response(self) -> None:
        def fake_urlopen(request, timeout):
            del request, timeout
            return FakeJsonResponse(b"")

        client = OpenAiCompatibleClient(
            LlmConfig(
                base_url="https://llm.example.com/v1",
                model="demo-model",
                enabled=True,
                stream=False,
                api_key="demo-token",
            )
        )

        with patch("live_note.llm.urlopen", side_effect=fake_urlopen):
            with self.assertRaisesRegex(LlmError, "LLM 返回为空"):
                client.generate_structured_note(
                    sample_metadata(),
                    [TranscriptEntry("seg-00001", 0, 2000, "今天讨论发布节奏。")],
                )
