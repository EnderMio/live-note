from __future__ import annotations

import unittest

from live_note.domain import ReviewItem, SessionMetadata, TranscriptEntry
from live_note.obsidian.renderer import (
    build_structured_failure_note,
    build_structured_pending_note,
    build_transcript_note,
)


class RendererTests(unittest.TestCase):
    def setUp(self) -> None:
        self.metadata = SessionMetadata(
            session_id="20260315-210500-机器学习",
            title="机器学习导论",
            kind="lecture",
            input_mode="live",
            source_label="MacBook Pro 麦克风",
            source_ref="1",
            language="zh",
            started_at="2026-03-15T13:05:00+00:00",
            transcript_note_path="Sessions/Transcripts/2026-03-15/机器学习导论-210500.md",
            structured_note_path="Sessions/Summaries/2026-03-15/机器学习导论-210500.md",
            session_dir="/tmp/session",
            status="live",
            transcript_source="live",
            refine_status="pending",
        )

    def test_build_transcript_note_contains_entries_and_sections(self) -> None:
        content = build_transcript_note(
            self.metadata,
            [
                TranscriptEntry(
                    segment_id="seg-00001",
                    started_ms=0,
                    ended_ms=2500,
                    text="今天讲梯度下降。",
                )
            ],
            status="live",
            review_items=[
                ReviewItem(
                    started_ms=0,
                    ended_ms=2500,
                    reason_labels=("存在明显重复",),
                    excerpt="今天今天今天讲梯度下降。",
                )
            ],
            session_audio_path="session.live.wav",
        )
        self.assertIn("## 转写记录", content)
        self.assertIn("- [00:00:00] 今天讲梯度下降。", content)
        self.assertIn("Session ID", content)
        self.assertIn("输入模式: `live`", content)
        self.assertIn("转写来源: `live`", content)
        self.assertIn("## 待复核段落", content)
        self.assertIn("session.live.wav", content)

    def test_build_structured_failure_note_links_back_to_transcript(self) -> None:
        content = build_structured_failure_note(
            self.metadata,
            transcript_note_path=self.metadata.transcript_note_path,
            reason="LLM 请求失败",
        )
        self.assertIn('status: "failed"', content)
        self.assertIn('refine_status: "pending"', content)
        self.assertIn("[[Sessions/Transcripts/2026-03-15/机器学习导论-210500|查看原文]]", content)

    def test_build_structured_pending_note_keeps_template_sections(self) -> None:
        content = build_structured_pending_note(
            self.metadata,
            transcript_note_path=self.metadata.transcript_note_path,
            reason="当前会话未启用自动整理",
        )
        self.assertIn('status: "pending"', content)
        self.assertIn("## 生成说明", content)
        self.assertIn("## 关键点", content)
        self.assertIn("## 时间线", content)
