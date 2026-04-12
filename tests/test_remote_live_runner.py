from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from live_note.session_workspace import SessionWorkspace
from live_note.audio.capture import InputDevice
from live_note.config import (
    AppConfig,
    AudioConfig,
    ImportConfig,
    LlmConfig,
    ObsidianConfig,
    RefineConfig,
    RemoteConfig,
    WhisperConfig,
)
from live_note.domain import AudioFrame
from live_note.remote.client import RemoteClientError
from live_note.remote.live_runner import RemoteLiveRunner, _RemoteAudioBatcher


def build_config(root: Path) -> AppConfig:
    model_path = root / "ggml-large-v3.bin"
    model_path.write_bytes(b"fake-model")
    return AppConfig(
        audio=AudioConfig(queue_size=16),
        importer=ImportConfig(),
        refine=RefineConfig(),
        whisper=WhisperConfig(
            binary="/Users/ender/whisper.cpp/build/bin/whisper-server",
            model=model_path,
            language="zh",
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
        remote=RemoteConfig(
            enabled=True,
            base_url="http://mini.local:8765",
            api_token="remote-token",
            timeout_seconds=2,
            live_chunk_ms=20,
        ),
        root_dir=root,
    )


def _session_started_payload(*, status: str = "ingesting") -> dict[str, object]:
    return {
        "type": "session_started",
        "runtime_status": status,
        "metadata": {
            "session_id": "remote-1",
            "title": "产品周会",
            "kind": "meeting",
            "input_mode": "live",
            "source_label": "BlackHole 2ch",
            "source_ref": "1",
            "language": "zh",
            "started_at": "2026-04-10T10:00:00+00:00",
            "transcript_note_path": "Remote/Transcripts/产品周会.md",
            "structured_note_path": "Remote/Summaries/产品周会.md",
            "session_dir": "",
            "status": status,
            "transcript_source": "live",
            "refine_status": "pending",
            "execution_target": "remote",
            "remote_session_id": "remote-1",
            "speaker_status": "disabled",
        },
    }


def _artifacts_payload(*, status: str = "handoff_committed") -> dict[str, object]:
    return {
        "runtime_status": status,
        "metadata": {
            **_session_started_payload(status=status)["metadata"],
            "status": status,
        },
        "entries": [
            {
                "segment_id": "seg-00001",
                "started_ms": 0,
                "ended_ms": 500,
                "text": "今天先过一下排期。",
                "speaker_label": None,
            }
        ],
        "transcript_content": "# 原文\n",
        "structured_content": None,
    }


class _FakeRemoteLiveConnection:
    def __init__(
        self,
        *,
        order: list[str] | None = None,
        startup_delay_seconds: float = 0.0,
    ) -> None:
        self.order = order if order is not None else []
        self.startup_delay_seconds = startup_delay_seconds
        self.sent_audio: list[bytes] = []
        self.sent_controls: list[str] = []
        self._stop_sent = threading.Event()

    def __enter__(self) -> _FakeRemoteLiveConnection:
        return self

    def __exit__(self, exc_type, exc, exc_tb) -> None:
        return None

    def send_audio(self, pcm16: bytes) -> None:
        self.sent_audio.append(pcm16)

    def send_control(self, command: str) -> None:
        self.sent_controls.append(command)
        if command == "stop":
            self._stop_sent.set()

    def iter_events(self):
        if self.startup_delay_seconds:
            time.sleep(self.startup_delay_seconds)
        self.order.append("session_started")
        yield _session_started_payload()
        self._stop_sent.wait(1)
        yield {
            "type": "stop_accepted",
            "session_id": "remote-1",
            "message": "远端已接受停止请求，正在封口与排空。",
        }
        yield {
            "type": "handoff_committed",
            "session_id": "remote-1",
            "task_id": "task-postprocess-1",
            "message": "后台整理任务已完成 durable handoff。",
        }
        yield {
            "type": "completed",
            "session_id": "remote-1",
        }


class _FakeRemoteClient:
    def __init__(self, connection: _FakeRemoteLiveConnection) -> None:
        self.connection = connection
        self.connect_payload: dict[str, object] | None = None
        self.artifact_calls: list[str] = []

    def connect_live(self, payload: dict[str, object]):
        self.connect_payload = dict(payload)
        return self.connection

    def get_session_artifacts(self, session_id: str) -> dict[str, object]:
        self.artifact_calls.append(session_id)
        return _artifacts_payload()


class _FakeAudioCaptureService:
    def __init__(
        self,
        _config,
        _device,
        frame_queue,
        *,
        order: list[str] | None = None,
    ) -> None:
        self._frame_queue = frame_queue
        self._order = order if order is not None else []
        self._alive = False
        self._paused = False
        self.error = None

    @property
    def is_alive(self) -> bool:
        return self._alive

    @property
    def is_paused(self) -> bool:
        return self._paused

    def start(self) -> None:
        self._order.append("capture_start")
        self._alive = True
        self._frame_queue.put(
            AudioFrame(
                started_ms=0,
                ended_ms=30,
                pcm16=b"\x01\x00\x02\x00",
            )
        )

    def stop(self) -> None:
        self._alive = False

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def join(self, timeout: float | None = None) -> None:
        return None

    def set_level_callback(self, callback) -> None:
        self._level_callback = callback


class RemoteLiveRunnerTests(unittest.TestCase):
    def test_remote_audio_batcher_buffers_until_threshold_and_flushes_partial(self) -> None:
        batcher = _RemoteAudioBatcher(chunk_ms=60)

        first = batcher.push(AudioFrame(started_ms=0, ended_ms=30, pcm16=b"\x01\x00"))
        second = batcher.push(AudioFrame(started_ms=30, ended_ms=60, pcm16=b"\x02\x00"))
        batcher.push(AudioFrame(started_ms=60, ended_ms=90, pcm16=b"\x03\x00"))
        tail = batcher.flush()

        self.assertIsNone(first)
        self.assertEqual(b"\x01\x00\x02\x00", second)
        self.assertEqual(b"\x03\x00", tail)

    def test_apply_session_started_requires_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = RemoteLiveRunner(
                config=build_config(Path(temp_dir)),
                title="产品周会",
                source="1",
                kind="meeting",
            )

            with self.assertRaisesRegex(RemoteClientError, "缺少 metadata"):
                runner._apply_session_started({"type": "session_started", "session_id": "remote-1"})

    def test_apply_session_started_creates_local_workspace_with_remote_note_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runner = RemoteLiveRunner(
                config=build_config(root),
                title="产品周会",
                source="1",
                kind="meeting",
            )

            runner._apply_session_started(_session_started_payload())

            assert runner.workspace is not None
            metadata = runner.workspace.read_session()
            self.assertEqual("remote-1", metadata.session_id)
            self.assertEqual("Remote/Transcripts/产品周会.md", metadata.transcript_note_path)
            self.assertEqual("Remote/Summaries/产品周会.md", metadata.structured_note_path)
            self.assertEqual("remote", metadata.execution_target)
            self.assertTrue(runner.workspace.session_toml.exists())

    def test_run_waits_for_session_started_then_stops_and_syncs_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = build_config(root)
            order: list[str] = []
            connection = _FakeRemoteLiveConnection(order=order, startup_delay_seconds=0.1)
            client = _FakeRemoteClient(connection)
            events: list[tuple[str, str]] = []
            runner = RemoteLiveRunner(
                config=config,
                title="产品周会",
                source="1",
                kind="meeting",
                on_progress=lambda event: events.append((event.stage, event.message)),
                client=client,
            )
            runner.request_stop()

            def capture_factory(audio_config, device, frame_queue):
                return _FakeAudioCaptureService(
                    audio_config,
                    device,
                    frame_queue,
                    order=order,
                )

            with (
                patch(
                    "live_note.remote.live_runner.resolve_input_device",
                    return_value=InputDevice(
                        index=1,
                        name="BlackHole 2ch",
                        max_input_channels=2,
                        default_samplerate=16000.0,
                    ),
                ),
                patch(
                    "live_note.remote.live_runner.AudioCaptureService",
                    side_effect=capture_factory,
                ),
                patch(
                    "live_note.remote.live_runner.DEFAULT_REMOTE_LIVE_SNAPSHOT_POLL_SECONDS",
                    1_000_000_000.0,
                ),
            ):
                exit_code = runner.run()

            self.assertEqual(0, exit_code)
            self.assertEqual(["session_started", "capture_start"], order)
            self.assertEqual(["stop"], connection.sent_controls)
            self.assertGreaterEqual(client.artifact_calls.count("remote-1"), 1)
            self.assertEqual("BlackHole 2ch", client.connect_payload["source_label"])
            workspace = SessionWorkspace.load(root / ".live-note" / "sessions" / "remote-1")
            entries = workspace.transcript_entries()
            self.assertEqual(["今天先过一下排期。"], [item.text for item in entries])
            self.assertIn(("listening", "已连接远端录音服务。"), events)
            self.assertIn(("stopping", "远端已接受停止请求，正在封口与排空。"), events)
            self.assertIn(("stopping", "后台整理任务已完成 durable handoff。"), events)
