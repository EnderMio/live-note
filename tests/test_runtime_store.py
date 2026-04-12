from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from live_note.runtime.types import ProgressEvent
from live_note.task_errors import TaskCancelledError
from live_note.domain import AudioFrame, SessionMetadata
from live_note.runtime import append_audio_frame
from live_note.runtime.domain.commands import CommandRecord
from live_note.runtime.domain.events import EventRecord
from live_note.runtime.domain.session_state import SessionRecord, SessionStatus
from live_note.runtime.domain.task_state import TaskRecord, TaskStatus
from live_note.runtime.read_model import (
    get_session,
    get_task,
    list_active_tasks,
    list_session_history,
)
from live_note.runtime.store import ControlDb, LogRepo, SessionRepo, TaskRepo, control_db_path
from live_note.runtime.supervisors import RuntimeHost, SessionSupervisor, TaskSupervisor


class ControlDbTests(unittest.TestCase):
    def test_control_db_initialize_creates_schema_and_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            db = ControlDb.for_root(root)

            self.assertEqual(control_db_path(root), db.path)
            self.assertTrue(db.path.exists())
            with db.connect() as connection:
                table_names = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }

        self.assertTrue({"schema_meta", "sessions", "tasks", "commands", "events"} <= table_names)

    def test_session_repo_round_trips_session_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = ControlDb.for_root(root)
            repo = SessionRepo(db)
            record = SessionRecord.from_metadata(_sample_metadata(root))

            persisted = repo.upsert(record)
            fetched = repo.get(record.session_id)
            queried = get_session(db, record.session_id)
            history = list_session_history(db)

        self.assertEqual(record.session_id, persisted.session_id)
        self.assertEqual(record.title, fetched.title)
        self.assertEqual(record.session_id, queried.session_id)
        self.assertEqual(record.to_metadata(), fetched.to_metadata())
        self.assertEqual([record.session_id], [item.session_id for item in history])

    def test_task_repo_round_trips_payload_and_resource_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = ControlDb.for_root(root)
            repo = TaskRepo(db)
            queued = TaskRecord(
                task_id="task-1",
                session_id="session-1",
                action="refine",
                label="离线精修并重写",
                status=TaskStatus.QUEUED.value,
                stage="queued",
                created_at="2026-04-09T00:00:00+00:00",
                updated_at="2026-04-09T00:00:00+00:00",
                request_id="req-1",
                dedupe_key="refine:session-1",
                resource_keys=("session:session-1", "session:session-1", "  "),
                payload={"session_id": "session-1", "speaker_enabled": True},
                can_cancel=True,
            )
            running = replace(
                queued,
                task_id="task-2",
                status=TaskStatus.RUNNING.value,
                stage="transcribing",
                request_id="req-2",
                dedupe_key="retranscribe:session-1",
                resource_keys=("session:session-1", "audio:session-1"),
                updated_at="2026-04-09T00:05:00+00:00",
                started_at="2026-04-09T00:01:00+00:00",
                attempt=1,
            )
            repo.upsert(queued)
            repo.upsert(running)

            fetched = get_task(db, "task-2")
            by_request = repo.find_by_request_id("req-1")
            active = list_active_tasks(db)

        self.assertEqual(("audio:session-1", "session:session-1"), fetched.resource_keys)
        self.assertEqual({"session_id": "session-1", "speaker_enabled": True}, fetched.payload)
        self.assertTrue(fetched.can_cancel)
        self.assertEqual("task-1", by_request.task_id)
        self.assertEqual(["task-2", "task-1"], [item.task_id for item in active])

    def test_log_repo_appends_and_filters_commands_and_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = ControlDb.for_root(root)
            repo = LogRepo(db)

            command = repo.append_command(
                CommandRecord(
                    command_id="cmd-1",
                    kind="start_live",
                    session_id="session-1",
                    created_at="2026-04-09T00:00:00+00:00",
                    payload={"title": "产品周会"},
                )
            )
            repo.append_command(
                CommandRecord(
                    command_id="cmd-2",
                    kind="stop_live",
                    session_id="session-2",
                    created_at="2026-04-09T00:01:00+00:00",
                    payload={},
                )
            )
            event = repo.append_event(
                EventRecord(
                    event_id="evt-1",
                    kind="handoff_committed",
                    session_id="session-1",
                    task_id="task-1",
                    created_at="2026-04-09T00:00:10+00:00",
                    payload={"task_id": "task-1"},
                )
            )

            commands = repo.list_commands(session_id="session-1")
            events = repo.list_events(task_id="task-1")

        self.assertEqual(1, command.sequence)
        self.assertEqual(1, event.sequence)
        self.assertEqual(["cmd-1"], [item.command_id for item in commands])
        self.assertEqual(["evt-1"], [item.event_id for item in events])
        self.assertEqual({"title": "产品周会"}, commands[0].payload)
        self.assertEqual({"task_id": "task-1"}, events[0].payload)

    def test_request_id_unique_constraint_rejects_conflicting_task_insert(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = ControlDb.for_root(root)
            repo = TaskRepo(db)
            repo.upsert(
                TaskRecord(
                    task_id="task-1",
                    action="import",
                    label="文件导入",
                    status=TaskStatus.QUEUED.value,
                    stage="queued",
                    created_at="2026-04-09T00:00:00+00:00",
                    updated_at="2026-04-09T00:00:00+00:00",
                    request_id="req-import-1",
                )
            )

            with self.assertRaises(sqlite3.IntegrityError):
                repo.upsert(
                    TaskRecord(
                        task_id="task-2",
                        action="import",
                        label="文件导入",
                        status=TaskStatus.QUEUED.value,
                        stage="queued",
                        created_at="2026-04-09T00:01:00+00:00",
                        updated_at="2026-04-09T00:01:00+00:00",
                        request_id="req-import-1",
                    )
                )

    def test_runtime_host_requeues_recoverable_tasks_and_interrupts_the_rest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = ControlDb.for_root(root)
            repo = TaskRepo(db)
            running_refine = repo.upsert(
                TaskRecord(
                    task_id="task-1",
                    session_id="session-1",
                    action="refine",
                    label="离线精修",
                    status=TaskStatus.RUNNING.value,
                    stage="refining",
                    created_at="2026-04-09T00:00:00+00:00",
                    updated_at="2026-04-09T00:00:05+00:00",
                    started_at="2026-04-09T00:00:01+00:00",
                    attempt=1,
                )
            )
            running_merge = repo.upsert(
                TaskRecord(
                    task_id="task-2",
                    session_id="session-2",
                    action="merge",
                    label="合并会话",
                    status=TaskStatus.RUNNING.value,
                    stage="merging",
                    created_at="2026-04-09T00:01:00+00:00",
                    updated_at="2026-04-09T00:01:05+00:00",
                    started_at="2026-04-09T00:01:01+00:00",
                    attempt=1,
                )
            )
            host = RuntimeHost(
                db,
                recoverable_actions={"refine"},
                now=lambda: "2026-04-09T00:10:00+00:00",
            )

            report = host.start()
            requeued = repo.get(running_refine.task_id)
            interrupted = repo.get(running_merge.task_id)
            events = LogRepo(db).list_events()

        self.assertEqual(("task-1",), report.requeued_task_ids)
        self.assertEqual(("task-2",), report.interrupted_task_ids)
        self.assertEqual(TaskStatus.QUEUED.value, requeued.status)
        self.assertEqual("recovered", requeued.stage)
        self.assertIsNone(requeued.started_at)
        self.assertIsNone(requeued.completed_at)
        self.assertEqual(TaskStatus.INTERRUPTED.value, interrupted.status)
        self.assertEqual("interrupted", interrupted.stage)
        self.assertEqual("2026-04-09T00:10:00+00:00", interrupted.completed_at)
        self.assertEqual(
            "runtime host restarted before task finished",
            interrupted.error,
        )
        self.assertEqual(
            [("task_interrupted", "task-2"), ("task_requeued", "task-1")],
            sorted((event.kind, event.task_id) for event in events),
        )

    def test_runtime_host_commits_session_task_handoff_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = ControlDb.for_root(root)
            host = RuntimeHost(
                db,
                now=lambda: "2026-04-09T00:10:00+00:00",
            )
            session = host.session_supervisor.create_or_load(_sample_metadata(root))
            host.session_supervisor.begin_ingest(session.session_id)
            host.session_supervisor.accept_stop(
                session.session_id,
                payload={"status": "stop_requested"},
            )

            handoff = host.commit_session_task_handoff(
                session_id=session.session_id,
                action="postprocess",
                label="后台整理",
                payload={
                    "action": "postprocess",
                    "session_id": session.session_id,
                    "speaker_enabled": True,
                },
                dedupe_key=f"postprocess:{session.session_id}",
                message="已提交后台整理任务。",
                event_payload={"spool_path": f"{session.session_dir}/live.ingest.pcm"},
            )
            fetched_session = host.sessions.get(session.session_id)
            fetched_task = host.tasks.get(handoff.task.task_id)
            events = LogRepo(db).list_events(session_id=session.session_id)

        self.assertIsNotNone(fetched_session)
        self.assertIsNotNone(fetched_task)
        self.assertEqual(SessionStatus.HANDOFF_COMMITTED.value, fetched_session.runtime_status)
        self.assertEqual(SessionStatus.HANDOFF_COMMITTED.value, fetched_session.display_status)
        self.assertEqual(TaskStatus.QUEUED.value, fetched_task.status)
        self.assertEqual(session.session_id, fetched_task.session_id)
        handoff_event = next(event for event in events if event.kind == "handoff_committed")
        self.assertEqual(fetched_task.task_id, handoff_event.payload["task_id"])
        self.assertIn("live.ingest.pcm", str(handoff_event.payload["spool_path"]))

    def test_session_supervisor_rejects_direct_lifecycle_status_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = ControlDb.for_root(root)
            supervisor = SessionSupervisor(
                db,
                now=_Clock(
                    "2026-04-09T00:00:05+00:00",
                ),
            )
            created = supervisor.create_or_load(_sample_metadata(root))

            with self.assertRaisesRegex(ValueError, "session lifecycle status"):
                supervisor.apply_metadata_changes(
                    created.session_id,
                    {"runtime_status": SessionStatus.PAUSED.value},
                    event_kind="test_status_change",
                )
            events = LogRepo(db).list_events(session_id=created.session_id)

        self.assertEqual(SessionStatus.STARTING.value, created.status)
        self.assertEqual(
            ["session_created"],
            [event.kind for event in events],
        )

    def test_live_session_cannot_complete_before_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = ControlDb.for_root(root)
            supervisor = SessionSupervisor(db)
            created = supervisor.create_or_load(_sample_metadata(root))

            with self.assertRaisesRegex(RuntimeError, "invalid session transition"):
                supervisor.complete_session(
                    created.session_id,
                    display_status="finalized",
                )

    def test_file_session_can_complete_from_starting(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = ControlDb.for_root(root)
            supervisor = SessionSupervisor(db)
            created = supervisor.create_or_load(_sample_metadata(root, input_mode="file"))

            completed = supervisor.complete_session(
                created.session_id,
                display_status="finalized",
            )

        self.assertEqual(SessionStatus.COMPLETED.value, completed.runtime_status)
        self.assertEqual("finalized", completed.display_status)

    def test_session_supervisor_apply_command_logs_command_and_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = ControlDb.for_root(root)
            supervisor = SessionSupervisor(
                db,
                now=_Clock(
                    "2026-04-09T00:00:05+00:00",
                    "2026-04-09T00:00:06+00:00",
                ),
            )
            created = supervisor.create_or_load(_sample_metadata(root))

            ingesting = supervisor.begin_ingest(created.session_id)
            stopped = supervisor.accept_stop(
                created.session_id,
                payload={"status": "stop_requested"},
            )
            commands = LogRepo(db).list_commands(session_id=created.session_id)
            events = LogRepo(db).list_events(session_id=created.session_id)

        self.assertEqual(SessionStatus.INGESTING.value, ingesting.runtime_status)
        self.assertEqual(SessionStatus.STOP_REQUESTED.value, stopped.runtime_status)
        self.assertEqual(
            ["session_begin_ingest", "session_accept_stop"],
            [command.kind for command in commands],
        )
        self.assertEqual(
            ["session_created", "ingest_started", "stop_accepted"],
            [event.kind for event in events],
        )

    def test_runtime_host_recovers_stale_live_session_into_postprocess_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = ControlDb.for_root(root)
            host = RuntimeHost(
                db,
                now=lambda: "2026-04-09T00:10:00+00:00",
                recoverable_actions={"postprocess"},
            )
            metadata = _sample_metadata(root)
            Path(metadata.session_dir).mkdir(parents=True, exist_ok=True)
            host.session_supervisor.create_or_load(metadata)
            host.session_supervisor.begin_ingest(metadata.session_id)
            append_audio_frame(
                Path(metadata.session_dir),
                AudioFrame(
                    started_ms=0,
                    ended_ms=120,
                    pcm16=b"\x01\x02" * 320,
                ),
            )

            report = host.start()
            recovered_session = host.sessions.get(metadata.session_id)
            tasks = host.tasks.list_all()
            commands = LogRepo(db).list_commands(session_id=metadata.session_id)
            events = LogRepo(db).list_events(session_id=metadata.session_id)

        self.assertEqual((metadata.session_id,), report.recovered_session_ids)
        self.assertEqual(SessionStatus.HANDOFF_COMMITTED.value, recovered_session.runtime_status)
        self.assertEqual(1, len(tasks))
        self.assertEqual("postprocess", tasks[0].action)
        self.assertTrue(bool(tasks[0].payload.get("recover_from_spool")))
        self.assertEqual(
            [
                "session_begin_ingest",
                "session_accept_stop",
                "session_commit_handoff",
            ],
            [command.kind for command in commands],
        )
        self.assertIn("handoff_committed", [event.kind for event in events])

    def test_task_supervisor_run_task_marks_success_and_logs_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = ControlDb.for_root(root)
            supervisor = TaskSupervisor(
                db,
                now=_Clock(
                    "2026-04-09T00:00:00+00:00",
                    "2026-04-09T00:00:05+00:00",
                    "2026-04-09T00:00:10+00:00",
                ),
            )
            task = supervisor.submit(
                action="refine",
                label="离线精修并重写",
                payload={"session_id": "session-1"},
            )

            result = supervisor.run_task(
                task.task_id,
                runner=lambda record, **_: 7 if record.status == TaskStatus.RUNNING.value else -1,
            )
            persisted = TaskRepo(db).get(task.task_id)
            events = LogRepo(db).list_events(task_id=task.task_id)

        self.assertEqual(7, result)
        self.assertEqual(TaskStatus.SUCCEEDED.value, persisted.status)
        self.assertEqual("done", persisted.stage)
        self.assertEqual(1, persisted.attempt)
        self.assertEqual("2026-04-09T00:00:05+00:00", persisted.started_at)
        self.assertEqual("2026-04-09T00:00:10+00:00", persisted.completed_at)
        self.assertEqual(
            ["task_queued", "task_started", "task_succeeded"],
            [event.kind for event in events],
        )

    def test_task_supervisor_run_task_marks_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = ControlDb.for_root(root)
            supervisor = TaskSupervisor(
                db,
                now=_Clock(
                    "2026-04-09T00:00:00+00:00",
                    "2026-04-09T00:00:05+00:00",
                    "2026-04-09T00:00:10+00:00",
                ),
                cancelled_exceptions=(TaskCancelledError,),
            )
            task = supervisor.submit(
                action="import",
                label="文件导入",
                payload={"file_path": "/tmp/demo.mp3", "kind": "generic"},
                can_cancel=True,
            )

            with self.assertRaises(TaskCancelledError):
                supervisor.run_task(
                    task.task_id,
                    runner=lambda *_args, **_kwargs: _raise(TaskCancelledError("cancelled")),
                )

            persisted = TaskRepo(db).get(task.task_id)
            events = LogRepo(db).list_events(task_id=task.task_id)

        self.assertEqual(TaskStatus.CANCELLED.value, persisted.status)
        self.assertEqual("cancelled", persisted.stage)
        self.assertEqual("cancelled", persisted.error)
        self.assertEqual(
            ["task_queued", "task_started", "task_cancelled"],
            [event.kind for event in events],
        )

    def test_task_supervisor_request_id_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = ControlDb.for_root(root)
            supervisor = TaskSupervisor(db, now=lambda: "2026-04-09T00:00:00+00:00")

            first = supervisor.submit(
                action="import",
                label="文件导入",
                payload={"file_path": "/tmp/a.mp3", "kind": "generic"},
                request_id="req-1",
            )
            second = supervisor.submit(
                action="import",
                label="文件导入",
                payload={"file_path": "/tmp/b.mp3", "kind": "generic"},
                request_id="req-1",
            )

        self.assertEqual(first.task_id, second.task_id)

    def test_task_supervisor_record_progress_does_not_finish_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = ControlDb.for_root(root)
            supervisor = TaskSupervisor(
                db,
                now=_Clock(
                    "2026-04-09T00:00:00+00:00",
                    "2026-04-09T00:00:05+00:00",
                    "2026-04-09T00:00:10+00:00",
                ),
            )
            task = supervisor.submit(
                action="refine",
                label="离线精修并重写",
                payload={"session_id": "session-1"},
            )
            supervisor.start_task(task.task_id)

            persisted = supervisor.record_progress(
                task.task_id,
                ProgressEvent(
                    stage="error",
                    message="模型调用失败，准备重试。",
                    session_id="session-1",
                    error="upstream failed",
                ),
                result_changed=True,
            )
            events = LogRepo(db).list_events(task_id=task.task_id)

        self.assertEqual(TaskStatus.RUNNING.value, persisted.status)
        self.assertEqual("error", persisted.stage)
        self.assertEqual("upstream failed", persisted.error)
        self.assertEqual(1, persisted.result_version)
        self.assertIsNone(persisted.completed_at)
        self.assertEqual(
            ["task_queued", "task_started", "task_progressed"],
            [event.kind for event in events],
        )

    def test_task_supervisor_record_progress_adds_session_resource_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = ControlDb.for_root(root)
            supervisor = TaskSupervisor(
                db,
                now=_Clock(
                    "2026-04-09T00:00:00+00:00",
                    "2026-04-09T00:00:05+00:00",
                    "2026-04-09T00:00:10+00:00",
                ),
            )
            task = supervisor.submit(
                action="live",
                label="实时录音",
                payload={"title": "产品周会", "source": "1", "kind": "meeting"},
                resource_keys=("live",),
            )
            supervisor.start_task(task.task_id)

            persisted = supervisor.record_progress(
                task.task_id,
                ProgressEvent(
                    stage="capturing",
                    message="正在录音",
                    session_id="session-1",
                ),
            )

        self.assertEqual(("live", "session:session-1"), persisted.resource_keys)

    def test_runtime_host_requeues_recoverable_tasks_without_clearing_cancel_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = ControlDb.for_root(root)
            repo = TaskRepo(db)
            repo.upsert(
                TaskRecord(
                    task_id="task-1",
                    session_id="session-1",
                    action="refine",
                    label="离线精修",
                    status=TaskStatus.RUNNING.value,
                    stage="cancel_requested",
                    created_at="2026-04-09T00:00:00+00:00",
                    updated_at="2026-04-09T00:00:05+00:00",
                    started_at="2026-04-09T00:00:01+00:00",
                    attempt=1,
                    cancel_requested=True,
                )
            )
            host = RuntimeHost(
                db,
                recoverable_actions={"refine"},
                now=lambda: "2026-04-09T00:10:00+00:00",
            )

            host.start()
            requeued = repo.get("task-1")

        self.assertEqual(TaskStatus.QUEUED.value, requeued.status)
        self.assertTrue(requeued.cancel_requested)


def _sample_metadata(root: Path, *, input_mode: str = "live") -> SessionMetadata:
    session_dir = root / ".live-note" / "sessions" / "session-1"
    if input_mode == "live":
        transcript_source = "live"
        refine_status = "pending"
    else:
        transcript_source = "refined"
        refine_status = "disabled"
    return SessionMetadata(
        session_id="session-1",
        title="产品周会",
        kind="meeting",
        input_mode=input_mode,
        source_label="BlackHole 2ch",
        source_ref="1",
        language="zh",
        started_at="2026-04-09T00:00:00+00:00",
        transcript_note_path="Sessions/Transcripts/2026-04-09/demo.md",
        structured_note_path="Sessions/Summaries/2026-04-09/demo.md",
        session_dir=str(session_dir),
        status=SessionStatus.STARTING.value,
        transcript_source=transcript_source,
        refine_status=refine_status,
        execution_target="local",
        remote_session_id=None,
        speaker_status="disabled",
    )


class _Clock:
    def __init__(self, *values: str) -> None:
        self._values = list(values)

    def __call__(self) -> str:
        if not self._values:
            raise AssertionError("clock exhausted")
        return self._values.pop(0)


def _raise(exc: BaseException) -> None:
    raise exc
