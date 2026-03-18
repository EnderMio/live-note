from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from live_note.app.journal import SessionWorkspace
from live_note.app.services import AppService, SettingsDraft
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
        self.assertEqual("ws://127.0.0.1:10095", reloaded.funasr.base_url)
        self.assertTrue(reloaded.speaker.enabled)
        self.assertEqual("/models/segmentation.onnx", str(reloaded.speaker.segmentation_model))
        self.assertEqual("/models/embedding.onnx", str(reloaded.speaker.embedding_model))
        self.assertAlmostEqual(0.42, reloaded.speaker.cluster_threshold)
        self.assertTrue(draft.remote_enabled)
        self.assertEqual("remote-token", draft.remote_api_token)

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

    def test_refine_remote_session_requests_remote_refine_and_syncs_artifacts(self) -> None:
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
            workspace = SessionWorkspace.create(
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
            remote_artifacts = {
                "metadata": {
                    "session_id": "remote-1",
                    "title": "产品周会",
                    "kind": "meeting",
                    "input_mode": "live",
                    "source_label": "BlackHole 2ch",
                    "source_ref": "1",
                    "language": "zh",
                    "started_at": "2026-03-18T10:00:00+00:00",
                    "status": "completed",
                    "transcript_source": "refined",
                    "refine_status": "done",
                    "execution_target": "remote",
                    "remote_session_id": "remote-1",
                    "speaker_status": "done",
                },
                "entries": [],
                "has_session_audio": True,
            }
            client = Mock()
            client.get_artifacts.return_value = remote_artifacts

            with (
                patch("live_note.app.services.RemoteClient", return_value=client),
                patch(
                    "live_note.app.services.apply_remote_artifacts",
                    return_value=workspace.read_session(),
                ) as sync_mock,
            ):
                exit_code = service.refine("remote-1")

        self.assertEqual(0, exit_code)
        client.refine.assert_called_once_with("remote-1")
        client.get_artifacts.assert_called_once_with("remote-1")
        sync_mock.assert_called_once()
        self.assertEqual("remote-1", sync_mock.call_args.args[1].session_id)
        self.assertEqual([], sync_mock.call_args.args[2])

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
