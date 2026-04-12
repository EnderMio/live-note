from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from live_note.config import load_config, render_config

TEST_WHISPER_BINARY = "/test-bin/whisper-server"
TEST_REMOTE_BASE_URL = "http://example.invalid:8765"


class RemoteConfigTests(unittest.TestCase):
    def test_load_config_reads_remote_runtime_sections(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_path = root / "ggml-large-v3.bin"
            model_path.write_bytes(b"model")
            config_path = root / "config.toml"
            config_path.write_text(
                "\n".join(
                    [
                        "[audio]",
                        "sample_rate = 16000",
                        "",
                        "[import]",
                        'ffmpeg_binary = "ffmpeg"',
                        "",
                        "[refine]",
                        "enabled = true",
                        "auto_after_live = true",
                        "",
                        "[whisper]",
                        f'binary = "{TEST_WHISPER_BINARY}"',
                        f'model = "{model_path}"',
                        "",
                        "[obsidian]",
                        "enabled = false",
                        "",
                        "[llm]",
                        "enabled = false",
                        "",
                        "[remote]",
                        "enabled = true",
                        f'base_url = "{TEST_REMOTE_BASE_URL}"',
                        'api_token = "remote-token"',
                        "timeout_seconds = 22",
                        "ws_ping_interval_seconds = 35",
                        "ws_ping_timeout_seconds = 55",
                        "live_chunk_ms = 320",
                        "",
                        "[serve]",
                        'host = "0.0.0.0"',
                        "port = 18765",
                        'api_token = "server-token"',
                        "ws_ping_interval_seconds = 30",
                        "ws_ping_timeout_seconds = 45",
                        "",
                        "[funasr]",
                        "enabled = true",
                        'base_url = "ws://127.0.0.1:10095"',
                        'mode = "2pass"',
                        "use_itn = false",
                        "",
                        "[speaker]",
                        "enabled = true",
                        'segmentation_model = "/models/seg.onnx"',
                        'embedding_model = "/models/embed.onnx"',
                        "expected_speakers = 3",
                        "cluster_threshold = 0.61",
                        "min_duration_on = 0.4",
                        "min_duration_off = 0.7",
                    ]
                ),
                encoding="utf-8",
            )

            config = load_config(config_path)

        self.assertTrue(config.remote.enabled)
        self.assertEqual(TEST_REMOTE_BASE_URL, config.remote.base_url)
        self.assertEqual("remote-token", config.remote.api_token)
        self.assertEqual(22, config.remote.timeout_seconds)
        self.assertEqual(35, config.remote.ws_ping_interval_seconds)
        self.assertEqual(55, config.remote.ws_ping_timeout_seconds)
        self.assertEqual(320, config.remote.live_chunk_ms)
        self.assertEqual("0.0.0.0", config.serve.host)
        self.assertEqual(18765, config.serve.port)
        self.assertEqual("server-token", config.serve.api_token)
        self.assertEqual(30, config.serve.ws_ping_interval_seconds)
        self.assertEqual(45, config.serve.ws_ping_timeout_seconds)
        self.assertTrue(config.funasr.enabled)
        self.assertEqual("ws://127.0.0.1:10095", config.funasr.base_url)
        self.assertEqual("2pass", config.funasr.mode)
        self.assertFalse(config.funasr.use_itn)
        self.assertTrue(config.speaker.enabled)
        self.assertEqual(Path("/models/seg.onnx"), config.speaker.segmentation_model)
        self.assertEqual(Path("/models/embed.onnx"), config.speaker.embedding_model)
        self.assertEqual(3, config.speaker.expected_speakers)
        self.assertAlmostEqual(0.61, config.speaker.cluster_threshold)
        self.assertAlmostEqual(0.4, config.speaker.min_duration_on)
        self.assertAlmostEqual(0.7, config.speaker.min_duration_off)

    def test_render_config_includes_remote_runtime_sections(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_path = root / "ggml-large-v3.bin"
            model_path.write_bytes(b"model")
            config_path = root / "config.toml"
            config_path.write_text(
                "\n".join(
                    [
                        "[audio]",
                        "",
                        "[import]",
                        "",
                        "[refine]",
                        "",
                        "[whisper]",
                        f'binary = "{TEST_WHISPER_BINARY}"',
                        f'model = "{model_path}"',
                        "",
                        "[obsidian]",
                        "",
                        "[llm]",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            rendered = render_config(load_config(config_path))

        self.assertIn("[remote]", rendered)
        self.assertIn("[serve]", rendered)
        self.assertIn("[funasr]", rendered)
        self.assertIn("[speaker]", rendered)
        self.assertIn("ws_ping_interval_seconds = 20", rendered)
        self.assertIn("ws_ping_timeout_seconds = 20", rendered)
        self.assertIn("expected_speakers = 0", rendered)

    def test_load_config_reads_pyannote_speaker_backend_and_env_token(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_path = root / "ggml-large-v3.bin"
            model_path.write_bytes(b"model")
            config_path = root / "config.toml"
            env_path = root / ".env"
            env_path.write_text("PYANNOTE_AUTH_TOKEN=hf-token\n", encoding="utf-8")
            config_path.write_text(
                "\n".join(
                    [
                        "[audio]",
                        "",
                        "[import]",
                        "",
                        "[refine]",
                        "",
                        "[whisper]",
                        f'binary = "{TEST_WHISPER_BINARY}"',
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

            config = load_config(config_path, env_path)

        self.assertTrue(config.speaker.enabled)
        self.assertEqual("pyannote", config.speaker.backend)
        self.assertEqual(
            "pyannote/speaker-diarization-community-1",
            config.speaker.pyannote_model,
        )
        self.assertEqual("hf-token", config.speaker.pyannote_auth_token)
