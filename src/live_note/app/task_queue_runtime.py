from __future__ import annotations

import threading
from dataclasses import replace
from typing import Any

from .task_queue import (
    QueuedTaskRecord,
    QueueLoadResult,
    TaskQueueStore,
    build_task_record,
    task_fingerprint,
)


class TaskQueueRuntime:
    def __init__(
        self,
        store: TaskQueueStore,
        *,
        initial_records: list[QueuedTaskRecord] | None = None,
    ) -> None:
        self.store = store
        self._lock = threading.Lock()
        self._records = list(initial_records or [])
        self._task_sequence = 0
        self._sync_task_sequence(self._records)

    @property
    def records(self) -> list[QueuedTaskRecord]:
        with self._lock:
            return list(self._records)

    def load(self) -> QueueLoadResult:
        loaded = self.store.load()
        with self._lock:
            self._records = list(loaded.active_records)
            self._sync_task_sequence(self._records)
            if loaded.interrupted_records:
                self.store.save(list(self._records))
        return loaded

    def next_task_id(self) -> str:
        with self._lock:
            self._task_sequence += 1
            return f"task-{self._task_sequence:04d}"

    def enqueue(
        self,
        *,
        label: str,
        action: str,
        payload: dict[str, Any],
        created_at: str,
    ) -> QueuedTaskRecord | None:
        fingerprint = task_fingerprint(action, payload)
        with self._lock:
            if any(item.fingerprint == fingerprint for item in self._records):
                return None
            self._task_sequence += 1
            record = build_task_record(
                task_id=f"task-{self._task_sequence:04d}",
                action=action,
                label=label,
                payload=payload,
                created_at=created_at,
            )
            self._records.append(record)
            self.store.save(list(self._records))
            return record

    def next_queued(self) -> QueuedTaskRecord | None:
        with self._lock:
            return next((record for record in self._records if record.status == "queued"), None)

    def mark_running(self, task_id: str, *, started_at: str) -> QueuedTaskRecord:
        with self._lock:
            updated_record: QueuedTaskRecord | None = None
            updated_records: list[QueuedTaskRecord] = []
            for record in self._records:
                if record.task_id == task_id:
                    updated_record = replace(record, status="running", started_at=started_at)
                    updated_records.append(updated_record)
                else:
                    updated_records.append(record)
            if updated_record is None:
                raise KeyError(task_id)
            self._records = updated_records
            self.store.save(list(self._records))
            return updated_record

    def remove(self, task_id: str) -> bool:
        with self._lock:
            remaining = [record for record in self._records if record.task_id != task_id]
            if len(remaining) == len(self._records):
                return False
            self._records = remaining
            self.store.save(list(self._records))
            return True

    def cancel(self, task_ids: set[str]) -> int:
        with self._lock:
            remaining = [
                record
                for record in self._records
                if record.task_id not in task_ids or record.status != "queued"
            ]
            cancelled = len(self._records) - len(remaining)
            if not cancelled:
                return 0
            self._records = remaining
            self.store.save(list(self._records))
            return cancelled

    def queued_count(self) -> int:
        with self._lock:
            return sum(1 for record in self._records if record.status == "queued")

    def get(self, task_id: str) -> QueuedTaskRecord | None:
        with self._lock:
            return next((record for record in self._records if record.task_id == task_id), None)

    def has_queued(self) -> bool:
        return self.queued_count() > 0

    def _sync_task_sequence(self, records: list[QueuedTaskRecord]) -> None:
        max_seen = 0
        for record in records:
            prefix, _, suffix = record.task_id.partition("-")
            if prefix != "task" or not suffix.isdigit():
                continue
            max_seen = max(max_seen, int(suffix))
        self._task_sequence = max_seen
