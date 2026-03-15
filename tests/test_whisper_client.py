from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from live_note.config import WhisperConfig
from live_note.transcribe.whisper import (
    WhisperInferenceClient,
    WhisperServerProcess,
    with_language_override,
)


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = body

    def read(self) -> bytes:
        return self.body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, exc_type, exc, exc_tb) -> None:
        return None


class WhisperInferenceClientTests(unittest.TestCase):
    def test_transcribe_posts_multipart_to_inference(self) -> None:
        captured = {}

        def fake_urlopen(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse("转写成功".encode())

        with tempfile.TemporaryDirectory() as temp_dir:
            wav_path = Path(temp_dir) / "sample.wav"
            wav_path.write_bytes(b"RIFF....WAVE")
            client = WhisperInferenceClient(
                WhisperConfig(
                    binary="whisper-server",
                    model=Path(temp_dir) / "model.bin",
                    host="127.0.0.1",
                    port=8178,
                    threads=4,
                    language="zh",
                    translate=False,
                    request_timeout_seconds=5,
                    startup_timeout_seconds=5,
                )
            )

            with patch("live_note.transcribe.whisper.urlopen", side_effect=fake_urlopen):
                text = client.transcribe(wav_path)

        request = captured["request"]
        self.assertEqual("转写成功", text)
        self.assertEqual("http://127.0.0.1:8178/inference", request.full_url)
        self.assertEqual(5, captured["timeout"])
        self.assertIn(b'name="response_format"', request.data)
        self.assertIn(b'name="no_timestamps"', request.data)
        self.assertIn(b'name="language"', request.data)
        self.assertIn(b"zh", request.data)
        self.assertIn(b'name="file"', request.data)

    def test_transcribe_includes_translate_flag_when_enabled(self) -> None:
        captured = {}

        def fake_urlopen(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse(b"translated")

        with tempfile.TemporaryDirectory() as temp_dir:
            wav_path = Path(temp_dir) / "sample.wav"
            wav_path.write_bytes(b"RIFF....WAVE")
            client = WhisperInferenceClient(
                WhisperConfig(
                    binary="whisper-server",
                    model=Path(temp_dir) / "model.bin",
                    host="127.0.0.1",
                    port=8178,
                    threads=4,
                    language="auto",
                    translate=True,
                    request_timeout_seconds=5,
                    startup_timeout_seconds=5,
                )
            )

            with patch("live_note.transcribe.whisper.urlopen", side_effect=fake_urlopen):
                text = client.transcribe(wav_path)

        self.assertEqual("translated", text)
        self.assertIn(b'name="translate"', captured["request"].data)
        self.assertIn(b"true", captured["request"].data)

    def test_transcribe_includes_prompt_when_provided(self) -> None:
        captured = {}

        def fake_urlopen(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse("整理成功".encode())

        with tempfile.TemporaryDirectory() as temp_dir:
            wav_path = Path(temp_dir) / "sample.wav"
            wav_path.write_bytes(b"RIFF....WAVE")
            client = WhisperInferenceClient(
                WhisperConfig(
                    binary="whisper-server",
                    model=Path(temp_dir) / "model.bin",
                    host="127.0.0.1",
                    port=8178,
                    threads=4,
                    language="zh",
                    translate=False,
                    request_timeout_seconds=5,
                    startup_timeout_seconds=5,
                )
            )

            with patch("live_note.transcribe.whisper.urlopen", side_effect=fake_urlopen):
                text = client.transcribe(wav_path, prompt="请使用简体中文输出")

        self.assertEqual("整理成功", text)
        self.assertIn(b'name="prompt"', captured["request"].data)
        self.assertIn("请使用简体中文输出".encode(), captured["request"].data)

    def test_server_process_uses_long_host_and_port_flags(self) -> None:
        captured: dict[str, object] = {}

        class FakeProcess:
            def poll(self):
                return None

            def terminate(self) -> None:
                return None

            def wait(self, timeout=None) -> int:
                del timeout
                return 0

        def fake_popen(command, stdout, stderr, text):
            captured["command"] = command
            captured["stdout"] = stdout
            captured["stderr"] = stderr
            captured["text"] = text
            return FakeProcess()

        with tempfile.TemporaryDirectory() as temp_dir:
            model_path = Path(temp_dir) / "model.bin"
            model_path.write_bytes(b"model")
            log_path = Path(temp_dir) / "logs.txt"
            binary_path = Path(temp_dir) / "whisper-server"
            binary_path.write_text("", encoding="utf-8")
            process = WhisperServerProcess(
                WhisperConfig(
                    binary=str(binary_path),
                    model=model_path,
                    host="127.0.0.1",
                    port=8178,
                    threads=4,
                    language="zh",
                    translate=False,
                    request_timeout_seconds=5,
                    startup_timeout_seconds=5,
                ),
                log_path=log_path,
            )

            with (
                patch("live_note.transcribe.whisper.subprocess.Popen", side_effect=fake_popen),
                patch.object(WhisperServerProcess, "_wait_until_ready", return_value=None),
            ):
                process.start()
                process.stop()

        command = captured["command"]
        self.assertIn("--host", command)
        self.assertIn("--port", command)
        self.assertIn("-l", command)
        self.assertIn("zh", command)
        self.assertNotIn("-host", command)
        self.assertNotIn("-port", command)

    def test_server_process_uses_auto_language_flag_instead_of_detect_language(self) -> None:
        captured: dict[str, object] = {}

        class FakeProcess:
            def poll(self):
                return None

            def terminate(self) -> None:
                return None

            def wait(self, timeout=None) -> int:
                del timeout
                return 0

        def fake_popen(command, stdout, stderr, text):
            captured["command"] = command
            return FakeProcess()

        with tempfile.TemporaryDirectory() as temp_dir:
            model_path = Path(temp_dir) / "model.bin"
            model_path.write_bytes(b"model")
            log_path = Path(temp_dir) / "logs.txt"
            binary_path = Path(temp_dir) / "whisper-server"
            binary_path.write_text("", encoding="utf-8")
            process = WhisperServerProcess(
                WhisperConfig(
                    binary=str(binary_path),
                    model=model_path,
                    host="127.0.0.1",
                    port=8178,
                    threads=4,
                    language="auto",
                    translate=False,
                    request_timeout_seconds=5,
                    startup_timeout_seconds=5,
                ),
                log_path=log_path,
            )

            with (
                patch("live_note.transcribe.whisper.subprocess.Popen", side_effect=fake_popen),
                patch.object(WhisperServerProcess, "_wait_until_ready", return_value=None),
            ):
                process.start()
                process.stop()

        command = captured["command"]
        self.assertIn("-l", command)
        self.assertIn("auto", command)
        self.assertNotIn("-dl", command)

    def test_server_process_cleans_up_when_startup_check_fails(self) -> None:
        class FakeProcess:
            def __init__(self) -> None:
                self.terminated = False

            def poll(self):
                return None

            def terminate(self) -> None:
                self.terminated = True

            def wait(self, timeout=None) -> int:
                del timeout
                return 0

        process_holder: dict[str, FakeProcess] = {}

        def fake_popen(command, stdout, stderr, text):
            del command, stdout, stderr, text
            process_holder["process"] = FakeProcess()
            return process_holder["process"]

        with tempfile.TemporaryDirectory() as temp_dir:
            model_path = Path(temp_dir) / "model.bin"
            model_path.write_bytes(b"model")
            log_path = Path(temp_dir) / "logs.txt"
            binary_path = Path(temp_dir) / "whisper-server"
            binary_path.write_text("", encoding="utf-8")
            process = WhisperServerProcess(
                WhisperConfig(
                    binary=str(binary_path),
                    model=model_path,
                    host="127.0.0.1",
                    port=8178,
                    threads=4,
                    language="zh",
                    translate=False,
                    request_timeout_seconds=5,
                    startup_timeout_seconds=5,
                ),
                log_path=log_path,
            )

            with (
                patch("live_note.transcribe.whisper.subprocess.Popen", side_effect=fake_popen),
                patch.object(
                    WhisperServerProcess,
                    "_wait_until_ready",
                    side_effect=RuntimeError("boom"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "boom"):
                    process.start()

        self.assertTrue(process_holder["process"].terminated)
        self.assertIsNone(process.process)
        self.assertIsNone(process._log_handle)

    def test_with_language_override_returns_session_specific_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = WhisperConfig(
                binary="whisper-server",
                model=Path(temp_dir) / "model.bin",
                host="127.0.0.1",
                port=8178,
                threads=4,
                language="auto",
                translate=False,
                request_timeout_seconds=5,
                startup_timeout_seconds=5,
            )

            overridden = with_language_override(config, "zh")

        self.assertEqual("zh", overridden.language)
        self.assertEqual("auto", config.language)
