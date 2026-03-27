from __future__ import annotations

import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from live_note.app.input_device_service import InputDeviceService
from live_note.app.journal import SessionWorkspace
from live_note.app.path_opener_service import PathOpenerService
from live_note.app.remote_tasks import (
    RemoteTaskAttachment,
    load_remote_tasks,
    replace_remote_task_records,
    upsert_pending_remote_task,
    upsert_remote_task_payload,
)
from live_note.app.services import AppService, SettingsDraft, _default_config
from live_note.app.session_action_service import SessionActionService
from live_note.app.task_queue import QueuedTaskRecord
from live_note.domain import SessionMetadata


def sample_metadata(session_dir: str) -> SessionMetadata:
    return SessionMetadata(
        session_id="20260315-210500-机器学习",
        title="机器学习导论",
        kind="lecture",
        input_mode="file",
        source_label="demo.mp4",
        source_ref="/tmp/demo.mp4",
        language="zh",
        started_at="2026-03-15T13:05:00+00:00",
        transcript_note_path="Sessions/Transcripts/2026-03-15/机器学习导论-210500.md",
        structured_note_path="Sessions/Summaries/2026-03-15/机器学习导论-210500.md",
        session_dir=session_dir,
        status="importing",
    )


class AppServiceTests(unittest.TestCase):
    def test_load_settings_draft_delegates_to_settings_service(self) -> None:
        service = AppService(Path("/tmp/config.toml"))
        delegated = SettingsDraft(
            ffmpeg_binary="/opt/homebrew/bin/ffmpeg",
            whisper_binary="/Users/demo/whisper-server",
            whisper_model="/tmp/model.bin",
        )

        with patch("live_note.app.services.SettingsService", create=True) as factory:
            factory.return_value.load_settings_draft.return_value = delegated

            result = service.load_settings_draft()

        self.assertIs(delegated, result)
        factory.assert_called_once_with(service.config_path, service.env_path)
        factory.return_value.load_settings_draft.assert_called_once_with()

    def test_save_settings_delegates_to_settings_service(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_path = root / "ggml-base.bin"
            model_path.write_bytes(b"fake-model")
            service = AppService(root / "config.toml")
            draft = SettingsDraft(
                ffmpeg_binary="/opt/homebrew/bin/ffmpeg",
                whisper_binary="/Users/demo/whisper-server",
                whisper_model=str(model_path),
            )
            delegated = object()

            with patch("live_note.app.services.SettingsService", create=True) as factory:
                factory.return_value.save_settings.return_value = delegated

                result = service.save_settings(draft)

        self.assertIs(delegated, result)
        factory.assert_called_once_with(service.config_path, service.env_path)
        factory.return_value.save_settings.assert_called_once_with(draft)

    def test_doctor_checks_delegate_to_doctor_service(self) -> None:
        service = AppService(Path("/tmp/config.toml"))
        delegated = [SimpleNamespace(name="config", status="OK", detail="已加载 /tmp/config.toml")]

        with patch("live_note.app.services.DoctorService", create=True) as factory:
            factory.return_value.doctor_checks.return_value = delegated

            result = service.doctor_checks()

        self.assertIs(delegated, result)
        factory.assert_called_once()
        factory.return_value.doctor_checks.assert_called_once_with()

    def test_list_input_devices_delegates_to_input_device_service(self) -> None:
        service = AppService(Path("/tmp/config.toml"))
        delegated = [SimpleNamespace(name="MacBook Pro 麦克风", index=0)]

        with patch("live_note.app.services.InputDeviceService", create=True) as factory:
            factory.return_value.list_input_devices.return_value = delegated

            result = service.list_input_devices()

        self.assertIs(delegated, result)
        factory.assert_called_once_with(
            list_input_devices=service._input_device_service.__globals__[
                "capture_list_input_devices"
            ]
        )
        factory.return_value.list_input_devices.assert_called_once_with()

    def test_input_device_service_calls_enumerator_and_returns_devices(self) -> None:
        delegated = [SimpleNamespace(name="MacBook Pro 麦克风", index=0)]
        enumerator = Mock(return_value=delegated)
        service = InputDeviceService(list_input_devices=enumerator)

        result = service.list_input_devices()

        self.assertIs(delegated, result)
        enumerator.assert_called_once_with()

    def test_list_session_summaries_delegate_to_session_query_service(self) -> None:
        service = AppService(Path("/tmp/config.toml"))
        delegated = [SimpleNamespace(session_id="demo")]

        with patch("live_note.app.services.SessionQueryService", create=True) as factory:
            factory.return_value.list_session_summaries.return_value = delegated

            result = service.list_session_summaries()

        self.assertIs(delegated, result)
        factory.assert_called_once()
        factory.return_value.list_session_summaries.assert_called_once_with()

    def test_list_remote_task_summaries_delegate_to_remote_task_service(self) -> None:
        service = AppService(Path("/tmp/config.toml"))
        delegated = SimpleNamespace(remote_available=False, availability_message="x", tasks=[])

        with patch("live_note.app.services.RemoteTaskService", create=True) as factory:
            factory.return_value.list_remote_task_summaries.return_value = delegated

            result = service.list_remote_task_summaries()

        self.assertIs(delegated, result)
        factory.assert_called_once()
        factory.return_value.list_remote_task_summaries.assert_called_once_with()

    def test_sync_remote_task_delegates_to_remote_task_service(self) -> None:
        service = AppService(Path("/tmp/config.toml"))
        delegated = {"task_id": "task-import-1"}

        with patch("live_note.app.services.RemoteTaskService", create=True) as factory:
            factory.return_value.sync_remote_task.return_value = delegated

            result = service.sync_remote_task("task-import-1")

        self.assertIs(delegated, result)
        factory.assert_called_once()
        factory.return_value.sync_remote_task.assert_called_once_with("task-import-1")

    def test_cancel_remote_task_delegates_to_remote_task_service(self) -> None:
        service = AppService(Path("/tmp/config.toml"))
        delegated = {"task_id": "task-import-1", "status": "cancelling"}

        with patch("live_note.app.services.RemoteTaskService", create=True) as factory:
            factory.return_value.cancel_remote_task.return_value = delegated

            result = service.cancel_remote_task("task-import-1")

        self.assertIs(delegated, result)
        factory.assert_called_once()
        factory.return_value.cancel_remote_task.assert_called_once_with("task-import-1")

    def test_create_live_coordinator_delegates_to_coordinator_factory_service(self) -> None:
        service = AppService(Path("/tmp/config.toml"))

        with patch("live_note.app.services.CoordinatorFactoryService", create=True) as factory:
            factory.return_value.create_live_coordinator.return_value = "runner"

            result = service.create_live_coordinator(
                title="产品周会",
                source="1",
                kind="meeting",
                language="zh",
            )

        self.assertEqual("runner", result)
        factory.assert_called_once()
        factory.return_value.create_live_coordinator.assert_called_once_with(
            title="产品周会",
            source="1",
            kind="meeting",
            language="zh",
            on_progress=None,
            auto_refine_after_live=None,
            speaker_enabled=None,
        )

    def test_create_import_coordinator_delegates_to_coordinator_factory_service(self) -> None:
        service = AppService(Path("/tmp/config.toml"))

        with patch("live_note.app.services.CoordinatorFactoryService", create=True) as factory:
            factory.return_value.create_import_coordinator.return_value = "runner"

            result = service.create_import_coordinator(
                file_path="/tmp/demo.mp3",
                title="远端导入",
                kind="meeting",
                language="zh",
            )

        self.assertEqual("runner", result)
        factory.assert_called_once()
        factory.return_value.create_import_coordinator.assert_called_once_with(
            file_path="/tmp/demo.mp3",
            title="远端导入",
            kind="meeting",
            language="zh",
            on_progress=None,
            cancel_event=None,
            speaker_enabled=None,
        )

    def test_start_live_session_runs_created_coordinator(self) -> None:
        service = AppService(Path("/tmp/config.toml"))
        runner = Mock()
        runner.run.return_value = 3

        with patch.object(service, "create_live_coordinator", return_value=runner) as factory:
            result = service.start_live_session(
                title="产品周会",
                source="1",
                kind="meeting",
                language="zh",
            )

        self.assertEqual(3, result)
        factory.assert_called_once_with(
            title="产品周会",
            source="1",
            kind="meeting",
            language="zh",
        )
        runner.run.assert_called_once_with()

    def test_import_audio_file_runs_created_coordinator(self) -> None:
        service = AppService(Path("/tmp/config.toml"))
        runner = Mock()
        runner.run.return_value = 4

        with patch.object(service, "create_import_coordinator", return_value=runner) as factory:
            result = service.import_audio_file(
                file_path="/tmp/demo.mp3",
                title="远端导入",
                kind="meeting",
                language="zh",
            )

        self.assertEqual(4, result)
        factory.assert_called_once_with(
            file_path="/tmp/demo.mp3",
            title="远端导入",
            kind="meeting",
            language="zh",
        )
        runner.run.assert_called_once_with()

    def test_refine_delegates_to_session_action_service(self) -> None:
        service = AppService(Path("/tmp/config.toml"))

        with patch("live_note.app.services.SessionActionService", create=True) as factory:
            factory.return_value.refine.return_value = 0

            result = service.refine("session-1")

        self.assertEqual(0, result)
        factory.assert_called_once()
        factory.return_value.refine.assert_called_once_with("session-1", on_progress=None)

    def test_finalize_delegates_to_session_action_service(self) -> None:
        service = AppService(Path("/tmp/config.toml"))
        on_progress = Mock()

        with patch("live_note.app.services.SessionActionService", create=True) as factory:
            factory.return_value.finalize.return_value = 0

            result = service.finalize("session-1", on_progress=on_progress)

        self.assertEqual(0, result)
        factory.assert_called_once()
        factory.return_value.finalize.assert_called_once_with("session-1", on_progress=on_progress)

    def test_session_action_service_factory_wires_finalize_session_dependency(self) -> None:
        service = AppService(Path("/tmp/config.toml"))

        with patch("live_note.app.services.SessionActionService", create=True) as factory:
            service._session_action_service()

        self.assertIs(
            factory.call_args.kwargs["finalize_session"],
            service.finalize.__globals__["finalize_session"],
        )

    def test_session_action_service_finalize_calls_finalize_session(self) -> None:
        load_config = Mock(return_value="config")
        finalize_session = Mock(return_value=7)
        service = SessionActionService(
            load_config=load_config,
            remote_tasks_path=Mock(),
            workspace_loader=Mock(),
            remote_client_factory=Mock(),
            upsert_pending_remote_task=Mock(),
            upsert_remote_task_payload=Mock(),
            merge_sessions=Mock(),
            republish_session=Mock(),
            sync_session_notes=Mock(),
            retranscribe_session=Mock(),
            refine_session=Mock(),
            finalize_session=finalize_session,
        )

        result = service.finalize("session-1")

        self.assertEqual(7, result)
        load_config.assert_called_once_with()
        finalize_session.assert_called_once_with("config", "session-1", on_progress=None)

    def test_open_path_delegates_to_path_opener_service(self) -> None:
        service = AppService(Path("/tmp/config.toml"))
        path = Path("/tmp/demo.md")

        with patch("live_note.app.services.PathOpenerService", create=True) as factory:
            service.open_path(path)

        factory.assert_called_once_with(run=service.open_path.__globals__["subprocess"].run)
        factory.return_value.open_path.assert_called_once_with(path)

    def test_path_opener_service_factory_wires_subprocess_run_dependency(self) -> None:
        service = AppService(Path("/tmp/config.toml"))

        with patch("live_note.app.services.PathOpenerService", create=True) as factory:
            service._path_opener_service()

        self.assertIs(
            factory.call_args.kwargs["run"], service.open_path.__globals__["subprocess"].run
        )

    def test_path_opener_service_calls_runner_with_open_command(self) -> None:
        run = Mock()
        service = PathOpenerService(run=run)
        path = Path("/tmp/demo.md")

        service.open_path(path)

        run.assert_called_once_with(["open", str(path)], check=False)

    def test_retranscribe_delegates_to_session_action_service(self) -> None:
        service = AppService(Path("/tmp/config.toml"))

        with patch("live_note.app.services.SessionActionService", create=True) as factory:
            factory.return_value.retranscribe.return_value = 0

            result = service.retranscribe("session-1")

        self.assertEqual(0, result)
        factory.assert_called_once()
        factory.return_value.retranscribe.assert_called_once_with("session-1", on_progress=None)

    def test_merge_delegates_to_session_action_service(self) -> None:
        service = AppService(Path("/tmp/config.toml"))

        with patch("live_note.app.services.SessionActionService", create=True) as factory:
            factory.return_value.merge.return_value = 0

            result = service.merge(["a", "b"], title="merged")

        self.assertEqual(0, result)
        factory.assert_called_once()
        factory.return_value.merge.assert_called_once_with(
            ["a", "b"], title="merged", on_progress=None
        )

    def test_republish_delegates_to_session_action_service(self) -> None:
        service = AppService(Path("/tmp/config.toml"))

        with patch("live_note.app.services.SessionActionService", create=True) as factory:
            factory.return_value.republish.return_value = 0

            result = service.republish("session-1")

        self.assertEqual(0, result)
        factory.assert_called_once()
        factory.return_value.republish.assert_called_once_with("session-1", on_progress=None)

    def test_resync_notes_delegates_to_session_action_service(self) -> None:
        service = AppService(Path("/tmp/config.toml"))

        with patch("live_note.app.services.SessionActionService", create=True) as factory:
            factory.return_value.resync_notes.return_value = 0

            result = service.resync_notes("session-1")

        self.assertEqual(0, result)
        factory.assert_called_once()
        factory.return_value.resync_notes.assert_called_once_with("session-1", on_progress=None)

    def test_run_queue_task_delegates_to_task_dispatch_service(self) -> None:
        service = AppService(Path("/tmp/config.toml"))
        record = QueuedTaskRecord(
            task_id="task-1",
            action="merge",
            label="合并会话",
            payload={"session_ids": ["a", "b"]},
            fingerprint="merge:a,b",
            status="queued",
            created_at="2026-03-24T00:00:00+00:00",
        )

        with patch("live_note.app.services.TaskDispatchService", create=True) as factory:
            factory.return_value.run_queue_task.return_value = 0

            result = service.run_queue_task(record)

        self.assertEqual(0, result)
        factory.assert_called_once()
        factory.return_value.run_queue_task.assert_called_once_with(
            record,
            on_progress=None,
            cancel_event=None,
        )

    def test_run_queue_task_import_passes_cancel_event_to_runner(self) -> None:
        service = AppService(Path("/tmp/config.toml"))
        cancel_event = threading.Event()
        record = QueuedTaskRecord(
            task_id="task-2",
            action="import",
            label="导入文件",
            payload={
                "file_path": "/tmp/demo.mp3",
                "title": "demo",
                "kind": "meeting",
                "speaker_enabled": True,
            },
            fingerprint="import:/tmp/demo.mp3",
            status="queued",
            created_at="2026-03-24T00:00:00+00:00",
        )
        runner = Mock()
        runner.run.return_value = 3

        with patch.object(service, "create_import_coordinator", return_value=runner) as factory:
            result = service.run_queue_task(record, cancel_event=cancel_event)

        self.assertEqual(3, result)
        factory.assert_called_once_with(
            file_path="/tmp/demo.mp3",
            title="demo",
            kind="meeting",
            language=None,
            on_progress=None,
            speaker_enabled=True,
            cancel_event=cancel_event,
        )
        runner.run.assert_called_once_with()

    def test_save_settings_writes_reloadable_config_and_env(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_path = root / "ggml-base.bin"
            model_path.write_bytes(b"fake-model")
            service = AppService(root / "config.toml")

            config = service.save_settings(
                SettingsDraft(
                    ffmpeg_binary="/opt/homebrew/bin/ffmpeg",
                    whisper_binary="/Users/demo/whisper-server",
                    whisper_model=str(model_path),
                    save_session_wav=True,
                    refine_enabled=True,
                    refine_auto_after_live=True,
                    obsidian_enabled=False,
                    llm_enabled=True,
                    llm_base_url="https://llm.example.com/v1",
                    llm_model="custom-model",
                    llm_stream=True,
                    llm_wire_api="responses",
                    llm_requires_openai_auth=True,
                    obsidian_api_key="obsidian-token",
                    llm_api_key="llm-token",
                )
            )

            self.assertEqual("/opt/homebrew/bin/ffmpeg", config.importer.ffmpeg_binary)
            self.assertFalse(config.obsidian.enabled)
            self.assertTrue(config.llm.enabled)
            self.assertTrue(config.audio.save_session_wav)
            self.assertTrue(config.refine.enabled)
            self.assertTrue(config.refine.auto_after_live)
            self.assertEqual("https://llm.example.com/v1", config.llm.base_url)
            self.assertEqual("custom-model", config.llm.model)
            self.assertTrue(config.llm.stream)
            self.assertTrue((root / "config.toml").exists())
            self.assertIn("OBSIDIAN_API_KEY=obsidian-token", (root / ".env").read_text())
            reloaded = service.load_config()
            self.assertFalse(reloaded.obsidian.enabled)
            self.assertTrue(reloaded.llm.enabled)
            self.assertTrue(reloaded.audio.save_session_wav)
            self.assertTrue(reloaded.refine.enabled)
            self.assertTrue(reloaded.refine.auto_after_live)
            self.assertEqual("https://llm.example.com/v1", reloaded.llm.base_url)
            self.assertEqual("custom-model", reloaded.llm.model)
            self.assertTrue(reloaded.llm.stream)
            self.assertEqual("responses", reloaded.llm.wire_api)
            self.assertTrue(reloaded.llm.requires_openai_auth)
            self.assertEqual("obsidian-token", reloaded.obsidian.api_key)
            self.assertEqual("llm-token", reloaded.llm.api_key)

    def test_save_settings_updates_openai_key_when_openai_auth_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_path = root / "ggml-base.bin"
            model_path.write_bytes(b"fake-model")
            env_path = root / ".env"
            env_path.write_text(
                "OPENAI_API_KEY=openai-token\nEXTRA_SETTING=keep-me\n",
                encoding="utf-8",
            )
            service = AppService(root / "config.toml")

            service.save_settings(
                SettingsDraft(
                    ffmpeg_binary="/opt/homebrew/bin/ffmpeg",
                    whisper_binary="/Users/demo/whisper-server",
                    whisper_model=str(model_path),
                    llm_enabled=True,
                    llm_requires_openai_auth=True,
                    llm_api_key="fallback-token",
                )
            )

            env_text = env_path.read_text(encoding="utf-8")
            reloaded = service.load_config()

        self.assertIn("EXTRA_SETTING=keep-me", env_text)
        self.assertIn("LLM_API_KEY=fallback-token", env_text)
        self.assertIn("OPENAI_API_KEY=fallback-token", env_text)
        self.assertEqual("fallback-token", reloaded.llm.api_key)

    def test_save_settings_persists_remote_runtime_sections(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_path = root / "ggml-base.bin"
            model_path.write_bytes(b"fake-model")
            service = AppService(root / "config.toml")

            service.save_settings(
                SettingsDraft(
                    ffmpeg_binary="/opt/homebrew/bin/ffmpeg",
                    whisper_binary="/Users/demo/whisper-server",
                    whisper_model=str(model_path),
                    remote_enabled=True,
                    remote_base_url="http://192.168.1.20:8765",
                    remote_api_token="remote-token",
                    remote_live_chunk_ms=640,
                    serve_host="0.0.0.0",
                    serve_port=9900,
                    funasr_enabled=True,
                    funasr_base_url="ws://127.0.0.1:10095",
                    speaker_enabled=True,
                    speaker_segmentation_model="/models/segmentation.onnx",
                    speaker_embedding_model="/models/embedding.onnx",
                    speaker_cluster_threshold=0.42,
                )
            )

            reloaded = service.load_config()
            draft = service.load_settings_draft()

        self.assertTrue(reloaded.remote.enabled)
        self.assertEqual("http://192.168.1.20:8765", reloaded.remote.base_url)
        self.assertEqual("remote-token", reloaded.remote.api_token)
        self.assertEqual(640, reloaded.remote.live_chunk_ms)
        self.assertEqual("0.0.0.0", reloaded.serve.host)
        self.assertEqual(9900, reloaded.serve.port)
        self.assertTrue(reloaded.funasr.enabled)
        self.assertEqual("ws://127.0.0.1:10095", reloaded.funasr.base_url)
        self.assertTrue(reloaded.speaker.enabled)
        self.assertEqual("/models/segmentation.onnx", str(reloaded.speaker.segmentation_model))
        self.assertEqual("/models/embedding.onnx", str(reloaded.speaker.embedding_model))
        self.assertAlmostEqual(0.42, reloaded.speaker.cluster_threshold)
        self.assertTrue(draft.remote_enabled)
        self.assertEqual("remote-token", draft.remote_api_token)
        self.assertTrue(draft.funasr_enabled)

    def test_create_live_coordinator_uses_remote_runner_when_remote_enabled(self) -> None:
        service = AppService(Path("/tmp/config.toml"))
        config = SimpleNamespace(remote=SimpleNamespace(enabled=True))

        with (
            patch.object(service, "load_config", return_value=config),
            patch(
                "live_note.app.services.RemoteLiveCoordinator",
                return_value="remote-runner",
            ) as factory,
        ):
            runner = service.create_live_coordinator(
                title="产品周会",
                source="1",
                kind="meeting",
                language="zh",
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

    def test_create_import_coordinator_uses_remote_runner_when_remote_enabled(self) -> None:
        service = AppService(Path("/tmp/config.toml"))
        config = SimpleNamespace(remote=SimpleNamespace(enabled=True))

        with (
            patch.object(service, "load_config", return_value=config),
            patch("live_note.app.services.FileImportCoordinator") as local_factory,
            patch(
                "live_note.app.services.RemoteImportCoordinator",
                return_value="remote-import-runner",
            ) as factory,
        ):
            runner = service.create_import_coordinator(
                file_path="/tmp/demo.mp3",
                title="远端导入",
                kind="meeting",
                language="zh",
            )

        self.assertEqual("remote-import-runner", runner)
        local_factory.assert_not_called()
        factory.assert_called_once_with(
            config=config,
            file_path="/tmp/demo.mp3",
            title="远端导入",
            kind="meeting",
            language="zh",
            on_progress=None,
        )

    def test_create_live_coordinator_passes_auto_refine_override_to_local_runner(self) -> None:
        service = AppService(Path("/tmp/config.toml"))
        config = SimpleNamespace(remote=SimpleNamespace(enabled=False))

        with (
            patch.object(service, "load_config", return_value=config),
            patch(
                "live_note.app.services.SessionCoordinator",
                return_value="local-runner",
            ) as factory,
        ):
            runner = service.create_live_coordinator(
                title="产品周会",
                source="1",
                kind="meeting",
                language="zh",
                auto_refine_after_live=False,
            )

        self.assertEqual("local-runner", runner)
        factory.assert_called_once_with(
            config=config,
            title="产品周会",
            source="1",
            kind="meeting",
            language="zh",
            on_progress=None,
            auto_refine_after_live=False,
        )

    def test_create_live_coordinator_applies_speaker_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _default_config(root)
            service = AppService(root / "config.toml")

            with (
                patch.object(service, "load_config", return_value=config),
                patch(
                    "live_note.app.services.SessionCoordinator",
                    return_value="local-runner",
                ) as factory,
            ):
                runner = service.create_live_coordinator(
                    title="产品周会",
                    source="1",
                    kind="meeting",
                    language="zh",
                    speaker_enabled=True,
                )

        self.assertEqual("local-runner", runner)
        self.assertTrue(factory.call_args.kwargs["config"].speaker.enabled)

    def test_create_import_coordinator_applies_speaker_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _default_config(root)
            service = AppService(root / "config.toml")

            with (
                patch.object(service, "load_config", return_value=config),
                patch(
                    "live_note.app.services.FileImportCoordinator",
                    return_value="local-import-runner",
                ) as factory,
            ):
                runner = service.create_import_coordinator(
                    file_path="/tmp/demo.mp3",
                    title="本地导入",
                    kind="lecture",
                    language="zh",
                    speaker_enabled=True,
                )

        self.assertEqual("local-import-runner", runner)
        self.assertTrue(factory.call_args.kwargs["config"].speaker.enabled)

    def test_refine_remote_session_creates_managed_remote_task_and_returns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_path = root / "ggml-base.bin"
            model_path.write_bytes(b"fake-model")
            service = AppService(root / "config.toml")
            service.save_settings(
                SettingsDraft(
                    ffmpeg_binary="/opt/homebrew/bin/ffmpeg",
                    whisper_binary="/Users/demo/whisper-server",
                    whisper_model=str(model_path),
                    remote_enabled=True,
                    remote_base_url="http://mini.local:8765",
                    remote_api_token="remote-token",
                )
            )
            session_dir = root / ".live-note" / "sessions" / "remote-1"
            SessionWorkspace.create(
                session_dir,
                replace(
                    sample_metadata(str(session_dir)),
                    session_id="remote-1",
                    title="产品周会",
                    kind="meeting",
                    input_mode="live",
                    source_label="BlackHole 2ch",
                    source_ref="1",
                    status="completed",
                    execution_target="remote",
                    remote_session_id="remote-1",
                    session_dir=str(session_dir),
                ),
            )
            client = Mock()
            client.refine.return_value = {
                "task_id": "task-refine-1",
                "server_id": "server-1",
                "action": "refine",
                "label": "离线精修并重写",
                "session_id": "remote-1",
                "status": "queued",
                "stage": "queued",
                "message": "已加入远端队列。",
                "result_version": 0,
                "can_cancel": False,
            }

            with patch("live_note.app.services.RemoteClient", return_value=client):
                exit_code = service.refine("remote-1")
            attachments = load_remote_tasks(root / ".live-note" / "remote_tasks.json")

        self.assertEqual(0, exit_code)
        client.refine.assert_called_once()
        self.assertEqual("remote-1", client.refine.call_args.args[0])
        self.assertTrue(client.refine.call_args.kwargs["request_id"].startswith("refine-"))
        client.get_artifacts.assert_not_called()
        self.assertEqual(1, len(attachments.records))
        self.assertEqual("task-refine-1", attachments.records[0].remote_task_id)
        self.assertEqual("refine", attachments.records[0].action)

    def test_retranscribe_remote_session_creates_managed_remote_task_and_returns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_path = root / "ggml-base.bin"
            model_path.write_bytes(b"fake-model")
            service = AppService(root / "config.toml")
            service.save_settings(
                SettingsDraft(
                    ffmpeg_binary="/opt/homebrew/bin/ffmpeg",
                    whisper_binary="/Users/demo/whisper-server",
                    whisper_model=str(model_path),
                    remote_enabled=True,
                    remote_base_url="http://mini.local:8765",
                    remote_api_token="remote-token",
                )
            )
            session_dir = root / ".live-note" / "sessions" / "remote-1"
            SessionWorkspace.create(
                session_dir,
                replace(
                    sample_metadata(str(session_dir)),
                    session_id="remote-1",
                    title="产品周会",
                    kind="meeting",
                    input_mode="live",
                    source_label="BlackHole 2ch",
                    source_ref="1",
                    status="completed",
                    execution_target="remote",
                    remote_session_id="remote-1",
                    session_dir=str(session_dir),
                ),
            )
            client = Mock()
            client.retranscribe.return_value = {
                "task_id": "task-rt-1",
                "server_id": "server-1",
                "action": "retranscribe",
                "label": "重转写并重写",
                "session_id": "remote-1",
                "status": "queued",
                "stage": "queued",
                "message": "已加入远端队列。",
                "result_version": 0,
                "can_cancel": False,
            }

            with patch("live_note.app.services.RemoteClient", return_value=client):
                exit_code = service.retranscribe("remote-1")
            attachments = load_remote_tasks(root / ".live-note" / "remote_tasks.json")

        self.assertEqual(0, exit_code)
        client.retranscribe.assert_called_once()
        self.assertEqual("remote-1", client.retranscribe.call_args.args[0])
        self.assertTrue(
            client.retranscribe.call_args.kwargs["request_id"].startswith("retranscribe-")
        )
        self.assertEqual(1, len(attachments.records))
        self.assertEqual("task-rt-1", attachments.records[0].remote_task_id)
        self.assertEqual("retranscribe", attachments.records[0].action)

    def test_list_remote_task_summaries_rebinds_by_request_id_and_syncs_completed_artifacts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_path = root / "ggml-base.bin"
            model_path.write_bytes(b"fake-model")
            service = AppService(root / "config.toml")
            service.save_settings(
                SettingsDraft(
                    ffmpeg_binary="/opt/homebrew/bin/ffmpeg",
                    whisper_binary="/Users/demo/whisper-server",
                    whisper_model=str(model_path),
                    remote_enabled=True,
                    remote_base_url="http://mini.local:8765",
                    remote_api_token="remote-token",
                )
            )
            upsert_pending_remote_task(
                service.remote_tasks_path(),
                action="import",
                label="文件导入",
                request_id="req-import-1",
            )
            client = Mock()
            client.list_tasks.return_value = {
                "server_id": "server-1",
                "active": [],
                "recent": [
                    {
                        "task_id": "task-import-1",
                        "server_id": "server-1",
                        "action": "import",
                        "label": "文件导入",
                        "session_id": "remote-import-1",
                        "request_id": "req-import-1",
                        "status": "completed",
                        "stage": "done",
                        "message": "远端导入已完成。",
                        "result_version": 2,
                        "can_cancel": False,
                    }
                ],
            }
            client.get_artifacts.return_value = {
                "metadata": {
                    "session_id": "remote-import-1",
                    "title": "股票课",
                    "kind": "lecture",
                    "input_mode": "file",
                    "source_label": "第1课.mp3",
                    "source_ref": "remote-upload://第1课.mp3",
                    "language": "zh",
                    "started_at": "2026-03-19T08:00:00+00:00",
                    "status": "completed",
                    "transcript_source": "refined",
                    "refine_status": "disabled",
                    "execution_target": "remote",
                    "remote_session_id": "remote-import-1",
                    "speaker_status": "disabled",
                },
                "entries": [],
                "has_session_audio": False,
            }

            with (
                patch("live_note.app.services.RemoteClient", return_value=client),
                patch(
                    "live_note.app.services.apply_remote_artifacts",
                    return_value=sample_metadata(
                        str(root / ".live-note" / "sessions" / "remote-import-1")
                    ),
                ) as apply_mock,
            ):
                snapshot = service.list_remote_task_summaries()
            attachments = load_remote_tasks(service.remote_tasks_path())

        self.assertTrue(snapshot.remote_available)
        self.assertEqual(1, len(snapshot.tasks))
        self.assertEqual("task-import-1", snapshot.tasks[0].remote_task_id)
        self.assertEqual("attached", snapshot.tasks[0].attachment_state)
        client.get_artifacts.assert_called_once_with("remote-import-1")
        apply_mock.assert_called_once()
        self.assertEqual(2, attachments.records[0].last_synced_result_version)

    def test_list_remote_task_summaries_marks_record_lost_when_server_id_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_path = root / "ggml-base.bin"
            model_path.write_bytes(b"fake-model")
            service = AppService(root / "config.toml")
            service.save_settings(
                SettingsDraft(
                    ffmpeg_binary="/opt/homebrew/bin/ffmpeg",
                    whisper_binary="/Users/demo/whisper-server",
                    whisper_model=str(model_path),
                    remote_enabled=True,
                    remote_base_url="http://mini.local:8765",
                    remote_api_token="remote-token",
                )
            )
            upsert_remote_task_payload(
                service.remote_tasks_path(),
                {
                    "task_id": "task-import-1",
                    "server_id": "server-old",
                    "action": "import",
                    "label": "文件导入",
                    "status": "running",
                    "stage": "transcribing",
                    "message": "正在转写",
                    "result_version": 1,
                },
            )
            client = Mock()
            client.list_tasks.return_value = {
                "server_id": "server-new",
                "active": [],
                "recent": [],
            }

            with patch("live_note.app.services.RemoteClient", return_value=client):
                snapshot = service.list_remote_task_summaries()

        self.assertTrue(snapshot.remote_available)
        self.assertEqual("lost", snapshot.tasks[0].attachment_state)

    def test_list_remote_task_summaries_keeps_terminal_record_attached_when_server_id_changes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_path = root / "ggml-base.bin"
            model_path.write_bytes(b"fake-model")
            service = AppService(root / "config.toml")
            service.save_settings(
                SettingsDraft(
                    ffmpeg_binary="/opt/homebrew/bin/ffmpeg",
                    whisper_binary="/Users/demo/whisper-server",
                    whisper_model=str(model_path),
                    remote_enabled=True,
                    remote_base_url="http://mini.local:8765",
                    remote_api_token="remote-token",
                )
            )
            upsert_remote_task_payload(
                service.remote_tasks_path(),
                {
                    "task_id": "task-import-1",
                    "server_id": "server-old",
                    "action": "import",
                    "label": "文件导入",
                    "status": "completed",
                    "stage": "done",
                    "message": "导入会话已完成。",
                    "session_id": "session-1",
                    "result_version": 1,
                },
            )
            client = Mock()
            client.list_tasks.return_value = {
                "server_id": "server-new",
                "active": [],
                "recent": [],
            }

            with patch("live_note.app.services.RemoteClient", return_value=client):
                snapshot = service.list_remote_task_summaries()

        self.assertTrue(snapshot.remote_available)
        self.assertEqual("attached", snapshot.tasks[0].attachment_state)
        self.assertEqual("completed", snapshot.tasks[0].status)

    def test_list_remote_task_summaries_recovers_terminal_record_from_stale_lost_marker(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_path = root / "ggml-base.bin"
            model_path.write_bytes(b"fake-model")
            service = AppService(root / "config.toml")
            service.save_settings(
                SettingsDraft(
                    ffmpeg_binary="/opt/homebrew/bin/ffmpeg",
                    whisper_binary="/Users/demo/whisper-server",
                    whisper_model=str(model_path),
                    remote_enabled=True,
                    remote_base_url="http://mini.local:8765",
                    remote_api_token="remote-token",
                )
            )
            replace_remote_task_records(
                service.remote_tasks_path(),
                [
                    RemoteTaskAttachment(
                        remote_task_id="task-import-1",
                        server_id="server-old",
                        action="import",
                        label="文件导入",
                        session_id="session-1",
                        request_id="req-import-1",
                        last_known_status="completed",
                        last_known_stage="done",
                        last_message="导入会话已完成。",
                        attachment_state="lost",
                        last_synced_result_version=1,
                        result_version=1,
                        updated_at="2026-03-23T07:00:00+00:00",
                        created_at="2026-03-23T06:00:00+00:00",
                        last_error="服务端已重置，任务无法恢复。",
                    )
                ],
            )
            client = Mock()
            client.list_tasks.return_value = {
                "server_id": "server-new",
                "active": [],
                "recent": [],
            }

            with patch("live_note.app.services.RemoteClient", return_value=client):
                snapshot = service.list_remote_task_summaries()

        self.assertTrue(snapshot.remote_available)
        self.assertEqual("attached", snapshot.tasks[0].attachment_state)
        self.assertIsNone(snapshot.tasks[0].last_error)

    def test_list_remote_task_summaries_sorts_active_attached_before_lost_running(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_path = root / "ggml-base.bin"
            model_path.write_bytes(b"fake-model")
            service = AppService(root / "config.toml")
            service.save_settings(
                SettingsDraft(
                    ffmpeg_binary="/opt/homebrew/bin/ffmpeg",
                    whisper_binary="/Users/demo/whisper-server",
                    whisper_model=str(model_path),
                    remote_enabled=False,
                )
            )
            service.remote_tasks_path().parent.mkdir(parents=True, exist_ok=True)
            from live_note.app.remote_tasks import replace_remote_task_records

            replace_remote_task_records(
                service.remote_tasks_path(),
                [
                    RemoteTaskAttachment(
                        remote_task_id="task-lost",
                        server_id="server-old",
                        action="import",
                        label="文件导入",
                        session_id="remote-lost",
                        request_id="req-lost",
                        last_known_status="running",
                        last_known_stage="speaker",
                        last_message="正在分析说话人特征。",
                        attachment_state="lost",
                        last_synced_result_version=0,
                        result_version=0,
                        updated_at="2026-03-22T11:00:00+00:00",
                        created_at="2026-03-22T10:00:00+00:00",
                    ),
                    RemoteTaskAttachment(
                        remote_task_id="task-done",
                        server_id="server-now",
                        action="import",
                        label="文件导入",
                        session_id="remote-done",
                        request_id="req-done",
                        last_known_status="completed",
                        last_known_stage="done",
                        last_message="导入会话已完成。",
                        attachment_state="attached",
                        last_synced_result_version=3,
                        result_version=3,
                        updated_at="2026-03-22T11:05:00+00:00",
                        created_at="2026-03-22T10:05:00+00:00",
                    ),
                ],
            )

            snapshot = service.list_remote_task_summaries()

        self.assertFalse(snapshot.remote_available)
        self.assertEqual("task-done", snapshot.tasks[0].remote_task_id)
        self.assertEqual("task-lost", snapshot.tasks[1].remote_task_id)

    def test_sync_remote_task_fetches_artifacts_and_marks_attachment_synced(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_path = root / "ggml-base.bin"
            model_path.write_bytes(b"fake-model")
            service = AppService(root / "config.toml")
            service.save_settings(
                SettingsDraft(
                    ffmpeg_binary="/opt/homebrew/bin/ffmpeg",
                    whisper_binary="/Users/demo/whisper-server",
                    whisper_model=str(model_path),
                    remote_enabled=True,
                    remote_base_url="http://mini.local:8765",
                    remote_api_token="remote-token",
                )
            )
            upsert_remote_task_payload(
                service.remote_tasks_path(),
                {
                    "task_id": "task-import-1",
                    "server_id": "server-1",
                    "action": "import",
                    "label": "文件导入",
                    "session_id": "remote-import-1",
                    "status": "completed",
                    "stage": "done",
                    "message": "远端导入已完成。",
                    "result_version": 3,
                },
            )
            attachments = load_remote_tasks(service.remote_tasks_path())
            self.assertEqual(0, attachments.records[0].last_synced_result_version)

            client = Mock()
            client.get_task.return_value = {
                "task_id": "task-import-1",
                "server_id": "server-1",
                "action": "import",
                "label": "文件导入",
                "session_id": "remote-import-1",
                "status": "completed",
                "stage": "done",
                "message": "远端导入已完成。",
                "result_version": 3,
            }
            client.get_artifacts.return_value = {
                "metadata": {
                    "session_id": "remote-import-1",
                    "title": "股票课",
                    "kind": "lecture",
                    "input_mode": "file",
                    "source_label": "第1课.mp3",
                    "source_ref": "remote-upload://第1课.mp3",
                    "language": "zh",
                    "started_at": "2026-03-19T08:00:00+00:00",
                    "status": "completed",
                    "transcript_source": "refined",
                    "refine_status": "disabled",
                    "execution_target": "remote",
                    "remote_session_id": "remote-import-1",
                    "speaker_status": "disabled",
                },
                "entries": [],
                "has_session_audio": False,
            }

            with (
                patch("live_note.app.services.RemoteClient", return_value=client),
                patch(
                    "live_note.app.services.apply_remote_artifacts",
                    return_value=sample_metadata(
                        str(root / ".live-note" / "sessions" / "remote-import-1")
                    ),
                ) as apply_mock,
            ):
                payload = service.sync_remote_task("task-import-1")

            synced = load_remote_tasks(service.remote_tasks_path())

        self.assertEqual("task-import-1", payload["task_id"])
        client.get_task.assert_called_once_with("task-import-1")
        client.get_artifacts.assert_called_once_with("remote-import-1")
        apply_mock.assert_called_once()
        self.assertEqual(3, synced.records[0].last_synced_result_version)
        self.assertIsNone(synced.records[0].last_error)

    def test_cancel_remote_task_rejects_when_remote_mode_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_path = root / "ggml-base.bin"
            model_path.write_bytes(b"fake-model")
            service = AppService(root / "config.toml")
            service.save_settings(
                SettingsDraft(
                    ffmpeg_binary="/opt/homebrew/bin/ffmpeg",
                    whisper_binary="/Users/demo/whisper-server",
                    whisper_model=str(model_path),
                    remote_enabled=False,
                )
            )

            with self.assertRaisesRegex(RuntimeError, "远端模式未启用"):
                service.cancel_remote_task("task-import-1")

    def test_cancel_remote_task_persists_updated_attachment_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_path = root / "ggml-base.bin"
            model_path.write_bytes(b"fake-model")
            service = AppService(root / "config.toml")
            service.save_settings(
                SettingsDraft(
                    ffmpeg_binary="/opt/homebrew/bin/ffmpeg",
                    whisper_binary="/Users/demo/whisper-server",
                    whisper_model=str(model_path),
                    remote_enabled=True,
                    remote_base_url="http://mini.local:8765",
                    remote_api_token="remote-token",
                )
            )
            upsert_remote_task_payload(
                service.remote_tasks_path(),
                {
                    "task_id": "task-import-1",
                    "server_id": "server-1",
                    "action": "import",
                    "label": "文件导入",
                    "session_id": "remote-import-1",
                    "status": "running",
                    "stage": "transcribing",
                    "message": "正在转写",
                    "result_version": 1,
                    "can_cancel": True,
                },
            )
            client = Mock()
            client.cancel_task.return_value = {
                "task_id": "task-import-1",
                "server_id": "server-1",
                "action": "import",
                "label": "文件导入",
                "session_id": "remote-import-1",
                "status": "cancelling",
                "stage": "cancelling",
                "message": "正在取消远端任务。",
                "result_version": 1,
                "can_cancel": False,
            }

            with patch("live_note.app.services.RemoteClient", return_value=client):
                payload = service.cancel_remote_task("task-import-1")

            attachments = load_remote_tasks(service.remote_tasks_path())

        self.assertEqual("cancelling", payload["status"])
        client.cancel_task.assert_called_once_with("task-import-1")
        self.assertEqual("cancelling", attachments.records[0].last_known_status)
        self.assertEqual("正在取消远端任务。", attachments.records[0].last_message)
        self.assertFalse(attachments.records[0].can_cancel)

    def test_list_session_summaries_reads_workspace_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_path = root / "ggml-base.bin"
            model_path.write_bytes(b"fake-model")
            service = AppService(root / "config.toml")
            service.save_settings(
                SettingsDraft(
                    ffmpeg_binary="/opt/homebrew/bin/ffmpeg",
                    whisper_binary="/Users/demo/whisper-server",
                    whisper_model=str(model_path),
                )
            )

            session_dir = root / ".live-note" / "sessions" / "20260315-210500-机器学习"
            workspace = SessionWorkspace.create(session_dir, sample_metadata(str(session_dir)))
            wav_path = workspace.next_wav_path("seg-00001")
            wav_path.parent.mkdir(parents=True, exist_ok=True)
            wav_path.write_bytes(b"wav")
            workspace.record_segment_created("seg-00001", 0, 2000, wav_path)
            workspace.record_segment_text("seg-00001", 0, 2000, "第一段")
            workspace.record_segment_created(
                "seg-00002",
                2000,
                4000,
                workspace.next_wav_path("seg-00002"),
            )
            workspace.record_segment_error("seg-00002", 2000, 4000, "timeout")
            workspace.update_session(transcript_source="refined", refine_status="done")

            summaries = service.list_session_summaries()

        self.assertEqual(1, len(summaries))
        self.assertEqual("机器学习导论", summaries[0].title)
        self.assertEqual(2, summaries[0].segment_count)
        self.assertEqual(1, summaries[0].transcribed_count)
        self.assertEqual(1, summaries[0].failed_count)
        self.assertEqual("timeout", summaries[0].latest_error)
        self.assertEqual("refined", summaries[0].transcript_source)
        self.assertEqual("done", summaries[0].refine_status)

    def test_list_session_summaries_keeps_broken_sessions_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_path = root / "ggml-base.bin"
            model_path.write_bytes(b"fake-model")
            service = AppService(root / "config.toml")
            service.save_settings(
                SettingsDraft(
                    ffmpeg_binary="/opt/homebrew/bin/ffmpeg",
                    whisper_binary="/Users/demo/whisper-server",
                    whisper_model=str(model_path),
                )
            )

            good_dir = root / ".live-note" / "sessions" / "20260315-210500-机器学习"
            SessionWorkspace.create(good_dir, sample_metadata(str(good_dir)))

            broken_meta_dir = root / ".live-note" / "sessions" / "20260315-220000-坏会话元数据"
            broken_meta_dir.mkdir(parents=True, exist_ok=True)
            (broken_meta_dir / "session.toml").write_text("not = [valid", encoding="utf-8")

            broken_segments_dir = root / ".live-note" / "sessions" / "20260315-223000-坏会话分段"
            broken_workspace = SessionWorkspace.create(
                broken_segments_dir,
                replace(
                    sample_metadata(str(broken_segments_dir)),
                    session_id="20260315-223000-坏会话分段",
                    session_dir=str(broken_segments_dir),
                ),
            )
            broken_workspace.segments_jsonl.write_text("{bad json}\n", encoding="utf-8")

            summaries = service.list_session_summaries()

        self.assertEqual(3, len(summaries))
        broken = {
            summary.session_id: summary for summary in summaries if summary.status == "broken"
        }
        self.assertEqual(2, len(broken))
        self.assertIn("20260315-220000-坏会话元数据", broken)
        self.assertIn("20260315-223000-坏会话分段", broken)
        self.assertTrue(any(summary.status != "broken" for summary in summaries))

    def test_doctor_checks_mark_disabled_integrations_as_skip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_path = root / "ggml-base.bin"
            model_path.write_bytes(b"fake-model")
            service = AppService(root / "config.toml")
            service.save_settings(
                SettingsDraft(
                    ffmpeg_binary="/opt/homebrew/bin/ffmpeg",
                    whisper_binary="/Users/demo/whisper-server",
                    whisper_model=str(model_path),
                    obsidian_enabled=False,
                    llm_enabled=False,
                )
            )

            checks = {check.name: check for check in service.doctor_checks()}

        self.assertEqual("SKIP", checks["obsidian"].status)
        self.assertEqual("SKIP", checks["llm"].status)

    def test_doctor_checks_include_remote_health_when_remote_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_path = root / "ggml-base.bin"
            model_path.write_bytes(b"fake-model")
            service = AppService(root / "config.toml")
            service.save_settings(
                SettingsDraft(
                    ffmpeg_binary="/opt/homebrew/bin/ffmpeg",
                    whisper_binary="/Users/demo/whisper-server",
                    whisper_model=str(model_path),
                    obsidian_enabled=False,
                    llm_enabled=False,
                    remote_enabled=True,
                    remote_base_url="http://mini.local:8765",
                    remote_api_token="remote-token",
                )
            )
            remote_client = Mock()
            remote_client.health.return_value = {
                "status": "ok",
                "service": "live-note-remote",
                "speaker_enabled": False,
            }

            with patch("live_note.app.services.RemoteClient", return_value=remote_client):
                checks = {check.name: check for check in service.doctor_checks()}

        self.assertEqual("OK", checks["remote_api_token"].status)
        self.assertEqual("OK", checks["remote_health"].status)
        self.assertIn("http://mini.local:8765", checks["remote_health"].detail)
        remote_client.health.assert_called_once()

    def test_doctor_checks_include_speaker_runtime_and_model_checks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_path = root / "ggml-base.bin"
            segmentation_model = root / "segmentation.onnx"
            embedding_model = root / "embedding.onnx"
            model_path.write_bytes(b"fake-model")
            segmentation_model.write_bytes(b"seg")
            embedding_model.write_bytes(b"embed")
            service = AppService(root / "config.toml")
            service.save_settings(
                SettingsDraft(
                    ffmpeg_binary="/opt/homebrew/bin/ffmpeg",
                    whisper_binary="/Users/demo/whisper-server",
                    whisper_model=str(model_path),
                    obsidian_enabled=False,
                    llm_enabled=False,
                    speaker_enabled=True,
                    speaker_segmentation_model=str(segmentation_model),
                    speaker_embedding_model=str(embedding_model),
                )
            )

            def fake_module_available(name: str) -> bool:
                return name in {"sounddevice", "webrtcvad", "numpy", "sherpa_onnx"}

            with patch(
                "live_note.app.services._module_available",
                side_effect=fake_module_available,
            ):
                checks = {check.name: check for check in service.doctor_checks()}

        self.assertEqual("OK", checks["speaker_segmentation_model"].status)
        self.assertEqual("OK", checks["speaker_embedding_model"].status)
        self.assertEqual("OK", checks["speaker_numpy"].status)
        self.assertEqual("OK", checks["speaker_sherpa_onnx"].status)

    def test_doctor_checks_include_pyannote_runtime_and_token_checks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_path = root / "ggml-base.bin"
            model_path.write_bytes(b"fake-model")
            (root / ".env").write_text("PYANNOTE_AUTH_TOKEN=hf-token\n", encoding="utf-8")
            (root / "config.toml").write_text(
                "\n".join(
                    [
                        "[audio]",
                        "",
                        "[import]",
                        "",
                        "[refine]",
                        "",
                        "[whisper]",
                        'binary = "/Users/demo/whisper-server"',
                        f'model = "{model_path}"',
                        "",
                        "[obsidian]",
                        "enabled = false",
                        "",
                        "[llm]",
                        "enabled = false",
                        "",
                        "[speaker]",
                        "enabled = true",
                        'backend = "pyannote"',
                        'pyannote_model = "pyannote/speaker-diarization-community-1"',
                    ]
                ),
                encoding="utf-8",
            )
            service = AppService(root / "config.toml")

            def fake_module_available(name: str) -> bool:
                return name in {"sounddevice", "webrtcvad", "pyannote.audio"}

            with patch(
                "live_note.app.services._module_available",
                side_effect=fake_module_available,
            ):
                checks = {check.name: check for check in service.doctor_checks()}

        self.assertEqual("OK", checks["speaker_pyannote_model"].status)
        self.assertEqual("OK", checks["speaker_pyannote_token"].status)
        self.assertEqual("OK", checks["speaker_pyannote_audio"].status)

    def test_list_session_summaries_clears_failed_count_after_segment_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_path = root / "ggml-base.bin"
            model_path.write_bytes(b"fake-model")
            service = AppService(root / "config.toml")
            service.save_settings(
                SettingsDraft(
                    ffmpeg_binary="/opt/homebrew/bin/ffmpeg",
                    whisper_binary="/Users/demo/whisper-server",
                    whisper_model=str(model_path),
                )
            )

            session_dir = root / ".live-note" / "sessions" / "20260315-210500-机器学习"
            workspace = SessionWorkspace.create(session_dir, sample_metadata(str(session_dir)))
            wav_path = workspace.next_wav_path("seg-00001")
            wav_path.parent.mkdir(parents=True, exist_ok=True)
            wav_path.write_bytes(b"wav")
            workspace.record_segment_created("seg-00001", 0, 2000, wav_path)
            workspace.record_segment_error("seg-00001", 0, 2000, "timeout")
            workspace.record_segment_text("seg-00001", 0, 2000, "第一段")

            summaries = service.list_session_summaries()

        self.assertEqual(1, len(summaries))
        self.assertEqual(1, summaries[0].transcribed_count)
        self.assertEqual(0, summaries[0].failed_count)
        self.assertIsNone(summaries[0].latest_error)

    def test_validate_settings_rejects_auto_refine_without_session_wav(self) -> None:
        service = AppService(Path("/tmp/config.toml"))

        errors = service.validate_settings(
            SettingsDraft(
                ffmpeg_binary="/opt/homebrew/bin/ffmpeg",
                whisper_binary="/Users/demo/whisper-server",
                whisper_model="/tmp/model.bin",
                save_session_wav=False,
                refine_enabled=True,
                refine_auto_after_live=True,
            )
        )

        self.assertIn("开启自动离线精修前，必须同时保存整场 WAV。", errors)
