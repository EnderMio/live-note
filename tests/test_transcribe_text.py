from __future__ import annotations

import unittest

from live_note.transcribe.text import (
    is_suspicious_transcript_text,
    should_admit_transcript_prompt,
)


class TranscribeTextContractTests(unittest.TestCase):
    def test_classifies_repeated_captioncube_output_as_suspicious(self) -> None:
        text = "中文字幕:CaptionCube 中文字幕:CaptionCube"
        self.assertTrue(is_suspicious_transcript_text(text))

    def test_does_not_classify_coherent_chinese_prose_as_suspicious(self) -> None:
        text = "今天我们先复盘昨晚美股，再看当前仓位和接下来的节奏安排。"
        self.assertFalse(is_suspicious_transcript_text(text))

    def test_does_not_classify_short_valid_phrase_as_suspicious(self) -> None:
        self.assertFalse(is_suspicious_transcript_text("可以啊"))

    def test_prompt_admission_rejects_suspicious_text(self) -> None:
        text = "中文字幕:CaptionCube 中文字幕:CaptionCube"
        self.assertFalse(should_admit_transcript_prompt(text))

    def test_prompt_admission_accepts_coherent_text(self) -> None:
        text = "这段内容表达完整，也没有明显重复或片尾口号。"
        self.assertTrue(should_admit_transcript_prompt(text))


if __name__ == "__main__":
    unittest.main()
