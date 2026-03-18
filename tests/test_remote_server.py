from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from live_note.app.journal import SessionWorkspace
from live_note.domain import SessionMetadata
from live_note.remote.server import build_session_artifacts_payload


class RemoteServerTests(unittest.TestCase):
    def test_build_session_artifacts_payload_includes_entries_and_session_audio_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_root = root / "session-1"
            workspace = SessionWorkspace.create(
                session_root,
                _sample_metadata(session_root),
            )
            wav_path = workspace.next_wav_path("seg-00001")
            wav_path.parent.mkdir(parents=True, exist_ok=True)
            wav_path.write_bytes(b"wav")
            workspace.record_segment_created("seg-00001", 0, 2000, wav_path)
            workspace.record_segment_text(
                "seg-00001",
                0,
                2000,
                "大家好。",
                speaker_label="Speaker 1",
            )
            workspace.session_live_wav.write_bytes(b"RIFF")

            payload = build_session_artifacts_payload(workspace)

        self.assertEqual("session-1", payload["metadata"]["session_id"])
        self.assertTrue(payload["has_session_audio"])
        self.assertEqual("Speaker 1", payload["entries"][0]["speaker_label"])


def _sample_metadata(session_dir: Path) -> SessionMetadata:
    return SessionMetadata(
        session_id="session-1",
        title="产品周会",
        kind="meeting",
        input_mode="live",
        source_label="BlackHole 2ch",
        source_ref="1",
        language="zh",
        started_at="2026-03-18T10:00:00+00:00",
        transcript_note_path="Sessions/Transcripts/2026-03-18/demo.md",
        structured_note_path="Sessions/Summaries/2026-03-18/demo.md",
        session_dir=str(session_dir),
        status="finalized",
        transcript_source="refined",
        refine_status="done",
        execution_target="remote",
        remote_session_id="session-1",
        speaker_status="done",
    )
