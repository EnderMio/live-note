from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from live_note.domain import SessionMetadata
from live_note.runtime.remote_session_projections import (
    list_remote_session_projections,
    mark_remote_session_projection_synced,
    upsert_remote_session_projection,
)
from live_note.runtime.remote_projection_target import reconcile_remote_projection_target
from live_note.runtime.remote_task_projections import (
    list_remote_task_projections,
    mark_remote_task_projection_synced,
    upsert_remote_task_projection_from_payload,
)


def _metadata(session_dir: str, *, session_id: str = "session-1") -> SessionMetadata:
    return SessionMetadata(
        session_id=session_id,
        title="远端课程",
        kind="lecture",
        input_mode="file",
        source_label="demo.mp3",
        source_ref="/tmp/demo.mp3",
        language="zh",
        started_at="2026-04-12T00:00:00+00:00",
        transcript_note_path="Sessions/Transcripts/demo.md",
        structured_note_path="Sessions/Summaries/demo.md",
        session_dir=session_dir,
        status="finalized",
        transcript_source="live",
        refine_status="done",
        execution_target="remote",
        remote_session_id=session_id,
        speaker_status="enabled",
    )


class RemoteProjectionSemanticsTests(unittest.TestCase):
    def test_reconcile_remote_projection_target_clears_legacy_rows_without_target_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_dir = root / ".live-note" / "sessions" / "session-1"
            remote_metadata = _metadata(str(session_dir))
            local_metadata = _metadata(str(session_dir))
            upsert_remote_task_projection_from_payload(
                root,
                {
                    "task_id": "remote-task-1",
                    "action": "refine",
                    "label": "离线精修并重写",
                    "status": "running",
                    "stage": "running",
                    "updated_at": "2026-04-12T10:00:00+00:00",
                },
            )
            upsert_remote_session_projection(
                root,
                remote_metadata=remote_metadata,
                local_metadata=local_metadata,
                runtime_status="running",
                remote_updated_at="2026-04-12T12:00:00+00:00",
            )

            cleared = reconcile_remote_projection_target(root, "http://172.21.0.64:8765")
            task_records = list_remote_task_projections(root)
            session_records = list_remote_session_projections(root)

        self.assertTrue(cleared)
        self.assertEqual([], task_records)
        self.assertEqual([], session_records)

    def test_reconcile_remote_projection_target_clears_rows_when_endpoint_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            reconcile_remote_projection_target(root, "http://172.21.0.159:8765")
            upsert_remote_task_projection_from_payload(
                root,
                {
                    "task_id": "remote-task-1",
                    "action": "refine",
                    "label": "离线精修并重写",
                    "status": "completed",
                    "stage": "done",
                    "updated_at": "2026-04-12T10:00:00+00:00",
                },
            )

            unchanged = reconcile_remote_projection_target(root, "http://172.21.0.159:8765")
            cleared = reconcile_remote_projection_target(root, "http://172.21.0.64:8765")
            task_records = list_remote_task_projections(root)

        self.assertFalse(unchanged)
        self.assertTrue(cleared)
        self.assertEqual([], task_records)

    def test_remote_task_projection_preserves_remote_updated_at_when_marking_synced(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            record = upsert_remote_task_projection_from_payload(
                root,
                {
                    "task_id": "remote-task-1",
                    "request_id": "req-1",
                    "action": "refine",
                    "label": "离线精修并重写",
                    "status": "running",
                    "stage": "running",
                    "message": "处理中",
                    "updated_at": "2026-04-12T10:00:00+00:00",
                    "result_version": 2,
                },
            )
            synced = mark_remote_task_projection_synced(
                root,
                remote_task_id="remote-task-1",
                result_version=3,
            )

        self.assertEqual("2026-04-12T10:00:00+00:00", record.updated_at)
        self.assertIsNotNone(synced)
        self.assertEqual("2026-04-12T10:00:00+00:00", synced.updated_at)
        self.assertEqual(3, synced.result_version)
        self.assertEqual(3, synced.last_synced_result_version)
        self.assertIsNotNone(synced.artifacts_synced_at)

    def test_remote_session_projection_preserves_remote_updated_at_when_marking_synced(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_dir = root / ".live-note" / "sessions" / "session-1"
            remote_metadata = _metadata(str(session_dir))
            local_metadata = _metadata(str(session_dir))
            record = upsert_remote_session_projection(
                root,
                remote_metadata=remote_metadata,
                local_metadata=local_metadata,
                runtime_status="completed",
                remote_updated_at="2026-04-12T12:00:00+00:00",
            )
            synced = mark_remote_session_projection_synced(root, "session-1")

        self.assertEqual("2026-04-12T12:00:00+00:00", record.remote_updated_at)
        self.assertIsNotNone(synced)
        self.assertEqual("2026-04-12T12:00:00+00:00", synced.remote_updated_at)
        self.assertIsNotNone(synced.artifacts_synced_at)
