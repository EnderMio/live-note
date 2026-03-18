from __future__ import annotations

import queue
import tempfile
import threading
import unittest
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from live_note.app.coordinator import (
    FRAME_STOP,
    SEGMENT_CONTEXT_RESET,
    SEGMENT_STOP,
    FileImportCoordinator,
    SessionCoordinator,
    finalize_session,
)
from live_note.app.journal import SessionWorkspace, list_sessions
from live_note.audio.capture import InputDevice
from live_note.audio.convert import AudioImportError
from live_note.config import (
    AppConfig,
    AudioConfig,
    ImportConfig,
    LlmConfig,
    ObsidianConfig,
    RefineConfig,
    WhisperConfig,
)
from live_note.domain import PendingSegment, SessionMetadata, TranscriptEntry


class _FakeObsidianClient:
    def put_note(self, path: str, content: str) -> None:
        del path, content


class CoordinatorFailureTests(unittest.TestCase):
    def test_session_coordinator_applies_auto_refine_override_to_runtime_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _sample_config(root)

            runner = SessionCoordinator(
                config=config,
                title="产品周会",
                source="1",
                kind="meeting",
                auto_refine_after_live=False,
            )

        self.assertTrue(config.refine.auto_after_live)
        self.assertFalse(runner.config.refine.auto_after_live)

    def test_live_coordinator_emits_capture_finished_before_segment_queue_drains(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = replace(
                _sample_config(root),
                refine=RefineConfig(enabled=False, auto_after_live=False),
            )
            events = []
            capture_started = threading.Event()
            allow_segment_finish = threading.Event()

            runner = SessionCoordinator(
                config=config,
                title="产品周会",
                source="1",
                kind="meeting",
                on_progress=events.append,
            )

            class _FakeCapture:
                def __init__(self, *_args, **_kwargs) -> None:
                    self._stopped = False

                @property
                def error(self) -> Exception | None:
                    return None

                @property
                def is_alive(self) -> bool:
                    return not self._stopped

                @property
                def is_paused(self) -> bool:
                    return False

                def start(self) -> None:
                    capture_started.set()

                def stop(self) -> None:
                    self._stopped = True

                def pause(self) -> None:
                    return None

                def resume(self) -> None:
                    return None

                def join(self, timeout: float | None = None) -> None:
                    del timeout

            def fake_segment_loop(
                _self,
                frame_queue,
                segment_queue,
                segmenter,
                workspace,
            ) -> None:
                del _self, segmenter, workspace
                while True:
                    item = frame_queue.get()
                    if item is FRAME_STOP:
                        allow_segment_finish.wait(timeout=2)
                        break
                segment_queue.put(SEGMENT_STOP)

            def fake_transcribe_loop(
                _self,
                segment_queue,
                workspace,
                metadata,
                obsidian,
                whisper_client,
            ) -> None:
                del _self, workspace, metadata, obsidian, whisper_client
                while True:
                    if segment_queue.get() is SEGMENT_STOP:
                        return

            with (
                patch(
                    "live_note.app.coordinator.resolve_input_device",
                    return_value=InputDevice(1, "BlackHole 2ch", 2, 48000),
                ),
                patch("live_note.app.coordinator._attach_console_logging"),
                patch(
                    "live_note.app.coordinator.ObsidianClient",
                    return_value=_FakeObsidianClient(),
                ),
                patch("live_note.app.coordinator.OpenAiCompatibleClient"),
                patch("live_note.app.coordinator.WhisperInferenceClient"),
                patch("live_note.app.coordinator.WhisperServerProcess", return_value=nullcontext()),
                patch("live_note.app.coordinator.write_initial_transcript"),
                patch("live_note.app.coordinator.publish_final_outputs"),
                patch("live_note.app.coordinator.SpeechSegmenter"),
                patch("live_note.app.coordinator.AudioCaptureService", _FakeCapture),
                patch.object(SessionCoordinator, "_segment_loop", fake_segment_loop),
                patch.object(SessionCoordinator, "_transcribe_loop", fake_transcribe_loop),
            ):
                worker = threading.Thread(target=runner.run, daemon=True)
                worker.start()
                self.assertTrue(capture_started.wait(timeout=1))
                runner.request_stop()

                for _ in range(20):
                    if any(event.stage == "capture_finished" for event in events):
                        break
                    threading.Event().wait(0.05)

                self.assertTrue(
                    any(event.stage == "capture_finished" for event in events),
                    "停止录音后应先进入后台收尾，再等待分段线程完全排空。",
                )
                self.assertTrue(worker.is_alive(), "后台收尾期间主任务线程应仍在运行。")

                allow_segment_finish.set()
                worker.join(timeout=2)
                self.assertFalse(worker.is_alive(), "释放分段线程后会话应完成退出。")

    def test_live_transcribe_loop_resets_prompt_context_after_pause_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _sample_config(root)
            runner = SessionCoordinator(
                config=config,
                title="产品周会",
                source="1",
                kind="meeting",
            )
            workspace = SessionWorkspace.create(
                root / ".live-note" / "sessions" / "session-1",
                replace(
                    _sample_metadata(root / ".live-note" / "sessions" / "session-1"),
                    session_dir=str(root / ".live-note" / "sessions" / "session-1"),
                ),
            )
            metadata = workspace.read_session()
            segment_queue: queue.Queue[object] = queue.Queue()
            segment_queue.put(_pending_segment(root, "seg-00001", 0, 2000))
            segment_queue.put(SEGMENT_CONTEXT_RESET)
            segment_queue.put(_pending_segment(root, "seg-00002", 2000, 4000))
            segment_queue.put(SEGMENT_STOP)
            prompt_lengths: list[int] = []

            def fake_process_segment(**kwargs) -> bool:
                context_entries = kwargs["context_entries"]
                entries = kwargs["entries"]
                pending = kwargs["pending"]
                prompt_lengths.append(len(context_entries))

                created = TranscriptEntry(
                    segment_id=pending.segment_id,
                    started_ms=pending.started_ms,
                    ended_ms=pending.ended_ms,
                    text=pending.segment_id,
                )
                entries.append(created)
                context_entries.append(created)
                return True

            with patch(
                "live_note.app.coordinator._process_segment",
                side_effect=fake_process_segment,
            ):
                runner._transcribe_loop(
                    segment_queue=segment_queue,
                    workspace=workspace,
                    metadata=metadata,
                    obsidian=_FakeObsidianClient(),
                    whisper_client=object(),
                )

        self.assertEqual([0, 0], prompt_lengths)

    def test_live_coordinator_marks_session_failed_when_startup_step_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _sample_config(root)
            runner = SessionCoordinator(
                config=config,
                title="产品周会",
                source="1",
                kind="meeting",
            )

            with (
                patch(
                    "live_note.app.coordinator.resolve_input_device",
                    return_value=InputDevice(1, "BlackHole 2ch", 2, 48000),
                ),
                patch("live_note.app.coordinator._attach_console_logging"),
                patch(
                    "live_note.app.coordinator.ObsidianClient",
                    return_value=_FakeObsidianClient(),
                ),
                patch("live_note.app.coordinator.OpenAiCompatibleClient"),
                patch("live_note.app.coordinator.WhisperInferenceClient"),
                patch("live_note.app.coordinator.WhisperServerProcess"),
                patch(
                    "live_note.app.coordinator.write_initial_transcript",
                    side_effect=RuntimeError("startup boom"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "startup boom"):
                    runner.run()

            metadata = _load_single_session_metadata(root)

        self.assertEqual("failed", metadata.status)

    def test_import_coordinator_marks_session_failed_when_processing_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            media_path = root / "sample.mp3"
            media_path.write_bytes(b"fake-audio")
            config = _sample_config(root)
            runner = FileImportCoordinator(
                config=config,
                file_path=str(media_path),
                title="课程录音",
                kind="lecture",
            )

            with (
                patch("live_note.app.coordinator._attach_console_logging"),
                patch(
                    "live_note.app.coordinator.ObsidianClient",
                    return_value=_FakeObsidianClient(),
                ),
                patch("live_note.app.coordinator.OpenAiCompatibleClient"),
                patch("live_note.app.coordinator.WhisperInferenceClient"),
                patch(
                    "live_note.app.coordinator.convert_audio_to_wav",
                    side_effect=AudioImportError("convert boom"),
                ),
            ):
                with self.assertRaisesRegex(AudioImportError, "convert boom"):
                    runner.run()

            metadata = _load_single_session_metadata(root)

        self.assertEqual("failed", metadata.status)

    def test_finalize_session_skips_whisper_runtime_when_no_segments_are_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _sample_config(root)
            session_dir = root / ".live-note" / "sessions" / "session-1"
            workspace = SessionWorkspace.create(session_dir, _sample_metadata(session_dir))
            wav_path = session_dir / "segments" / "seg-00001.wav"
            wav_path.parent.mkdir(parents=True, exist_ok=True)
            wav_path.write_bytes(b"RIFF")
            workspace.record_segment_created("seg-00001", 0, 1000, wav_path)
            workspace.record_segment_text("seg-00001", 0, 1000, "已有转写")

            with (
                patch("live_note.app.coordinator._attach_console_logging"),
                patch(
                    "live_note.app.coordinator.ObsidianClient",
                    return_value=_FakeObsidianClient(),
                ),
                patch("live_note.app.coordinator.OpenAiCompatibleClient"),
                patch("live_note.app.coordinator.publish_final_outputs"),
                patch(
                    "live_note.app.coordinator._runtime_whisper_config",
                    side_effect=AssertionError("不应在无缺失片段时初始化 whisper"),
                ),
            ):
                exit_code = finalize_session(config, "session-1")

        self.assertEqual(0, exit_code)


def _sample_config(root: Path) -> AppConfig:
    model_path = root / "ggml-large-v3.bin"
    model_path.write_bytes(b"fake-model")
    return AppConfig(
        audio=AudioConfig(),
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
        root_dir=root,
    )


def _sample_metadata(session_dir: Path) -> SessionMetadata:
    return SessionMetadata(
        session_id="session-1",
        title="产品周会",
        kind="meeting",
        input_mode="live",
        source_label="BlackHole 2ch",
        source_ref="1",
        language="zh",
        started_at="2026-03-18T10:00:00+00:00",
        transcript_note_path="Sessions/Transcripts/2026-03-18/demo.md",
        structured_note_path="Sessions/Summaries/2026-03-18/demo.md",
        session_dir=str(session_dir),
        status="live",
    )


def _pending_segment(root: Path, segment_id: str, started_ms: int, ended_ms: int) -> PendingSegment:
    wav_path = root / f"{segment_id}.wav"
    wav_path.write_bytes(b"RIFF")

    return PendingSegment(
        segment_id=segment_id,
        started_ms=started_ms,
        ended_ms=ended_ms,
        pcm16=b"\x00\x00" * 1600,
        wav_path=wav_path,
    )


def _load_single_session_metadata(root: Path):
    session_root = next(iter(list_sessions(root)))
    return SessionWorkspace.load(session_root).read_session()
