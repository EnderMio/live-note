from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from live_note.config import (
    AppConfig,
    AudioConfig,
    ImportConfig,
    LlmConfig,
    ObsidianConfig,
    RefineConfig,
    RemoteConfig,
    ServeConfig,
    WhisperConfig,
)
from live_note.remote.service import RemoteSessionService
from live_note.remote.tasks import RemoteTaskRegistry


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
        remote=RemoteConfig(enabled=True, base_url="http://mini.local:8765"),
        serve=ServeConfig(api_token="remote-token"),
        root_dir=root,
    )


class RemoteTaskRegistryTests(unittest.TestCase):
    def _wait_for_status(
        self,
        registry: RemoteTaskRegistry,
        task_id: str,
        status: str,
        *,
        timeout: float = 1.0,
    ) -> dict[str, object]:
        end = time.time() + timeout
        while time.time() < end:
            payload = registry.task_payload(task_id)
            if payload["status"] == status:
                return payload
            time.sleep(0.01)
        self.fail(f"任务 {task_id} 未在 {timeout}s 内进入状态 {status}")

    def test_list_tasks_returns_active_and_recent_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir))
            registry = RemoteTaskRegistry(config)
            done = threading.Event()

            completed = registry.create_task(
                action="retranscribe",
                label="重转写并重写",
                session_id="session-1",
                request_id="req-rt-1",
                build_runner=lambda _task_id, _cancel_event: lambda: None,
            )
            for _ in range(50):
                payload = registry.task_payload(str(completed["task_id"]))
                if payload["status"] == "completed":
                    break
                time.sleep(0.01)
            completed_payload = registry.task_payload(str(completed["task_id"]))
            self.assertEqual("completed", completed_payload["status"])

            def import_job() -> None:
                done.wait(timeout=1)

            running = registry.create_task(
                action="import",
                label="文件导入",
                request_id="req-import-1",
                build_runner=lambda _task_id, _cancel_event: import_job,
                can_cancel=True,
            )
            snapshot = registry.list_tasks()
            done.set()
            self._wait_for_status(registry, str(running["task_id"]), "completed")
            registry.shutdown()

        self.assertEqual(registry.server_id, snapshot["server_id"])
        self.assertTrue(any(item["task_id"] == running["task_id"] for item in snapshot["active"]))
        self.assertTrue(any(item["task_id"] == completed["task_id"] for item in snapshot["recent"]))

    def test_create_import_task_is_idempotent_by_request_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir))
            registry = RemoteTaskRegistry(config)

            first = registry.create_task(
                action="import",
                label="文件导入",
                request_id="req-import-1",
                build_runner=lambda _task_id, _cancel_event: lambda: None,
                can_cancel=True,
            )
            second = registry.create_task(
                action="import",
                label="文件导入",
                request_id="req-import-1",
                build_runner=lambda _task_id, _cancel_event: lambda: None,
                can_cancel=True,
            )
            self._wait_for_status(registry, str(first["task_id"]), "completed")
            registry.shutdown()

        self.assertEqual(first["task_id"], second["task_id"])

    def test_session_mutation_task_reuses_existing_active_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir))
            registry = RemoteTaskRegistry(config)
            done = threading.Event()

            def postprocess_job() -> None:
                done.wait(timeout=1)

            postprocess = registry.create_task(
                action="postprocess",
                label="后台整理",
                session_id="session-1",
                build_runner=lambda _task_id, _cancel_event: postprocess_job,
            )
            refine = registry.create_task(
                action="refine",
                label="离线精修并重写",
                session_id="session-1",
                request_id="req-refine-1",
                build_runner=lambda _task_id, _cancel_event: lambda: None,
            )
            done.set()
            self._wait_for_status(registry, str(postprocess["task_id"]), "completed")
            registry.shutdown()

        self.assertEqual(postprocess["task_id"], refine["task_id"])

    def test_cancel_marks_task_cancelling_for_cancellable_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir))
            registry = RemoteTaskRegistry(config)
            started = threading.Event()

            def import_job() -> None:
                started.set()
                time.sleep(0.3)

            created = registry.create_task(
                action="import",
                label="文件导入",
                request_id="req-import-2",
                build_runner=lambda _task_id, _cancel_event: import_job,
                can_cancel=True,
            )
            self.assertTrue(started.wait(timeout=1))
            payload = registry.cancel_task(str(created["task_id"]))
            registry.shutdown()

        self.assertEqual("cancelling", payload["status"])
        self.assertEqual("cancelling", payload["stage"])

    def test_registry_runs_tasks_sequentially(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir))
            registry = RemoteTaskRegistry(config)
            first_started = threading.Event()
            second_started = threading.Event()
            allow_first_finish = threading.Event()

            def first_job() -> None:
                first_started.set()
                allow_first_finish.wait(timeout=1)

            def second_job() -> None:
                second_started.set()

            registry.create_task(
                action="import",
                label="文件导入 1",
                request_id="req-import-1",
                build_runner=lambda _task_id, _cancel_event: first_job,
                can_cancel=True,
            )
            registry.create_task(
                action="import",
                label="文件导入 2",
                request_id="req-import-2",
                build_runner=lambda _task_id, _cancel_event: second_job,
                can_cancel=True,
            )

            self.assertTrue(first_started.wait(timeout=1))
            self.assertFalse(second_started.is_set(), "第二个任务不应在第一个完成前开始执行")
            allow_first_finish.set()
            self.assertTrue(second_started.wait(timeout=1))
            registry.shutdown()

    def test_cancel_queued_task_prevents_runner_from_starting(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir))
            registry = RemoteTaskRegistry(config)
            first_started = threading.Event()
            allow_first_finish = threading.Event()
            second_started = threading.Event()

            def first_job() -> None:
                first_started.set()
                allow_first_finish.wait(timeout=1)

            def second_job() -> None:
                second_started.set()

            registry.create_task(
                action="import",
                label="文件导入 1",
                request_id="req-import-1",
                build_runner=lambda _task_id, _cancel_event: first_job,
                can_cancel=True,
            )
            queued = registry.create_task(
                action="import",
                label="文件导入 2",
                request_id="req-import-2",
                build_runner=lambda _task_id, _cancel_event: second_job,
                can_cancel=True,
            )

            self.assertTrue(first_started.wait(timeout=1))
            payload = registry.cancel_task(str(queued["task_id"]))
            self.assertEqual("cancelled", payload["status"])
            allow_first_finish.set()
            time.sleep(0.05)
            self.assertFalse(second_started.is_set(), "已取消的排队任务不应开始执行")
            self.assertEqual(
                "cancelled",
                registry.task_payload(str(queued["task_id"]))["status"],
            )

    def test_cancel_during_dispatch_window_marks_task_cancelling_instead_of_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir))
            registry = RemoteTaskRegistry(config)
            original_run_task = registry._run_task
            run_entered = threading.Event()
            allow_run = threading.Event()

            def delayed_run_task(task_id: str, runner) -> None:
                run_entered.set()
                allow_run.wait(timeout=1)
                original_run_task(task_id, runner)

            setattr(registry, "_run_task", delayed_run_task)
            started = threading.Event()
            release = threading.Event()

            created = registry.create_task(
                action="import",
                label="派发窗口任务",
                request_id="req-dispatch-window",
                build_runner=lambda _task_id, _cancel_event: (
                    lambda: (started.set(), release.wait(timeout=1))
                ),
                can_cancel=True,
                task_spec={"action": "import", "uploaded_path": "unused"},
            )

            self.assertTrue(run_entered.wait(timeout=1))
            payload = registry.cancel_task(str(created["task_id"]))
            self.assertEqual("cancelling", payload["status"])

            allow_run.set()
            self.assertTrue(started.wait(timeout=1))
            release.set()
            self._wait_for_status(registry, str(created["task_id"]), "completed")
            registry.shutdown()
            registry.shutdown()

    def test_restart_recovers_server_id_queued_fifo_and_request_idempotency(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir))
            first_started = threading.Event()
            release_first = threading.Event()

            def first_job() -> None:
                first_started.set()
                release_first.wait(timeout=1)

            registry = RemoteTaskRegistry(config)
            running = registry.create_task(
                action="import",
                label="运行中任务",
                request_id="req-running-before-restart",
                build_runner=lambda _task_id, _cancel_event: first_job,
                can_cancel=True,
                task_spec={"kind": "import", "name": "running-before-restart"},
            )
            self.assertTrue(first_started.wait(timeout=1))

            queued_1 = registry.create_task(
                action="import",
                label="排队任务 1",
                request_id="req-queued-1",
                build_runner=lambda _task_id, _cancel_event: lambda: None,
                can_cancel=True,
                task_spec={"kind": "import", "name": "queued-1"},
            )
            queued_2 = registry.create_task(
                action="import",
                label="排队任务 2",
                request_id="req-queued-2",
                build_runner=lambda _task_id, _cancel_event: lambda: None,
                can_cancel=True,
                task_spec={"kind": "import", "name": "queued-2"},
            )

            recovered_started: list[str] = []
            recovered_first_started = threading.Event()
            recovered_release_first = threading.Event()
            recovered_second_started = threading.Event()

            def recover_runner(
                _task_id: str,
                task_spec: dict[str, object] | None,
                _cancel_event: threading.Event | None,
            ) -> callable:
                name = str((task_spec or {}).get("name"))

                def recovered_job() -> None:
                    recovered_started.append(name)
                    if name == "queued-1":
                        recovered_first_started.set()
                        recovered_release_first.wait(timeout=1)
                    if name == "queued-2":
                        recovered_second_started.set()

                return recovered_job

            recovered = RemoteTaskRegistry(config, recover_runner=recover_runner)

            self.assertEqual(registry.server_id, recovered.server_id)
            self.assertIn(
                recovered.task_payload(str(queued_1["task_id"]))["status"],
                {"queued", "running"},
            )
            self.assertEqual(
                "queued",
                recovered.task_payload(str(queued_2["task_id"]))["status"],
            )

            duplicate = recovered.create_task(
                action="import",
                label="排队任务 1（重试）",
                request_id="req-queued-1",
                build_runner=lambda _task_id, _cancel_event: lambda: None,
                can_cancel=True,
                task_spec={"kind": "import", "name": "queued-1"},
            )
            self.assertEqual(queued_1["task_id"], duplicate["task_id"])

            self.assertTrue(recovered_first_started.wait(timeout=1))
            self.assertFalse(recovered_second_started.is_set())
            recovered_release_first.set()
            self.assertTrue(recovered_second_started.wait(timeout=1))
            self.assertEqual(["queued-1", "queued-2"], recovered_started)

            release_first.set()
            self._wait_for_status(registry, str(running["task_id"]), "completed")
            self._wait_for_status(recovered, str(queued_1["task_id"]), "completed")
            self._wait_for_status(recovered, str(queued_2["task_id"]), "completed")
            self._wait_for_status(recovered, str(running["task_id"]), "failed")
            recovered.shutdown()
            registry.shutdown()

    def test_restart_marks_running_and_cancelling_tasks_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir))
            started = threading.Event()
            release = threading.Event()

            def cancellable_job() -> None:
                started.set()
                release.wait(timeout=1)

            registry = RemoteTaskRegistry(config)
            task = registry.create_task(
                action="import",
                label="可取消任务",
                request_id="req-cancelling-before-restart",
                build_runner=lambda _task_id, _cancel_event: cancellable_job,
                can_cancel=True,
                task_spec={"kind": "import", "name": "cancelling-before-restart"},
            )
            self.assertTrue(started.wait(timeout=1))
            registry.cancel_task(str(task["task_id"]))

            recovered = RemoteTaskRegistry(config)
            payload = recovered.task_payload(str(task["task_id"]))
            self.assertEqual("failed", payload["status"])
            self.assertEqual("failed", payload["stage"])
            self.assertIn("重启", str(payload["message"]))
            self.assertIn("重启", str(payload["error"]))
            recent_ids = [item["task_id"] for item in recovered.list_tasks()["recent"]]
            self.assertIn(str(task["task_id"]), recent_ids)

            release.set()
            self._wait_for_status(registry, str(task["task_id"]), "completed")
            recovered.shutdown()
            registry.shutdown()

    def test_restart_preserves_recent_terminal_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir))
            registry = RemoteTaskRegistry(config)

            first = registry.create_task(
                action="retranscribe",
                label="终态任务 1",
                session_id="session-1",
                request_id="req-terminal-1",
                build_runner=lambda _task_id, _cancel_event: lambda: None,
                task_spec={"kind": "retranscribe", "session_id": "session-1"},
            )
            second = registry.create_task(
                action="retranscribe",
                label="终态任务 2",
                session_id="session-2",
                request_id="req-terminal-2",
                build_runner=lambda _task_id, _cancel_event: lambda: None,
                task_spec={"kind": "retranscribe", "session_id": "session-2"},
            )

            self._wait_for_status(registry, str(first["task_id"]), "completed")
            self._wait_for_status(registry, str(second["task_id"]), "completed")

            recovered = RemoteTaskRegistry(config)
            snapshot = recovered.list_tasks()
            recent_ids = [item["task_id"] for item in snapshot["recent"]]
            self.assertGreaterEqual(len(recent_ids), 2)
            self.assertEqual([second["task_id"], first["task_id"]], recent_ids[:2])
            recovered.shutdown()
            registry.shutdown()

    def test_restart_keeps_recovered_failed_task_visible_when_recent_limit_is_full(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir))
            completed_registry = RemoteTaskRegistry(config, recent_limit=1)

            completed = completed_registry.create_task(
                action="retranscribe",
                label="已完成任务",
                session_id="session-1",
                request_id="req-completed-before-restart",
                build_runner=lambda _task_id, _cancel_event: lambda: None,
                task_spec={"kind": "retranscribe", "session_id": "session-1"},
            )
            self._wait_for_status(completed_registry, str(completed["task_id"]), "completed")

            started = threading.Event()
            release = threading.Event()

            def running_job() -> None:
                started.set()
                release.wait(timeout=1)

            running = completed_registry.create_task(
                action="import",
                label="重启中断任务",
                request_id="req-running-visible-after-restart",
                build_runner=lambda _task_id, _cancel_event: running_job,
                can_cancel=True,
                task_spec={"kind": "import", "name": "visible-after-restart"},
            )
            self.assertTrue(started.wait(timeout=1))

            recovered = RemoteTaskRegistry(config, recent_limit=1)
            snapshot = recovered.list_tasks()
            recent_ids = [item["task_id"] for item in snapshot["recent"]]
            self.assertEqual([str(running["task_id"])], recent_ids)

            release.set()
            self._wait_for_status(completed_registry, str(running["task_id"]), "completed")
            recovered.shutdown()
            completed_registry.shutdown()

    def test_restart_surfaces_invalid_persisted_task_record_as_recovery_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir))
            state_path = config.root_dir / ".live-note" / "remote_task_registry.json"
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(
                '{"version":1,"server_id":"server-test","tasks":{"broken":"oops"},"pending_task_ids":[],"recent_terminal_ids":[]}',
                encoding="utf-8",
            )

            recovered = RemoteTaskRegistry(config)
            recent = recovered.list_tasks()["recent"]
            self.assertTrue(recent)
            self.assertEqual("recovery", recent[0]["action"])
            self.assertEqual("failed", recent[0]["status"])
            recovered.shutdown()

    def test_restart_surfaces_invalid_top_level_state_file_as_recovery_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir))
            state_path = config.root_dir / ".live-note" / "remote_task_registry.json"
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text("{broken-json", encoding="utf-8")

            recovered = RemoteTaskRegistry(config)
            recent = recovered.list_tasks()["recent"]
            self.assertTrue(recent)
            self.assertEqual("recovery", recent[0]["action"])
            self.assertEqual("failed", recent[0]["status"])
            recovered.shutdown()


