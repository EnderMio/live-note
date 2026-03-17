from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from live_note.app.task_queue import QueueLoadResult, build_task_record
from live_note.app.task_queue_runtime import TaskQueueRuntime


class TaskQueueRuntimeTests(unittest.TestCase):
    def test_load_keeps_active_records_and_persists_after_interrupted_recovery(self) -> None:
        queued = build_task_record(
            task_id="task-0007",
            action="session_action",
            label="重转写并重写",
            payload={"action": "retranscribe", "session_id": "session-1"},
            created_at="2026-03-16T10:00:00+00:00",
        )
        interrupted = build_task_record(
            task_id="task-0003",
            action="import",
            label="导入文件",
            payload={"file_path": "~/demo.mp3", "kind": "generic"},
            created_at="2026-03-16T10:01:00+00:00",
            status="running",
            started_at="2026-03-16T10:02:00+00:00",
        )
        store = SimpleNamespace(
            load=Mock(
                return_value=QueueLoadResult(
                    active=[queued],
                    interrupted=[interrupted],
                    warnings=["队列文件损坏，已忽略。"],
                )
            ),
            save=Mock(),
        )
        runtime = TaskQueueRuntime(store)

        loaded = runtime.load()

        self.assertEqual([queued], runtime.records)
        self.assertEqual([queued], loaded.active_records)
        self.assertEqual([interrupted], loaded.interrupted_records)
        store.save.assert_called_once_with([queued])
        self.assertEqual("task-0008", runtime.next_task_id())

    def test_enqueue_rejects_duplicate_fingerprint(self) -> None:
        existing = build_task_record(
            task_id="task-0001",
            action="merge",
            label="合并会话",
            payload={"session_ids": ["a", "b"]},
            created_at="2026-03-16T10:00:00+00:00",
        )
        store = SimpleNamespace(load=Mock(), save=Mock())
        runtime = TaskQueueRuntime(store, initial_records=[existing])

        duplicate = runtime.enqueue(
            label="合并会话",
            action="merge",
            payload={"session_ids": ["b", "a"]},
            created_at="2026-03-16T10:01:00+00:00",
        )

        self.assertIsNone(duplicate)
        self.assertEqual([existing], runtime.records)
        store.save.assert_not_called()

    def test_enqueue_appends_new_record_and_persists_queue(self) -> None:
        store = SimpleNamespace(load=Mock(), save=Mock())
        runtime = TaskQueueRuntime(store)

        record = runtime.enqueue(
            label="重新生成整理",
            action="session_action",
            payload={"action": "republish", "session_id": "session-1"},
            created_at="2026-03-16T10:01:00+00:00",
        )

        self.assertIsNotNone(record)
        self.assertEqual([record], runtime.records)
        store.save.assert_called_once_with([record])
        self.assertEqual("task-0002", runtime.next_task_id())

    def test_mark_running_updates_selected_record(self) -> None:
        queued = build_task_record(
            task_id="task-0001",
            action="session_action",
            label="重转写并重写",
            payload={"action": "retranscribe", "session_id": "session-1"},
            created_at="2026-03-16T10:00:00+00:00",
        )
        store = SimpleNamespace(load=Mock(), save=Mock())
        runtime = TaskQueueRuntime(store, initial_records=[queued])

        running = runtime.mark_running("task-0001", started_at="2026-03-16T10:02:00+00:00")

        self.assertEqual("running", running.status)
        self.assertEqual("2026-03-16T10:02:00+00:00", running.started_at)
        self.assertEqual([running], runtime.records)
        store.save.assert_called_once_with([running])

    def test_cancel_selected_only_removes_queued_records(self) -> None:
        queued = build_task_record(
            task_id="task-0001",
            action="session_action",
            label="重转写并重写",
            payload={"action": "retranscribe", "session_id": "session-1"},
            created_at="2026-03-16T10:00:00+00:00",
        )
        running = build_task_record(
            task_id="task-0002",
            action="session_action",
            label="离线精修并重写",
            payload={"action": "refine", "session_id": "session-2"},
            created_at="2026-03-16T10:01:00+00:00",
            status="running",
            started_at="2026-03-16T10:02:00+00:00",
        )
        store = SimpleNamespace(load=Mock(), save=Mock())
        runtime = TaskQueueRuntime(store, initial_records=[queued, running])

        cancelled = runtime.cancel({"task-0001", "task-0002"})

        self.assertEqual(1, cancelled)
        self.assertEqual([running], runtime.records)
        store.save.assert_called_once_with([running])

    def test_next_queued_returns_first_waiting_record(self) -> None:
        running = build_task_record(
            task_id="task-0001",
            action="session_action",
            label="离线精修并重写",
            payload={"action": "refine", "session_id": "session-2"},
            created_at="2026-03-16T10:01:00+00:00",
            status="running",
            started_at="2026-03-16T10:02:00+00:00",
        )
        queued = build_task_record(
            task_id="task-0002",
            action="session_action",
            label="重新生成整理",
            payload={"action": "republish", "session_id": "session-3"},
            created_at="2026-03-16T10:03:00+00:00",
        )
        runtime = TaskQueueRuntime(
            SimpleNamespace(load=Mock(), save=Mock()),
            initial_records=[running, queued],
        )

        self.assertEqual(queued, runtime.next_queued())
        self.assertEqual(1, runtime.queued_count())


if __name__ == "__main__":
    unittest.main()
