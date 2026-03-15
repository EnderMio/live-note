from __future__ import annotations

import unittest

from live_note.audio.segmentation import SpeechSegmenter
from live_note.config import AudioConfig
from live_note.domain import AudioFrame


class FakeVad:
    def __init__(self, decisions: list[bool]):
        self.decisions = decisions
        self.index = 0

    def is_speech(self, pcm16: bytes, sample_rate: int) -> bool:
        decision = self.decisions[self.index]
        self.index += 1
        return decision


class SpeechSegmenterTests(unittest.TestCase):
    def test_finalize_after_silence_gap(self) -> None:
        config = AudioConfig(
            sample_rate=16000,
            frame_duration_ms=500,
            silence_ms=800,
            min_segment_ms=2000,
            max_segment_ms=5000,
        )
        segmenter = SpeechSegmenter(config, vad=FakeVad([True, True, False, False]))
        outputs = []
        for index in range(4):
            outputs.extend(
                segmenter.feed(
                    AudioFrame(
                        started_ms=index * 500,
                        ended_ms=(index + 1) * 500,
                        pcm16=b"\x00\x00" * 8000,
                    )
                )
            )

        self.assertEqual(1, len(outputs))
        self.assertEqual(0, outputs[0].started_ms)
        self.assertEqual(2000, outputs[0].ended_ms)
