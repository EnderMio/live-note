from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch

from live_note.app.services import AppService, _default_config
from live_note.runtime.domain.task_state import TaskStatus
from live_note.runtime.task_runners import TaskRunnerFactory


def _remote_enabled_config(root: Path):
    base = _default_config(root)
    return replace(
        base,
        remote=replace(base.remote, enabled=True),
        speaker=replace(base.speaker, enabled=True),
    )


class AppServiceTests(unittest.TestCase):
    def test_import_audio_file_queues_local_task_when_remote_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = AppService(root / "config.toml")
            config = _default_config(root)
            record = SimpleNamespace(task_id="task-import-1")

            with (
                patch.object(service, "load_config", return_value=config),
                patch.object(service, "enqueue_queue_task", return_value=record) as enqueue_queue_task,
            ):
                result = service.import_audio_file(
                    file_path="/tmp/demo.mp3",
                    title="本地导入",
                    kind="meeting",
                    language="zh",
                    speaker_enabled=True,
                )

        self.assertEqual("task-import-1", result)
        enqueue_queue_task.assert_called_once_with(
            label="文件导入",
            action="import",
            payload={
                "file_path": "/tmp/demo.mp3",
                "title": "本地导入",
                "kind": "meeting",
                "language": "zh",
                "speaker_enabled": True,
            },
        )

    def test_import_audio_file_submits_remote_task_directly_when_remote_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = AppService(root / "config.toml")
            config = _remote_enabled_config(root)

            with (
                patch.object(service, "load_config", return_value=config),
                patch.object(service, "_ensure_runtime_started"),
                patch.object(service, "enqueue_queue_task") as enqueue_queue_task,
                patch("live_note.app.services.RemoteClient") as remote_client_factory,
                patch(
                    "live_note.app.services.upsert_remote_task_projection_from_payload",
                    return_value=SimpleNamespace(remote_task_id="remote-task-1", request_id="req-1"),
                ) as upsert_projection,
            ):
                remote_client_factory.return_value.create_import_task.return_value = {
                    "task_id": "remote-task-1",
                    "request_id": "req-1",
                    "action": "import",
                    "status": "queued",
                }

                result = service.import_audio_file(
                    file_path="/tmp/demo.mp3",
                    title="远端导入",
                    kind="meeting",
                    language="zh",
                )

        self.assertEqual("remote-task-1", result)
        enqueue_queue_task.assert_not_called()
        remote_client_factory.return_value.create_import_task.assert_called_once_with(
            "/tmp/demo.mp3",
            title="远端导入",
            kind="meeting",
            language="zh",
            speaker_enabled=True,
            request_id=ANY,
        )
        upsert_projection.assert_called_once()

    def test_refine_queues_local_task_for_local_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = AppService(root / "config.toml")
            config = _default_config(root)

            with (
                patch.object(service, "load_config", return_value=config),
                patch(
                    "live_note.app.services.require_runtime_session",
                    return_value=SimpleNamespace(
                        execution_target="local",
                        remote_session_id=None,
                        session_id="session-1",
                    ),
                ),
                patch.object(
                    service,
                    "_submit_queue_task",
                    return_value=(SimpleNamespace(task_id="task-refine-1"), True),
                ) as submit_task,
                patch("live_note.app.services.RemoteClient") as remote_client_factory,
            ):
                result = service.refine("session-1")

        self.assertEqual("task-refine-1", result)
        remote_client_factory.assert_not_called()
        submit_task.assert_called_once_with(
            label="离线精修并重写",
            action="refine",
            payload={"session_id": "session-1"},
        )

    def test_refine_submits_remote_task_directly_for_remote_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = AppService(root / "config.toml")
            config = _remote_enabled_config(root)

            with (
                patch.object(service, "load_config", return_value=config),
                patch(
                    "live_note.app.services.require_runtime_session",
                    return_value=SimpleNamespace(
                        execution_target="remote",
                        remote_session_id="remote-session-1",
                        session_id="session-1",
                    ),
                ),
                patch.object(service, "_submit_queue_task") as submit_task,
                patch("live_note.app.services.RemoteClient") as remote_client_factory,
                patch(
                    "live_note.app.services.upsert_remote_task_projection_from_payload",
                    return_value=SimpleNamespace(remote_task_id="remote-task-1", request_id="req-1"),
                ),
            ):
                remote_client_factory.return_value.refine_session.return_value = {
                    "task_id": "remote-task-1",
                    "request_id": "req-1",
                    "action": "refine",
                    "status": "queued",
                }
                result = service.refine("session-1")

        self.assertEqual("remote-task-1", result)
        submit_task.assert_not_called()
        remote_client_factory.return_value.refine_session.assert_called_once_with(
            "remote-session-1",
            request_id=ANY,
        )

    def test_resync_notes_stays_local_even_for_remote_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = AppService(root / "config.toml")
            config = _remote_enabled_config(root)

            with (
                patch.object(service, "load_config", return_value=config),
                patch(
                    "live_note.app.services.require_runtime_session",
                    return_value=SimpleNamespace(
                        execution_target="remote",
                        remote_session_id="remote-session-1",
                        session_id="session-1",
                    ),
                ),
                patch.object(
                    service,
                    "_submit_queue_task",
                    return_value=(SimpleNamespace(task_id="task-resync-1"), True),
                ) as submit_task,
                patch("live_note.app.services.RemoteClient") as remote_client_factory,
            ):
                result = service.resync_notes("session-1")

        self.assertEqual("task-resync-1", result)
        remote_client_factory.assert_not_called()
        submit_task.assert_called_once_with(
            label="重新同步 Obsidian",
            action="resync_notes",
            payload={"session_id": "session-1"},
        )

    def test_submit_queue_task_rejects_remote_import(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = AppService(root / "config.toml")
            config = _remote_enabled_config(root)

            with patch.object(service, "load_config", return_value=config):
                with self.assertRaisesRegex(RuntimeError, "remote import"):
                    service._submit_queue_task(
                        label="文件导入",
                        action="import",
                        payload={"file_path": "/tmp/demo.mp3", "kind": "meeting"},
                    )

    def test_list_queue_tasks_hides_live_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = AppService(root / "config.toml")
            service.enqueue_queue_task(
                label="合并会话",
                action="merge",
                payload={"session_ids": ["a", "b"]},
            )
            service.runtime_host().task_supervisor.submit(
                task_id="task-live-1",
                action="live",
                label="实时录音",
                payload={"title": "周会", "source": "1", "kind": "meeting"},
                resource_keys=("live",),
            )

            queued = service.list_queue_tasks()
            hidden = service.get_queue_task("task-live-1")

        self.assertEqual(["merge"], [item.action for item in queued])
        self.assertIsNone(hidden)


class TaskRunnerFactoryTests(unittest.TestCase):
    def test_build_live_runner_uses_remote_runner_when_remote_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _remote_enabled_config(root)
            execution = TaskRunnerFactory(load_config=Mock(return_value=config))

            with patch(
                "live_note.remote.live_runner.RemoteLiveRunner",
                return_value="remote-runner",
            ) as factory:
                runner = execution.build_live_runner(
                    title="产品周会",
                    source="1",
                    kind="meeting",
                    language="zh",
                    on_progress=None,
                    auto_refine_after_live=None,
                    speaker_enabled=None,
                )

        self.assertEqual("remote-runner", runner)
        factory.assert_called_once_with(
            config=config,
            title="产品周会",
            source="1",
            kind="meeting",
            language="zh",
            on_progress=None,
        )

    def test_build_import_runner_rejects_remote_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _remote_enabled_config(root)
            execution = TaskRunnerFactory(load_config=Mock(return_value=config))

            with self.assertRaisesRegex(RuntimeError, "remote import"):
                execution.build_import_runner(
                    file_path="/tmp/demo.mp3",
                    title="远端导入",
                    kind="meeting",
                    language="zh",
                    on_progress=None,
                    cancel_event=None,
                    speaker_enabled=None,
                )

    def test_run_task_action_dispatches_refine_directly(self) -> None:
        execution = TaskRunnerFactory(
            load_config=Mock(return_value=SimpleNamespace(remote=SimpleNamespace(enabled=False)))
        )

        with patch("live_note.runtime.session_workflows.refine_session", return_value=5) as refine:
            result = execution.run_task_action(
                action="refine",
                payload={"session_id": "session-1"},
                on_progress="progress",
            )

        self.assertEqual(5, result)
        refine.assert_called_once_with(
            execution._load_config.return_value,
            "session-1",
            on_progress="progress",
        )

    def test_build_import_runner_applies_local_speaker_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _default_config(root)
            execution = TaskRunnerFactory(load_config=Mock(return_value=config))

            with patch(
                "live_note.runtime.task_runners.build_local_import_runner",
                return_value="local-import-runner",
            ) as factory:
                runner = execution.build_import_runner(
                    file_path="/tmp/demo.mp3",
                    title="本地导入",
                    kind="lecture",
                    language="zh",
                    on_progress=None,
                    cancel_event=None,
                    speaker_enabled=True,
                )

        self.assertEqual("local-import-runner", runner)
        self.assertTrue(factory.call_args.kwargs["config"].speaker.enabled)
