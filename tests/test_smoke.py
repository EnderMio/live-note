from __future__ import annotations

import tempfile
import unittest
import wave
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from live_note.app.cli import main
from live_note.session_workspace import build_workspace
from live_note.app.services import AppService, SettingsDraft
from live_note.domain import TranscriptEntry


def _write_silent_wav(path: Path, *, sample_rate: int, seconds: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"\x00\x00" * sample_rate * seconds)


class SmokeTests(unittest.TestCase):
    def test_cli_import_and_finalize_complete_minimal_local_flow(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_path = root / "ggml-base.bin"
            model_path.write_bytes(b"model")
            media_path = root / "demo.mp3"
            media_path.write_bytes(b"fake-audio")
            service = AppService(root / "config.toml")
            service.save_settings(
                SettingsDraft(
                    ffmpeg_binary="/usr/bin/ffmpeg",
                    whisper_binary="/usr/bin/whisper-server",
                    whisper_model=str(model_path),
                    obsidian_enabled=False,
                    llm_enabled=False,
                )
            )

            def fake_convert_audio_to_wav(
                input_path: Path,
                output_path: Path,
                sample_rate: int,
                ffmpeg_binary: str = "ffmpeg",
            ) -> Path:
                self.assertEqual(media_path.name, input_path.name)
                self.assertEqual("/usr/bin/ffmpeg", ffmpeg_binary)
                _write_silent_wav(output_path, sample_rate=sample_rate, seconds=2)
                return output_path

            def fake_process_segment(
                *,
                pending,
                workspace,
                entries,
                **_kwargs,
            ) -> bool:
                text = f"转写 {pending.segment_id}"
                workspace.record_segment_text(
                    pending.segment_id,
                    pending.started_ms,
                    pending.ended_ms,
                    text,
                )
                entries.append(
                    TranscriptEntry(
                        segment_id=pending.segment_id,
                        started_ms=pending.started_ms,
                        ended_ms=pending.ended_ms,
                        text=text,
                    )
                )
                return True

            with (
                patch(
                    "live_note.runtime.local_runners.convert_audio_to_wav",
                    side_effect=fake_convert_audio_to_wav,
                ),
                patch(
                    "live_note.runtime.local_runners._runtime_whisper_config",
                    side_effect=lambda config, _language: config,
                ),
                patch(
                    "live_note.runtime.local_runners.WhisperServerProcess",
                    side_effect=lambda *_args, **_kwargs: nullcontext(),
                ),
                patch(
                    "live_note.runtime.local_runners._process_segment",
                    side_effect=fake_process_segment,
                ),
            ):
                import_exit = main(
                    [
                        "--config",
                        str(root / "config.toml"),
                        "import",
                        "--file",
                        str(media_path),
                        "--kind",
                        "meeting",
                    ]
                )

            self.assertEqual(0, import_exit)
            summaries = service.list_session_summaries()
            self.assertEqual(1, len(summaries))
            session_id = summaries[0].session_id

            finalize_exit = main(
                [
                    "--config",
                    str(root / "config.toml"),
                    "finalize",
                    "--session",
                    session_id,
                ]
            )

            self.assertEqual(0, finalize_exit)
            workspace = build_workspace(root, session_id)
            metadata = workspace.read_session()
            transcript_text = workspace.transcript_md.read_text(encoding="utf-8")
            structured_text = workspace.structured_md.read_text(encoding="utf-8")

            self.assertEqual("transcript_only", metadata.status)
            self.assertIn("转写 seg-00001", transcript_text)
            self.assertIn("当前会话未启用自动整理", structured_text)
