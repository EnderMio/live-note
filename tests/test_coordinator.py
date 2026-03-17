from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from live_note.app.coordinator import FileImportCoordinator, SessionCoordinator
from live_note.app.journal import SessionWorkspace, list_sessions
from live_note.audio.capture import InputDevice
from live_note.audio.convert import AudioImportError
from live_note.config import (
    AppConfig,
    AudioConfig,
    ImportConfig,
    LlmConfig,
    ObsidianConfig,
    RefineConfig,
    WhisperConfig,
)


class _FakeObsidianClient:
    def put_note(self, path: str, content: str) -> None:
        del path, content


class CoordinatorFailureTests(unittest.TestCase):
    def test_live_coordinator_marks_session_failed_when_startup_step_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _sample_config(root)
            runner = SessionCoordinator(
                config=config,
                title="产品周会",
                source="1",
                kind="meeting",
            )

            with (
                patch(
                    "live_note.app.coordinator.resolve_input_device",
                    return_value=InputDevice(1, "BlackHole 2ch", 2, 48000),
                ),
                patch("live_note.app.coordinator._attach_console_logging"),
                patch(
                    "live_note.app.coordinator.ObsidianClient",
                    return_value=_FakeObsidianClient(),
                ),
                patch("live_note.app.coordinator.OpenAiCompatibleClient"),
                patch("live_note.app.coordinator.WhisperInferenceClient"),
                patch("live_note.app.coordinator.WhisperServerProcess"),
                patch(
                    "live_note.app.coordinator.write_initial_transcript",
                    side_effect=RuntimeError("startup boom"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "startup boom"):
                    runner.run()

            metadata = _load_single_session_metadata(root)

        self.assertEqual("failed", metadata.status)

    def test_import_coordinator_marks_session_failed_when_processing_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            media_path = root / "sample.mp3"
            media_path.write_bytes(b"fake-audio")
            config = _sample_config(root)
            runner = FileImportCoordinator(
                config=config,
                file_path=str(media_path),
                title="课程录音",
                kind="lecture",
            )

            with (
                patch("live_note.app.coordinator._attach_console_logging"),
                patch(
                    "live_note.app.coordinator.ObsidianClient",
                    return_value=_FakeObsidianClient(),
                ),
                patch("live_note.app.coordinator.OpenAiCompatibleClient"),
                patch("live_note.app.coordinator.WhisperInferenceClient"),
                patch(
                    "live_note.app.coordinator.convert_audio_to_wav",
                    side_effect=AudioImportError("convert boom"),
                ),
            ):
                with self.assertRaisesRegex(AudioImportError, "convert boom"):
                    runner.run()

            metadata = _load_single_session_metadata(root)

        self.assertEqual("failed", metadata.status)


def _sample_config(root: Path) -> AppConfig:
    model_path = root / "ggml-large-v3.bin"
    model_path.write_bytes(b"fake-model")
    return AppConfig(
        audio=AudioConfig(),
        importer=ImportConfig(ffmpeg_binary="/opt/homebrew/bin/ffmpeg"),
        refine=RefineConfig(),
        whisper=WhisperConfig(
            binary="/Users/demo/whisper-server",
            model=model_path,
        ),
        obsidian=ObsidianConfig(
            base_url="https://127.0.0.1:27124",
            transcript_dir="Sessions/Transcripts",
            structured_dir="Sessions/Summaries",
            enabled=False,
        ),
        llm=LlmConfig(
            base_url="https://api.openai.com/v1",
            model="gpt-4.1-mini",
            enabled=False,
        ),
        root_dir=root,
    )


def _load_single_session_metadata(root: Path):
    session_root = next(iter(list_sessions(root)))
    return SessionWorkspace.load(session_root).read_session()
