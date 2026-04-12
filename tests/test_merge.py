from __future__ import annotations

import tempfile
import unittest
import wave
from array import array
from pathlib import Path
from unittest.mock import patch

from live_note.session_workspace import SessionWorkspace, list_sessions
from live_note.app.services import AppService, SettingsDraft
from live_note.domain import SessionMetadata
from live_note.runtime.domain.session_state import SessionStatus
from live_note.runtime.session_mutations import create_workspace_session
from live_note.runtime.session_workflows import merge_sessions, refine_session

TEST_WHISPER_BINARY = "/test-bin/whisper-server"


def _write_wav(path: Path, sample_rate: int, samples: list[int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = array("h", samples).tobytes()
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(frames)


def _build_metadata(
    session_dir: Path,
    *,
    session_id: str,
    title: str,
    started_at: str,
) -> SessionMetadata:
    return SessionMetadata(
        session_id=session_id,
        title=title,
        kind="meeting",
        input_mode="live",
        source_label="BlackHole 2ch",
        source_ref="2",
        language="zh",
        started_at=started_at,
        transcript_note_path=f"Sessions/Transcripts/2026-03-15/{session_id}.md",
        structured_note_path=f"Sessions/Summaries/2026-03-15/{session_id}.md",
        session_dir=str(session_dir),
        status=SessionStatus.INGESTING.value,
        transcript_source="live",
        refine_status="pending",
    )


def _create_live_session(
    root: Path,
    *,
    session_id: str,
    title: str,
    started_at: str,
    segment_samples: list[int],
    text: str,
    sample_rate: int = 16000,
) -> None:
    session_dir = root / ".live-note" / "sessions" / session_id
    workspace = create_workspace_session(
        root,
        _build_metadata(
            session_dir,
            session_id=session_id,
            title=title,
            started_at=started_at,
        ),
    )
    segment_wav = workspace.next_wav_path("seg-00001")
    _write_wav(segment_wav, sample_rate, segment_samples)
    _write_wav(workspace.session_live_wav, sample_rate, segment_samples)
    duration_ms = round(len(segment_samples) * 1000 / sample_rate)
    workspace.record_segment_created("seg-00001", 0, duration_ms, segment_wav)
    workspace.record_segment_text("seg-00001", 0, duration_ms, text)


class MergeSessionTests(unittest.TestCase):
    def test_refine_session_reconstructs_missing_session_live_audio_from_segments(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_path = root / "ggml-large-v3.bin"
            model_path.write_bytes(b"fake-model")
            service = AppService(root / "config.toml")
            service.save_settings(
                SettingsDraft(
                    ffmpeg_binary="/opt/homebrew/bin/ffmpeg",
                    whisper_binary=TEST_WHISPER_BINARY,
                    whisper_model=str(model_path),
                    obsidian_enabled=False,
                    llm_enabled=False,
                )
            )
            session_id = "20260315-210500-课程片段回拼"
            session_dir = root / ".live-note" / "sessions" / session_id
            workspace = create_workspace_session(
                root,
                _build_metadata(
                    session_dir,
                    session_id=session_id,
                    title="课程片段回拼",
                    started_at="2026-03-15T13:05:00+00:00",
                ),
            )
            first = workspace.next_wav_path("seg-00001")
            second = workspace.next_wav_path("seg-00002")
            _write_wav(first, 16000, [1000, -1000] * 8000)
            _write_wav(second, 16000, [2000, -2000] * 8000)
            workspace.record_segment_created("seg-00001", 0, 1000, first)
            workspace.record_segment_created("seg-00002", 1500, 2500, second)
            self.assertFalse(workspace.session_live_wav.exists())

            with (
                patch("live_note.runtime.session_workflows._attach_console_logging"),
                patch("live_note.runtime.session_workflows.ObsidianClient"),
                patch("live_note.runtime.session_workflows.OpenAiCompatibleClient"),
                patch("live_note.runtime.session_workflows.publish_final_outputs"),
                patch(
                    "live_note.runtime.session_workflows._run_live_refinement",
                    side_effect=lambda **kwargs: kwargs["metadata"],
                ),
            ):
                refine_session(service.load_config(), session_id)

            self.assertTrue(workspace.session_live_wav.exists())
            with wave.open(str(workspace.session_live_wav), "rb") as handle:
                self.assertEqual(16000, handle.getframerate())
                self.assertEqual(40000, handle.getnframes())

    def test_merge_sessions_creates_new_combined_session_and_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_path = root / "ggml-large-v3.bin"
            model_path.write_bytes(b"fake-model")
            service = AppService(root / "config.toml")
            service.save_settings(
                SettingsDraft(
                    ffmpeg_binary="/opt/homebrew/bin/ffmpeg",
                    whisper_binary=TEST_WHISPER_BINARY,
                    whisper_model=str(model_path),
                    obsidian_enabled=False,
                    llm_enabled=False,
                )
            )
            _create_live_session(
                root,
                session_id="20260315-210500-产品周会上半场",
                title="产品周会",
                started_at="2026-03-15T13:05:00+00:00",
                segment_samples=[1000, -1000] * 8000,
                text="第一部分讨论排期。",
            )
            _create_live_session(
                root,
                session_id="20260315-213500-产品周会下半场",
                title="产品周会",
                started_at="2026-03-15T13:35:00+00:00",
                segment_samples=[2000, -2000] * 12000,
                text="第二部分讨论风险。",
            )

            merge_sessions(
                service.load_config(),
                ["20260315-210500-产品周会上半场", "20260315-213500-产品周会下半场"],
            )

            session_roots = list(list_sessions(root))
            self.assertEqual(3, len(session_roots))
            merged_root = next(
                path
                for path in session_roots
                if path.name
                not in {"20260315-210500-产品周会上半场", "20260315-213500-产品周会下半场"}
            )
            workspace = SessionWorkspace.load(merged_root)
            metadata = workspace.read_session()
            entries = workspace.transcript_entries()

            self.assertEqual("产品周会（合并）", metadata.title)
            self.assertEqual("live", metadata.input_mode)
            self.assertEqual("pending", metadata.refine_status)
            self.assertEqual(2, len(entries))
            self.assertEqual("第一部分讨论排期。", entries[0].text)
            self.assertEqual("第二部分讨论风险。", entries[1].text)
            self.assertEqual(0, entries[0].started_ms)
            self.assertEqual(1000, entries[0].ended_ms)
            self.assertEqual(1000, entries[1].started_ms)
            self.assertEqual(2500, entries[1].ended_ms)
            self.assertTrue(workspace.session_live_wav.exists())
            self.assertTrue(workspace.transcript_md.exists())
            self.assertTrue(workspace.structured_md.exists())
            transcript_body = workspace.transcript_md.read_text(encoding="utf-8")
            self.assertIn("第一部分讨论排期。", transcript_body)
            self.assertIn("第二部分讨论风险。", transcript_body)

            with wave.open(str(workspace.session_live_wav), "rb") as handle:
                self.assertEqual(16000, handle.getframerate())
                self.assertEqual(40000, handle.getnframes())

    def test_merge_sessions_keeps_text_when_live_audio_cannot_be_concatenated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_path = root / "ggml-large-v3.bin"
            model_path.write_bytes(b"fake-model")
            service = AppService(root / "config.toml")
            service.save_settings(
                SettingsDraft(
                    ffmpeg_binary="/opt/homebrew/bin/ffmpeg",
                    whisper_binary=TEST_WHISPER_BINARY,
                    whisper_model=str(model_path),
                    obsidian_enabled=False,
                    llm_enabled=False,
                )
            )
            _create_live_session(
                root,
                session_id="20260315-210500-课程上半场",
                title="宏观经济学",
                started_at="2026-03-15T13:05:00+00:00",
                segment_samples=[1000, -1000] * 8000,
                text="第一部分讨论供给。",
                sample_rate=16000,
            )
            _create_live_session(
                root,
                session_id="20260315-213500-课程下半场",
                title="宏观经济学",
                started_at="2026-03-15T13:35:00+00:00",
                segment_samples=[2000, -2000] * 22050,
                text="第二部分讨论需求。",
                sample_rate=44100,
            )

            merge_sessions(
                service.load_config(),
                ["20260315-210500-课程上半场", "20260315-213500-课程下半场"],
            )

            session_roots = list(list_sessions(root))
            self.assertEqual(3, len(session_roots))
            merged_root = next(
                path
                for path in session_roots
                if path.name not in {"20260315-210500-课程上半场", "20260315-213500-课程下半场"}
            )
            workspace = SessionWorkspace.load(merged_root)
            metadata = workspace.read_session()
            entries = workspace.transcript_entries()

            self.assertEqual("宏观经济学（合并）", metadata.title)
            self.assertEqual("disabled", metadata.refine_status)
            self.assertFalse(workspace.session_live_wav.exists())
            self.assertEqual(2, len(entries))
            self.assertEqual("第一部分讨论供给。", entries[0].text)
            self.assertEqual("第二部分讨论需求。", entries[1].text)
            self.assertTrue(workspace.transcript_md.exists())
            self.assertTrue(workspace.structured_md.exists())

    def test_merge_sessions_keeps_text_when_session_live_wav_is_corrupted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_path = root / "ggml-large-v3.bin"
            model_path.write_bytes(b"fake-model")
            service = AppService(root / "config.toml")
            service.save_settings(
                SettingsDraft(
                    ffmpeg_binary="/opt/homebrew/bin/ffmpeg",
                    whisper_binary=TEST_WHISPER_BINARY,
                    whisper_model=str(model_path),
                    obsidian_enabled=False,
                    llm_enabled=False,
                )
            )
            first_id = "20260315-210500-课程上半场"
            second_id = "20260315-213500-课程下半场"
            _create_live_session(
                root,
                session_id=first_id,
                title="国际政治",
                started_at="2026-03-15T13:05:00+00:00",
                segment_samples=[1000, -1000] * 8000,
                text="第一部分讨论联盟。",
            )
            _create_live_session(
                root,
                session_id=second_id,
                title="国际政治",
                started_at="2026-03-15T13:35:00+00:00",
                segment_samples=[2000, -2000] * 8000,
                text="第二部分讨论关税。",
            )
            broken_wav = root / ".live-note" / "sessions" / second_id / "session.live.wav"
            broken_wav.write_bytes(b"not-a-valid-wav")

            merge_sessions(service.load_config(), [first_id, second_id])

            session_roots = list(list_sessions(root))
            self.assertEqual(3, len(session_roots))
            merged_root = next(
                path for path in session_roots if path.name not in {first_id, second_id}
            )
            workspace = SessionWorkspace.load(merged_root)
            metadata = workspace.read_session()
            entries = workspace.transcript_entries()

        self.assertEqual("国际政治（合并）", metadata.title)
        self.assertEqual("disabled", metadata.refine_status)
        self.assertFalse(workspace.session_live_wav.exists())
        self.assertEqual(["第一部分讨论联盟。", "第二部分讨论关税。"], [e.text for e in entries])
