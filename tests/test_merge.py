from __future__ import annotations

import tempfile
import unittest
import wave
from array import array
from pathlib import Path

from live_note.app.coordinator import merge_sessions
from live_note.app.journal import SessionWorkspace, list_sessions
from live_note.app.services import AppService, SettingsDraft
from live_note.domain import SessionMetadata


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
        status="live",
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
) -> None:
    session_dir = root / ".live-note" / "sessions" / session_id
    workspace = SessionWorkspace.create(
        session_dir,
        _build_metadata(
            session_dir,
            session_id=session_id,
            title=title,
            started_at=started_at,
        ),
    )
    segment_wav = workspace.next_wav_path("seg-00001")
    _write_wav(segment_wav, 16000, segment_samples)
    _write_wav(workspace.session_live_wav, 16000, segment_samples)
    duration_ms = round(len(segment_samples) * 1000 / 16000)
    workspace.record_segment_created("seg-00001", 0, duration_ms, segment_wav)
    workspace.record_segment_text("seg-00001", 0, duration_ms, text)


class MergeSessionTests(unittest.TestCase):
    def test_merge_sessions_creates_new_combined_session_and_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_path = root / "ggml-large-v3.bin"
            model_path.write_bytes(b"fake-model")
            service = AppService(root / "config.toml")
            service.save_settings(
                SettingsDraft(
                    ffmpeg_binary="/opt/homebrew/bin/ffmpeg",
                    whisper_binary="/Users/demo/whisper-server",
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
