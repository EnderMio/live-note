from __future__ import annotations

import logging
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from live_note.session_workspace import SessionWorkspace
from live_note.runtime.session_outputs import (
    build_structured_output,
    publish_failure_outputs,
    publish_final_outputs,
    try_sync_note,
    write_initial_transcript,
)
from live_note.config import LlmConfig, ObsidianConfig
from live_note.domain import SessionMetadata, TranscriptEntry
from live_note.llm import LlmError, OpenAiCompatibleClient
from live_note.obsidian.client import ObsidianClient, ObsidianError
from live_note.runtime.domain.session_state import SessionStatus
from live_note.runtime.session_mutations import create_workspace_session


class SessionOutputsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.metadata = SessionMetadata(
            session_id="20260318-120000-课程记录",
            title="课程记录",
            kind="lecture",
            input_mode="live",
            source_label="BlackHole 2ch",
            source_ref="1",
            language="zh",
            started_at="2026-03-18T04:00:00+00:00",
            transcript_note_path="Sessions/Transcripts/2026-03-18/课程记录-120000.md",
            structured_note_path="Sessions/Summaries/2026-03-18/课程记录-120000.md",
            session_dir="/tmp/20260318-120000-课程记录",
            status=SessionStatus.HANDOFF_COMMITTED.value,
            transcript_source="live",
            refine_status="pending",
        )

    def test_build_structured_output_returns_failure_when_entries_missing(self) -> None:
        llm_client = OpenAiCompatibleClient(
            LlmConfig(base_url="https://api.openai.com/v1", model="gpt-4.1-mini", enabled=False)
        )

        body, status = build_structured_output(
            llm_client=llm_client,
            metadata=self.metadata,
            entries=[],
            transcript_note_path=self.metadata.transcript_note_path,
        )

        self.assertEqual("structured_failed", status)
        self.assertIn("当前会话没有可用的转写文本。", body)

    def test_build_structured_output_returns_pending_template_when_llm_disabled(self) -> None:
        llm_client = OpenAiCompatibleClient(
            LlmConfig(base_url="https://api.openai.com/v1", model="gpt-4.1-mini", enabled=False)
        )
        entries = [
            TranscriptEntry(
                segment_id="seg-00001",
                started_ms=0,
                ended_ms=1000,
                text="今天讲随机梯度下降。",
            )
        ]

        body, status = build_structured_output(
            llm_client=llm_client,
            metadata=self.metadata,
            entries=entries,
            transcript_note_path=self.metadata.transcript_note_path,
        )

        self.assertEqual("transcript_only", status)
        self.assertIn("当前会话未启用自动整理", body)
        self.assertIn("## 关键点", body)

    def test_publish_final_outputs_writes_notes_and_updates_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_dir = Path(temp_dir) / ".live-note" / "sessions" / self.metadata.session_id
            metadata = replace(self.metadata, session_dir=str(session_dir))
            workspace = create_workspace_session(Path(temp_dir), metadata)
            workspace.record_segment_created(
                "seg-00001",
                0,
                1200,
                workspace.next_wav_path("seg-00001"),
            )
            workspace.record_segment_text("seg-00001", 0, 1200, "今天讲随机梯度下降。")
            llm_client = OpenAiCompatibleClient(
                LlmConfig(base_url="https://api.openai.com/v1", model="gpt-4.1-mini", enabled=False)
            )
            obsidian = ObsidianClient(
                ObsidianConfig(
                    base_url="https://127.0.0.1:27124",
                    transcript_dir="Sessions/Transcripts",
                    structured_dir="Sessions/Summaries",
                    enabled=False,
                )
            )
            logger = logging.getLogger("test.session_outputs")
            progress = Mock()

            with patch("live_note.runtime.session_outputs.detect_review_items", return_value=[]):
                publish_final_outputs(
                    workspace=workspace,
                    metadata=metadata,
                    obsidian=obsidian,
                    llm_client=llm_client,
                    logger=logger,
                    on_progress=progress,
                )

            saved_metadata = workspace.read_session()
            transcript = workspace.transcript_md.read_text(encoding="utf-8")
            structured = workspace.structured_md.read_text(encoding="utf-8")

        self.assertEqual("transcript_only", saved_metadata.status)
        self.assertIn("今天讲随机梯度下降。", transcript)
        self.assertIn("## 关键点", structured)
        self.assertTrue(progress.called)

    def test_write_initial_transcript_does_not_sync_obsidian_live_draft(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_dir = Path(temp_dir) / ".live-note" / "sessions" / self.metadata.session_id
            metadata = replace(
                self.metadata,
                session_dir=str(session_dir),
                status=SessionStatus.INGESTING.value,
            )
            workspace = create_workspace_session(Path(temp_dir), metadata)
            obsidian = Mock(spec=ObsidianClient)

            write_initial_transcript(
                workspace=workspace,
                metadata=metadata,
                obsidian=obsidian,
                logger=logging.getLogger("test.session_outputs"),
                status=SessionStatus.INGESTING.value,
            )

            transcript = workspace.transcript_md.read_text(encoding="utf-8")

        obsidian.put_note.assert_not_called()
        self.assertIn(f'status: "{SessionStatus.INGESTING.value}"', transcript)

    def test_publish_final_outputs_syncs_transcript_and_structured_notes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_dir = Path(temp_dir) / ".live-note" / "sessions" / self.metadata.session_id
            metadata = replace(self.metadata, session_dir=str(session_dir))
            workspace = create_workspace_session(Path(temp_dir), metadata)
            workspace.record_segment_text("seg-00001", 0, 1200, "今天讲随机梯度下降。")
            llm_client = OpenAiCompatibleClient(
                LlmConfig(base_url="https://api.openai.com/v1", model="gpt-4.1-mini", enabled=False)
            )
            obsidian = Mock(spec=ObsidianClient)

            with patch("live_note.runtime.session_outputs.detect_review_items", return_value=[]):
                publish_final_outputs(
                    workspace=workspace,
                    metadata=metadata,
                    obsidian=obsidian,
                    llm_client=llm_client,
                    logger=logging.getLogger("test.session_outputs"),
                )

        self.assertEqual(2, obsidian.put_note.call_count)
        self.assertEqual(metadata.transcript_note_path, obsidian.put_note.call_args_list[0].args[0])
        self.assertEqual(metadata.structured_note_path, obsidian.put_note.call_args_list[1].args[0])

    def test_publish_failure_outputs_writes_minimal_failure_notes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_dir = Path(temp_dir) / ".live-note" / "sessions" / self.metadata.session_id
            metadata = replace(
                self.metadata,
                session_dir=str(session_dir),
                status=SessionStatus.INGESTING.value,
            )
            workspace = create_workspace_session(Path(temp_dir), metadata)
            workspace.record_segment_text("seg-00001", 0, 1200, "这是实时草稿。")
            obsidian = Mock(spec=ObsidianClient)

            failed_metadata = publish_failure_outputs(
                workspace=workspace,
                metadata=metadata,
                obsidian=obsidian,
                logger=logging.getLogger("test.session_outputs"),
                reason="LLM 请求失败",
            )

            transcript = workspace.transcript_md.read_text(encoding="utf-8")
            structured = workspace.structured_md.read_text(encoding="utf-8")

        self.assertEqual("failed", failed_metadata.status)
        self.assertEqual(2, obsidian.put_note.call_count)
        self.assertNotIn("这是实时草稿。", transcript)
        self.assertIn("原文暂未成功生成", transcript)
        self.assertIn("LLM 请求失败", structured)

    def test_build_structured_output_returns_failure_note_when_llm_errors(self) -> None:
        llm_client = OpenAiCompatibleClient(
            LlmConfig(
                base_url="https://api.openai.com/v1",
                model="gpt-4.1-mini",
                enabled=True,
                api_key="demo",
            )
        )
        entries = [
            TranscriptEntry(
                segment_id="seg-00001",
                started_ms=0,
                ended_ms=1000,
                text="今天讲随机梯度下降。",
            )
        ]

        with patch.object(
            OpenAiCompatibleClient,
            "generate_structured_note",
            side_effect=LlmError("LLM 请求失败"),
        ):
            body, status = build_structured_output(
                llm_client=llm_client,
                metadata=self.metadata,
                entries=entries,
                transcript_note_path=self.metadata.transcript_note_path,
            )

        self.assertEqual("structured_failed", status)
        self.assertIn("LLM 请求失败", body)

    def test_try_sync_note_swallows_obsidian_error_and_logs_warning(self) -> None:
        obsidian = Mock(spec=ObsidianClient)
        obsidian.put_note.side_effect = ObsidianError("ssl failed")
        logger = Mock()

        try_sync_note(
            obsidian,
            "Sessions/Transcripts/2026-03-18/test.md",
            "# test",
            logger,
            "原文笔记",
        )

        logger.warning.assert_called_once()
        self.assertEqual(
            "%s 同步失败，将保留在本地 journal 中: %s",
            logger.warning.call_args.args[0],
        )
        self.assertEqual("原文笔记", logger.warning.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
