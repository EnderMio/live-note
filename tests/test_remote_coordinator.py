from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from live_note.app.remote_coordinator import RemoteLiveCoordinator, _RemoteAudioBatcher
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
from live_note.remote.service import RemoteLiveSessionRunner


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


def _sample_config(root: Path) -> AppConfig:
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
        remote=RemoteConfig(enabled=True),
        serve=ServeConfig(),
        funasr=FunAsrConfig(),
        speaker=SpeakerConfig(),
        root_dir=root,
    )
