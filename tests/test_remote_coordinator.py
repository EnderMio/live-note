from __future__ import annotations

import asyncio
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from live_note.app.coordinator import create_session_metadata
from live_note.app.journal import SessionWorkspace
from live_note.app.remote_coordinator import RemoteLiveCoordinator, _RemoteAudioBatcher
from live_note.app.remote_tasks import load_remote_tasks
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
from live_note.domain import AudioFrame
from live_note.remote.protocol import LiveStartRequest
from live_note.remote.service import (
    RemoteLiveSessionRunner,
    RemoteSessionService,
    _FunAsrAudioBatcher,
    _FunAsrDraftTracker,
)
from live_note.transcribe.funasr import FunAsrMessage


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
    def __init__(self, *, completed_payload: dict[str, object] | None = None) -> None:
        self.sent_audio: list[bytes] = []
        self.sent_controls: list[str] = []
        self._stop_received = threading.Event()
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
            config = _sample_config(root)
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

    def test_remote_coordinator_attaches_postprocess_task_without_fetching_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            connection = _FakeRemoteLiveConnection(
                completed_payload={
                    "type": "completed",
                    "session_id": "remote-1",
                    "postprocess_task": {
                        "task_id": "task-post-1",
                        "server_id": "server-1",
                        "action": "postprocess",
                        "label": "后台整理",
                        "session_id": "remote-1",
                        "status": "running",
                        "stage": "refining",
                        "message": "正在后台整理。",
                        "result_version": 0,
                        "can_cancel": False,
                    },
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
