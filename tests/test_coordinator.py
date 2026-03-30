from __future__ import annotations

import logging
import queue
import tempfile
import threading
import unittest
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from live_note.app.coordinator import (
    FRAME_STOP,
    SEGMENT_CONTEXT_RESET,
    SEGMENT_STOP,
    FileImportCoordinator,
    SessionCoordinator,
    _process_segment,
    finalize_session,
    retranscribe_session,
)
from live_note.app.journal import SessionWorkspace, list_sessions
from live_note.app.task_errors import TaskCancelledError
from live_note.audio.capture import InputDevice, InputLevel
from live_note.audio.convert import AudioImportError
from live_note.config import (
    AppConfig,
    AudioConfig,
    ImportConfig,
    LlmConfig,
    ObsidianConfig,
    RefineConfig,
    SpeakerConfig,
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

    def test_session_coordinator_input_level_callback_emits_progress_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            events = []
            runner = SessionCoordinator(
                config=_sample_config(root),
                title="产品周会",
                source="1",
                kind="meeting",
                on_progress=events.append,
            )

            callback = runner._build_input_level_callback("session-1")
            callback(InputLevel(normalized=0.52, peak=0.52, clipping=False))

        self.assertEqual("input_level", events[0].stage)
        self.assertEqual("OK", events[0].message)
        self.assertEqual("session-1", events[0].session_id)
        self.assertEqual(52, events[0].current)
        self.assertEqual(100, events[0].total)

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

    def test_live_coordinator_runs_speaker_diarization_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = replace(
                _sample_config(root),
                refine=RefineConfig(enabled=False, auto_after_live=False),
                speaker=SpeakerConfig(
                    enabled=True,
                    segmentation_model=Path("/models/segmentation.onnx"),
                    embedding_model=Path("/models/embedding.onnx"),
                ),
            )
            runner = SessionCoordinator(
                config=config,
                title="产品周会",
                source="1",
                kind="meeting",
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
                    return None

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
                    if frame_queue.get() is FRAME_STOP:
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

            def fake_apply_speaker_labels(_config, workspace, _metadata, **_kwargs):
                return workspace.update_session(speaker_status="done")

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
                patch(
                    "live_note.app.coordinator.apply_speaker_labels",
                    side_effect=fake_apply_speaker_labels,
                    create=True,
                ) as apply_mock,
            ):
                worker = threading.Thread(target=runner.run, daemon=True)
                worker.start()
                runner.request_stop()
                worker.join(timeout=2)
                self.assertFalse(worker.is_alive(), "会话停止后应完成退出。")

            metadata = _load_single_session_metadata(root)

        apply_mock.assert_called_once()
        self.assertEqual("done", metadata.speaker_status)

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

    def test_process_segment_keeps_live_draft_local_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = SessionWorkspace.create(
                root / ".live-note" / "sessions" / "session-1",
                replace(
                    _sample_metadata(root / ".live-note" / "sessions" / "session-1"),
                    session_dir=str(root / ".live-note" / "sessions" / "session-1"),
                ),
            )
            metadata = workspace.read_session()
            pending = _pending_segment(root, "seg-00001", 0, 2000)
            obsidian = _FakeObsidianClient()
            entries: list[TranscriptEntry] = []

            with (
                patch(
                    "live_note.app.coordinator._transcribe_segment_text",
                    return_value="大家好，开始吧。",
                ),
                patch(
                    "live_note.app.coordinator.try_sync_note",
                    side_effect=AssertionError("live 片段不应同步到 Obsidian"),
                ),
            ):
                result = _process_segment(
                    pending=pending,
                    workspace=workspace,
                    metadata=metadata,
                    obsidian=obsidian,
                    whisper_client=object(),
                    logger=logging.getLogger("test.coordinator"),
                    entries=entries,
                    live_status="live",
                )

            self.assertTrue(result)
            self.assertEqual(1, len(entries))
            self.assertIn("大家好，开始吧。", workspace.transcript_md.read_text(encoding="utf-8"))

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

    def test_import_coordinator_marks_session_cancelled_when_cancel_requested(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            media_path = root / "sample.mp3"
            media_path.write_bytes(b"fake-audio")
            config = _sample_config(root)
            cancel_event = threading.Event()
            runner = FileImportCoordinator(
                config=config,
                file_path=str(media_path),
                title="课程录音",
                kind="lecture",
                cancel_event=cancel_event,
            )

            def process_and_cancel(**_kwargs):
                cancel_event.set()
                return True

            def fake_split_wav_file(*, output_dir, **_kwargs):
                return [
                    SimpleNamespace(
                        segment_id="seg-00001",
                        started_ms=0,
                        ended_ms=1000,
                        wav_path=output_dir / "seg-00001.wav",
                    ),
                    SimpleNamespace(
                        segment_id="seg-00002",
                        started_ms=1000,
                        ended_ms=2000,
                        wav_path=output_dir / "seg-00002.wav",
                    ),
                ]

            with (
                patch("live_note.app.coordinator._attach_console_logging"),
                patch(
                    "live_note.app.coordinator.ObsidianClient",
                    return_value=_FakeObsidianClient(),
                ),
                patch("live_note.app.coordinator.OpenAiCompatibleClient"),
                patch("live_note.app.coordinator.WhisperInferenceClient"),
                patch("live_note.app.coordinator.convert_audio_to_wav"),
                patch("live_note.app.coordinator.split_wav_file", side_effect=fake_split_wav_file),
                patch("live_note.app.coordinator.WhisperServerProcess"),
                patch(
                    "live_note.app.coordinator._process_segment",
                    side_effect=process_and_cancel,
                ),
            ):
                with self.assertRaisesRegex(TaskCancelledError, "取消"):
                    runner.run()

            metadata = _load_single_session_metadata(root)

        self.assertEqual("cancelled", metadata.status)

    def test_import_coordinator_runs_speaker_diarization_with_normalized_audio_when_enabled(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            media_path = root / "sample.mp3"
            media_path.write_bytes(b"fake-audio")
            config = replace(
                _sample_config(root),
                speaker=SpeakerConfig(
                    enabled=True,
                    segmentation_model=Path("/models/segmentation.onnx"),
                    embedding_model=Path("/models/embedding.onnx"),
                ),
            )
            runner = FileImportCoordinator(
                config=config,
                file_path=str(media_path),
                title="课程录音",
                kind="lecture",
            )

            def fake_convert_audio_to_wav(*, output_path, **_kwargs):
                output_path.write_bytes(b"RIFF")
                return output_path

            def fake_split_wav_file(*, output_dir, **_kwargs):
                self.assertEqual(15, _kwargs["chunk_seconds"])
                chunk_path = output_dir / "seg-00001.wav"
                chunk_path.parent.mkdir(parents=True, exist_ok=True)
                chunk_path.write_bytes(b"RIFF")
                return [
                    SimpleNamespace(
                        segment_id="seg-00001",
                        started_ms=0,
                        ended_ms=1000,
                        wav_path=chunk_path,
                    )
                ]

            def fake_apply_speaker_labels(_config, workspace, metadata, *, audio_path, **_kwargs):
                self.assertEqual(workspace.root / "source.normalized.wav", audio_path)
                self.assertTrue(audio_path.exists())
                return workspace.update_session(speaker_status="done")

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
                    side_effect=fake_convert_audio_to_wav,
                ),
                patch("live_note.app.coordinator.split_wav_file", side_effect=fake_split_wav_file),
                patch("live_note.app.coordinator.WhisperServerProcess", return_value=nullcontext()),
                patch("live_note.app.coordinator._process_segment", return_value=True),
                patch(
                    "live_note.app.coordinator.apply_speaker_labels",
                    side_effect=fake_apply_speaker_labels,
                    create=True,
                ) as apply_mock,
                patch("live_note.app.coordinator.publish_final_outputs"),
            ):
                exit_code = runner.run()

            metadata = _load_single_session_metadata(root)

        self.assertEqual(0, exit_code)
        apply_mock.assert_called_once()
        self.assertEqual("done", metadata.speaker_status)

    def test_import_coordinator_marks_session_cancelled_when_speaker_stage_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            media_path = root / "sample.mp3"
            media_path.write_bytes(b"fake-audio")
            cancel_event = threading.Event()
            config = replace(
                _sample_config(root),
                speaker=SpeakerConfig(
                    enabled=True,
                    segmentation_model=Path("/models/segmentation.onnx"),
                    embedding_model=Path("/models/embedding.onnx"),
                ),
            )
            runner = FileImportCoordinator(
                config=config,
                file_path=str(media_path),
                title="课程录音",
                kind="lecture",
                cancel_event=cancel_event,
            )

            def fake_apply_speaker_labels(
                _config,
                _workspace,
                _metadata,
                *,
                cancel_callback=None,
                **_kwargs,
            ):
                cancel_event.set()
                assert cancel_callback is not None
                cancel_callback()
                raise AssertionError("应在 cancel_callback 内直接抛出取消")

            with (
                patch("live_note.app.coordinator._attach_console_logging"),
                patch(
                    "live_note.app.coordinator.ObsidianClient",
                    return_value=_FakeObsidianClient(),
                ),
                patch("live_note.app.coordinator.OpenAiCompatibleClient"),
                patch("live_note.app.coordinator.WhisperInferenceClient"),
                patch("live_note.app.coordinator.convert_audio_to_wav"),
                patch(
                    "live_note.app.coordinator.split_wav_file",
                    side_effect=lambda *, output_dir, **_kwargs: [
                        SimpleNamespace(
                            segment_id="seg-00001",
                            started_ms=0,
                            ended_ms=1000,
                            wav_path=output_dir / "seg-00001.wav",
                        )
                    ],
                ),
                patch("live_note.app.coordinator.WhisperServerProcess", return_value=nullcontext()),
                patch("live_note.app.coordinator._process_segment", return_value=True),
                patch(
                    "live_note.app.coordinator.apply_speaker_labels",
                    side_effect=fake_apply_speaker_labels,
                    create=True,
                ),
            ):
                with self.assertRaisesRegex(TaskCancelledError, "取消"):
                    runner.run()

            metadata = _load_single_session_metadata(root)

        self.assertEqual("cancelled", metadata.status)

    def test_import_coordinator_keeps_success_when_speaker_diarization_falls_back_to_failed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            media_path = root / "sample.mp3"
            media_path.write_bytes(b"fake-audio")
            config = replace(
                _sample_config(root),
                speaker=SpeakerConfig(
                    enabled=True,
                    segmentation_model=Path("/models/segmentation.onnx"),
                    embedding_model=Path("/models/embedding.onnx"),
                ),
            )
            runner = FileImportCoordinator(
                config=config,
                file_path=str(media_path),
                title="课程录音",
                kind="lecture",
            )

            def fake_convert_audio_to_wav(*, output_path, **_kwargs):
                output_path.write_bytes(b"RIFF")
                return output_path

            def fake_split_wav_file(*, output_dir, **_kwargs):
                chunk_path = output_dir / "seg-00001.wav"
                chunk_path.parent.mkdir(parents=True, exist_ok=True)
                chunk_path.write_bytes(b"RIFF")
                return [
                    SimpleNamespace(
                        segment_id="seg-00001",
                        started_ms=0,
                        ended_ms=1000,
                        wav_path=chunk_path,
                    )
                ]

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
                    side_effect=fake_convert_audio_to_wav,
                ),
                patch("live_note.app.coordinator.split_wav_file", side_effect=fake_split_wav_file),
                patch("live_note.app.coordinator.WhisperServerProcess", return_value=nullcontext()),
                patch("live_note.app.coordinator._process_segment", return_value=True),
                patch(
                    "live_note.app.coordinator.apply_speaker_labels",
                    side_effect=lambda _config, workspace, _metadata, **_kwargs: (
                        workspace.update_session(speaker_status="failed")
                    ),
                    create=True,
                ) as apply_mock,
                patch("live_note.app.coordinator.publish_final_outputs"),
            ):
                exit_code = runner.run()

            metadata = _load_single_session_metadata(root)

        self.assertEqual(0, exit_code)
        apply_mock.assert_called_once()
        self.assertEqual("failed", metadata.speaker_status)

    def test_import_suspicious_first_pass_triggers_retry_without_prompt(self) -> None:
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

            suspicious_text = "中文字幕:CaptionCube 中文字幕:CaptionCube"
            prompt_calls: list[str | None] = []

            class _FakeWhisperClient:
                def transcribe(self, _wav_path: Path, *, prompt: str | None = None) -> str:
                    prompt_calls.append(prompt)
                    if len(prompt_calls) == 1:
                        return suspicious_text
                    return "老师先讲目标，再讲实现步骤。"

            def fake_convert_audio_to_wav(*, output_path, **_kwargs):
                output_path.write_bytes(b"RIFF")
                return output_path

            def fake_split_wav_file(*, output_dir, **_kwargs):
                chunk_path = output_dir / "seg-00001.wav"
                chunk_path.parent.mkdir(parents=True, exist_ok=True)
                chunk_path.write_bytes(b"RIFF")
                return [
                    SimpleNamespace(
                        segment_id="seg-00001",
                        started_ms=0,
                        ended_ms=45_000,
                        wav_path=chunk_path,
                    )
                ]

            with (
                patch("live_note.app.coordinator._attach_console_logging"),
                patch(
                    "live_note.app.coordinator.ObsidianClient",
                    return_value=_FakeObsidianClient(),
                ),
                patch("live_note.app.coordinator.OpenAiCompatibleClient"),
                patch(
                    "live_note.app.coordinator.WhisperInferenceClient",
                    return_value=_FakeWhisperClient(),
                ),
                patch(
                    "live_note.app.coordinator.convert_audio_to_wav",
                    side_effect=fake_convert_audio_to_wav,
                ),
                patch("live_note.app.coordinator.split_wav_file", side_effect=fake_split_wav_file),
                patch(
                    "live_note.app.coordinator._read_wav_pcm16",
                    return_value=(b"\x00\x00" * 1600, 16000),
                ),
                patch("live_note.app.coordinator.WhisperServerProcess", return_value=nullcontext()),
                patch(
                    "live_note.app.coordinator.apply_speaker_labels",
                    side_effect=lambda _config, _workspace, metadata, **_kwargs: metadata,
                    create=True,
                ),
                patch("live_note.app.coordinator.publish_final_outputs"),
            ):
                exit_code = runner.run()

        self.assertEqual(0, exit_code)
        self.assertEqual(
            2,
            len(prompt_calls),
            "可疑首轮结果应触发一次无提示词重试，而不是直接接纳。",
        )
        self.assertIn("Transcribe only audible speech", prompt_calls[0] or "")
        self.assertIn(prompt_calls[1], (None, ""))

    def test_import_suspicious_text_does_not_enter_next_chunk_prompt_context(self) -> None:
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

            suspicious_text = "中文字幕:CaptionCube 中文字幕:CaptionCube"
            prompt_calls: list[str] = []
            transcribe_calls = 0

            class _FakeWhisperClient:
                def transcribe(self, _wav_path: Path, *, prompt: str | None = None) -> str:
                    nonlocal transcribe_calls
                    transcribe_calls += 1
                    prompt_calls.append(prompt or "")
                    if transcribe_calls == 1:
                        return suspicious_text
                    return "第二段是正常中文句子。"

            def fake_convert_audio_to_wav(*, output_path, **_kwargs):
                output_path.write_bytes(b"RIFF")
                return output_path

            def fake_split_wav_file(*, output_dir, **_kwargs):
                first_path = output_dir / "seg-00001.wav"
                second_path = output_dir / "seg-00002.wav"
                first_path.parent.mkdir(parents=True, exist_ok=True)
                first_path.write_bytes(b"RIFF")
                second_path.write_bytes(b"RIFF")
                return [
                    SimpleNamespace(
                        segment_id="seg-00001",
                        started_ms=0,
                        ended_ms=45_000,
                        wav_path=first_path,
                    ),
                    SimpleNamespace(
                        segment_id="seg-00002",
                        started_ms=45_000,
                        ended_ms=90_000,
                        wav_path=second_path,
                    ),
                ]

            with (
                patch("live_note.app.coordinator._attach_console_logging"),
                patch(
                    "live_note.app.coordinator.ObsidianClient",
                    return_value=_FakeObsidianClient(),
                ),
                patch("live_note.app.coordinator.OpenAiCompatibleClient"),
                patch(
                    "live_note.app.coordinator.WhisperInferenceClient",
                    return_value=_FakeWhisperClient(),
                ),
                patch(
                    "live_note.app.coordinator.convert_audio_to_wav",
                    side_effect=fake_convert_audio_to_wav,
                ),
                patch("live_note.app.coordinator.split_wav_file", side_effect=fake_split_wav_file),
                patch(
                    "live_note.app.coordinator._read_wav_pcm16",
                    return_value=(b"\x00\x00" * 1600, 16000),
                ),
                patch("live_note.app.coordinator.WhisperServerProcess", return_value=nullcontext()),
                patch(
                    "live_note.app.coordinator.apply_speaker_labels",
                    side_effect=lambda _config, _workspace, metadata, **_kwargs: metadata,
                    create=True,
                ),
                patch("live_note.app.coordinator.publish_final_outputs"),
            ):
                exit_code = runner.run()

        self.assertEqual(0, exit_code)
        self.assertEqual(3, len(prompt_calls))
        self.assertEqual("", prompt_calls[1], "可疑首轮后应先进行一次无提示词重试。")
        self.assertNotIn(
            suspicious_text,
            prompt_calls[2],
            "可疑文本不应进入下一段的提示词上下文。",
        )

    def test_import_still_suspicious_45_second_chunk_triggers_single_15_second_split_fallback(
        self,
    ) -> None:
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

            suspicious_text = "中文字幕:CaptionCube 中文字幕:CaptionCube"
            split_chunk_seconds: list[int] = []

            class _FakeWhisperClient:
                def transcribe(self, _wav_path: Path, *, prompt: str | None = None) -> str:
                    del prompt
                    return suspicious_text

            def fake_convert_audio_to_wav(*, output_path, **_kwargs):
                output_path.write_bytes(b"RIFF")
                return output_path

            def fake_split_wav_file(*, output_dir, chunk_seconds, **_kwargs):
                split_chunk_seconds.append(chunk_seconds)
                output_dir.mkdir(parents=True, exist_ok=True)
                if chunk_seconds == 45:
                    first = output_dir / "seg-00001.wav"
                    first.write_bytes(b"RIFF")
                    return [
                        SimpleNamespace(
                            segment_id="seg-00001",
                            started_ms=0,
                            ended_ms=45_000,
                            wav_path=first,
                        )
                    ]
                sub_1 = output_dir / "seg-00001-sub-1.wav"
                sub_2 = output_dir / "seg-00001-sub-2.wav"
                sub_3 = output_dir / "seg-00001-sub-3.wav"
                sub_1.write_bytes(b"RIFF")
                sub_2.write_bytes(b"RIFF")
                sub_3.write_bytes(b"RIFF")
                return [
                    SimpleNamespace(
                        segment_id="seg-00001-sub-1",
                        started_ms=0,
                        ended_ms=15_000,
                        wav_path=sub_1,
                    ),
                    SimpleNamespace(
                        segment_id="seg-00001-sub-2",
                        started_ms=15_000,
                        ended_ms=30_000,
                        wav_path=sub_2,
                    ),
                    SimpleNamespace(
                        segment_id="seg-00001-sub-3",
                        started_ms=30_000,
                        ended_ms=45_000,
                        wav_path=sub_3,
                    ),
                ]

            with (
                patch("live_note.app.coordinator._attach_console_logging"),
                patch(
                    "live_note.app.coordinator.ObsidianClient",
                    return_value=_FakeObsidianClient(),
                ),
                patch("live_note.app.coordinator.OpenAiCompatibleClient"),
                patch(
                    "live_note.app.coordinator.WhisperInferenceClient",
                    return_value=_FakeWhisperClient(),
                ),
                patch(
                    "live_note.app.coordinator.convert_audio_to_wav",
                    side_effect=fake_convert_audio_to_wav,
                ),
                patch("live_note.app.coordinator.split_wav_file", side_effect=fake_split_wav_file),
                patch(
                    "live_note.app.coordinator._read_wav_pcm16",
                    return_value=(b"\x00\x00" * 1600, 16000),
                ),
                patch("live_note.app.coordinator.WhisperServerProcess", return_value=nullcontext()),
                patch(
                    "live_note.app.coordinator.apply_speaker_labels",
                    side_effect=lambda _config, _workspace, metadata, **_kwargs: metadata,
                    create=True,
                ),
                patch("live_note.app.coordinator.publish_final_outputs"),
            ):
                exit_code = runner.run()

            workspace = SessionWorkspace.load(next(iter(list_sessions(root))))
            states = workspace.rebuild_segment_states()
            segments_journal = workspace.segments_jsonl.read_text(encoding="utf-8")

        self.assertEqual(0, exit_code)
        self.assertEqual(
            [45, 15],
            split_chunk_seconds,
            "45 秒片段在重试后仍可疑时应只降一级切到 15 秒。",
        )
        self.assertEqual(1, len(states))
        self.assertEqual("seg-00001", states[0].segment_id)
        self.assertEqual("疑似字幕/幻觉，自动恢复失败", states[0].error)
        self.assertNotIn("seg-00001-sub", segments_journal)

    def test_import_split_fallback_merges_only_accepted_child_text_into_parent_segment(
        self,
    ) -> None:
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

            suspicious_text = "中文字幕:CaptionCube 中文字幕:CaptionCube"
            split_chunk_seconds: list[int] = []
            transcribe_calls: list[tuple[str, str | None]] = []

            class _FakeWhisperClient:
                def transcribe(self, wav_path: Path, *, prompt: str | None = None) -> str:
                    transcribe_calls.append((wav_path.name, prompt))
                    if wav_path.name == "seg-00001.wav":
                        return suspicious_text
                    if wav_path.name.endswith("sub-1.wav"):
                        return suspicious_text
                    if wav_path.name.endswith("sub-2.wav"):
                        return "老师先确认需求范围。"
                    if wav_path.name.endswith("sub-3.wav"):
                        return "然后明确验收标准。"
                    raise AssertionError(f"unexpected wav path: {wav_path}")

            def fake_convert_audio_to_wav(*, output_path, **_kwargs):
                output_path.write_bytes(b"RIFF")
                return output_path

            def fake_split_wav_file(*, output_dir, chunk_seconds, **_kwargs):
                split_chunk_seconds.append(chunk_seconds)
                output_dir.mkdir(parents=True, exist_ok=True)
                if chunk_seconds == 45:
                    first = output_dir / "seg-00001.wav"
                    first.write_bytes(b"RIFF")
                    return [
                        SimpleNamespace(
                            segment_id="seg-00001",
                            started_ms=0,
                            ended_ms=45_000,
                            wav_path=first,
                        )
                    ]
                sub_1 = output_dir / "seg-00001-sub-1.wav"
                sub_2 = output_dir / "seg-00001-sub-2.wav"
                sub_3 = output_dir / "seg-00001-sub-3.wav"
                sub_1.write_bytes(b"RIFF")
                sub_2.write_bytes(b"RIFF")
                sub_3.write_bytes(b"RIFF")
                return [
                    SimpleNamespace(
                        segment_id="seg-00001-sub-1",
                        started_ms=0,
                        ended_ms=15_000,
                        wav_path=sub_1,
                    ),
                    SimpleNamespace(
                        segment_id="seg-00001-sub-2",
                        started_ms=15_000,
                        ended_ms=30_000,
                        wav_path=sub_2,
                    ),
                    SimpleNamespace(
                        segment_id="seg-00001-sub-3",
                        started_ms=30_000,
                        ended_ms=45_000,
                        wav_path=sub_3,
                    ),
                ]

            with (
                patch("live_note.app.coordinator._attach_console_logging"),
                patch(
                    "live_note.app.coordinator.ObsidianClient",
                    return_value=_FakeObsidianClient(),
                ),
                patch("live_note.app.coordinator.OpenAiCompatibleClient"),
                patch(
                    "live_note.app.coordinator.WhisperInferenceClient",
                    return_value=_FakeWhisperClient(),
                ),
                patch(
                    "live_note.app.coordinator.convert_audio_to_wav",
                    side_effect=fake_convert_audio_to_wav,
                ),
                patch("live_note.app.coordinator.split_wav_file", side_effect=fake_split_wav_file),
                patch(
                    "live_note.app.coordinator._read_wav_pcm16",
                    return_value=(b"\x00\x00" * 1600, 16000),
                ),
                patch("live_note.app.coordinator.WhisperServerProcess", return_value=nullcontext()),
                patch(
                    "live_note.app.coordinator.apply_speaker_labels",
                    side_effect=lambda _config, _workspace, metadata, **_kwargs: metadata,
                    create=True,
                ),
                patch("live_note.app.coordinator.publish_final_outputs"),
            ):
                exit_code = runner.run()

            workspace = SessionWorkspace.load(next(iter(list_sessions(root))))
            states = workspace.rebuild_segment_states()
            segments_journal = workspace.segments_jsonl.read_text(encoding="utf-8")

        self.assertEqual(0, exit_code)
        self.assertEqual([45, 15], split_chunk_seconds)
        self.assertEqual(1, len(states))
        self.assertEqual("seg-00001", states[0].segment_id)
        self.assertEqual("老师先确认需求范围。 然后明确验收标准。", states[0].text)
        self.assertIsNone(states[0].error)
        self.assertNotIn("seg-00001-sub", segments_journal)
        child_prompts = [prompt for wav_name, prompt in transcribe_calls if "sub-" in wav_name]
        self.assertTrue(child_prompts)
        self.assertTrue(all(prompt in (None, "") for prompt in child_prompts))

    def test_import_suspicious_15_second_chunk_records_deterministic_failure(self) -> None:
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

            suspicious_text = "中文字幕:CaptionCube 中文字幕:CaptionCube"
            prompt_calls: list[str | None] = []
            split_chunk_seconds: list[int] = []

            class _FakeWhisperClient:
                def transcribe(self, _wav_path: Path, *, prompt: str | None = None) -> str:
                    prompt_calls.append(prompt)
                    return suspicious_text

            def fake_convert_audio_to_wav(*, output_path, **_kwargs):
                output_path.write_bytes(b"RIFF")
                return output_path

            def fake_split_wav_file(*, output_dir, chunk_seconds, **_kwargs):
                split_chunk_seconds.append(chunk_seconds)
                chunk_path = output_dir / "seg-00001.wav"
                chunk_path.parent.mkdir(parents=True, exist_ok=True)
                chunk_path.write_bytes(b"RIFF")
                return [
                    SimpleNamespace(
                        segment_id="seg-00001",
                        started_ms=0,
                        ended_ms=15_000,
                        wav_path=chunk_path,
                    )
                ]

            with (
                patch("live_note.app.coordinator._attach_console_logging"),
                patch(
                    "live_note.app.coordinator.ObsidianClient",
                    return_value=_FakeObsidianClient(),
                ),
                patch("live_note.app.coordinator.OpenAiCompatibleClient"),
                patch(
                    "live_note.app.coordinator.WhisperInferenceClient",
                    return_value=_FakeWhisperClient(),
                ),
                patch(
                    "live_note.app.coordinator.convert_audio_to_wav",
                    side_effect=fake_convert_audio_to_wav,
                ),
                patch("live_note.app.coordinator.split_wav_file", side_effect=fake_split_wav_file),
                patch(
                    "live_note.app.coordinator._read_wav_pcm16",
                    return_value=(b"\x00\x00" * 1600, 16000),
                ),
                patch("live_note.app.coordinator.WhisperServerProcess", return_value=nullcontext()),
                patch(
                    "live_note.app.coordinator.apply_speaker_labels",
                    side_effect=lambda _config, _workspace, metadata, **_kwargs: metadata,
                    create=True,
                ),
                patch("live_note.app.coordinator.publish_final_outputs"),
            ):
                exit_code = runner.run()

            workspace = SessionWorkspace.load(next(iter(list_sessions(root))))
            states = workspace.rebuild_segment_states()

        self.assertEqual(0, exit_code)
        self.assertEqual([45], split_chunk_seconds)
        self.assertEqual(2, len(prompt_calls), "短片段仅应首轮+无提示词重试，不应进入切分。")
        self.assertIn("Transcribe only audible speech", prompt_calls[0] or "")
        self.assertIn(prompt_calls[1], (None, ""))
        self.assertEqual(1, len(states))
        self.assertEqual("seg-00001", states[0].segment_id)
        self.assertEqual("疑似字幕/幻觉，自动恢复失败", states[0].error)
        self.assertIsNone(states[0].text)

    def test_finalize_recovery_45_second_chunk_uses_same_split_fallback_ladder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _sample_config(root)
            session_dir = root / ".live-note" / "sessions" / "session-1"
            workspace = SessionWorkspace.create(session_dir, _sample_metadata(session_dir))
            wav_path = session_dir / "segments" / "seg-00001.wav"
            wav_path.parent.mkdir(parents=True, exist_ok=True)
            wav_path.write_bytes(b"RIFF")
            workspace.record_segment_created("seg-00001", 0, 45_000, wav_path)
            split_chunk_seconds: list[int] = []
            suspicious_text = "中文字幕:CaptionCube 中文字幕:CaptionCube"
            transcribe_calls: list[tuple[str, str | None]] = []

            class _FakeWhisperClient:
                def transcribe(self, wav: Path, *, prompt: str | None = None) -> str:
                    transcribe_calls.append((wav.name, prompt))
                    if wav.name == "seg-00001.wav":
                        return suspicious_text
                    if wav.name.endswith("sub-1.wav"):
                        return suspicious_text
                    if wav.name.endswith("sub-2.wav"):
                        return "先补齐关键事实。"
                    if wav.name.endswith("sub-3.wav"):
                        return "再写后续行动项。"
                    raise AssertionError(f"unexpected wav path: {wav}")

            def fake_split_wav_file(*, output_dir, chunk_seconds, **_kwargs):
                split_chunk_seconds.append(chunk_seconds)
                output_dir.mkdir(parents=True, exist_ok=True)
                sub_1 = output_dir / "seg-00001-sub-1.wav"
                sub_2 = output_dir / "seg-00001-sub-2.wav"
                sub_3 = output_dir / "seg-00001-sub-3.wav"
                sub_1.write_bytes(b"RIFF")
                sub_2.write_bytes(b"RIFF")
                sub_3.write_bytes(b"RIFF")
                return [
                    SimpleNamespace(
                        segment_id="seg-00001-sub-1",
                        started_ms=0,
                        ended_ms=15_000,
                        wav_path=sub_1,
                    ),
                    SimpleNamespace(
                        segment_id="seg-00001-sub-2",
                        started_ms=15_000,
                        ended_ms=30_000,
                        wav_path=sub_2,
                    ),
                    SimpleNamespace(
                        segment_id="seg-00001-sub-3",
                        started_ms=30_000,
                        ended_ms=45_000,
                        wav_path=sub_3,
                    ),
                ]

            with (
                patch("live_note.app.coordinator._attach_console_logging"),
                patch(
                    "live_note.app.coordinator.ObsidianClient",
                    return_value=_FakeObsidianClient(),
                ),
                patch("live_note.app.coordinator.OpenAiCompatibleClient"),
                patch(
                    "live_note.app.coordinator.WhisperInferenceClient",
                    return_value=_FakeWhisperClient(),
                ),
                patch("live_note.app.coordinator.WhisperServerProcess", return_value=nullcontext()),
                patch("live_note.app.coordinator.split_wav_file", side_effect=fake_split_wav_file),
                patch(
                    "live_note.app.coordinator._read_wav_pcm16",
                    return_value=(b"\x00\x00" * 1600, 16000),
                ),
                patch("live_note.app.coordinator.publish_final_outputs"),
            ):
                exit_code = finalize_session(config, "session-1")

            states = workspace.rebuild_segment_states()
            segments_journal = workspace.segments_jsonl.read_text(encoding="utf-8")

        self.assertEqual(0, exit_code)
        self.assertEqual([15], split_chunk_seconds)
        self.assertEqual(1, len(states))
        self.assertEqual("seg-00001", states[0].segment_id)
        self.assertEqual("先补齐关键事实。 再写后续行动项。", states[0].text)
        self.assertIsNone(states[0].error)
        self.assertNotIn("seg-00001-sub", segments_journal)
        child_prompts = [prompt for wav_name, prompt in transcribe_calls if "sub-" in wav_name]
        self.assertTrue(child_prompts)
        self.assertTrue(all(prompt in (None, "") for prompt in child_prompts))

    def test_import_healthy_chunks_keep_prompt_context_without_split(self) -> None:
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

            healthy_first = "老师先讲今天的议程，再安排下周任务。"
            split_chunk_seconds: list[int] = []
            prompt_calls: list[str] = []

            class _FakeWhisperClient:
                def transcribe(self, _wav_path: Path, *, prompt: str | None = None) -> str:
                    prompt_calls.append(prompt or "")
                    if len(prompt_calls) == 1:
                        return healthy_first
                    return "第二段继续讨论风险和排期。"

            def fake_convert_audio_to_wav(*, output_path, **_kwargs):
                output_path.write_bytes(b"RIFF")
                return output_path

            def fake_split_wav_file(*, output_dir, chunk_seconds, **_kwargs):
                split_chunk_seconds.append(chunk_seconds)
                first_path = output_dir / "seg-00001.wav"
                second_path = output_dir / "seg-00002.wav"
                first_path.parent.mkdir(parents=True, exist_ok=True)
                first_path.write_bytes(b"RIFF")
                second_path.write_bytes(b"RIFF")
                return [
                    SimpleNamespace(
                        segment_id="seg-00001",
                        started_ms=0,
                        ended_ms=45_000,
                        wav_path=first_path,
                    ),
                    SimpleNamespace(
                        segment_id="seg-00002",
                        started_ms=45_000,
                        ended_ms=90_000,
                        wav_path=second_path,
                    ),
                ]

            with (
                patch("live_note.app.coordinator._attach_console_logging"),
                patch(
                    "live_note.app.coordinator.ObsidianClient",
                    return_value=_FakeObsidianClient(),
                ),
                patch("live_note.app.coordinator.OpenAiCompatibleClient"),
                patch(
                    "live_note.app.coordinator.WhisperInferenceClient",
                    return_value=_FakeWhisperClient(),
                ),
                patch(
                    "live_note.app.coordinator.convert_audio_to_wav",
                    side_effect=fake_convert_audio_to_wav,
                ),
                patch("live_note.app.coordinator.split_wav_file", side_effect=fake_split_wav_file),
                patch(
                    "live_note.app.coordinator._read_wav_pcm16",
                    return_value=(b"\x00\x00" * 1600, 16000),
                ),
                patch("live_note.app.coordinator.WhisperServerProcess", return_value=nullcontext()),
                patch(
                    "live_note.app.coordinator.apply_speaker_labels",
                    side_effect=lambda _config, _workspace, metadata, **_kwargs: metadata,
                    create=True,
                ),
                patch("live_note.app.coordinator.publish_final_outputs"),
            ):
                exit_code = runner.run()

        self.assertEqual(0, exit_code)
        self.assertEqual([45], split_chunk_seconds)
        self.assertEqual(2, len(prompt_calls))
        self.assertIn(healthy_first, prompt_calls[1])

    def test_finalize_session_short_suspicious_chunk_fails_with_deterministic_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _sample_config(root)
            session_dir = root / ".live-note" / "sessions" / "session-1"
            workspace = SessionWorkspace.create(session_dir, _sample_metadata(session_dir))
            wav_path = session_dir / "segments" / "seg-00001.wav"
            wav_path.parent.mkdir(parents=True, exist_ok=True)
            wav_path.write_bytes(b"RIFF")
            workspace.record_segment_created("seg-00001", 0, 15_000, wav_path)
            prompt_calls: list[str | None] = []

            class _FakeWhisperClient:
                def transcribe(self, _wav_path: Path, *, prompt: str | None = None) -> str:
                    prompt_calls.append(prompt)
                    return "中文字幕:CaptionCube 中文字幕:CaptionCube"

            with (
                patch("live_note.app.coordinator._attach_console_logging"),
                patch(
                    "live_note.app.coordinator.ObsidianClient",
                    return_value=_FakeObsidianClient(),
                ),
                patch("live_note.app.coordinator.OpenAiCompatibleClient"),
                patch(
                    "live_note.app.coordinator.WhisperInferenceClient",
                    return_value=_FakeWhisperClient(),
                ),
                patch("live_note.app.coordinator.WhisperServerProcess", return_value=nullcontext()),
                patch(
                    "live_note.app.coordinator._read_wav_pcm16",
                    return_value=(b"\x00\x00" * 1600, 16000),
                ),
                patch("live_note.app.coordinator.publish_final_outputs"),
            ):
                exit_code = finalize_session(config, "session-1")

            states = workspace.rebuild_segment_states()

        self.assertEqual(0, exit_code)
        self.assertEqual(2, len(prompt_calls))
        self.assertIn("只转写实际听到的语音", prompt_calls[0] or "")
        self.assertIn(prompt_calls[1], (None, ""))
        self.assertEqual(1, len(states))
        self.assertIsNone(states[0].text)
        self.assertEqual("疑似字幕/幻觉，自动恢复失败", states[0].error)

    def test_retranscribe_session_still_suspicious_45_second_chunk_uses_single_split_fallback(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _sample_config(root)
            session_dir = root / ".live-note" / "sessions" / "session-1"
            workspace = SessionWorkspace.create(session_dir, _sample_metadata(session_dir))
            wav_path = session_dir / "segments" / "seg-00001.wav"
            wav_path.parent.mkdir(parents=True, exist_ok=True)
            wav_path.write_bytes(b"RIFF")
            workspace.record_segment_created("seg-00001", 0, 45_000, wav_path)
            split_chunk_seconds: list[int] = []
            transcribe_calls: list[tuple[str, str | None]] = []

            class _FakeWhisperClient:
                def transcribe(self, wav_path: Path, *, prompt: str | None = None) -> str:
                    transcribe_calls.append((wav_path.name, prompt))
                    if wav_path.name == "seg-00001.wav":
                        return "中文字幕:CaptionCube 中文字幕:CaptionCube"
                    if wav_path.name.endswith("sub-1.wav"):
                        return "中文字幕:CaptionCube 中文字幕:CaptionCube"
                    if wav_path.name.endswith("sub-2.wav"):
                        return "老师先确认需求范围。"
                    if wav_path.name.endswith("sub-3.wav"):
                        return "然后明确验收标准。"
                    raise AssertionError(f"unexpected wav path: {wav_path}")

            def fake_split_wav_file(*, output_dir, chunk_seconds, **_kwargs):
                split_chunk_seconds.append(chunk_seconds)
                output_dir.mkdir(parents=True, exist_ok=True)
                sub_1 = output_dir / "seg-00001-sub-1.wav"
                sub_2 = output_dir / "seg-00001-sub-2.wav"
                sub_3 = output_dir / "seg-00001-sub-3.wav"
                sub_1.write_bytes(b"RIFF")
                sub_2.write_bytes(b"RIFF")
                sub_3.write_bytes(b"RIFF")
                return [
                    SimpleNamespace(
                        segment_id="seg-00001-sub-1",
                        started_ms=0,
                        ended_ms=15_000,
                        wav_path=sub_1,
                    ),
                    SimpleNamespace(
                        segment_id="seg-00001-sub-2",
                        started_ms=15_000,
                        ended_ms=30_000,
                        wav_path=sub_2,
                    ),
                    SimpleNamespace(
                        segment_id="seg-00001-sub-3",
                        started_ms=30_000,
                        ended_ms=45_000,
                        wav_path=sub_3,
                    ),
                ]

            with (
                patch("live_note.app.coordinator._attach_console_logging"),
                patch(
                    "live_note.app.coordinator.ObsidianClient",
                    return_value=_FakeObsidianClient(),
                ),
                patch("live_note.app.coordinator.OpenAiCompatibleClient"),
                patch(
                    "live_note.app.coordinator.WhisperInferenceClient",
                    return_value=_FakeWhisperClient(),
                ),
                patch("live_note.app.coordinator.WhisperServerProcess", return_value=nullcontext()),
                patch(
                    "live_note.app.coordinator.split_wav_file",
                    side_effect=fake_split_wav_file,
                ),
                patch(
                    "live_note.app.coordinator._read_wav_pcm16",
                    return_value=(b"\x00\x00" * 1600, 16000),
                ),
                patch("live_note.app.coordinator.publish_final_outputs"),
            ):
                exit_code = retranscribe_session(config, "session-1")

            states = workspace.rebuild_segment_states()

        self.assertEqual(0, exit_code)
        self.assertEqual([15], split_chunk_seconds)
        self.assertEqual(1, len(states))
        self.assertEqual("老师先确认需求范围。 然后明确验收标准。", states[0].text)
        self.assertIsNone(states[0].error)
        self.assertEqual(5, len(transcribe_calls))
        self.assertEqual("seg-00001.wav", transcribe_calls[0][0])
        self.assertIn("只转写实际听到的语音", transcribe_calls[0][1] or "")
        self.assertEqual("seg-00001.wav", transcribe_calls[1][0])
        self.assertIn(transcribe_calls[1][1], (None, ""))
        child_prompts = [prompt for wav_name, prompt in transcribe_calls if "sub-" in wav_name]
        self.assertTrue(all(prompt in (None, "") for prompt in child_prompts))

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
