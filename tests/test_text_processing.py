from __future__ import annotations

import unittest
from array import array

from live_note.domain import TranscriptEntry
from live_note.transcribe.text import (
    _SIMPLIFIER,
    build_transcription_prompt,
    normalize_transcript_text,
)


class TextProcessingTests(unittest.TestCase):
    def test_build_transcription_prompt_uses_recent_context(self) -> None:
        prompt = build_transcription_prompt(
            "zh",
            [
                TranscriptEntry("seg-00001", 0, 2000, "前面提到了美股和关税。"),
                TranscriptEntry("seg-00002", 2000, 4000, "后面还会继续讲资产配置。"),
            ],
        )

        self.assertIsNotNone(prompt)
        self.assertIn("只转写实际听到的语音", prompt)
        self.assertIn("前面提到了美股和关税", prompt)

    def test_build_transcription_prompt_returns_guard_without_context(self) -> None:
        prompt = build_transcription_prompt("zh", [])
        self.assertIn("只转写实际听到的语音", prompt)
        self.assertNotIn("最近上下文", prompt)

    def test_build_transcription_prompt_for_auto_preserves_multilingual_output(self) -> None:
        prompt = build_transcription_prompt(
            "auto",
            [
                TranscriptEntry("seg-00001", 0, 2000, "今天我们先看 NVIDIA earnings。"),
            ],
        )

        self.assertIn("Preserve the original spoken languages and scripts", prompt)
        self.assertIn("Recent context:", prompt)
        self.assertNotIn("简体中文", prompt)

    def test_normalize_transcript_text_collapses_lines(self) -> None:
        text = normalize_transcript_text(" 第一行 \n\n 第二行 ", "en")
        self.assertEqual("第一行 第二行", text)

    def test_normalize_transcript_text_drops_low_energy_outro_hallucination(self) -> None:
        pcm16 = array("h", [0, 8, -6, 5] * 4000).tobytes()
        text = normalize_transcript_text(
            "谢谢观看 欢迎订阅我的频道",
            "zh",
            pcm16=pcm16,
            sample_rate=16000,
        )
        self.assertEqual("", text)

    def test_normalize_transcript_text_keeps_high_energy_short_phrase(self) -> None:
        pcm16 = array("h", [6000, -6000, 4200, -4200] * 4000).tobytes()
        text = normalize_transcript_text(
            "谢谢大家",
            "zh",
            pcm16=pcm16,
            sample_rate=16000,
        )
        self.assertEqual("谢谢大家", text)

    @unittest.skipUnless(_SIMPLIFIER is not None, "opencc 未安装")
    def test_normalize_transcript_text_simplifies_chinese_when_opencc_available(self) -> None:
        text = normalize_transcript_text("對於這個目前來講", "zh")
        self.assertEqual("对于这个目前来讲", text)

    @unittest.skipUnless(_SIMPLIFIER is not None, "opencc 未安装")
    def test_normalize_transcript_text_keeps_original_script_in_auto_mode(self) -> None:
        text = normalize_transcript_text("對於這個 AI topic 目前來講", "auto")
        self.assertEqual("對於這個 AI topic 目前來講", text)
