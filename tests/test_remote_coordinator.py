from __future__ import annotations

import asyncio
import json
import queue
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from live_note.app.coordinator import create_session_metadata
from live_note.app.journal import SessionWorkspace
from live_note.app.remote_coordinator import RemoteLiveCoordinator, _RemoteAudioBatcher
from live_note.app.remote_tasks import load_remote_tasks
from live_note.audio.capture import InputLevel
from live_note.config import (
    AppConfig,
    AudioConfig,
    FunAsrConfig,
    ImportConfig,
    LlmConfig,
    ObsidianConfig,
    RefineConfig,
    RemoteConfig,
    ServeConfig,
    SpeakerConfig,
    WhisperConfig,
)
from live_note.domain import AudioFrame, TranscriptEntry
from live_note.obsidian.renderer import build_transcript_note
from live_note.remote.protocol import LiveStartRequest
from live_note.remote.service import (
    RemoteLiveSessionRunner,
    RemoteSessionService,
    _FunAsrAudioBatcher,
    _FunAsrDraftTracker,
    _run_remote_postprocess,
)
from live_note.transcribe.funasr import FunAsrMessage
from live_note.utils import compact_text


class _FakeFunAsrConnection:
    def __init__(
        self,
        responses: list[object] | None = None,
        *,
        timeout_delay_seconds: float = 0.0,
    ) -> None:
        self.sent_audio: list[bytes] = []
        self.stop_sent = False
        self.closed = False
        self._responses = list(responses or [])
        self._timeout_delay_seconds = timeout_delay_seconds

    def send_audio(self, payload: bytes) -> None:
        self.sent_audio.append(payload)

    def send_stop(self) -> None:
        self.stop_sent = True

    def recv_message(self, timeout: float | None = None) -> FunAsrMessage:
        if not self._responses:
            if self._timeout_delay_seconds > 0:
                time.sleep(self._timeout_delay_seconds)
            raise TimeoutError(timeout)
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            if self._timeout_delay_seconds > 0:
                time.sleep(self._timeout_delay_seconds)
            raise item
        assert isinstance(item, FunAsrMessage)
        return item

    def close(self) -> None:
        self.closed = True


class _FakeRemoteLiveConnection:
    def __init__(
        self,
        *,
        stop_received_payload: dict[str, object] | None = None,
        completed_payload: dict[str, object] | None = None,
    ) -> None:
        self.sent_audio: list[bytes] = []
        self.sent_controls: list[str] = []
        self._stop_received = threading.Event()
        self._stop_received_payload = stop_received_payload or {
            "type": "stop_received",
            "session_id": "remote-1",
            "message": "远端已确认停止，后台整理任务已创建。",
            "postprocess_task": {
                "task_id": "task-post-1",
                "server_id": "server-1",
                "action": "postprocess",
                "label": "后台整理",
                "session_id": "remote-1",
                "status": "running",
                "stage": "handoff",
                "message": "后台整理已接管。",
                "result_version": 0,
                "can_cancel": False,
            },
        }
        self._completed_payload = completed_payload or {
            "type": "completed",
            "session_id": "remote-1",
        }

    def __enter__(self) -> _FakeRemoteLiveConnection:
        return self

    def __exit__(self, exc_type, exc, exc_tb) -> None:
        return None

    def send_audio(self, payload: bytes) -> None:
        self.sent_audio.append(payload)

    def send_control(self, command: str) -> None:
        self.sent_controls.append(command)
        if command == "stop":
            self._stop_received.set()

    def iter_events(self):
        yield {
            "type": "session_started",
            "session_id": "remote-1",
            "started_at": "2026-03-18T10:00:00+00:00",
            "title": "产品周会",
            "kind": "meeting",
            "language": "zh",
            "source_label": "BlackHole 2ch",
            "source_ref": "1",
        }
        self._stop_received.wait(1)
        yield dict(self._stop_received_payload)
        yield dict(self._completed_payload)


class _DelayedSessionStartedConnection(_FakeRemoteLiveConnection):
    def __init__(self, *, startup_delay_seconds: float = 0.2) -> None:
        super().__init__()
        self._startup_delay_seconds = startup_delay_seconds

    def iter_events(self):
        time.sleep(self._startup_delay_seconds)
        yield {
            "type": "session_started",
            "session_id": "remote-1",
            "started_at": "2026-03-18T10:00:00+00:00",
            "title": "产品周会",
            "kind": "meeting",
            "language": "zh",
            "source_label": "BlackHole 2ch",
            "source_ref": "1",
        }
        self._stop_received.wait(1)
        yield dict(self._completed_payload)


class _DisconnectAfterStartConnection(_FakeRemoteLiveConnection):
    def iter_events(self):
        yield {
            "type": "session_started",
            "session_id": "remote-1",
            "started_at": "2026-03-18T10:00:00+00:00",
            "title": "产品周会",
            "kind": "meeting",
            "language": "zh",
            "source_label": "BlackHole 2ch",
            "source_ref": "1",
        }
        self._stop_received.wait(0.2)
        return


class _DisconnectAfterStopAckConnection(_FakeRemoteLiveConnection):
    def iter_events(self):
        yield {
            "type": "session_started",
            "session_id": "remote-1",
            "started_at": "2026-03-18T10:00:00+00:00",
            "title": "产品周会",
            "kind": "meeting",
            "language": "zh",
            "source_label": "BlackHole 2ch",
            "source_ref": "1",
        }
        self._stop_received.wait(1)
        yield {
            "type": "stop_received",
            "session_id": "remote-1",
            "message": "远端已确认停止，等待后台整理接管。",
        }
        return


class _StopMustArrivePromptlyConnection(_FakeRemoteLiveConnection):
    def __init__(self, *, wait_seconds: float = 0.2) -> None:
        super().__init__()
        self._wait_seconds = wait_seconds

    def iter_events(self):
        yield {
            "type": "session_started",
            "session_id": "remote-1",
            "started_at": "2026-03-18T10:00:00+00:00",
            "title": "产品周会",
            "kind": "meeting",
            "language": "zh",
            "source_label": "BlackHole 2ch",
            "source_ref": "1",
        }
        if not self._stop_received.wait(self._wait_seconds):
            raise RuntimeError("stop not sent promptly")
        yield dict(self._completed_payload)


class _FakeStartupWebSocket:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload
        self.accepted = False
        self.closed = False
        self.sent_payloads: list[dict[str, object]] = []

    async def accept(self) -> None:
        self.accepted = True

    async def receive_json(self) -> dict[str, object]:
        return dict(self._payload)

    async def send_json(self, payload: dict[str, object]) -> None:
        self.sent_payloads.append(dict(payload))

    async def close(self) -> None:
        self.closed = True


class _SequencedLiveWebSocket:
    def __init__(self, start_payload: dict[str, object], messages: list[dict[str, object]]) -> None:
        self._start_payload = start_payload
        self._messages = list(messages)
        self.accepted = False
        self.closed = False
        self.sent_payloads: list[dict[str, object]] = []

    async def accept(self) -> None:
        self.accepted = True

    async def receive_json(self) -> dict[str, object]:
        return dict(self._start_payload)

    async def receive(self) -> dict[str, object]:
        if self._messages:
            return dict(self._messages.pop(0))
        return {"type": "websocket.disconnect"}

    async def send_json(self, payload: dict[str, object]) -> None:
        self.sent_payloads.append(dict(payload))

    async def close(self) -> None:
        self.closed = True


class _FailingSendWebSocket(_SequencedLiveWebSocket):
    def __init__(
        self,
        start_payload: dict[str, object],
        messages: list[dict[str, object]],
        *,
        fail_types: set[str],
    ) -> None:
        super().__init__(start_payload, messages)
        self._fail_types = set(fail_types)

    async def send_json(self, payload: dict[str, object]) -> None:
        payload_type = str(payload.get("type", ""))
        if payload_type in self._fail_types:
            raise RuntimeError(f"send failed for {payload_type}")
        await super().send_json(payload)


class _QueueingRunner:
    def __init__(self) -> None:
        self.enqueued_audio: list[bytes] = []
        self.stop_requested = False
        self.pause_requested = False
        self.resume_requested = False
        self.session_id = "remote-1"
        self.entries: list[TranscriptEntry] = []
        self.failure_message: str | None = None
        self.postprocess_task_payload: dict[str, object] | None = None
        self.on_event = None

    @property
    def is_alive(self) -> bool:
        return False

    def enqueue_audio_bytes(self, payload: bytes) -> None:
        self.enqueued_audio.append(payload)
        return True

    def ensure_postprocess_task_payload(self):
        return self.postprocess_task_payload

    def request_stop(self) -> None:
        self.stop_requested = True

    def request_pause(self) -> None:
        self.pause_requested = True

    def request_resume(self) -> None:
        self.resume_requested = True


class _FakeRemoteClient:
    def __init__(self, connection: _FakeRemoteLiveConnection) -> None:
        self.connection = connection
        self.artifact_calls: list[str] = []

    def connect_live(self, payload):
        return self.connection

    def get_session_artifacts(self, session_id: str) -> dict[str, object]:
        self.artifact_calls.append(session_id)
        return {
            "session_id": session_id,
            "metadata": {
                "session_id": session_id,
                "title": "产品周会",
                "kind": "meeting",
                "input_mode": "live",
                "source_label": "BlackHole 2ch",
                "source_ref": "1",
                "language": "zh",
                "started_at": "2026-03-18T10:00:00+00:00",
                "status": "completed",
                "transcript_source": "live",
                "refine_status": "pending",
                "execution_target": "remote",
                "remote_session_id": session_id,
                "speaker_status": "disabled",
            },
            "entries": [],
            "has_session_audio": False,
        }


class _FakeAudioCaptureService:
    def __init__(self, _config, _device, frame_queue) -> None:
        self.frame_queue = frame_queue
        self.error = None
        self._alive = False
        self._paused = False

    def start(self) -> None:
        self._alive = True
        self.frame_queue.put(AudioFrame(started_ms=0, ended_ms=120, pcm16=b"a" * 8))
        self.frame_queue.put(AudioFrame(started_ms=120, ended_ms=240, pcm16=b"b" * 8))

    def stop(self) -> None:
        self._alive = False

    def join(self, timeout: float | None = None) -> None:
        return None

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    @property
    def is_alive(self) -> bool:
        return self._alive

    @property
    def is_paused(self) -> bool:
        return self._paused