class RemoteSessionServiceRecoveryTests(unittest.TestCase):
    class _StubRemoteSessionService(RemoteSessionService):
        def __init__(self, config: AppConfig):
            self.recovered_runs: list[tuple[str, str]] = []
            super().__init__(config)

        def _build_import_runner(
            self,
            *,
            task_id: str,
            uploaded_path: Path,
            title: str | None,
            kind: str,
            language: str | None,
            speaker_enabled: bool | None,
            cancel_event: threading.Event | None,
        ):
            def run() -> None:
                self.recovered_runs.append(("import", uploaded_path.name))

            return run

        def _build_refine_runner(self, task_id: str, session_id: str):
            def run() -> None:
                self.recovered_runs.append(("refine", session_id))

            return run

        def _build_retranscribe_runner(self, task_id: str, session_id: str):
            def run() -> None:
                self.recovered_runs.append(("retranscribe", session_id))

            return run

        def _build_postprocess_runner(
            self,
            task_id: str,
            session_id: str,
            *,
            speaker_enabled: bool | None = None,
        ):
            def run() -> None:
                self.recovered_runs.append(("postprocess", session_id))

            return run

    def _wait_for_status(
        self,
        service: RemoteSessionService,
        task_id: str,
        status: str,
        *,
        timeout: float = 1.5,
    ) -> dict[str, object]:
        end = time.time() + timeout
        while time.time() < end:
            payload = service.task_payload(task_id)
            if payload["status"] == status:
                return payload
            time.sleep(0.01)
        self.fail(f"任务 {task_id} 未在 {timeout}s 内进入状态 {status}")

    def test_service_restart_recovers_import_refine_retranscribe_runners(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir))
            service = self._StubRemoteSessionService(config)
            started = threading.Event()
            release = threading.Event()

            blocker = service.tasks.create_task(
                action="import",
                label="阻塞任务",
                request_id="req-blocker",
                can_cancel=True,
                task_spec={"action": "import", "uploaded_path": "unused"},
                build_runner=lambda _task_id, _cancel_event: (
                    lambda: (started.set(), release.wait(timeout=1))
                ),
            )
            self.assertTrue(started.wait(timeout=1))

            import_task = service.create_import_task(
                filename="demo.wav",
                title="导入恢复",
                kind="meeting",
                language="zh",
                speaker_enabled=True,
                request_id="req-import-recover",
                file_bytes=b"fake-audio",
            )
            refine_task = service.request_refine(
                "session-refine-recover", request_id="req-refine-recover"
            )
            retranscribe_task = service.request_retranscribe(
                "session-retranscribe-recover",
                request_id="req-retranscribe-recover",
            )

            recovered = self._StubRemoteSessionService(config)

            self.assertEqual(service.tasks.server_id, recovered.tasks.server_id)
            self._wait_for_status(recovered, str(import_task["task_id"]), "completed")
            self._wait_for_status(recovered, str(refine_task["task_id"]), "completed")
            self._wait_for_status(recovered, str(retranscribe_task["task_id"]), "completed")
            self._wait_for_status(recovered, str(blocker["task_id"]), "failed")
            self.assertEqual(
                [
                    ("import", "upload.bin"),
                    ("refine", "session-refine-recover"),
                    ("retranscribe", "session-retranscribe-recover"),
                ],
                recovered.recovered_runs,
            )

            duplicate = recovered.create_import_task(
                filename="demo.wav",
                title="导入恢复",
                kind="meeting",
                language="zh",
                speaker_enabled=True,
                request_id="req-import-recover",
                file_bytes=b"fake-audio-duplicate",
            )
            self.assertEqual(import_task["task_id"], duplicate["task_id"])

            release.set()
            self._wait_for_status(service, str(blocker["task_id"]), "completed")
            recovered.tasks.shutdown()
            service.tasks.shutdown()

    def test_service_restart_marks_postprocess_recovery_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir))
            service = self._StubRemoteSessionService(config)
            started = threading.Event()
            release = threading.Event()

            service.tasks.create_task(
                action="import",
                label="阻塞任务",
                request_id="req-blocker-postprocess",
                can_cancel=True,
                task_spec={"action": "import", "uploaded_path": "unused"},
                build_runner=lambda _task_id, _cancel_event: (
                    lambda: (started.set(), release.wait(timeout=1))
                ),
            )
            self.assertTrue(started.wait(timeout=1))

            postprocess = service._create_postprocess_task(
                session_id="session-postprocess-recover",
                speaker_enabled=True,
            )

            recovered = self._StubRemoteSessionService(config)
            payload = self._wait_for_status(recovered, str(postprocess["task_id"]), "failed")
            self.assertIn("postprocess", str(payload["error"]).lower())
            self.assertFalse(any(action == "postprocess" for action, _ in recovered.recovered_runs))

            release.set()
            recovered.tasks.shutdown()
            service.tasks.shutdown()

    def test_duplicate_import_request_id_does_not_write_second_upload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir))
            service = self._StubRemoteSessionService(config)
            started = threading.Event()
            release = threading.Event()

            service.tasks.create_task(
                action="import",
                label="阻塞任务",
                request_id="req-import-blocker",
                can_cancel=True,
                task_spec={"action": "import", "uploaded_path": "unused"},
                build_runner=lambda _task_id, _cancel_event: (
                    lambda: (started.set(), release.wait(timeout=1))
                ),
            )
            self.assertTrue(started.wait(timeout=1))

            first = service.create_import_task(
                filename="demo.wav",
                title="导入幂等",
                kind="meeting",
                language="zh",
                speaker_enabled=True,
                request_id="req-import-idempotent",
                file_bytes=b"first-audio",
            )
            second = service.create_import_task(
                filename="demo.wav",
                title="导入幂等",
                kind="meeting",
                language="zh",
                speaker_enabled=True,
                request_id="req-import-idempotent",
                file_bytes=b"second-audio",
            )

            self.assertEqual(first["task_id"], second["task_id"])
            upload_files = sorted(
                path.relative_to(config.root_dir)
                for path in (config.root_dir / ".live-note" / "remote-imports").rglob("*")
                if path.is_file()
            )
            self.assertEqual(1, len(upload_files))

            release.set()
            service.tasks.shutdown()

    def test_concurrent_duplicate_import_request_id_does_not_overwrite_first_upload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir))
            service = self._StubRemoteSessionService(config)
            blocker_started = threading.Event()
            blocker_release = threading.Event()

            service.tasks.create_task(
                action="retranscribe",
                label="阻塞任务",
                session_id="session-blocker",
                request_id="req-concurrency-blocker",
                task_spec={"action": "retranscribe", "session_id": "session-blocker"},
                build_runner=lambda _task_id, _cancel_event: (
                    lambda: (blocker_started.set(), blocker_release.wait(timeout=1))
                ),
            )
            self.assertTrue(blocker_started.wait(timeout=1))

            original_create_task = service.tasks.create_task
            first_import_create_entered = threading.Event()
            allow_first_import_create = threading.Event()
            first_import_gate_opened = False

            def delayed_create_task(*args, **kwargs):
                nonlocal first_import_gate_opened
                if (
                    kwargs.get("action") == "import"
                    and kwargs.get("request_id") == "req-import-concurrent"
                    and not first_import_gate_opened
                ):
                    first_import_gate_opened = True
                    first_import_create_entered.set()
                    allow_first_import_create.wait(timeout=1)
                return original_create_task(*args, **kwargs)

            setattr(service.tasks, "create_task", delayed_create_task)
            first_result: dict[str, object] = {}

            def first_request() -> None:
                first_result.update(
                    service.create_import_task(
                        filename="demo.wav",
                        title="导入并发",
                        kind="meeting",
                        language="zh",
                        speaker_enabled=True,
                        request_id="req-import-concurrent",
                        file_bytes=b"first-audio",
                    )
                )

            first_thread = threading.Thread(target=first_request)
            first_thread.start()
            self.assertTrue(first_import_create_entered.wait(timeout=1))

            second = service.create_import_task(
                filename="demo.wav",
                title="导入并发",
                kind="meeting",
                language="zh",
                speaker_enabled=True,
                request_id="req-import-concurrent",
                file_bytes=b"second-audio",
            )

            upload_files = [
                path
                for path in (config.root_dir / ".live-note" / "remote-imports").rglob("*")
                if path.is_file()
            ]
            self.assertEqual(1, len(upload_files))
            self.assertEqual(b"first-audio", upload_files[0].read_bytes())

            allow_first_import_create.set()
            first_thread.join(timeout=1)
            self.assertFalse(first_thread.is_alive())
            self.assertEqual(second["task_id"], first_result["task_id"])

            blocker_release.set()
            service.tasks.shutdown()

    def test_concurrent_duplicate_import_request_id_with_different_filenames_keeps_single_upload(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir))
            service = self._StubRemoteSessionService(config)
            blocker_started = threading.Event()
            blocker_release = threading.Event()

            service.tasks.create_task(
                action="retranscribe",
                label="阻塞任务",
                session_id="session-blocker",
                request_id="req-concurrency-blocker-2",
                task_spec={"action": "retranscribe", "session_id": "session-blocker"},
                build_runner=lambda _task_id, _cancel_event: (
                    lambda: (blocker_started.set(), blocker_release.wait(timeout=1))
                ),
            )
            self.assertTrue(blocker_started.wait(timeout=1))

            original_create_task = service.tasks.create_task
            first_import_create_entered = threading.Event()
            allow_first_import_create = threading.Event()
            first_import_gate_opened = False

            def delayed_create_task(*args, **kwargs):
                nonlocal first_import_gate_opened
                if (
                    kwargs.get("action") == "import"
                    and kwargs.get("request_id") == "req-import-concurrent-different-name"
                    and not first_import_gate_opened
                ):
                    first_import_gate_opened = True
                    first_import_create_entered.set()
                    allow_first_import_create.wait(timeout=1)
                return original_create_task(*args, **kwargs)

            setattr(service.tasks, "create_task", delayed_create_task)
            first_result: dict[str, object] = {}

            def first_request() -> None:
                first_result.update(
                    service.create_import_task(
                        filename="first.wav",
                        title="导入并发不同文件名",
                        kind="meeting",
                        language="zh",
                        speaker_enabled=True,
                        request_id="req-import-concurrent-different-name",
                        file_bytes=b"first-audio",
                    )
                )

            first_thread = threading.Thread(target=first_request)
            first_thread.start()
            self.assertTrue(first_import_create_entered.wait(timeout=1))

            second = service.create_import_task(
                filename="second.mp3",
                title="导入并发不同文件名",
                kind="meeting",
                language="zh",
                speaker_enabled=True,
                request_id="req-import-concurrent-different-name",
                file_bytes=b"second-audio",
            )

            upload_files = [
                path
                for path in (config.root_dir / ".live-note" / "remote-imports").rglob("*")
                if path.is_file()
            ]
            self.assertEqual(1, len(upload_files))
            self.assertEqual(b"first-audio", upload_files[0].read_bytes())

            allow_first_import_create.set()
            first_thread.join(timeout=1)
            self.assertFalse(first_thread.is_alive())
            self.assertEqual(second["task_id"], first_result["task_id"])

            blocker_release.set()
            service.tasks.shutdown()

    def test_import_upload_is_cleaned_after_terminal_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir))
            service = RemoteSessionService(config)

            with patch("live_note.app.coordinator.FileImportCoordinator") as coordinator_cls:
                coordinator_cls.return_value.run.return_value = 0

                created = service.create_import_task(
                    filename="cleanup.wav",
                    title="导入清理",
                    kind="meeting",
                    language="zh",
                    speaker_enabled=False,
                    request_id="req-import-cleanup",
                    file_bytes=b"cleanup-audio",
                )

                self._wait_for_status(service, str(created["task_id"]), "completed")
                upload_files = [
                    path
                    for path in (config.root_dir / ".live-note" / "remote-imports").rglob("*")
                    if path.is_file()
                ]
                self.assertEqual([], upload_files)

            service.tasks.shutdown()

    def test_stale_same_request_upload_is_replaced_before_new_task_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir))
            service = self._StubRemoteSessionService(config)
            started = threading.Event()
            release = threading.Event()

            service.tasks.create_task(
                action="retranscribe",
                label="阻塞任务",
                session_id="session-blocker",
                request_id="req-stale-blocker",
                task_spec={"action": "retranscribe", "session_id": "session-blocker"},
                build_runner=lambda _task_id, _cancel_event: (
                    lambda: (started.set(), release.wait(timeout=1))
                ),
            )
            self.assertTrue(started.wait(timeout=1))

            stale_path = service._uploads_dir(request_id="req-import-stale") / "upload.bin"
            stale_path.parent.mkdir(parents=True, exist_ok=True)
            stale_path.write_bytes(b"stale-audio")

            created = service.create_import_task(
                filename="fresh.wav",
                title="导入陈旧重试",
                kind="meeting",
                language="zh",
                speaker_enabled=True,
                request_id="req-import-stale",
                file_bytes=b"fresh-audio",
            )

            self.assertEqual(b"fresh-audio", stale_path.read_bytes())
            self.assertEqual("queued", service.task_payload(str(created["task_id"]))["status"])

            release.set()
            service.tasks.shutdown()

    def test_recovered_import_runner_rejects_empty_uploaded_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir))
            service = RemoteSessionService(config)

            with self.assertRaises(ValueError):
                service._build_recovered_runner(
                    "task-import-invalid-path",
                    {"action": "import", "uploaded_path": ""},
                    None,
                )

            service.tasks.shutdown()

    def test_cancelling_queued_import_cleans_uploaded_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir))
            service = self._StubRemoteSessionService(config)
            started = threading.Event()
            release = threading.Event()

            service.tasks.create_task(
                action="retranscribe",
                label="阻塞任务",
                session_id="session-blocker",
                request_id="req-cancel-blocker",
                task_spec={"action": "retranscribe", "session_id": "session-blocker"},
                build_runner=lambda _task_id, _cancel_event: (
                    lambda: (started.set(), release.wait(timeout=1))
                ),
            )
            self.assertTrue(started.wait(timeout=1))

            created = service.create_import_task(
                filename="queued-cancel.wav",
                title="排队取消清理",
                kind="meeting",
                language="zh",
                speaker_enabled=False,
                request_id="req-queued-cancel-cleanup",
                file_bytes=b"queued-cancel-audio",
            )

            upload_files_before_cancel = [
                path
                for path in (config.root_dir / ".live-note" / "remote-imports").rglob("*")
                if path.is_file()
            ]
            self.assertEqual(1, len(upload_files_before_cancel))

            payload = service.cancel_task(str(created["task_id"]))
            self.assertEqual("cancelled", payload["status"])
            upload_files_after_cancel = [
                path
                for path in (config.root_dir / ".live-note" / "remote-imports").rglob("*")
                if path.is_file()
            ]
            self.assertEqual([], upload_files_after_cancel)

            release.set()
            service.tasks.shutdown()

    def test_import_cleanup_ignores_uploaded_path_outside_remote_imports(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = build_config(Path(temp_dir))
            registry = RemoteTaskRegistry(config)
            outside_path = config.root_dir / "outside-upload.bin"
            outside_path.write_bytes(b"outside")
            started = threading.Event()
            release = threading.Event()

            registry.create_task(
                action="retranscribe",
                label="阻塞任务",
                session_id="session-blocker",
                request_id="req-outside-blocker",
                task_spec={"action": "retranscribe", "session_id": "session-blocker"},
                build_runner=lambda _task_id, _cancel_event: (
                    lambda: (started.set(), release.wait(timeout=1))
                ),
            )
            self.assertTrue(started.wait(timeout=1))

            created = registry.create_task(
                action="import",
                label="外部路径导入",
                request_id="req-outside-upload",
                can_cancel=True,
                task_spec={"action": "import", "uploaded_path": str(outside_path)},
                build_runner=lambda _task_id, _cancel_event: lambda: None,
            )

            payload = registry.cancel_task(str(created["task_id"]))
            self.assertEqual("cancelled", payload["status"])
            self.assertTrue(outside_path.exists())

            release.set()
            registry.shutdown()
