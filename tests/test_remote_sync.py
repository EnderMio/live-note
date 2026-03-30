from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from live_note.app.remote_sync import apply_remote_artifacts, sync_remote_transcript_snapshot
from live_note.config import (
    AppConfig,
    AudioConfig,
    ImportConfig,
    LlmConfig,
    ObsidianConfig,
    RefineConfig,
    WhisperConfig,
)
from live_note.domain import SessionMetadata, TranscriptEntry


class RemoteSyncTests(unittest.TestCase):
    def test_sync_remote_transcript_snapshot_keeps_running_draft_local_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _sample_config(root)
            metadata = _sample_metadata(root, "remote-live-1")
            entries = [
                TranscriptEntry(
                    segment_id="seg-00001",
                    started_ms=0,
                    ended_ms=1200,
                    text="大家好，开始吧。",
                )
            ]

            with patch(
                "live_note.app.remote_sync.try_sync_note",
                side_effect=AssertionError("远端 snapshot 不应同步到 Obsidian"),
            ):
                local_metadata = sync_remote_transcript_snapshot(config, metadata, entries)

            transcript = (
                root / ".live-note" / "sessions" / "remote-live-1" / "transcript.md"
            ).read_text(encoding="utf-8")

        self.assertEqual("remote-live-1", local_metadata.session_id)
        self.assertIn("大家好，开始吧。", transcript)

    def test_apply_remote_artifacts_writes_remote_note_contents_without_local_republish(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _sample_config(root)
            metadata = _sample_metadata(root, "remote-import-1")
            entries = [
                TranscriptEntry(
                    segment_id="seg-00001",
                    started_ms=0,
                    ended_ms=1200,
                    text="今天先看市场结构。",
                    speaker_label="Speaker 1",
                )
            ]
            transcript_content = "# 远端原文\n\n- [00:00:00] Speaker 1: 今天先看市场结构。\n"
            structured_content = "# 远端整理\n\n- 要点 1\n"

            with patch(
                "live_note.app.remote_sync.publish_final_outputs",
                side_effect=AssertionError("提供远端成品后不应再本地重写"),
            ):
                local_metadata = apply_remote_artifacts(
                    config,
                    metadata,
                    entries,
                    transcript_content=transcript_content,
                    structured_content=structured_content,
                )

            workspace_root = root / ".live-note" / "sessions" / "remote-import-1"
            transcript_path = workspace_root / "transcript.md"
            structured_path = workspace_root / "structured.md"
            transcript_value = transcript_path.read_text(encoding="utf-8")
            structured_value = structured_path.read_text(encoding="utf-8")

        self.assertEqual("remote-import-1", local_metadata.session_id)
        self.assertEqual(transcript_content, transcript_value)
        self.assertEqual(structured_content, structured_value)


def _sample_config(root: Path) -> AppConfig:
    model_path = root / "ggml-large-v3.bin"
    model_path.write_bytes(b"fake-model")
    return AppConfig(
        audio=AudioConfig(),
        importer=ImportConfig(),
        refine=RefineConfig(),
        whisper=WhisperConfig(
            binary="/Users/demo/whisper-server",
            model=model_path,
        ),
        obsidian=ObsidianConfig(
            enabled=False,
            base_url="https://127.0.0.1:27124",
            transcript_dir="Sessions/Transcripts",
            structured_dir="Sessions/Summaries",
        ),
        llm=LlmConfig(
            enabled=False,
            base_url="https://api.openai.com/v1",
            model="gpt-4.1-mini",
        ),
        root_dir=root,
    )


def _sample_metadata(root: Path, session_id: str) -> SessionMetadata:
    return SessionMetadata(
        session_id=session_id,
        title="股票课",
        kind="lecture",
        input_mode="file",
        source_label="第1课.mp3",
        source_ref="remote-upload://第1课.mp3",
        language="zh",
        started_at="2026-03-19T08:00:00+00:00",
        transcript_note_path="Sessions/Transcripts/2026-03-19/股票课.md",
        structured_note_path="Sessions/Summaries/2026-03-19/股票课.md",
        session_dir=str(root / ".live-note" / "sessions" / session_id),
        status="transcript_only",
        transcript_source="refined",
        refine_status="disabled",
        execution_target="remote",
        remote_session_id=session_id,
        speaker_status="done",
    )
