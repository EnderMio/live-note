from __future__ import annotations

import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from live_note.audio.convert import convert_audio_to_wav, split_wav_file


class AudioConvertTests(unittest.TestCase):
    def test_split_wav_file_creates_time_ordered_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "sample.wav"
            with wave.open(str(input_path), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(16000)
                handle.writeframes(b"\x00\x00" * 16000 * 3)

            chunks = split_wav_file(input_path, Path(temp_dir) / "segments", chunk_seconds=2)

            self.assertEqual(2, len(chunks))
            self.assertEqual("seg-00001", chunks[0].segment_id)
            self.assertEqual(0, chunks[0].started_ms)
            self.assertEqual(2000, chunks[0].ended_ms)
            self.assertEqual(2000, chunks[1].started_ms)
            self.assertEqual(3000, chunks[1].ended_ms)
            self.assertTrue(chunks[0].wav_path.exists())

    def test_convert_audio_to_wav_invokes_ffmpeg_with_pcm_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "sample.mp3"
            output_path = Path(temp_dir) / "normalized.wav"
            input_path.write_bytes(b"fake")

            with patch("live_note.audio.convert.shutil.which", return_value="/usr/bin/ffmpeg"):
                with patch(
                    "live_note.audio.convert.subprocess.run",
                    return_value=SimpleNamespace(returncode=0, stderr="", stdout=""),
                ) as run_mock:
                    convert_audio_to_wav(input_path, output_path, sample_rate=16000)

        command = run_mock.call_args.args[0]
        self.assertEqual("/usr/bin/ffmpeg", command[0])
        self.assertIn("-ac", command)
        self.assertIn("1", command)
        self.assertIn("pcm_s16le", command)
        self.assertEqual(str(output_path), command[-1])
