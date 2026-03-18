from __future__ import annotations

import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace

from live_note.app.session_actions import (
    build_history_detail,
    build_import_task_request,
    build_session_task_request,
    supports_refine,
)


class SessionActionsTests(unittest.TestCase):
    def test_build_import_task_request_preserves_payload(self) -> None:
        request = build_import_task_request(
            file_path=Path("/tmp/demo.mp3"),
            title="课程录音",
            kind="lecture",
            language="zh",
        )

        self.assertEqual("文件导入", request.label)
        self.assertEqual("import", request.action)
        self.assertEqual("/tmp/demo.mp3", request.payload["file_path"])
        self.assertEqual("课程录音", request.payload["title"])
        self.assertEqual("lecture", request.payload["kind"])
        self.assertEqual("zh", request.payload["language"])

    def test_build_session_task_request_uses_session_action_channel(self) -> None:
        request = build_session_task_request(
            label="重新生成整理",
            operation="republish",
            session_id="20260318-demo",
        )

        self.assertEqual("session_action", request.action)
        self.assertEqual("republish", request.payload["action"])
        self.assertEqual("20260318-demo", request.payload["session_id"])

    def test_build_history_detail_for_multiple_summaries(self) -> None:
        summaries = [
            SimpleNamespace(title="周会 A"),
            SimpleNamespace(title="周会 B"),
            SimpleNamespace(title="周会 C"),
            SimpleNamespace(title="周会 D"),
        ]

        detail = build_history_detail(summaries)

        self.assertIn("已选择 4 条会话", detail)
        self.assertIn("周会 A / 周会 B / 周会 C / ...", detail)

    def test_build_history_detail_warns_when_remote_sessions_cannot_be_merged(self) -> None:
        summaries = [
            SimpleNamespace(title="远端课程 A", execution_target="remote"),
            SimpleNamespace(title="远端课程 B", execution_target="remote"),
            SimpleNamespace(title="周会 D"),
        ]

        detail = build_history_detail(summaries)

        self.assertIn("当前选择包含远端会话", detail)

    def test_supports_refine_when_segments_can_reconstruct_session_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_dir = Path(temp_dir)
            segments_dir = session_dir / "segments"
            segments_dir.mkdir(parents=True, exist_ok=True)
            wav_path = segments_dir / "seg-00001.wav"
            with wave.open(str(wav_path), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(16000)
                handle.writeframes(b"\x00\x00" * 16000)
            (session_dir / "segments.jsonl").write_text(
                (
                    '{"kind":"segment_created","segment_id":"seg-00001","started_ms":0,'
                    '"ended_ms":1000,"created_at":"2026-03-17T00:00:00+00:00",'
                    '"wav_path":"segments/seg-00001.wav","text":null,"error":null}\n'
                ),
                encoding="utf-8",
            )

            summary = SimpleNamespace(input_mode="live", session_dir=session_dir)

            self.assertTrue(supports_refine(summary))