class RemoteCoordinatorTests(unittest.TestCase):
    def test_remote_audio_batcher_emits_buffer_once_chunk_threshold_reached(self) -> None:
        batcher = _RemoteAudioBatcher(chunk_ms=240)

        first = batcher.push(AudioFrame(started_ms=0, ended_ms=120, pcm16=b"a" * 8))
        second = batcher.push(AudioFrame(started_ms=120, ended_ms=240, pcm16=b"b" * 8))

        self.assertIsNone(first)
        self.assertEqual(b"a" * 8 + b"b" * 8, second)
        self.assertIsNone(batcher.flush())

    def test_remote_audio_batcher_flushes_partial_buffer(self) -> None:
        batcher = _RemoteAudioBatcher(chunk_ms=240)

        batcher.push(AudioFrame(started_ms=0, ended_ms=90, pcm16=b"a" * 8))

        self.assertEqual(b"a" * 8, batcher.flush())
        self.assertIsNone(batcher.flush())

    def test_remote_runner_feed_audio_splits_batched_pcm_into_vad_sized_frames(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = RemoteLiveSessionRunner(
                config=_sample_config(Path(temp_dir)),
                request=LiveStartRequest(
                    title="产品周会",
                    kind="meeting",
                    language="zh",
                    source_label="BlackHole 2ch",
                    source_ref="1",
                ),
                on_progress=lambda _event: None,
            )

            runner.feed_audio(b"\x00\x00" * 3840)

            frames = [runner.frame_queue.get_nowait() for _ in range(runner.frame_queue.qsize())]

        self.assertEqual(8, len(frames))
        self.assertEqual(0, frames[0].started_ms)
        self.assertEqual(30, frames[0].ended_ms)
        self.assertEqual(210, frames[-1].started_ms)
        self.assertEqual(240, frames[-1].ended_ms)

    def test_remote_runner_feed_audio_buffers_incomplete_tail_until_next_packet(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = RemoteLiveSessionRunner(
                config=_sample_config(Path(temp_dir)),
                request=LiveStartRequest(
                    title="产品周会",
                    kind="meeting",
                    language="zh",
                    source_label="BlackHole 2ch",
                    source_ref="1",
                ),
                on_progress=lambda _event: None,
            )

            runner.feed_audio(b"\x00\x00" * 720)
            first_batch = [
                runner.frame_queue.get_nowait() for _ in range(runner.frame_queue.qsize())
            ]
            runner.feed_audio(b"\x00\x00" * 240)
            second_batch = [
                runner.frame_queue.get_nowait() for _ in range(runner.frame_queue.qsize())
            ]

        self.assertEqual(1, len(first_batch))
        self.assertEqual(1, len(second_batch))
        self.assertEqual((0, 30), (first_batch[0].started_ms, first_batch[0].ended_ms))
        self.assertEqual((30, 60), (second_batch[0].started_ms, second_batch[0].ended_ms))

    def test_remote_runner_postprocess_factory_forwards_start_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            forwarded: dict[str, object] = {}
            ready_event = threading.Event()
            request = LiveStartRequest(
                title="产品周会",
                kind="meeting",
                language="zh",
                source_label="BlackHole 2ch",
                source_ref="1",
            )

            def _create_postprocess_task(session_id: str, start_event=None):
                forwarded["session_id"] = session_id
                forwarded["start_event"] = start_event
                return {"task_id": "task-post-1"}

            runner = RemoteLiveSessionRunner(
                config=_sample_config(Path(temp_dir)),
                request=request,
                on_progress=lambda _event: None,
                create_postprocess_task=_create_postprocess_task,
            )
            runner.session_id = "remote-1"
            runner._postprocess_ready_event = ready_event

            payload = runner.ensure_postprocess_task_payload()

        self.assertEqual({"task_id": "task-post-1"}, payload)
        self.assertEqual("remote-1", forwarded["session_id"])
        self.assertIs(ready_event, forwarded["start_event"])

    def test_apply_session_started_creates_local_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            coordinator = RemoteLiveCoordinator(
                config=_sample_config(root),
                title="产品周会",
                source="1",
                kind="meeting",
            )

            coordinator._apply_session_started(
                {
                    "session_id": "remote-1",
                    "started_at": "2026-03-18T10:00:00+00:00",
                    "title": "产品周会",
                    "kind": "meeting",
                    "language": "zh",
                    "source_label": "BlackHole 2ch",
                    "source_ref": "1",
                }
            )

            self.assertEqual("remote-1", coordinator.session_id)
            self.assertIsNotNone(coordinator.workspace)
            self.assertTrue((coordinator.workspace.root / "session.toml").exists())
            metadata = coordinator.workspace.read_session()

        self.assertEqual("remote", metadata.execution_target)
        self.assertEqual("remote-1", metadata.remote_session_id)
        self.assertEqual("pending", metadata.refine_status)
        self.assertEqual("disabled", metadata.speaker_status)

    def test_apply_session_started_preserves_remote_note_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            coordinator = RemoteLiveCoordinator(
                config=_sample_config(root),
                title="产品周会",
                source="1",
                kind="meeting",
            )

            coordinator._apply_session_started(
                {
                    "metadata": {
                        "session_id": "remote-1",
                        "title": "产品周会",
                        "kind": "meeting",
                        "input_mode": "live",
                        "source_label": "BlackHole 2ch",
                        "source_ref": "1",
                        "language": "zh",
                        "started_at": "2026-03-18T10:00:00+00:00",
                        "transcript_note_path": (
                            "Sessions/Transcripts/2026-03-18/产品周会-100000.md"
                        ),
                        "structured_note_path": (
                            "Sessions/Summaries/2026-03-18/产品周会-100000.md"
                        ),
                        "session_dir": "/remote/sessions/remote-1",
                        "status": "live",
                        "transcript_source": "live",
                        "refine_status": "pending",
                        "execution_target": "remote",
                        "remote_session_id": "remote-1",
                        "speaker_status": "disabled",
                    }
                }
            )

            metadata = coordinator.workspace.read_session()

        self.assertEqual(
            "Sessions/Transcripts/2026-03-18/产品周会-100000.md",
            metadata.transcript_note_path,
        )
        self.assertEqual(
            "Sessions/Summaries/2026-03-18/产品周会-100000.md",
            metadata.structured_note_path,
        )

    def test_remote_coordinator_start_payload_includes_auto_refine_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            coordinator = RemoteLiveCoordinator(
                config=_sample_config(root),
                title="产品周会",
                source="1",
                kind="meeting",
                auto_refine_after_live=False,
            )

            payload = coordinator._live_start_payload(
                SimpleNamespace(index=1, name="BlackHole 2ch")
            )

        self.assertFalse(payload["auto_refine_after_live"])
        self.assertFalse(payload["speaker_enabled"])

    def test_remote_runner_applies_auto_refine_override_to_runtime_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = RemoteLiveSessionRunner(
                config=_sample_config(Path(temp_dir)),
                request=LiveStartRequest(
                    title="产品周会",
                    kind="meeting",
                    language="zh",
                    source_label="BlackHole 2ch",
                    source_ref="1",
                    auto_refine_after_live=False,
                ),
                on_progress=lambda _event: None,
            )

        self.assertFalse(runner.config.refine.auto_after_live)

    def test_remote_runner_applies_speaker_override_to_runtime_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = RemoteLiveSessionRunner(
                config=_sample_config(Path(temp_dir)),
                request=LiveStartRequest(
                    title="产品周会",
                    kind="meeting",
                    language="zh",
                    source_label="BlackHole 2ch",
                    source_ref="1",
                    speaker_enabled=True,
                ),
                on_progress=lambda _event: None,
            )

        self.assertTrue(runner.config.speaker.enabled)

    def test_sync_artifacts_rewrites_local_journal_with_speaker_labels(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = replace(
                _sample_config(root),
                refine=RefineConfig(enabled=False, auto_after_live=False),
                obsidian=replace(_sample_config(root).obsidian, enabled=True),
            )
            coordinator = RemoteLiveCoordinator(
                config=config,
                title="产品周会",
                source="1",
                kind="meeting",
            )
            coordinator._apply_session_started(
                {
                    "session_id": "remote-1",
                    "started_at": "2026-03-18T10:00:00+00:00",
                    "title": "产品周会",
                    "kind": "meeting",
                    "language": "zh",
                    "source_label": "BlackHole 2ch",
                    "source_ref": "1",
                }
            )

            coordinator._sync_remote_artifacts(
                {
                    "metadata": {
                        "session_id": "remote-1",
                        "title": "产品周会",
                        "kind": "meeting",
                        "input_mode": "live",
                        "source_label": "BlackHole 2ch",
                        "source_ref": "1",
                        "language": "zh",
                        "started_at": "2026-03-18T10:00:00+00:00",
                        "transcript_note_path": (
                            "Sessions/Transcripts/2026-03-18/产品周会-100000.md"
                        ),
                        "structured_note_path": (
                            "Sessions/Summaries/2026-03-18/产品周会-100000.md"
                        ),
                        "session_dir": "/remote/sessions/remote-1",
                        "status": "finalized",
                        "transcript_source": "refined",
                        "refine_status": "done",
                        "execution_target": "remote",
                        "remote_session_id": "remote-1",
                        "speaker_status": "done",
                    },
                    "entries": [
                        {
                            "segment_id": "seg-00001",
                            "started_ms": 0,
                            "ended_ms": 2000,
                            "text": "大家好，开始吧。",
                            "speaker_label": "Speaker 1",
                        }
                    ],
                    "has_session_audio": True,
                }
            )

            entries = coordinator.workspace.transcript_entries()
            metadata = coordinator.workspace.read_session()
            transcript_exists = coordinator.workspace.transcript_md.exists()
            structured_exists = coordinator.workspace.structured_md.exists()

        self.assertEqual("done", metadata.speaker_status)
        self.assertEqual("refined", metadata.transcript_source)
        self.assertEqual(
            "Sessions/Transcripts/2026-03-18/产品周会-100000.md",
            metadata.transcript_note_path,
        )
        self.assertEqual("Speaker 1", entries[0].speaker_label)
        self.assertTrue(transcript_exists)
        self.assertTrue(structured_exists)

    def test_remote_progress_does_not_duplicate_segment_transcribed_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            events = []
            coordinator = RemoteLiveCoordinator(
                config=_sample_config(root),
                title="产品周会",
                source="1",
                kind="meeting",
                on_progress=events.append,
            )
            coordinator._apply_session_started(
                {
                    "session_id": "remote-1",
                    "started_at": "2026-03-18T10:00:00+00:00",
                    "title": "产品周会",
                    "kind": "meeting",
                    "language": "zh",
                    "source_label": "BlackHole 2ch",
                    "source_ref": "1",
                }
            )

            coordinator._append_live_segment(
                {
                    "segment_id": "seg-00001",
                    "started_ms": 0,
                    "ended_ms": 2000,
                    "text": "大家好，开始吧。",
                }
            )
            coordinator._emit_progress_payload(
                {
                    "stage": "segment_transcribed",
                    "message": "片段 seg-00001 已转写",
                    "session_id": "remote-1",
                    "current": 1,
                }
            )

        transcribed_events = [event for event in events if event.stage == "segment_transcribed"]
        self.assertEqual(1, len(transcribed_events))
        self.assertEqual("片段 seg-00001 已转写", transcribed_events[0].message)

    def test_repeated_segment_final_does_not_duplicate_segment_transcribed_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            events = []
            coordinator = RemoteLiveCoordinator(
                config=_sample_config(root),
                title="产品周会",
                source="1",
                kind="meeting",
                on_progress=events.append,
            )
            coordinator._apply_session_started(
                {
                    "session_id": "remote-1",
                    "started_at": "2026-03-18T10:00:00+00:00",
                    "title": "产品周会",
                    "kind": "meeting",
                    "language": "zh",
                    "source_label": "BlackHole 2ch",
                    "source_ref": "1",
                }
            )

            coordinator._append_live_segment(
                {
                    "segment_id": "seg-00002",
                    "started_ms": 1000,
                    "ended_ms": 1400,
                    "text": "第一版",
                }
            )
            coordinator._append_live_segment(
                {
                    "segment_id": "seg-00002",
                    "started_ms": 1000,
                    "ended_ms": 1800,
                    "text": "第一版 更新后",
                }
            )

            entries = coordinator.workspace.transcript_entries()

        transcribed_events = [event for event in events if event.stage == "segment_transcribed"]
        self.assertEqual(1, len(transcribed_events))
        self.assertEqual("片段 seg-00002 已转写", transcribed_events[0].message)
        self.assertEqual(1, len(entries))
        self.assertEqual("第一版 更新后", entries[0].text)
        self.assertEqual(1800, entries[0].ended_ms)

    def test_remote_coordinator_input_level_callback_emits_progress_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            events = []
            coordinator = RemoteLiveCoordinator(
                config=_sample_config(root),
                title="产品周会",
                source="1",
                kind="meeting",
                on_progress=events.append,
            )
            coordinator.session_id = "remote-1"

            callback = coordinator._build_input_level_callback()
            callback(InputLevel(normalized=0.99, peak=1.0, clipping=True))

        self.assertEqual("input_level", events[0].stage)
        self.assertEqual("Clipping", events[0].message)
        self.assertEqual("remote-1", events[0].session_id)
        self.assertEqual(99, events[0].current)
        self.assertEqual(100, events[0].total)

    def test_remote_partial_segment_updates_local_draft_without_emitting_final_progress(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            events = []
            coordinator = RemoteLiveCoordinator(
                config=_sample_config(root),
                title="产品周会",
                source="1",
                kind="meeting",
                on_progress=events.append,
            )
            coordinator._apply_session_started(
                {
                    "session_id": "remote-1",
                    "started_at": "2026-03-18T10:00:00+00:00",
                    "title": "产品周会",
                    "kind": "meeting",
                    "language": "zh",
                    "source_label": "BlackHole 2ch",
                    "source_ref": "1",
                }
            )
            coordinator._event_queue.put(
                {
                    "type": "segment_partial",
                    "segment_id": "seg-00001",
                    "started_ms": 0,
                    "ended_ms": 900,
                    "text": "大家好",
                }
            )

            coordinator._drain_remote_events(threading.Event())
            entries = coordinator.workspace.transcript_entries()

        self.assertEqual(1, len(entries))
        self.assertEqual("大家好", entries[0].text)
        self.assertEqual([], [event for event in events if event.stage == "segment_transcribed"])

    def test_append_live_segment_keeps_remote_draft_local_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            coordinator = RemoteLiveCoordinator(
                config=_sample_config(root),
                title="产品周会",
                source="1",
                kind="meeting",
            )
            coordinator._apply_session_started(
                {
                    "session_id": "remote-1",
                    "started_at": "2026-03-18T10:00:00+00:00",
                    "title": "产品周会",
                    "kind": "meeting",
                    "language": "zh",
                    "source_label": "BlackHole 2ch",
                    "source_ref": "1",
                }
            )

            coordinator._append_live_segment(
                {
                    "segment_id": "seg-00001",
                    "started_ms": 0,
                    "ended_ms": 900,
                    "text": "大家好",
                },
                emit_final_progress=False,
            )

            entries = coordinator.workspace.transcript_entries()
            transcript = coordinator.workspace.transcript_md.read_text(encoding="utf-8")

        self.assertEqual(1, len(entries))
        self.assertEqual("大家好", entries[0].text)
        self.assertIn("大家好", transcript)

    def test_append_live_segment_updates_existing_segment_instead_of_appending_duplicate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            coordinator = RemoteLiveCoordinator(
                config=_sample_config(root),
                title="产品周会",
                source="1",
                kind="meeting",
            )
            coordinator._apply_session_started(
                {
                    "session_id": "remote-1",
                    "started_at": "2026-03-18T10:00:00+00:00",
                    "title": "产品周会",
                    "kind": "meeting",
                    "language": "zh",
                    "source_label": "BlackHole 2ch",
                    "source_ref": "1",
                }
            )

            coordinator._append_live_segment(
                {
                    "segment_id": "seg-00001",
                    "started_ms": 0,
                    "ended_ms": 2000,
                    "text": "大家好",
                }
            )
            coordinator._append_live_segment(
                {
                    "segment_id": "seg-00001",
                    "started_ms": 0,
                    "ended_ms": 2400,
                    "text": "大家好，开始吧。",
                }
            )

            entries = coordinator.workspace.transcript_entries()

        self.assertEqual(1, len(entries))
        self.assertEqual("大家好，开始吧。", entries[0].text)
        self.assertEqual(2400, entries[0].ended_ms)

    def test_remote_partial_segment_accumulates_delta_fragments_locally(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            coordinator = RemoteLiveCoordinator(
                config=_sample_config(root),
                title="产品周会",
                source="1",
                kind="meeting",
            )
            coordinator._apply_session_started(
                {
                    "session_id": "remote-1",
                    "started_at": "2026-03-18T10:00:00+00:00",
                    "title": "产品周会",
                    "kind": "meeting",
                    "language": "zh",
                    "source_label": "BlackHole 2ch",
                    "source_ref": "1",
                }
            )

            coordinator._append_live_segment(
                {
                    "segment_id": "seg-00001",
                    "started_ms": 0,
                    "ended_ms": 200,
                    "text": "so",
                },
                emit_final_progress=False,
            )
            coordinator._append_live_segment(
                {
                    "segment_id": "seg-00001",
                    "started_ms": 0,
                    "ended_ms": 500,
                    "text": "today's class",
                },
                emit_final_progress=False,
            )
            coordinator._append_live_segment(
                {
                    "segment_id": "seg-00001",
                    "started_ms": 0,
                    "ended_ms": 900,
                    "text": "organized",
                },
                emit_final_progress=False,
            )

            entries = coordinator.workspace.transcript_entries()

        self.assertEqual(1, len(entries))
        self.assertEqual("so today's class organized", compact_text(entries[0].text))
        self.assertEqual(900, entries[0].ended_ms)

    def test_remote_partial_segment_does_not_downgrade_to_shorter_fragment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            coordinator = RemoteLiveCoordinator(
                config=_sample_config(root),
                title="产品周会",
                source="1",
                kind="meeting",
            )
            coordinator._apply_session_started(
                {
                    "session_id": "remote-1",
                    "started_at": "2026-03-18T10:00:00+00:00",
                    "title": "产品周会",
                    "kind": "meeting",
                    "language": "zh",
                    "source_label": "BlackHole 2ch",
                    "source_ref": "1",
                }
            )

            coordinator._append_live_segment(
                {
                    "segment_id": "seg-00001",
                    "started_ms": 0,
                    "ended_ms": 500,
                    "text": "so today's class",
                },
                emit_final_progress=False,
            )
            coordinator._append_live_segment(
                {
                    "segment_id": "seg-00001",
                    "started_ms": 0,
                    "ended_ms": 650,
                    "text": "class",
                },
                emit_final_progress=False,
            )

            entries = coordinator.workspace.transcript_entries()

        self.assertEqual(1, len(entries))
        self.assertEqual("so today's class", compact_text(entries[0].text))
        self.assertEqual(650, entries[0].ended_ms)

    def test_remote_partial_segment_no_space_cjk_delta_does_not_inject_spaces(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            coordinator = RemoteLiveCoordinator(
                config=_sample_config(root),
                title="产品周会",
                source="1",
                kind="meeting",
            )
            coordinator._apply_session_started(
                {
                    "session_id": "remote-1",
                    "started_at": "2026-03-18T10:00:00+00:00",
                    "title": "产品周会",
                    "kind": "meeting",
                    "language": "zh",
                    "source_label": "BlackHole 2ch",
                    "source_ref": "1",
                }
            )

            coordinator._append_live_segment(
                {
                    "segment_id": "seg-00001",
                    "started_ms": 0,
                    "ended_ms": 200,
                    "text": "今天",
                },
                emit_final_progress=False,
            )
            coordinator._append_live_segment(
                {
                    "segment_id": "seg-00001",
                    "started_ms": 0,
                    "ended_ms": 500,
                    "text": "天气",
                },
                emit_final_progress=False,
            )
            coordinator._append_live_segment(
                {
                    "segment_id": "seg-00001",
                    "started_ms": 0,
                    "ended_ms": 900,
                    "text": "很好",
                },
                emit_final_progress=False,
            )

            entries = coordinator.workspace.transcript_entries()

        self.assertEqual(1, len(entries))
        self.assertEqual("今天天气很好", entries[0].text)
        self.assertEqual(900, entries[0].ended_ms)

    def test_remote_partial_segment_no_space_cjk_punctuation_boundary_does_not_inject_spaces(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            coordinator = RemoteLiveCoordinator(
                config=_sample_config(root),
                title="产品周会",
                source="1",
                kind="meeting",
            )
            coordinator._apply_session_started(
                {
                    "session_id": "remote-1",
                    "started_at": "2026-03-18T10:00:00+00:00",
                    "title": "产品周会",
                    "kind": "meeting",
                    "language": "zh",
                    "source_label": "BlackHole 2ch",
                    "source_ref": "1",
                }
            )

            coordinator._append_live_segment(
                {
                    "segment_id": "seg-00001",
                    "started_ms": 0,
                    "ended_ms": 500,
                    "text": "今天天气，",
                },
                emit_final_progress=False,
            )
            coordinator._append_live_segment(
                {
                    "segment_id": "seg-00001",
                    "started_ms": 0,
                    "ended_ms": 900,
                    "text": "很好",
                },
                emit_final_progress=False,
            )

            entries = coordinator.workspace.transcript_entries()

        self.assertEqual(1, len(entries))
        self.assertEqual("今天天气，很好", entries[0].text)
        self.assertEqual(900, entries[0].ended_ms)

    def test_partial_after_final_does_not_overwrite_local_final_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            coordinator = RemoteLiveCoordinator(
                config=_sample_config(root),
                title="产品周会",
                source="1",
                kind="meeting",
            )
            coordinator._apply_session_started(
                {
                    "session_id": "remote-1",
                    "started_at": "2026-03-18T10:00:00+00:00",
                    "title": "产品周会",
                    "kind": "meeting",
                    "language": "zh",
                    "source_label": "BlackHole 2ch",
                    "source_ref": "1",
                }
            )

            coordinator._append_live_segment(
                {
                    "type": "segment_final",
                    "segment_id": "seg-00001",
                    "started_ms": 100,
                    "ended_ms": 800,
                    "text": "大家好，开始吧。",
                }
            )
            coordinator._event_queue.put(
                {
                    "type": "segment_partial",
                    "segment_id": "seg-00001",
                    "started_ms": 100,
                    "ended_ms": 520,
                    "text": "大家好",
                }
            )

            coordinator._drain_remote_events(threading.Event())
            entries = coordinator.workspace.transcript_entries()

        self.assertEqual(1, len(entries))
        self.assertEqual("大家好，开始吧。", entries[0].text)
        self.assertEqual(800, entries[0].ended_ms)

    def test_remote_service_health_payload_reports_funasr_backend_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = RemoteSessionService(_sample_config(Path(temp_dir), funasr_enabled=True))

            payload = service.health_payload()

        self.assertTrue(payload["funasr_enabled"])
        self.assertEqual("funasr", payload["realtime_backend"])

    def test_funasr_draft_tracker_reuses_segment_id_until_finalized(self) -> None:
        tracker = _FunAsrDraftTracker()

        tracker.start_stream(0)
        partial = tracker.build_partial_payload("大家好", current_ms=600, bounds_ms=(100, 600))
        final = tracker.build_final_entry(
            "大家好，开始吧。",
            current_ms=1200,
            bounds_ms=(100, 1200),
        )
        tracker.start_stream(1200)
        next_partial = tracker.build_partial_payload("第二段", current_ms=1800, bounds_ms=(0, 300))

        self.assertEqual("seg-00001", partial["segment_id"])
        self.assertEqual("seg-00001", final.segment_id)
        self.assertEqual(1200, final.ended_ms)
        self.assertEqual("seg-00002", next_partial["segment_id"])
        self.assertEqual(1200, next_partial["started_ms"])

    def test_funasr_draft_tracker_accumulates_online_delta_fragments(self) -> None:
        tracker = _FunAsrDraftTracker()

        tracker.start_stream(0)
        first = tracker.build_partial_payload("so", current_ms=200, bounds_ms=(0, 200))
        second = tracker.build_partial_payload("today's class", current_ms=500, bounds_ms=(0, 500))
        duplicate = tracker.build_partial_payload("class", current_ms=650, bounds_ms=(0, 650))
        third = tracker.build_partial_payload("organized", current_ms=900, bounds_ms=(0, 900))

        self.assertEqual("so", first["text"])
        self.assertEqual("so today's class", compact_text(str(second["text"])))
        self.assertIsNone(duplicate)
        self.assertEqual(
            "so today's class organized",
            compact_text(str(third["text"])),
        )

    def test_funasr_draft_tracker_accepts_cumulative_online_snapshots_without_duplication(
        self,
    ) -> None:
        tracker = _FunAsrDraftTracker()

        tracker.start_stream(0)
        first = tracker.build_partial_payload("so", current_ms=200, bounds_ms=(0, 200))
        second = tracker.build_partial_payload(
            "so today's class",
            current_ms=500,
            bounds_ms=(0, 500),
        )
        regression = tracker.build_partial_payload(
            "today's class", current_ms=650, bounds_ms=(0, 650)
        )
        third = tracker.build_partial_payload(
            "so today's class is organized",
            current_ms=900,
            bounds_ms=(0, 900),
        )

        self.assertEqual("so", first["text"])
        self.assertEqual("so today's class", compact_text(str(second["text"])))
        self.assertIsNone(regression)
        self.assertEqual(
            "so today's class is organized",
            compact_text(str(third["text"])),
        )

    def test_funasr_forced_final_preserves_accumulated_online_draft(self) -> None:
        tracker = _FunAsrDraftTracker()

        tracker.start_stream(0)
        tracker.build_partial_payload("so", current_ms=200, bounds_ms=(0, 200))
        tracker.build_partial_payload("today's class", current_ms=500, bounds_ms=(0, 500))
        tracker.build_partial_payload("organized", current_ms=900, bounds_ms=(0, 900))

        final = tracker.force_finalize()

        self.assertIsNotNone(final)
        assert final is not None
        self.assertEqual(
            "so today's class organized",
            compact_text(final.text),
        )
        self.assertEqual(900, final.ended_ms)

        tracker.start_stream(final.ended_ms)
        next_partial = tracker.build_partial_payload(
            "next segment",
            current_ms=1200,
            bounds_ms=(0, 300),
        )

        self.assertEqual("seg-00002", next_partial["segment_id"])
        self.assertEqual(final.ended_ms, next_partial["started_ms"])

    def test_funasr_draft_tracker_no_space_cjk_delta_does_not_inject_spaces(self) -> None:
        tracker = _FunAsrDraftTracker()

        tracker.start_stream(0)
        first = tracker.build_partial_payload("今天", current_ms=200, bounds_ms=(0, 200))
        second = tracker.build_partial_payload("天气", current_ms=500, bounds_ms=(0, 500))
        third = tracker.build_partial_payload("很好", current_ms=900, bounds_ms=(0, 900))

        self.assertEqual("今天", first["text"])
        self.assertEqual("今天天气", second["text"])
        self.assertEqual("今天天气很好", third["text"])

    def test_funasr_draft_tracker_no_space_cjk_punctuation_boundary_does_not_inject_spaces(
        self,
    ) -> None:
        tracker = _FunAsrDraftTracker()

        tracker.start_stream(0)
        first = tracker.build_partial_payload("今天天气，", current_ms=500, bounds_ms=(0, 500))
        second = tracker.build_partial_payload("很好", current_ms=900, bounds_ms=(0, 900))

        self.assertEqual("今天天气，", first["text"])
        self.assertEqual("今天天气，很好", second["text"])

    def test_funasr_forced_final_no_space_cjk_draft_preserves_joined_text(self) -> None:
        tracker = _FunAsrDraftTracker()

        tracker.start_stream(0)
        tracker.build_partial_payload("今天", current_ms=200, bounds_ms=(0, 200))
        tracker.build_partial_payload("天气", current_ms=500, bounds_ms=(0, 500))
        tracker.build_partial_payload("很好", current_ms=900, bounds_ms=(0, 900))

        final = tracker.force_finalize()

        self.assertIsNotNone(final)
        assert final is not None
        self.assertEqual("今天天气很好", final.text)
        self.assertEqual(900, final.ended_ms)

    def test_funasr_forced_final_no_space_cjk_punctuation_draft_preserves_joined_text(
        self,
    ) -> None:
        tracker = _FunAsrDraftTracker()

        tracker.start_stream(0)
        tracker.build_partial_payload("今天天气，", current_ms=500, bounds_ms=(0, 500))
        tracker.build_partial_payload("很好", current_ms=900, bounds_ms=(0, 900))

        final = tracker.force_finalize()

        self.assertIsNotNone(final)
        assert final is not None
        self.assertEqual("今天天气，很好", final.text)
        self.assertEqual(900, final.ended_ms)

    def test_funasr_offline_final_overrides_online_draft_snapshot(self) -> None:
        tracker = _FunAsrDraftTracker()

        tracker.start_stream(0)
        tracker.build_partial_payload("hello", current_ms=200, bounds_ms=(0, 200))
        tracker.build_partial_payload("world", current_ms=500, bounds_ms=(0, 500))

        final = tracker.build_final_entry(
            "  hello   brave   world  ",
            current_ms=1200,
            bounds_ms=(100, 1200),
        )

        self.assertIsNotNone(final)
        assert final is not None
        self.assertEqual("  hello   brave   world  ", final.text)
        self.assertEqual("seg-00001", final.segment_id)
        self.assertEqual(0, final.started_ms)
        self.assertEqual(1200, final.ended_ms)
        self.assertIsNone(tracker.force_finalize())

        tracker.start_stream(final.ended_ms)
        next_partial = tracker.build_partial_payload(
            "fresh start",
            current_ms=1500,
            bounds_ms=(0, 300),
        )

        self.assertEqual("seg-00002", next_partial["segment_id"])
        self.assertEqual(final.ended_ms, next_partial["started_ms"])
        self.assertEqual("fresh start", next_partial["text"])

    def test_real_fragment_trace_rebuilds_one_live_row_for_open_segment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            coordinator = RemoteLiveCoordinator(
                config=_sample_config(root),
                title="产品周会",
                source="1",
                kind="meeting",
            )
            coordinator._apply_session_started(
                {
                    "session_id": "remote-1",
                    "started_at": "2026-03-18T10:00:00+00:00",
                    "title": "产品周会",
                    "kind": "meeting",
                    "language": "zh",
                    "source_label": "BlackHole 2ch",
                    "source_ref": "1",
                }
            )

            tracker = _FunAsrDraftTracker()
            tracker.start_stream(0)
            trace = [
                "so",
                "'s ass",
                "is",
                "troductary",
                "class",
                "will talk",
                "about the class",
                "self",
                "and",
                "h",
            ]

            current_ms = 0
            for token in trace:
                current_ms += 600
                payload = tracker.build_partial_payload(
                    token,
                    current_ms=current_ms,
                    bounds_ms=(0, current_ms),
                )
                if payload is None:
                    continue
                coordinator._append_live_segment(payload, emit_final_progress=False)

            entries = coordinator.workspace.transcript_entries()

        self.assertEqual(1, len(entries))
        self.assertEqual("seg-00001", entries[0].segment_id)
        self.assertEqual(
            "so 's ass is troductary class will talk about the class self and h",
            compact_text(entries[0].text),
        )

    def test_live_transcript_note_keeps_one_row_per_open_segment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            coordinator = RemoteLiveCoordinator(
                config=_sample_config(root),
                title="产品周会",
                source="1",
                kind="meeting",
            )
            coordinator._apply_session_started(
                {
                    "session_id": "remote-1",
                    "started_at": "2026-03-18T10:00:00+00:00",
                    "title": "产品周会",
                    "kind": "meeting",
                    "language": "zh",
                    "source_label": "BlackHole 2ch",
                    "source_ref": "1",
                }
            )

            tracker = _FunAsrDraftTracker()
            tracker.start_stream(0)
            current_ms = 0
            for token in ["so", "today's class", "organized"]:
                current_ms += 300
                payload = tracker.build_partial_payload(
                    token,
                    current_ms=current_ms,
                    bounds_ms=(0, current_ms),
                )
                if payload is None:
                    continue
                coordinator._append_live_segment(payload, emit_final_progress=False)

            entries = coordinator.workspace.transcript_entries()
            note = build_transcript_note(
                coordinator.workspace.read_session(),
                entries,
                status="live",
            )

        self.assertEqual(1, len(entries))
        self.assertEqual("so today's class organized", compact_text(entries[0].text))
        self.assertEqual(1, note.count("- [00:00:00]"))
        expected_line = "- [00:00:00] so today's class organized"
        self.assertIn(expected_line, note.splitlines())
        self.assertEqual(1, note.splitlines().count(expected_line))

    def test_live_journal_rebuild_does_not_emit_fragment_history_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            coordinator = RemoteLiveCoordinator(
                config=_sample_config(root),
                title="产品周会",
                source="1",
                kind="meeting",
            )
            coordinator._apply_session_started(
                {
                    "session_id": "remote-1",
                    "started_at": "2026-03-18T10:00:00+00:00",
                    "title": "产品周会",
                    "kind": "meeting",
                    "language": "zh",
                    "source_label": "BlackHole 2ch",
                    "source_ref": "1",
                }
            )

            tracker = _FunAsrDraftTracker()
            tracker.start_stream(0)
            current_ms = 0
            for token in ["so", "today's class", "organized"]:
                current_ms += 300
                payload = tracker.build_partial_payload(
                    token,
                    current_ms=current_ms,
                    bounds_ms=(0, current_ms),
                )
                if payload is None:
                    continue
                coordinator.workspace.record_segment_text(
                    str(payload["segment_id"]),
                    int(payload["started_ms"]),
                    int(payload["ended_ms"]),
                    str(payload["text"]),
                )

            entries = coordinator.workspace.transcript_entries()
            transcribed_events = [
                event
                for event in coordinator.workspace.load_events()
                if event.kind == "segment_transcribed"
            ]

        self.assertGreater(len(transcribed_events), 1)
        self.assertEqual(1, len(entries))
        self.assertEqual("seg-00001", entries[0].segment_id)
        self.assertEqual("so today's class organized", compact_text(entries[0].text))
        self.assertEqual(
            compact_text(transcribed_events[-1].text or ""),
            compact_text(entries[0].text),
        )

    def test_mixed_online_partials_and_late_final_keep_segment_boundaries_stable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            coordinator = RemoteLiveCoordinator(
                config=_sample_config(root),
                title="产品周会",
                source="1",
                kind="meeting",
            )
            coordinator._apply_session_started(
                {
                    "session_id": "remote-1",
                    "started_at": "2026-03-18T10:00:00+00:00",
                    "title": "产品周会",
                    "kind": "meeting",
                    "language": "zh",
                    "source_label": "BlackHole 2ch",
                    "source_ref": "1",
                }
            )

            tracker = _FunAsrDraftTracker()
            tracker.start_stream(0)
            seg1_partial_1 = tracker.build_partial_payload(
                "so",
                current_ms=300,
                bounds_ms=(0, 300),
            )
            seg1_partial_2 = tracker.build_partial_payload(
                "today's class",
                current_ms=700,
                bounds_ms=(0, 700),
            )
            seg1_final = tracker.build_final_entry(
                "segment one final authority",
                current_ms=1200,
                bounds_ms=(0, 1200),
            )
            assert seg1_partial_1 is not None
            assert seg1_partial_2 is not None
            assert seg1_final is not None

            tracker.start_stream(seg1_final.ended_ms)
            seg2_partial_1 = tracker.build_partial_payload(
                "segment two",
                current_ms=1500,
                bounds_ms=(0, 300),
            )
            seg2_partial_2 = tracker.build_partial_payload(
                "keeps drafting",
                current_ms=1800,
                bounds_ms=(0, 600),
            )
            assert seg2_partial_1 is not None
            assert seg2_partial_2 is not None

            self.assertEqual("seg-00001", seg1_partial_1["segment_id"])
            self.assertEqual("seg-00001", seg1_partial_2["segment_id"])
            self.assertEqual("seg-00001", seg1_final.segment_id)
            self.assertEqual("seg-00002", seg2_partial_1["segment_id"])
            self.assertEqual("seg-00002", seg2_partial_2["segment_id"])

            coordinator._append_live_segment(seg1_partial_1, emit_final_progress=False)
            coordinator._append_live_segment(seg1_partial_2, emit_final_progress=False)
            coordinator._append_live_segment(seg2_partial_1, emit_final_progress=False)
            coordinator._append_live_segment(seg2_partial_2, emit_final_progress=False)
            coordinator._append_live_segment(
                {
                    "segment_id": seg1_final.segment_id,
                    "started_ms": seg1_final.started_ms,
                    "ended_ms": seg1_final.ended_ms,
                    "text": seg1_final.text,
                }
            )

            entries = coordinator.workspace.transcript_entries()
            note = build_transcript_note(
                coordinator.workspace.read_session(),
                entries,
                status="live",
            )

        self.assertEqual(2, len(entries))
        by_segment_id = {entry.segment_id: entry for entry in entries}
        self.assertEqual({"seg-00001", "seg-00002"}, set(by_segment_id))
        self.assertEqual("segment one final authority", by_segment_id["seg-00001"].text)
        self.assertEqual(
            "segment two keeps drafting",
            compact_text(by_segment_id["seg-00002"].text),
        )
        self.assertEqual(1, note.count("- [00:00:00]"))
        expected_seg2_line = "- [00:00:01] segment two keeps drafting"
        self.assertIn(expected_seg2_line, note.splitlines())
        self.assertEqual(1, note.splitlines().count(expected_seg2_line))

    def test_handle_funasr_message_uses_realtime_modes_and_timestamp_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _sample_config(root, funasr_enabled=True, remote_timeout_seconds=2)
            runner = RemoteLiveSessionRunner(
                config=config,
                request=LiveStartRequest(
                    title="产品周会",
                    kind="meeting",
                    language="zh",
                    source_label="BlackHole 2ch",
                    source_ref="1",
                ),
                on_progress=lambda _event: None,
            )
            metadata = create_session_metadata(
                config=config,
                title="产品周会",
                kind="meeting",
                language="zh",
                input_mode="live",
                source_label="BlackHole 2ch",
                source_ref="1",
            )
            workspace = SessionWorkspace.create(Path(metadata.session_dir), metadata)
            tracker = _FunAsrDraftTracker()
            tracker.start_stream(0)

            runner._handle_funasr_message(
                FunAsrMessage(
                    text="大家好",
                    mode="2pass-online",
                    is_final=True,
                    wav_name=metadata.session_id,
                    timestamp_ms=((100, 320), (320, 500)),
                    sentence_spans_ms=((100, 500),),
                    raw_payload={},
                ),
                tracker,
                workspace,
                metadata,
                current_ms=900,
            )
            runner._handle_funasr_message(
                FunAsrMessage(
                    text="大家好，开始吧。",
                    mode="2pass-offline",
                    is_final=False,
                    wav_name=metadata.session_id,
                    timestamp_ms=((100, 320), (320, 500), (500, 860)),
                    sentence_spans_ms=((100, 860),),
                    raw_payload={},
                ),
                tracker,
                workspace,
                metadata,
                current_ms=1200,
            )
            entries = workspace.transcript_entries()

        self.assertEqual(1, len(entries))
        self.assertEqual("大家好，开始吧。", entries[0].text)
        self.assertEqual(100, entries[0].started_ms)
        self.assertEqual(860, entries[0].ended_ms)
        self.assertEqual(1, len(runner.entries))

    def test_run_remote_postprocess_generates_structured_output_and_preserves_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = replace(
                _sample_config(root),
                refine=RefineConfig(enabled=False, auto_after_live=False),
            )
            metadata = create_session_metadata(
                config=config,
                title="产品周会",
                kind="meeting",
                language="zh",
                input_mode="live",
                source_label="BlackHole 2ch",
                source_ref="1",
            )
            workspace = SessionWorkspace.create(Path(metadata.session_dir), metadata)
            workspace.record_segment_text("seg-00001", 0, 1200, "大家好，开始吧。")

            with patch(
                "live_note.remote.service.apply_speaker_labels",
                side_effect=lambda _config, _workspace, current, **_kwargs: current,
            ):
                final_metadata = _run_remote_postprocess(
                    config=config,
                    workspace=workspace,
                    metadata=metadata,
                    logger=workspace.session_logger(),
                    on_progress=lambda _event: None,
                )

            structured = workspace.structured_md.read_text(encoding="utf-8")
            transcript = workspace.transcript_md.read_text(encoding="utf-8")
            saved_status = workspace.read_session().status

        self.assertEqual("transcript_only", final_metadata.status)
        self.assertEqual("transcript_only", saved_status)
        self.assertIn("## 关键点", structured)
        self.assertIn("大家好，开始吧。", transcript)

    def test_run_remote_postprocess_uses_disabled_obsidian_for_final_publish(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = replace(
                _sample_config(root),
                refine=RefineConfig(enabled=False, auto_after_live=False),
            )
            metadata = create_session_metadata(
                config=config,
                title="产品周会",
                kind="meeting",
                language="zh",
                input_mode="live",
                source_label="BlackHole 2ch",
                source_ref="1",
            )
            workspace = SessionWorkspace.create(Path(metadata.session_dir), metadata)
            workspace.record_segment_text("seg-00001", 0, 1200, "大家好，开始吧。")

            def _fake_publish_final_outputs(*, workspace, obsidian, **_kwargs):
                self.assertFalse(obsidian.is_enabled())
                workspace.update_session(status="transcript_only")

            with (
                patch(
                    "live_note.remote.service.apply_speaker_labels",
                    side_effect=lambda _config, _workspace, current, **_kwargs: current,
                ),
                patch(
                    "live_note.remote.service.publish_final_outputs",
                    side_effect=_fake_publish_final_outputs,
                ) as publish_mock,
            ):
                final_metadata = _run_remote_postprocess(
                    config=config,
                    workspace=workspace,
                    metadata=metadata,
                    logger=workspace.session_logger(),
                    on_progress=lambda _event: None,
                )

        publish_mock.assert_called_once()
        self.assertEqual("transcript_only", final_metadata.status)

    def test_remote_runner_failure_marks_session_with_disabled_obsidian(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            base_config = _sample_config(root, remote_timeout_seconds=1)
            config = replace(
                base_config,
                obsidian=replace(base_config.obsidian, enabled=True),
            )
            runner = RemoteLiveSessionRunner(
                config=config,
                request=LiveStartRequest(
                    title="产品周会",
                    kind="meeting",
                    language="zh",
                    source_label="BlackHole 2ch",
                    source_ref="1",
                ),
                on_progress=lambda _event: None,
            )
            metadata = create_session_metadata(
                config=config,
                title="产品周会",
                kind="meeting",
                language="zh",
                input_mode="live",
                source_label="BlackHole 2ch",
                source_ref="1",
            )
            metadata = replace(
                metadata,
                execution_target="remote",
                remote_session_id=metadata.session_id,
            )
            runner.metadata = metadata
            runner.workspace = SessionWorkspace.create(Path(metadata.session_dir), metadata)

            with (
                patch.object(
                    RemoteLiveSessionRunner,
                    "_run_whisper_live_backend",
                    side_effect=RuntimeError("boom"),
                ),
                patch(
                    "live_note.remote.service._mark_session_failed",
                ) as failed_mock,
                patch(
                    "live_note.remote.service._attach_console_logging",
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "boom"):
                    runner.run()

        failed_mock.assert_called_once()
        self.assertIn("obsidian", failed_mock.call_args.kwargs)
        self.assertFalse(failed_mock.call_args.kwargs["obsidian"].is_enabled())

    def test_flush_funasr_stream_keeps_waiting_for_delayed_offline_final(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runner = RemoteLiveSessionRunner(
                config=_sample_config(root, funasr_enabled=True),
                request=LiveStartRequest(
                    title="产品周会",
                    kind="meeting",
                    language="zh",
                    source_label="BlackHole 2ch",
                    source_ref="1",
                ),
                on_progress=lambda _event: None,
            )
            metadata = create_session_metadata(
                config=runner.config,
                title="产品周会",
                kind="meeting",
                language="zh",
                input_mode="live",
                source_label="BlackHole 2ch",
                source_ref="1",
            )
            workspace = SessionWorkspace.create(Path(metadata.session_dir), metadata)
            tracker = _FunAsrDraftTracker()
            tracker.start_stream(0)
            tracker.build_partial_payload("大家好", current_ms=400, bounds_ms=(0, 400))
            connection = _FakeFunAsrConnection(
                [
                    TimeoutError(),
                    TimeoutError(),
                    TimeoutError(),
                    FunAsrMessage(
                        text="大家好，开始吧。",
                        mode="2pass-offline",
                        is_final=False,
                        wav_name=metadata.session_id,
                        timestamp_ms=((0, 700),),
                        sentence_spans_ms=((0, 700),),
                        raw_payload={},
                    ),
                ],
                timeout_delay_seconds=0.35,
            )
            runner._flush_funasr_stream(
                connection,
                _FunAsrAudioBatcher(chunk_ms=60),
                tracker,
                workspace,
                metadata,
                current_ms=700,
            )

        self.assertEqual(1, len(runner.entries))
        self.assertEqual("大家好，开始吧。", runner.entries[0].text)

    def test_flush_funasr_stream_forces_final_entry_when_offline_result_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _sample_config(root, funasr_enabled=True, remote_timeout_seconds=1)
            runner = RemoteLiveSessionRunner(
                config=config,
                request=LiveStartRequest(
                    title="产品周会",
                    kind="meeting",
                    language="zh",
                    source_label="BlackHole 2ch",
                    source_ref="1",
                ),
                on_progress=lambda _event: None,
            )
            metadata = create_session_metadata(
                config=config,
                title="产品周会",
                kind="meeting",
                language="zh",
                input_mode="live",
                source_label="BlackHole 2ch",
                source_ref="1",
            )
            workspace = SessionWorkspace.create(Path(metadata.session_dir), metadata)
            tracker = _FunAsrDraftTracker()
            tracker.start_stream(0)
            tracker.build_partial_payload("大家好", current_ms=420, bounds_ms=(0, 420))
            connection = _FakeFunAsrConnection(timeout_delay_seconds=0.2)
            runner._flush_funasr_stream(
                connection,
                _FunAsrAudioBatcher(chunk_ms=60),
                tracker,
                workspace,
                metadata,
                current_ms=420,
            )

        self.assertEqual(1, len(runner.entries))
        self.assertEqual("大家好", runner.entries[0].text)
        self.assertEqual(420, runner.entries[0].ended_ms)

    def test_flush_funasr_stream_stop_interrupts_pause_wait_promptly(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _sample_config(root, funasr_enabled=True, remote_timeout_seconds=2)
            runner = RemoteLiveSessionRunner(
                config=config,
                request=LiveStartRequest(
                    title="产品周会",
                    kind="meeting",
                    language="zh",
                    source_label="BlackHole 2ch",
                    source_ref="1",
                ),
                on_progress=lambda _event: None,
            )
            metadata = create_session_metadata(
                config=config,
                title="产品周会",
                kind="meeting",
                language="zh",
                input_mode="live",
                source_label="BlackHole 2ch",
                source_ref="1",
            )
            workspace = SessionWorkspace.create(Path(metadata.session_dir), metadata)
            tracker = _FunAsrDraftTracker()
            tracker.start_stream(0)
            tracker.build_partial_payload("大家好", current_ms=420, bounds_ms=(0, 420))
            connection = _FakeFunAsrConnection(timeout_delay_seconds=0.05)

            def _request_stop_later() -> None:
                time.sleep(0.1)
                runner.request_stop()

            stop_thread = threading.Thread(target=_request_stop_later, daemon=True)
            stop_thread.start()
            started = time.monotonic()
            runner._flush_funasr_stream(
                connection,
                _FunAsrAudioBatcher(chunk_ms=60),
                tracker,
                workspace,
                metadata,
                current_ms=420,
            )
            elapsed = time.monotonic() - started
            stop_thread.join(timeout=1)

        self.assertLess(elapsed, 0.8)
        self.assertEqual(1, len(runner.entries))
        self.assertEqual("大家好", runner.entries[0].text)

    def test_funasr_backend_drains_buffered_frames_before_stop_finalize(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runner = RemoteLiveSessionRunner(
                config=_sample_config(root, funasr_enabled=True),
                request=LiveStartRequest(
                    title="产品周会",
                    kind="meeting",
                    language="zh",
                    source_label="BlackHole 2ch",
                    source_ref="1",
                ),
                on_progress=lambda _event: None,
            )
            metadata = create_session_metadata(
                config=runner.config,
                title="产品周会",
                kind="meeting",
                language="zh",
                input_mode="live",
                source_label="BlackHole 2ch",
                source_ref="1",
            )
            workspace = SessionWorkspace.create(Path(metadata.session_dir), metadata)
            logger = workspace.session_logger()
            pcm16 = b"\x01\x00" * 960
            runner.frame_queue.put(AudioFrame(started_ms=0, ended_ms=60, pcm16=pcm16))
            runner._stop_event.set()
            connection = _FakeFunAsrConnection()

            with patch.object(runner, "_open_funasr_connection", return_value=connection):
                runner._run_funasr_live_backend(workspace, metadata, logger)

        self.assertEqual([pcm16], connection.sent_audio)
        self.assertTrue(connection.stop_sent)

    def test_remote_coordinator_drains_local_frames_before_sending_stop(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            connection = _FakeRemoteLiveConnection()
            coordinator = RemoteLiveCoordinator(
                config=_sample_config(root),
                title="产品周会",
                source="1",
                kind="meeting",
                client=_FakeRemoteClient(connection),
            )
            coordinator.request_stop()

            with (
                patch(
                    "live_note.app.remote_coordinator.resolve_input_device",
                    return_value=SimpleNamespace(index=1, name="BlackHole 2ch"),
                ),
                patch(
                    "live_note.app.remote_coordinator.AudioCaptureService",
                    _FakeAudioCaptureService,
                ),
            ):
                exit_code = coordinator.run()

        self.assertEqual(0, exit_code)
        self.assertEqual([b"a" * 8 + b"b" * 8], connection.sent_audio)
        self.assertEqual(["stop"], connection.sent_controls)

    def test_remote_coordinator_waits_for_session_started_before_starting_capture(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            connection = _DelayedSessionStartedConnection(startup_delay_seconds=0.2)
            coordinator = RemoteLiveCoordinator(
                config=_sample_config(root),
                title="产品周会",
                source="1",
                kind="meeting",
                client=_FakeRemoteClient(connection),
            )
            coordinator.request_stop()

            class _SessionAwareCaptureService(_FakeAudioCaptureService):
                def start(self) -> None:
                    if coordinator.session_id is None:
                        raise AssertionError("capture started before session_started")
                    super().start()

            with (
                patch(
                    "live_note.app.remote_coordinator.resolve_input_device",
                    return_value=SimpleNamespace(index=1, name="BlackHole 2ch"),
                ),
                patch(
                    "live_note.app.remote_coordinator.AudioCaptureService",
                    _SessionAwareCaptureService,
                ),
            ):
                exit_code = coordinator.run()

        self.assertEqual(0, exit_code)

    def test_remote_coordinator_sends_stop_without_waiting_for_capture_thread_exit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            connection = _StopMustArrivePromptlyConnection(wait_seconds=0.2)
            coordinator = RemoteLiveCoordinator(
                config=_sample_config(root),
                title="产品周会",
                source="1",
                kind="meeting",
                client=_FakeRemoteClient(connection),
            )

            class _StickyStopAudioCaptureService(_FakeAudioCaptureService):
                def start(self) -> None:
                    self._alive = True
                    self.frame_queue.put(AudioFrame(started_ms=0, ended_ms=120, pcm16=b"a" * 8))
                    coordinator.request_stop()

                def stop(self) -> None:
                    return None

                @property
                def is_alive(self) -> bool:
                    return True

            with (
                patch(
                    "live_note.app.remote_coordinator.resolve_input_device",
                    return_value=SimpleNamespace(index=1, name="BlackHole 2ch"),
                ),
                patch(
                    "live_note.app.remote_coordinator.AudioCaptureService",
                    _StickyStopAudioCaptureService,
                ),
            ):
                exit_code = coordinator.run()

        self.assertEqual(0, exit_code)
        self.assertEqual([b"a" * 8], connection.sent_audio)
        self.assertEqual(["stop"], connection.sent_controls)

    def test_remote_runner_start_times_out_when_backend_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = RemoteLiveSessionRunner(
                config=_sample_config(Path(temp_dir), remote_timeout_seconds=1),
                request=LiveStartRequest(
                    title="产品周会",
                    kind="meeting",
                    language="zh",
                    source_label="BlackHole 2ch",
                    source_ref="1",
                ),
                on_progress=lambda _event: None,
            )

            def _run_without_backend_ready(self) -> int:
                self._stop_event.wait(5)
                return 0

            with patch.object(RemoteLiveSessionRunner, "run", _run_without_backend_ready):
                try:
                    with self.assertRaisesRegex(RuntimeError, "启动超时"):
                        runner.start()
                finally:
                    runner.request_stop()
                    runner.join(timeout=1)
                    self.assertFalse(runner.is_alive)

    def test_remote_service_live_session_reports_startup_error_before_session_started(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = RemoteSessionService(_sample_config(Path(temp_dir)))
            websocket = _FakeStartupWebSocket(
                {
                    "type": "start",
                    "title": "产品周会",
                    "kind": "meeting",
                    "language": "zh",
                    "source_label": "BlackHole 2ch",
                    "source_ref": "1",
                }
            )

            with patch.object(
                RemoteLiveSessionRunner,
                "start",
                side_effect=RuntimeError("后端未就绪"),
            ):
                asyncio.run(service.live_session(websocket))

        self.assertTrue(websocket.accepted)
        self.assertTrue(websocket.closed)
        self.assertGreaterEqual(len(websocket.sent_payloads), 1)
        self.assertEqual("error", websocket.sent_payloads[0]["type"])
        self.assertNotIn("session_started", {item["type"] for item in websocket.sent_payloads})

    def test_remote_service_live_session_startup_wait_does_not_block_event_loop(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = RemoteSessionService(_sample_config(Path(temp_dir)))
            websocket = _FakeStartupWebSocket(
                {
                    "type": "start",
                    "title": "产品周会",
                    "kind": "meeting",
                    "language": "zh",
                    "source_label": "BlackHole 2ch",
                    "source_ref": "1",
                }
            )
            async_ready_signal = threading.Event()

            def _start_waiting_for_async_signal(_self) -> None:
                if not async_ready_signal.wait(timeout=0.6):
                    raise RuntimeError("event loop blocked")
                raise RuntimeError("后端未就绪")

            async def _run_case() -> None:
                async def _signal_from_loop() -> None:
                    await asyncio.sleep(0.05)
                    async_ready_signal.set()

                signal_task = asyncio.create_task(_signal_from_loop())
                await service.live_session(websocket)
                await signal_task

            with patch.object(
                RemoteLiveSessionRunner,
                "start",
                _start_waiting_for_async_signal,
            ):
                asyncio.run(_run_case())

        self.assertTrue(websocket.accepted)
        self.assertTrue(websocket.closed)
        self.assertGreaterEqual(len(websocket.sent_payloads), 1)
        self.assertEqual("error", websocket.sent_payloads[0]["type"])
        self.assertIn("后端未就绪", str(websocket.sent_payloads[0].get("error")))
        self.assertNotIn("event loop blocked", str(websocket.sent_payloads[0].get("error")))
        self.assertNotIn("session_started", {item["type"] for item in websocket.sent_payloads})

    def test_remote_service_receive_loop_uses_nonblocking_audio_enqueue_contract(self) -> None:
        service = RemoteSessionService(_sample_config(Path(tempfile.mkdtemp())))
        websocket = _SequencedLiveWebSocket(
            {"type": "start", "title": "产品周会"},
            [
                {"type": "websocket.receive", "bytes": b"\x01\x02", "text": None},
                {"type": "websocket.receive", "text": json.dumps({"type": "stop"}), "bytes": None},
            ],
        )
        runner = _QueueingRunner()

        asyncio.run(service._receive_live_messages(websocket, runner))

        self.assertEqual([b"\x01\x02"], runner.enqueued_audio)
        self.assertTrue(runner.stop_requested)

    def test_remote_service_live_session_emits_stop_received_before_completed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = RemoteSessionService(_sample_config(Path(temp_dir)))
            websocket = _SequencedLiveWebSocket(
                {"type": "start", "title": "产品周会"},
                [
                    {
                        "type": "websocket.receive",
                        "text": json.dumps({"type": "stop"}),
                        "bytes": None,
                    },
                ],
            )
            metadata = create_session_metadata(
                config=service.config,
                title="产品周会",
                kind="meeting",
                language="zh",
                input_mode="live",
                source_label="BlackHole 2ch",
                source_ref="1",
            )

            class _StubRunner(_QueueingRunner):
                def __init__(self, on_event=None) -> None:
                    super().__init__()
                    self.on_event = on_event
                    self.postprocess_task_payload = {
                        "task_id": "task-post-1",
                        "server_id": "server-1",
                        "action": "postprocess",
                        "label": "后台整理",
                        "session_id": "remote-1",
                        "status": "running",
                        "stage": "handoff",
                        "message": "后台整理已接管。",
                        "result_version": 0,
                        "can_cancel": False,
                    }

                def start(self):
                    return metadata

                def join(self, timeout: float | None = None) -> None:
                    return None

                def ensure_postprocess_task_payload(self):
                    return self.postprocess_task_payload

            def _runner_factory(*_args, **kwargs):
                return _StubRunner(on_event=kwargs.get("on_event"))

            with patch(
                "live_note.remote.service.RemoteLiveSessionRunner", side_effect=_runner_factory
            ):
                asyncio.run(service.live_session(websocket))

        event_types = [item["type"] for item in websocket.sent_payloads]
        self.assertEqual("session_started", event_types[0])
        self.assertEqual("stop_received", event_types[1])
        self.assertEqual("task-post-1", websocket.sent_payloads[1]["postprocess_task"]["task_id"])
        self.assertEqual("completed", event_types[-1])

    def test_remote_service_live_session_keeps_handoff_when_stop_ack_send_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = RemoteSessionService(_sample_config(Path(temp_dir)))
            websocket = _FailingSendWebSocket(
                {"type": "start", "title": "产品周会"},
                [
                    {
                        "type": "websocket.receive",
                        "text": json.dumps({"type": "stop"}),
                        "bytes": None,
                    },
                ],
                fail_types={"stop_received", "completed"},
            )
            metadata = create_session_metadata(
                config=service.config,
                title="产品周会",
                kind="meeting",
                language="zh",
                input_mode="live",
                source_label="BlackHole 2ch",
                source_ref="1",
            )

            class _StubRunner(_QueueingRunner):
                def __init__(self, on_event=None) -> None:
                    super().__init__()
                    self.on_event = on_event
                    self.postprocess_task_payload = {
                        "task_id": "task-post-1",
                        "server_id": "server-1",
                        "action": "postprocess",
                        "status": "running",
                    }

                def start(self):
                    return metadata

                def join(self, timeout: float | None = None) -> None:
                    return None

            def _runner_factory(*_args, **kwargs):
                return _StubRunner(on_event=kwargs.get("on_event"))

            with patch(
                "live_note.remote.service.RemoteLiveSessionRunner", side_effect=_runner_factory
            ):
                asyncio.run(service.live_session(websocket))

        self.assertTrue(websocket.accepted)
        self.assertTrue(websocket.closed)

    def test_remote_runner_enqueue_audio_bytes_waits_for_capacity_when_ingress_queue_is_full(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = RemoteLiveSessionRunner(
                config=_sample_config(Path(temp_dir)),
                request=LiveStartRequest(
                    title="产品周会",
                    kind="meeting",
                    language="zh",
                    source_label="BlackHole 2ch",
                    source_ref="1",
                ),
                on_progress=lambda _event: None,
            )
            runner._ingress_audio_queue = queue.Queue(maxsize=1)
            runner._ingress_audio_queue.put(b"first")
            release_done = threading.Event()

            def _release_capacity_later() -> None:
                time.sleep(0.05)
                runner._ingress_audio_queue.get_nowait()
                release_done.set()

            releaser = threading.Thread(target=_release_capacity_later, daemon=True)
            releaser.start()
            started_at = time.monotonic()

            accepted = runner.enqueue_audio_bytes(b"second")
            waited_seconds = time.monotonic() - started_at

            releaser.join(timeout=1)

        self.assertTrue(accepted)
        self.assertFalse(runner._stop_event.is_set())
        self.assertIsNone(runner.failure_message)
        self.assertTrue(release_done.is_set())
        self.assertGreaterEqual(waited_seconds, 0.04)
        self.assertEqual(1, runner._ingress_audio_queue.qsize())

    def test_remote_service_receive_loop_waits_for_ingress_backpressure(self) -> None:
        service = RemoteSessionService(_sample_config(Path(tempfile.mkdtemp())))
        websocket = _SequencedLiveWebSocket(
            {"type": "start", "title": "产品周会"},
            [
                {"type": "websocket.receive", "bytes": b"\x01\x02", "text": None},
                {"type": "websocket.receive", "bytes": b"\x03\x04", "text": None},
            ],
        )

        class _BackpressureRunner(_QueueingRunner):
            def __init__(self) -> None:
                super().__init__()
                self._calls = 0
                self.waited = threading.Event()

            def enqueue_audio_bytes(self, payload: bytes) -> bool:
                self._calls += 1
                if self._calls == 2:
                    time.sleep(0.05)
                    self.waited.set()
                super().enqueue_audio_bytes(payload)
                return True

        runner = _BackpressureRunner()

        asyncio.run(service._receive_live_messages(websocket, runner))

        self.assertTrue(runner.stop_requested)
        self.assertTrue(runner.waited.is_set())
        self.assertEqual([b"\x01\x02", b"\x03\x04"], runner.enqueued_audio)

    def test_remote_runner_ingress_drain_thread_moves_audio_without_backend_loop(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            drained = threading.Event()

            class _SignalRunner(RemoteLiveSessionRunner):
                def feed_audio(self, pcm16: bytes) -> None:
                    super().feed_audio(pcm16)
                    drained.set()

            runner = _SignalRunner(
                config=_sample_config(Path(temp_dir)),
                request=LiveStartRequest(
                    title="产品周会",
                    kind="meeting",
                    language="zh",
                    source_label="BlackHole 2ch",
                    source_ref="1",
                ),
                on_progress=lambda _event: None,
            )
            runner._start_ingress_thread()
            try:
                accepted = runner.enqueue_audio_bytes(b"\x00\x00" * 480)
                self.assertTrue(accepted)
                self.assertTrue(drained.wait(timeout=1))
                diagnostics = runner.ingress_diagnostics()
                self.assertGreaterEqual(int(diagnostics["enqueue_count"]), 1)
                self.assertIn("enqueue_wait_max_ms", diagnostics)
            finally:
                runner.request_stop()
                runner._stop_ingress_thread(drain_pending=False)

    def test_remote_coordinator_emits_stopping_when_server_acknowledges_stop(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            events = []
            coordinator = RemoteLiveCoordinator(
                config=_sample_config(root),
                title="产品周会",
                source="1",
                kind="meeting",
                on_progress=events.append,
            )
            coordinator._apply_session_started(
                {
                    "session_id": "remote-1",
                    "started_at": "2026-03-18T10:00:00+00:00",
                    "title": "产品周会",
                    "kind": "meeting",
                    "language": "zh",
                    "source_label": "BlackHole 2ch",
                    "source_ref": "1",
                }
            )

            coordinator._event_queue.put(
                {
                    "type": "stop_received",
                    "session_id": "remote-1",
                    "message": "远端已确认停止，正在收尾当前片段。",
                }
            )
            coordinator._drain_remote_events(threading.Event())

        stopping_events = [event for event in events if event.stage == "stopping"]
        self.assertEqual(1, len(stopping_events))
        self.assertEqual("远端已确认停止，正在收尾当前片段。", stopping_events[0].message)

    def test_remote_coordinator_attaches_postprocess_task_without_fetching_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            connection = _FakeRemoteLiveConnection(
                completed_payload={
                    "type": "completed",
                    "session_id": "remote-1",
                }
            )
            client = _FakeRemoteClient(connection)
            coordinator = RemoteLiveCoordinator(
                config=_sample_config(root),
                title="产品周会",
                source="1",
                kind="meeting",
                client=client,
            )
            coordinator.request_stop()

            with (
                patch(
                    "live_note.app.remote_coordinator.resolve_input_device",
                    return_value=SimpleNamespace(index=1, name="BlackHole 2ch"),
                ),
                patch(
                    "live_note.app.remote_coordinator.AudioCaptureService",
                    _FakeAudioCaptureService,
                ),
            ):
                exit_code = coordinator.run()

            attachments = load_remote_tasks(root / ".live-note" / "remote_tasks.json")

        self.assertEqual(0, exit_code)
        self.assertEqual([], client.artifact_calls)
        self.assertEqual(1, len(attachments.records))
        self.assertEqual("task-post-1", attachments.records[0].remote_task_id)
        self.assertEqual("postprocess", attachments.records[0].action)

    def test_remote_coordinator_publishes_local_failure_notes_after_session_started(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            base_config = _sample_config(root)
            config = replace(
                base_config,
                obsidian=replace(base_config.obsidian, enabled=True),
            )
            connection = _FakeRemoteLiveConnection(
                completed_payload={
                    "type": "error",
                    "session_id": "remote-1",
                    "error": "远端后台失败",
                }
            )
            coordinator = RemoteLiveCoordinator(
                config=config,
                title="产品周会",
                source="1",
                kind="meeting",
                client=_FakeRemoteClient(connection),
            )
            coordinator.request_stop()

            def _fake_publish_failure_outputs(*, workspace, obsidian, reason, **_kwargs):
                self.assertEqual("remote-1", workspace.read_session().session_id)
                self.assertTrue(obsidian.is_enabled())
                self.assertIn("远端后台失败", reason)

            with (
                patch(
                    "live_note.app.remote_coordinator.resolve_input_device",
                    return_value=SimpleNamespace(index=1, name="BlackHole 2ch"),
                ),
                patch(
                    "live_note.app.remote_coordinator.AudioCaptureService",
                    _FakeAudioCaptureService,
                ),
                patch(
                    "live_note.app.remote_coordinator.publish_failure_outputs",
                    side_effect=_fake_publish_failure_outputs,
                ) as publish_mock,
            ):
                with self.assertRaisesRegex(Exception, "远端后台失败"):
                    coordinator.run()

        publish_mock.assert_called_once()

    def test_remote_coordinator_does_not_publish_failure_notes_on_post_start_disconnect(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            base_config = _sample_config(root)
            config = replace(
                base_config,
                obsidian=replace(base_config.obsidian, enabled=True),
            )
            connection = _DisconnectAfterStartConnection()
            coordinator = RemoteLiveCoordinator(
                config=config,
                title="产品周会",
                source="1",
                kind="meeting",
                client=_FakeRemoteClient(connection),
            )
            coordinator.request_stop()

            with (
                patch(
                    "live_note.app.remote_coordinator.resolve_input_device",
                    return_value=SimpleNamespace(index=1, name="BlackHole 2ch"),
                ),
                patch(
                    "live_note.app.remote_coordinator.AudioCaptureService",
                    _FakeAudioCaptureService,
                ),
                patch(
                    "live_note.app.remote_coordinator.publish_failure_outputs",
                ) as publish_mock,
            ):
                with self.assertRaisesRegex(Exception, "远端连接已断开"):
                    coordinator.run()

        publish_mock.assert_not_called()

    def test_disconnect_after_stop_ack_detaches_to_pending_postprocess(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            events = []
            connection = _DisconnectAfterStopAckConnection()
            client = _FakeRemoteClient(connection)
            coordinator = RemoteLiveCoordinator(
                config=_sample_config(root),
                title="产品周会",
                source="1",
                kind="meeting",
                client=client,
                on_progress=events.append,
            )
            coordinator.request_stop()

            with (
                patch(
                    "live_note.app.remote_coordinator.resolve_input_device",
                    return_value=SimpleNamespace(index=1, name="BlackHole 2ch"),
                ),
                patch(
                    "live_note.app.remote_coordinator.AudioCaptureService",
                    _FakeAudioCaptureService,
                ),
            ):
                exit_code = coordinator.run()

            attachments = load_remote_tasks(root / ".live-note" / "remote_tasks.json")

        self.assertEqual(0, exit_code)
        self.assertEqual([], client.artifact_calls)
        self.assertEqual(1, len(attachments.records))
        self.assertEqual("postprocess", attachments.records[0].action)
        self.assertEqual("remote-1", attachments.records[0].session_id)
        self.assertEqual("awaiting_rebind", attachments.records[0].attachment_state)
        self.assertEqual("running", attachments.records[0].last_known_status)
        attached_events = [event for event in events if event.stage == "postprocess_attached"]
        self.assertEqual(1, len(attached_events))
        self.assertIn("等待后台整理接管", attached_events[0].message)

    def test_remote_coordinator_attaches_postprocess_task_on_stop_received(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            events = []
            coordinator = RemoteLiveCoordinator(
                config=_sample_config(root),
                title="产品周会",
                source="1",
                kind="meeting",
                on_progress=events.append,
            )
            coordinator._apply_session_started(
                {
                    "session_id": "remote-1",
                    "started_at": "2026-03-18T10:00:00+00:00",
                    "title": "产品周会",
                    "kind": "meeting",
                    "language": "zh",
                    "source_label": "BlackHole 2ch",
                    "source_ref": "1",
                }
            )

            coordinator._event_queue.put(
                {
                    "type": "stop_received",
                    "session_id": "remote-1",
                    "message": "远端已确认停止，后台整理任务已创建。",
                    "postprocess_task": {
                        "task_id": "task-post-1",
                        "server_id": "server-1",
                        "action": "postprocess",
                        "label": "后台整理",
                        "session_id": "remote-1",
                        "status": "running",
                        "stage": "handoff",
                        "message": "后台整理已接管。",
                        "result_version": 0,
                        "can_cancel": False,
                    },
                }
            )
            coordinator._drain_remote_events(threading.Event())
            attachments = load_remote_tasks(root / ".live-note" / "remote_tasks.json")

        self.assertEqual(1, len(attachments.records))
        self.assertEqual("task-post-1", attachments.records[0].remote_task_id)
        self.assertEqual("attached", attachments.records[0].attachment_state)
        self.assertEqual("postprocess", attachments.records[0].action)
        attached_events = [event for event in events if event.stage == "postprocess_attached"]
        self.assertEqual(1, len(attached_events))
        self.assertIn("后台整理任务已创建", attached_events[0].message)


def _sample_config(
    root: Path,
    *,
    funasr_enabled: bool = False,
    remote_timeout_seconds: int = 20,
) -> AppConfig:
    model_path = root / "ggml-large-v3.bin"
    model_path.write_bytes(b"fake-model")
    return AppConfig(
        audio=AudioConfig(save_session_wav=True),
        importer=ImportConfig(ffmpeg_binary="/opt/homebrew/bin/ffmpeg"),
        refine=RefineConfig(),
        whisper=WhisperConfig(
            binary="/Users/demo/whisper-server",
            model=model_path,
        ),
        obsidian=ObsidianConfig(
            base_url="https://127.0.0.1:27124",
            transcript_dir="Sessions/Transcripts",
            structured_dir="Sessions/Summaries",
            enabled=False,
        ),
        llm=LlmConfig(
            base_url="https://api.openai.com/v1",
            model="gpt-4.1-mini",
            enabled=False,
        ),
        remote=RemoteConfig(enabled=True, timeout_seconds=remote_timeout_seconds),
        serve=ServeConfig(),
        funasr=FunAsrConfig(enabled=funasr_enabled),
        speaker=SpeakerConfig(),
        root_dir=root,
    )
