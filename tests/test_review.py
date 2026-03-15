from __future__ import annotations

import unittest

from live_note.domain import TranscriptEntry
from live_note.review import detect_review_items


class ReviewTests(unittest.TestCase):
    def test_detect_review_items_flags_repeated_and_groups_nearby_segments(self) -> None:
        items = detect_review_items(
            [
                TranscriptEntry("seg-00001", 0, 1800, "这这这这是什么"),
                TranscriptEntry("seg-00002", 2500, 4200, "123abc今天又来了一次"),
                TranscriptEntry("seg-00003", 9000, 15000, "好"),
            ],
            language="zh",
        )

        self.assertEqual(2, len(items))
        self.assertEqual(0, items[0].started_ms)
        self.assertEqual(4200, items[0].ended_ms)
        self.assertIn("存在明显重复", items[0].reason_labels)
        self.assertIn("中英数字混杂异常", items[0].reason_labels)
        self.assertIn("时长偏长但文本过短", items[1].reason_labels)

    def test_detect_review_items_returns_empty_when_text_looks_normal(self) -> None:
        items = detect_review_items(
            [
                TranscriptEntry("seg-00001", 0, 5000, "今天主要讨论项目排期和下周的交付风险。"),
                TranscriptEntry(
                    "seg-00002",
                    6000,
                    11000,
                    "结论是先收敛范围，再确认负责人和时间点。",
                ),
            ],
            language="zh",
        )

        self.assertEqual([], items)
