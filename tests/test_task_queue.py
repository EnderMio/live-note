from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from live_note.app.task_queue import QueuedTaskRecord, TaskQueueStore, build_task_record


class TaskQueueStoreTests(unittest.TestCase):
    def test_save_and_load_preserves_fifo_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TaskQueueStore(Path(temp_dir) / "task_queue.json")
            first = build_task_record(
                task_id="task-0001",
                action="session_action",
                label="重转写并重写",
                payload={"action": "retranscribe", "session_id": "session-1"},
                created_at="2026-03-16T10:00:00+00:00",
            )
            second = build_task_record(
                task_id="task-0002",
                action="import",
                label="导入文件",
                payload={"file_path": "~/demo.mp3", "kind": "generic"},
                created_at="2026-03-16T10:01:00+00:00",
            )

            store.save([first, second])
            loaded = store.load()

        self.assertEqual([], loaded.interrupted_records)
        self.assertEqual([], loaded.warnings)
        self.assertEqual(
            ["task-0001", "task-0002"],
            [record.task_id for record in loaded.active_records],
        )

    def test_load_moves_running_records_to_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TaskQueueStore(Path(temp_dir) / "task_queue.json")
            queued = build_task_record(
                task_id="task-0001",
                action="session_action",
                label="重新生成整理",
                payload={"action": "republish", "session_id": "session-1"},
                created_at="2026-03-16T10:00:00+00:00",
            )
            running = QueuedTaskRecord(
                task_id="task-0002",
                action="session_action",
                label="离线精修并重写",
                payload={"action": "refine", "session_id": "session-2"},
                fingerprint="session-action-refine",
                status="running",
                created_at="2026-03-16T10:01:00+00:00",
                started_at="2026-03-16T10:02:00+00:00",
            )
            store.save([queued, running])

            loaded = store.load()

        self.assertEqual(["task-0001"], [record.task_id for record in loaded.active_records])
        self.assertEqual(["task-0002"], [record.task_id for record in loaded.interrupted_records])

    def test_load_returns_warning_for_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "task_queue.json"
            path.write_text("{bad json}", encoding="utf-8")
            store = TaskQueueStore(path)

            loaded = store.load()

        self.assertEqual([], loaded.active_records)
        self.assertEqual([], loaded.interrupted_records)
        self.assertEqual(1, len(loaded.warnings))

    def test_load_returns_empty_result_when_file_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TaskQueueStore(Path(temp_dir) / "missing-task-queue.json")

            loaded = store.load()

        self.assertEqual([], loaded.active_records)
        self.assertEqual([], loaded.interrupted_records)
        self.assertEqual([], loaded.warnings)

    def test_build_task_record_normalizes_payload_for_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            a = build_task_record(
                task_id="task-0001",
                action="merge",
                label="合并会话",
                payload={"session_ids": ["b", "a", "a"], "title": "课程合并"},
                created_at="2026-03-16T10:00:00+00:00",
            )
            b = build_task_record(
                task_id="task-0002",
                action="merge",
                label="合并会话",
                payload={"session_ids": ["a", "b"], "title": "课程合并"},
                created_at="2026-03-16T10:01:00+00:00",
            )
            imported = build_task_record(
                task_id="task-0003",
                action="import",
                label="导入文件",
                payload={"file_path": str(root / ".." / root.name / "demo.mp3"), "kind": "generic"},
                created_at="2026-03-16T10:02:00+00:00",
            )

        self.assertEqual(a.fingerprint, b.fingerprint)
        self.assertEqual(["a", "b"], a.payload["session_ids"])
        self.assertEqual(str((root / "demo.mp3").resolve()), imported.payload["file_path"])

    def test_build_task_record_normalizes_session_action_payload(self) -> None:
        record = build_task_record(
            task_id="task-0001",
            action="session_action",
            label="重跑会话动作",
            payload={"action": " refine ", "session_id": " session-1 "},
            created_at="2026-03-16T10:00:00+00:00",
        )

        self.assertEqual("refine", record.payload["action"])
        self.assertEqual("session-1", record.payload["session_id"])

    def test_save_writes_json_serializable_records(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "task_queue.json"
            store = TaskQueueStore(path)
            record = build_task_record(
                task_id="task-0001",
                action="session_action",
                label="重新同步 Obsidian",
                payload={"action": "resync", "session_id": "session-1"},
                created_at="2026-03-16T10:00:00+00:00",
            )

            store.save([record])
            content = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual("task-0001", content["records"][0]["task_id"])


if __name__ == "__main__":
    unittest.main()
