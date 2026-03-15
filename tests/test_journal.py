from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from live_note.app.journal import SessionWorkspace
from live_note.domain import SessionMetadata


def sample_metadata(session_dir: str) -> SessionMetadata:
    return SessionMetadata(
        session_id="20260315-210500-机器学习",
        title="机器学习导论",
        kind="lecture",
        input_mode="live",
        source_label="BlackHole 2ch",
        source_ref="2",
        language="zh",
        started_at="2026-03-15T13:05:00+00:00",
        transcript_note_path="Sessions/Transcripts/2026-03-15/机器学习导论-210500.md",
        structured_note_path="Sessions/Summaries/2026-03-15/机器学习导论-210500.md",
        session_dir=session_dir,
        status="live",
    )


class SessionWorkspaceTests(unittest.TestCase):
    def test_rebuild_segment_states_and_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SessionWorkspace.create(Path(temp_dir), sample_metadata(temp_dir))
            wav_path = workspace.next_wav_path("seg-00001")
            wav_path.parent.mkdir(parents=True, exist_ok=True)
            wav_path.write_bytes(b"wav")

            workspace.record_segment_created("seg-00001", 0, 2000, wav_path)
            workspace.record_segment_text("seg-00001", 0, 2000, "第一段")
            workspace.record_segment_created(
                "seg-00002",
                2000,
                4000,
                workspace.next_wav_path("seg-00002"),
            )
            workspace.record_segment_error("seg-00002", 2000, 4000, "timeout")

            states = workspace.rebuild_segment_states()
            self.assertEqual(2, len(states))
            self.assertEqual("第一段", states[0].text)
            self.assertEqual("timeout", states[1].error)

            entries = workspace.transcript_entries()
            self.assertEqual(1, len(entries))
            self.assertEqual("第一段", entries[0].text)

    def test_rebuild_segment_states_clears_previous_error_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SessionWorkspace.create(Path(temp_dir), sample_metadata(temp_dir))
            wav_path = workspace.next_wav_path("seg-00001")
            wav_path.parent.mkdir(parents=True, exist_ok=True)
            wav_path.write_bytes(b"wav")

            workspace.record_segment_created("seg-00001", 0, 2000, wav_path)
            workspace.record_segment_error("seg-00001", 0, 2000, "timeout")
            workspace.record_segment_text("seg-00001", 0, 2000, "修正后的文本")

            states = workspace.rebuild_segment_states()

        self.assertEqual(1, len(states))
        self.assertEqual("修正后的文本", states[0].text)
        self.assertIsNone(states[0].error)

    def test_replace_canonical_journal_keeps_live_backup_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SessionWorkspace.create(Path(temp_dir), sample_metadata(temp_dir))
            workspace.record_segment_created(
                "seg-00001",
                0,
                2000,
                workspace.next_wav_path("seg-00001"),
            )
            replacement = workspace.refined_segments_tmp_jsonl
            workspace.record_segment_created(
                "seg-10001",
                0,
                4000,
                workspace.next_refined_wav_path("seg-10001"),
                journal_path=replacement,
            )

            workspace.replace_canonical_journal(replacement)

            self.assertTrue(workspace.segments_live_jsonl.exists())
            self.assertIn("seg-00001", workspace.segments_live_jsonl.read_text(encoding="utf-8"))
            self.assertIn("seg-10001", workspace.segments_jsonl.read_text(encoding="utf-8"))
