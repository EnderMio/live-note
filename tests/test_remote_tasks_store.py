from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from live_note.app.remote_tasks import (
    RemoteTaskAttachment,
    load_remote_tasks,
    mark_remote_task_synced,
    save_remote_tasks,
    upsert_pending_remote_task,
    upsert_remote_task_payload,
)


class RemoteTaskStoreTests(unittest.TestCase):
    def test_load_remote_tasks_ignores_broken_records(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "remote_tasks.json"
            path.write_text(
                '{"records":[{"remote_task_id":"task-1","action":"import"},{"not":"valid"}]}',
                encoding="utf-8",
            )

            loaded = load_remote_tasks(path)

        self.assertEqual([], loaded.records)
        self.assertEqual(2, len(loaded.warnings))

    def test_upsert_pending_remote_task_creates_awaiting_rebind_record(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "remote_tasks.json"

            record = upsert_pending_remote_task(
                path,
                action="import",
                label="文件导入",
                request_id="req-import-1",
                session_id=None,
            )
            loaded = load_remote_tasks(path)

        self.assertEqual("awaiting_rebind", record.attachment_state)
        self.assertEqual("req-import-1", loaded.records[0].request_id)
        self.assertIsNone(loaded.records[0].remote_task_id)

    def test_upsert_remote_task_payload_rebinds_existing_request_id_record(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "remote_tasks.json"
            upsert_pending_remote_task(
                path,
                action="import",
                label="文件导入",
                request_id="req-import-1",
            )

            record = upsert_remote_task_payload(
                path,
                {
                    "task_id": "task-import-1",
                    "server_id": "server-1",
                    "action": "import",
                    "label": "文件导入",
                    "status": "running",
                    "stage": "transcribing",
                    "message": "正在转写片段 1/2",
                    "request_id": "req-import-1",
                    "result_version": 1,
                    "can_cancel": True,
                },
            )

        self.assertEqual("task-import-1", record.remote_task_id)
        self.assertEqual("attached", record.attachment_state)
        self.assertEqual("running", record.last_known_status)
        self.assertEqual(1, record.result_version)

    def test_mark_remote_task_synced_updates_result_version_and_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "remote_tasks.json"
            upsert_remote_task_payload(
                path,
                {
                    "task_id": "task-import-1",
                    "server_id": "server-1",
                    "action": "import",
                    "label": "文件导入",
                    "status": "completed",
                    "stage": "done",
                    "message": "完成",
                    "result_version": 2,
                },
            )

            mark_remote_task_synced(path, remote_task_id="task-import-1", result_version=2)
            loaded = load_remote_tasks(path)

        self.assertEqual(2, loaded.records[0].last_synced_result_version)
        self.assertIsNotNone(loaded.records[0].artifacts_synced_at)

    def test_save_remote_tasks_handles_concurrent_writes_without_shared_tmp_race(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "remote_tasks.json"
            barrier = threading.Barrier(2)
            errors: list[BaseException] = []
            original_write_text = Path.write_text

            def delayed_write_text(target: Path, data: str, *args, **kwargs):
                result = original_write_text(target, data, *args, **kwargs)
                if target.parent == path.parent and target.name.endswith(".tmp"):
                    barrier.wait(timeout=2)
                return result

            def worker(record: RemoteTaskAttachment) -> None:
                try:
                    save_remote_tasks(path, [record])
                except BaseException as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

            first = _attachment("task-1")
            second = _attachment("task-2")

            with patch("pathlib.Path.write_text", new=delayed_write_text):
                first_thread = threading.Thread(target=worker, args=(first,))
                second_thread = threading.Thread(target=worker, args=(second,))
                first_thread.start()
                second_thread.start()
                first_thread.join(timeout=2)
                second_thread.join(timeout=2)

            self.assertFalse(first_thread.is_alive())
            self.assertFalse(second_thread.is_alive())
            self.assertEqual([], errors)
            loaded = load_remote_tasks(path)

        self.assertEqual(1, len(loaded.records))
        self.assertIn(loaded.records[0].remote_task_id, {"task-1", "task-2"})


def _attachment(task_id: str) -> RemoteTaskAttachment:
    return RemoteTaskAttachment(
        remote_task_id=task_id,
        server_id="server-1",
        action="import",
        label="文件导入",
        session_id=None,
        request_id=None,
        last_known_status="running",
        last_known_stage="running",
        last_message="处理中",
        attachment_state="attached",
        last_synced_result_version=0,
        result_version=0,
        updated_at="2026-03-23T00:00:00+00:00",
        created_at="2026-03-23T00:00:00+00:00",
    )
