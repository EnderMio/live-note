from __future__ import annotations

import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace

from live_note.app.session_actions import build_history_detail, can_merge_summaries, supports_refine


class SessionActionsTests(unittest.TestCase):
    def test_can_merge_summaries_requires_all_local(self) -> None:
        self.assertTrue(
            can_merge_summaries(
                [
                    SimpleNamespace(execution_target="local"),
                    SimpleNamespace(execution_target="local"),
                ]
            )
        )
        self.assertFalse(
            can_merge_summaries(
                [
                    SimpleNamespace(execution_target="remote"),
                    SimpleNamespace(execution_target="local"),
                ]
            )
        )

    def test_build_history_detail_for_multiple_summaries(self) -> None:
        summaries = [
            SimpleNamespace(title="周会 A", execution_target="local"),
            SimpleNamespace(title="周会 B", execution_target="local"),
            SimpleNamespace(title="周会 C", execution_target="local"),
            SimpleNamespace(title="周会 D", execution_target="local"),
        ]

        detail = build_history_detail(summaries)

        self.assertIn("已选择 4 条会话", detail)
        self.assertIn("周会 A / 周会 B / 周会 C / ...", detail)

    def test_build_history_detail_warns_when_remote_sessions_cannot_be_merged(self) -> None:
        summaries = [
            SimpleNamespace(title="远端课程 A", execution_target="remote"),
            SimpleNamespace(title="远端课程 B", execution_target="remote"),
            SimpleNamespace(title="周会 D", execution_target="local"),
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
