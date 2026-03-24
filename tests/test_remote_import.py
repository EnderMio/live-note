from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from live_note.app.remote_import import RemoteImportCoordinator
from live_note.app.remote_tasks import load_remote_tasks
from live_note.app.task_errors import TaskCancelledError
from live_note.config import (
    AppConfig,
    AudioConfig,
    ImportConfig,
    LlmConfig,
    ObsidianConfig,
    RefineConfig,
    RemoteConfig,
    WhisperConfig,
)
from live_note.domain import SessionMetadata
from live_note.remote.client import RemoteClientError


def build_config(root: Path) -> AppConfig:
    model_path = root / "ggml-large-v3.bin"
    model_path.write_bytes(b"fake-model")
    return AppConfig(
        audio=AudioConfig(),
        importer=ImportConfig(),
        refine=RefineConfig(),
        whisper=WhisperConfig(
            binary="/Users/ender/whisper.cpp/build/bin/whisper-server",
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
        remote=RemoteConfig(
            enabled=True,
            base_url="http://mini.local:8765",
            api_token="remote-token",
            timeout_seconds=12,
        ),
        root_dir=root,
    )


class _FakeRemoteClient:
    def __init__(self) -> None:
        self.created: list[dict[str, object]] = []
        self.cancelled: list[str] = []
        self.artifact_calls: list[str] = []
        self.import_states = [
            {
                "task_id": "import-1",
                "status": "queued",
                "stage": "queued",
                "message": "已接收上传。",
            },
            {
                "task_id": "import-1",
                "session_id": "remote-import-1",
                "status": "running",
                "stage": "transcribing",
                "message": "正在转写片段 1/2",
                "current": 1,
                "total": 2,
            },
            {
                "task_id": "import-1",
                "session_id": "remote-import-1",
                "status": "completed",
                "stage": "done",
                "message": "远端导入已完成。",
                "current": 2,
                "total": 2,
            },
        ]
        self.health_payload = {"status": "ok", "supports_imports": True}

    def health(self) -> dict[str, object]:
        return dict(self.health_payload)

    def create_import_task(
        self,
        file_path: str,
        *,
        title: str | None,
        kind: str,
        language: str | None,
        speaker_enabled: bool | None = None,
        request_id: str | None = None,
    ) -> dict[str, object]:
        self.created.append(
            {
                "file_path": file_path,
                "title": title,
                "kind": kind,
                "language": language,
                "speaker_enabled": speaker_enabled,
                "request_id": request_id,
            }
        )
        return dict(self.import_states[0])

    def get_import_task(self, task_id: str) -> dict[str, object]:
        return dict(self.import_states.pop(0 if len(self.import_states) == 1 else 1))

    def get_task(self, task_id: str) -> dict[str, object]:
        return self.get_import_task(task_id)

    def get_artifacts(self, session_id: str) -> dict[str, object]:
        self.artifact_calls.append(session_id)
        return {
            "metadata": {
                "session_id": session_id,
                "title": "股票课",
                "kind": "lecture",
                "input_mode": "file",
                "source_label": "第1课.mp3",
                "source_ref": "remote-upload://第1课.mp3",
                "language": "zh",
                "started_at": "2026-03-19T08:00:00+00:00",
                "status": "completed",
                "transcript_source": "imported",
                "refine_status": "disabled",
                "execution_target": "remote",
                "remote_session_id": session_id,
                "speaker_status": "disabled",
            },
            "transcript_content": "# 远端原文\n",
            "structured_content": "# 远端整理\n",
            "entries": [
                {
                    "segment_id": "seg-00001",
                    "started_ms": 0,
                    "ended_ms": 1500,
                    "text": "今天先看市场结构。",
                    "speaker_label": None,
                }
            ],
            "has_session_audio": False,
        }

    def cancel_import_task(self, task_id: str) -> dict[str, object]:
        self.cancelled.append(task_id)
        return {
            "task_id": task_id,
            "status": "cancelled",
            "stage": "cancelled",
            "message": "远端导入已取消。",
        }

    def cancel_task(self, task_id: str) -> dict[str, object]:
        return self.cancel_import_task(task_id)


class RemoteImportCoordinatorTests(unittest.TestCase):
    def test_run_rejects_old_remote_service_before_upload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = build_config(root)
            media_path = root / "第1课.mp3"
            media_path.write_bytes(b"fake-audio")
            client = _FakeRemoteClient()
            client.health_payload = {"status": "ok"}

            coordinator = RemoteImportCoordinator(
                config=config,
                file_path=str(media_path),
                title="股票课",
                kind="lecture",
                language="zh",
                client=client,
                poll_interval_seconds=0.0,
            )

            with self.assertRaisesRegex(RemoteClientError, "远端服务版本过旧"):
                coordinator.run()

        self.assertEqual([], client.created)

    def test_run_uploads_remote_file_polls_status_and_syncs_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = build_config(root)
            media_path = root / "第1课.mp3"
            media_path.write_bytes(b"fake-audio")
            client = _FakeRemoteClient()
            progress_events: list[tuple[str, str]] = []

            coordinator = RemoteImportCoordinator(
                config=config,
                file_path=str(media_path),
                title="股票课",
                kind="lecture",
                language="zh",
                on_progress=lambda event: progress_events.append((event.stage, event.message)),
                client=client,
                poll_interval_seconds=0.0,
            )

            with patch(
                "live_note.app.remote_import.apply_remote_artifacts",
                return_value=SessionMetadata(
                    session_id="remote-import-1",
                    title="股票课",
                    kind="lecture",
                    input_mode="file",
                    source_label="第1课.mp3",
                    source_ref="remote-upload://第1课.mp3",
                    language="zh",
                    started_at="2026-03-19T08:00:00+00:00",
                    transcript_note_path="Sessions/Transcripts/2026-03-19/股票课.md",
                    structured_note_path="Sessions/Summaries/2026-03-19/股票课.md",
                    session_dir=str(root / ".live-note" / "sessions" / "remote-import-1"),
                    status="completed",
                    execution_target="remote",
                    remote_session_id="remote-import-1",
                ),
            ) as apply_mock:
                exit_code = coordinator.run()

        self.assertEqual(0, exit_code)
        self.assertEqual(
            [
                {
                    "file_path": str(media_path.resolve()),
                    "title": "股票课",
                    "kind": "lecture",
                    "language": "zh",
                    "speaker_enabled": False,
                    "request_id": client.created[0]["request_id"],
                }
            ],
            client.created,
        )
        self.assertEqual(["remote-import-1", "remote-import-1"], client.artifact_calls)
        apply_mock.assert_called_once()
        self.assertEqual("# 远端原文\n", apply_mock.call_args.kwargs["transcript_content"])
        self.assertEqual("# 远端整理\n", apply_mock.call_args.kwargs["structured_content"])
        self.assertTrue(any(stage == "uploading" for stage, _ in progress_events))
        self.assertTrue(any(stage == "transcribing" for stage, _ in progress_events))

    def test_run_persists_remote_task_attachment_record(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = build_config(root)
            media_path = root / "第1课.mp3"
            media_path.write_bytes(b"fake-audio")
            client = _FakeRemoteClient()

            coordinator = RemoteImportCoordinator(
                config=config,
                file_path=str(media_path),
                title="股票课",
                kind="lecture",
                language="zh",
                client=client,
                poll_interval_seconds=0.0,
            )

            with patch(
                "live_note.app.remote_import.apply_remote_artifacts",
                return_value=SessionMetadata(
                    session_id="remote-import-1",
                    title="股票课",
                    kind="lecture",
                    input_mode="file",
                    source_label="第1课.mp3",
                    source_ref="remote-upload://第1课.mp3",
                    language="zh",
                    started_at="2026-03-19T08:00:00+00:00",
                    transcript_note_path="Sessions/Transcripts/2026-03-19/股票课.md",
                    structured_note_path="Sessions/Summaries/2026-03-19/股票课.md",
                    session_dir=str(root / ".live-note" / "sessions" / "remote-import-1"),
                    status="completed",
                    execution_target="remote",
                    remote_session_id="remote-import-1",
                ),
            ):
                coordinator.run()

            loaded = load_remote_tasks(root / ".live-note" / "remote_tasks.json")

        self.assertEqual(1, len(loaded.records))
        self.assertEqual("import", loaded.records[0].action)
        self.assertEqual("import-1", loaded.records[0].remote_task_id)

    def test_run_syncs_remote_transcript_snapshot_before_final_apply(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = build_config(root)
            media_path = root / "第1课.mp3"
            media_path.write_bytes(b"fake-audio")
            client = _FakeRemoteClient()

            coordinator = RemoteImportCoordinator(
                config=config,
                file_path=str(media_path),
                title="股票课",
                kind="lecture",
                language="zh",
                client=client,
                poll_interval_seconds=0.0,
            )

            with (
                patch(
                    "live_note.app.remote_import.sync_remote_transcript_snapshot"
                ) as snapshot_mock,
                patch(
                    "live_note.app.remote_import.apply_remote_artifacts",
                    return_value=SessionMetadata(
                        session_id="remote-import-1",
                        title="股票课",
                        kind="lecture",
                        input_mode="file",
                        source_label="第1课.mp3",
                        source_ref="remote-upload://第1课.mp3",
                        language="zh",
                        started_at="2026-03-19T08:00:00+00:00",
                        transcript_note_path="Sessions/Transcripts/2026-03-19/股票课.md",
                        structured_note_path="Sessions/Summaries/2026-03-19/股票课.md",
                        session_dir=str(root / ".live-note" / "sessions" / "remote-import-1"),
                        status="completed",
                        execution_target="remote",
                        remote_session_id="remote-import-1",
                    ),
                ),
            ):
                coordinator.run()

        snapshot_mock.assert_called_once()
        self.assertEqual("remote-import-1", snapshot_mock.call_args.args[1].session_id)

    def test_run_cancels_remote_import_when_cancel_requested(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = build_config(root)
            media_path = root / "第1课.mp3"
            media_path.write_bytes(b"fake-audio")
            client = _FakeRemoteClient()
            cancel_event = threading.Event()
            original_get_import_task = client.get_import_task

            def cancel_during_poll(task_id: str) -> dict[str, object]:
                cancel_event.set()
                return original_get_import_task(task_id)

            client.get_import_task = cancel_during_poll  # type: ignore[method-assign]

            coordinator = RemoteImportCoordinator(
                config=config,
                file_path=str(media_path),
                title="股票课",
                kind="lecture",
                language="zh",
                client=client,
                poll_interval_seconds=0.0,
                cancel_event=cancel_event,
            )

            with self.assertRaisesRegex(TaskCancelledError, "取消"):
                coordinator.run()

        self.assertEqual(["import-1"], client.cancelled)
