from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

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

    def test_save_settings_preserves_existing_env_keys(self) -> None:
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

        self.assertIn("OPENAI_API_KEY=openai-token", env_text)
        self.assertIn("EXTRA_SETTING=keep-me", env_text)
        self.assertIn("LLM_API_KEY=fallback-token", env_text)
        self.assertEqual("openai-token", reloaded.llm.api_key)

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
