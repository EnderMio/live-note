from __future__ import annotations

import unittest
from pathlib import Path

from live_note.app.services import _whisper_model_sort_key


class WhisperModelSelectionTests(unittest.TestCase):
    def test_whisper_model_sort_key_prefers_multilingual_larger_models(self) -> None:
        candidates = [
            Path("ggml-base.bin"),
            Path("ggml-medium.bin"),
            Path("ggml-large-v3-turbo.bin"),
            Path("ggml-small.en.bin"),
        ]

        ordered = sorted(candidates, key=_whisper_model_sort_key)

        self.assertEqual("ggml-large-v3-turbo.bin", ordered[0].name)
        self.assertEqual("ggml-medium.bin", ordered[1].name)
        self.assertEqual("ggml-base.bin", ordered[2].name)
        self.assertEqual("ggml-small.en.bin", ordered[3].name)
