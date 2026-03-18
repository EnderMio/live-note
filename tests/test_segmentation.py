from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from live_note.audio.segmentation import SegmentationError, SpeechSegmenter
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
    def test_load_vad_falls_back_to_native_module_when_webrtcvad_needs_pkg_resources(self) -> None:
        fake_native = SimpleNamespace(
            create=lambda: object(),
            init=lambda handle: None,
            set_mode=lambda handle, mode: None,
            process=lambda handle, sample_rate, pcm16, frame_count: True,
        )

        def fake_import(name: str):
            if name == "webrtcvad":
                raise ModuleNotFoundError("No module named 'pkg_resources'")
            if name == "_webrtcvad":
                return fake_native
            raise AssertionError(f"unexpected import: {name}")

        with patch("live_note.audio.segmentation.importlib.import_module", side_effect=fake_import):
            segmenter = SpeechSegmenter(AudioConfig())

        self.assertTrue(segmenter.vad.is_speech(b"\x00\x00" * 160, 16000))

    def test_load_vad_raises_clear_error_when_both_modules_are_missing(self) -> None:
        def fake_import(name: str):
            if name in {"webrtcvad", "_webrtcvad"}:
                raise ModuleNotFoundError(f"No module named '{name}'")
            raise AssertionError(f"unexpected import: {name}")

        with patch("live_note.audio.segmentation.importlib.import_module", side_effect=fake_import):
            with self.assertRaisesRegex(SegmentationError, "无法加载 webrtcvad"):
                SpeechSegmenter(AudioConfig())

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
