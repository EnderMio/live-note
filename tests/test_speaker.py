from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from queue import Empty
from types import ModuleType
from unittest.mock import patch

from live_note.session_workspace import SessionWorkspace
from live_note.task_errors import TaskCancelledError
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
from live_note.domain import SessionMetadata, TranscriptEntry
from live_note.remote import speaker


class SpeakerDiarizationTests(unittest.TestCase):
    def test_with_speaker_labels_splits_entry_into_finer_turn_aligned_entries(self) -> None:
        entry = TranscriptEntry(
            segment_id="seg-00001",
            started_ms=0,
            ended_ms=6000,
            text="老师先讲第一点。学生插一句。老师继续第二点。",
        )
        turns = [
            speaker.SpeakerTurn(started_ms=0, ended_ms=2500, speaker_id=7),
            speaker.SpeakerTurn(started_ms=2500, ended_ms=3500, speaker_id=9),
            speaker.SpeakerTurn(started_ms=3500, ended_ms=6000, speaker_id=7),
        ]

        labeled = speaker._with_speaker_labels([entry], turns)

        self.assertEqual(
            [
                ("seg-00001-utt-001", 0, 2500, "老师先讲第一点。", "Speaker 1"),
                ("seg-00001-utt-002", 2500, 3500, "学生插一句。", "Speaker 2"),
                ("seg-00001-utt-003", 3500, 6000, "老师继续第二点。", "Speaker 1"),
            ],
            [
                (
                    item.segment_id,
                    item.started_ms,
                    item.ended_ms,
                    item.text,
                    item.speaker_label,
                )
                for item in labeled
            ],
        )

    def test_with_speaker_labels_keeps_chinese_fallback_without_inserting_spaces(self) -> None:
        entry = TranscriptEntry(
            segment_id="seg-00001",
            started_ms=0,
            ended_ms=6000,
            text="老师先讲第一点学生插一句老师继续第二点",
        )
        turns = [
            speaker.SpeakerTurn(started_ms=0, ended_ms=2000, speaker_id=7),
            speaker.SpeakerTurn(started_ms=2000, ended_ms=4000, speaker_id=9),
            speaker.SpeakerTurn(started_ms=4000, ended_ms=6000, speaker_id=7),
        ]

        labeled = speaker._with_speaker_labels([entry], turns)

        self.assertEqual(3, len(labeled))
        self.assertEqual(entry.text, "".join(item.text for item in labeled))
        self.assertTrue(all(" " not in item.text for item in labeled))

    def test_match_speaker_prefers_total_overlap_over_midpoint_blip(self) -> None:
        entry = TranscriptEntry(
            segment_id="seg-00001",
            started_ms=0,
            ended_ms=10000,
            text="大家好。",
        )
        turns = [
            speaker.SpeakerTurn(started_ms=0, ended_ms=4000, speaker_id=7),
            speaker.SpeakerTurn(started_ms=4500, ended_ms=5500, speaker_id=9),
            speaker.SpeakerTurn(started_ms=5500, ended_ms=10000, speaker_id=7),
        ]

        label = speaker._match_speaker(entry, turns)

        self.assertEqual("Speaker 1", label)

    def test_apply_speaker_labels_marks_running_and_normalizes_labels(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _sample_config(root)
            workspace = _sample_workspace(root)
            metadata = workspace.read_session()
            audio_path = root / "normalized.wav"
            audio_path.write_bytes(b"fake-wav")
            progress = []

            def fake_run(
                job: speaker.SpeakerDiarizationJob,
                *,
                cancel_callback=None,
            ) -> list[speaker.SpeakerTurn]:
                self.assertIsNone(cancel_callback)
                self.assertTrue(job.audio_path.endswith("normalized.wav"))
                self.assertEqual("running", workspace.read_session().speaker_status)
                return [
                    speaker.SpeakerTurn(started_ms=0, ended_ms=1200, speaker_id=73),
                    speaker.SpeakerTurn(started_ms=1200, ended_ms=2400, speaker_id=183),
                ]

            with (
                patch("live_note.remote.speaker.importlib.util.find_spec", return_value=object()),
                patch("live_note.remote.speaker._run_diarization_job", side_effect=fake_run),
            ):
                updated = speaker.apply_speaker_labels(
                    config,
                    workspace,
                    metadata,
                    audio_path=audio_path,
                    on_progress=progress.append,
                )
                stored_status = workspace.read_session().speaker_status
                labels = [item.speaker_label for item in workspace.transcript_entries()]

        self.assertEqual("done", updated.speaker_status)
        self.assertEqual("done", stored_status)
        self.assertEqual(["Speaker 1", "Speaker 2"], labels)
        self.assertEqual(
            [("speaker", 1, 3), ("speaker", 2, 3), ("speaker", 3, 3)],
            [(event.stage, event.current, event.total) for event in progress],
        )

    def test_apply_speaker_labels_uses_pyannote_backend_job_for_file_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _sample_config(root, speaker_backend="pyannote")
            workspace = _sample_workspace(root)
            metadata = workspace.read_session()
            audio_path = root / "normalized.wav"
            audio_path.write_bytes(b"fake-wav")

            def fake_run(
                job: speaker.SpeakerDiarizationJob,
                *,
                cancel_callback=None,
            ) -> list[speaker.SpeakerTurn]:
                self.assertEqual("pyannote", job.backend)
                self.assertEqual(
                    "pyannote/speaker-diarization-community-1",
                    job.pyannote_model,
                )
                self.assertEqual("hf-token", job.pyannote_auth_token)
                return [
                    speaker.SpeakerTurn(started_ms=0, ended_ms=1200, speaker_id=1),
                    speaker.SpeakerTurn(started_ms=1200, ended_ms=2400, speaker_id=2),
                ]

            with (
                patch("live_note.remote.speaker.importlib.util.find_spec", return_value=object()),
                patch("live_note.remote.speaker._run_diarization_job", side_effect=fake_run),
            ):
                updated = speaker.apply_speaker_labels(
                    config,
                    workspace,
                    metadata,
                    audio_path=audio_path,
                )

        self.assertEqual("done", updated.speaker_status)

    def test_execute_pyannote_diarization_job_maps_labels_and_passes_num_speakers(self) -> None:
        captured: dict[str, object] = {}

        class _FakeTurn:
            def __init__(self, start: float, end: float) -> None:
                self.start = start
                self.end = end

        class _FakeAnnotation:
            def itertracks(self, yield_label: bool = False):
                assert yield_label is True
                return iter(
                    [
                        (_FakeTurn(0.0, 1.2), None, "SPEAKER_A"),
                        (_FakeTurn(1.2, 2.7), None, "SPEAKER_B"),
                        (_FakeTurn(2.7, 3.6), None, "SPEAKER_A"),
                    ]
                )

        class _FakePipelineInstance:
            def __call__(self, audio_path: str, **kwargs):
                captured["audio_path"] = audio_path
                captured["kwargs"] = dict(kwargs)
                return _FakeAnnotation()

        class _FakePipeline:
            @staticmethod
            def from_pretrained(model: str, **kwargs):
                captured["model"] = model
                captured["from_pretrained_kwargs"] = dict(kwargs)
                return _FakePipelineInstance()

        pyannote_package = ModuleType("pyannote")
        pyannote_audio = ModuleType("pyannote.audio")
        pyannote_audio.Pipeline = _FakePipeline
        pyannote_package.audio = pyannote_audio

        job = speaker.SpeakerDiarizationJob(
            backend="pyannote",
            audio_path="/tmp/demo.wav",
            segmentation_model=None,
            embedding_model=None,
            expected_speakers=3,
            cluster_threshold=0.5,
            min_duration_on=0.3,
            min_duration_off=0.5,
            pyannote_model="pyannote/speaker-diarization-community-1",
            pyannote_auth_token="hf-token",
        )

        with patch.dict(
            sys.modules,
            {"pyannote": pyannote_package, "pyannote.audio": pyannote_audio},
        ):
            turns = speaker._execute_diarization_job(job)

        self.assertEqual("pyannote/speaker-diarization-community-1", captured["model"])
        self.assertEqual({"token": "hf-token"}, captured["from_pretrained_kwargs"])
        self.assertEqual({"num_speakers": 3}, captured["kwargs"])
        self.assertEqual(
            [
                (0, 1200, 1),
                (1200, 2700, 2),
                (2700, 3600, 1),
            ],
            [(item.started_ms, item.ended_ms, item.speaker_id) for item in turns],
        )

    def test_rewrite_canonical_transcript_honors_cancel_before_replace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = _sample_workspace(root)
            original_journal = workspace.segments_jsonl.read_text(encoding="utf-8")

            with self.assertRaisesRegex(TaskCancelledError, "取消"):
                speaker._rewrite_canonical_transcript(
                    workspace,
                    [
                        TranscriptEntry(
                            segment_id="seg-00001-utt-001",
                            started_ms=0,
                            ended_ms=1200,
                            text="第一位。",
                            speaker_label="Speaker 1",
                        )
                    ],
                    cancel_callback=lambda: (_ for _ in ()).throw(
                        TaskCancelledError("导入任务已取消。")
                    ),
                )

            self.assertEqual(original_journal, workspace.segments_jsonl.read_text(encoding="utf-8"))

    def test_apply_speaker_labels_rewrites_journal_with_turn_entries_and_keeps_source_audio(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _sample_config(root)
            session_root = root / ".live-note" / "sessions" / "speaker-session"
            metadata = SessionMetadata(
                session_id="speaker-session",
                title="多人讨论",
                kind="meeting",
                input_mode="file",
                source_label="meeting.wav",
                source_ref="file://meeting.wav",
                language="zh",
                started_at="2026-03-19T10:00:00+00:00",
                transcript_note_path="Sessions/Transcripts/2026-03-19/demo.md",
                structured_note_path="Sessions/Summaries/2026-03-19/demo.md",
                session_dir=str(session_root),
                status="transcript_only",
                transcript_source="refined",
                refine_status="disabled",
                execution_target="remote",
                remote_session_id="speaker-session",
                speaker_status="pending",
            )
            workspace = SessionWorkspace.create(session_root, metadata)
            wav_path = workspace.next_wav_path("seg-00001")
            wav_path.parent.mkdir(parents=True, exist_ok=True)
            wav_path.write_bytes(b"fake")
            workspace.record_segment_created("seg-00001", 0, 6000, wav_path)
            workspace.record_segment_text(
                "seg-00001",
                0,
                6000,
                "老师先讲第一点。学生插一句。老师继续第二点。",
            )
            audio_path = root / "normalized.wav"
            audio_path.write_bytes(b"fake-wav")

            with (
                patch("live_note.remote.speaker.importlib.util.find_spec", return_value=object()),
                patch(
                    "live_note.remote.speaker._run_diarization_job",
                    return_value=[
                        speaker.SpeakerTurn(started_ms=0, ended_ms=2500, speaker_id=7),
                        speaker.SpeakerTurn(started_ms=2500, ended_ms=3500, speaker_id=9),
                        speaker.SpeakerTurn(started_ms=3500, ended_ms=6000, speaker_id=7),
                    ],
                ),
            ):
                speaker.apply_speaker_labels(config, workspace, metadata, audio_path=audio_path)

            states = workspace.rebuild_segment_states()
            entries = workspace.transcript_entries()

        original = next(item for item in states if item.segment_id == "seg-00001")
        self.assertEqual(wav_path, original.wav_path)
        self.assertEqual(
            [
                ("seg-00001-utt-001", 0, 2500, "老师先讲第一点。", "Speaker 1"),
                ("seg-00001-utt-002", 2500, 3500, "学生插一句。", "Speaker 2"),
                ("seg-00001-utt-003", 3500, 6000, "老师继续第二点。", "Speaker 1"),
            ],
            [
                (
                    item.segment_id,
                    item.started_ms,
                    item.ended_ms,
                    item.text,
                    item.speaker_label,
                )
                for item in entries
            ],
        )

    def test_apply_speaker_labels_returns_failed_when_job_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _sample_config(root)
            workspace = _sample_workspace(root)
            metadata = workspace.read_session()
            audio_path = root / "normalized.wav"
            audio_path.write_bytes(b"fake-wav")
            progress = []

            with (
                patch("live_note.remote.speaker.importlib.util.find_spec", return_value=object()),
                patch(
                    "live_note.remote.speaker._run_diarization_job",
                    side_effect=RuntimeError("speaker boom"),
                ),
            ):
                updated = speaker.apply_speaker_labels(
                    config,
                    workspace,
                    metadata,
                    audio_path=audio_path,
                    on_progress=progress.append,
                )
                stored_status = workspace.read_session().speaker_status

        self.assertEqual("failed", updated.speaker_status)
        self.assertEqual("failed", stored_status)
        self.assertEqual("speaker", progress[-1].stage)
        self.assertEqual("speaker boom", progress[-1].error)

    def test_apply_speaker_labels_propagates_cancel_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _sample_config(root)
            workspace = _sample_workspace(root)
            metadata = workspace.read_session()
            audio_path = root / "normalized.wav"
            audio_path.write_bytes(b"fake-wav")

            def fake_run(
                _job: speaker.SpeakerDiarizationJob,
                *,
                cancel_callback=None,
            ) -> list[speaker.SpeakerTurn]:
                assert cancel_callback is not None
                cancel_callback()
                return []

            with (
                patch("live_note.remote.speaker.importlib.util.find_spec", return_value=object()),
                patch("live_note.remote.speaker._run_diarization_job", side_effect=fake_run),
            ):
                with self.assertRaisesRegex(TaskCancelledError, "取消"):
                    speaker.apply_speaker_labels(
                        config,
                        workspace,
                        metadata,
                        audio_path=audio_path,
                        cancel_callback=lambda: (_ for _ in ()).throw(
                            TaskCancelledError("导入任务已取消。")
                        ),
                    )

    def test_run_diarization_job_terminates_worker_when_cancelled(self) -> None:
        job = speaker.SpeakerDiarizationJob(
            backend="sherpa_onnx",
            audio_path="/tmp/demo.wav",
            segmentation_model="/tmp/seg.onnx",
            embedding_model="/tmp/embed.onnx",
            expected_speakers=0,
            cluster_threshold=0.5,
            min_duration_on=0.3,
            min_duration_off=0.5,
            pyannote_model="pyannote/speaker-diarization-community-1",
        )

        class _FakeQueue:
            def get(self, timeout: float):
                raise Empty

        class _FakeProcess:
            def __init__(self) -> None:
                self.terminated = False
                self.join_calls: list[float | None] = []
                self.pid = 1234

            def is_alive(self) -> bool:
                return not self.terminated

            def terminate(self) -> None:
                self.terminated = True

            def join(self, timeout: float | None = None) -> None:
                self.join_calls.append(timeout)

        fake_process = _FakeProcess()
        fake_queue = _FakeQueue()

        with patch(
            "live_note.remote.speaker._launch_diarization_worker",
            return_value=(fake_process, fake_queue),
        ):
            with self.assertRaisesRegex(TaskCancelledError, "取消"):
                speaker._run_diarization_job(
                    job,
                    cancel_callback=lambda: (_ for _ in ()).throw(
                        TaskCancelledError("导入任务已取消。")
                    ),
                )

        self.assertTrue(fake_process.terminated)
        self.assertTrue(fake_process.join_calls)

    def test_cluster_count_uses_expected_speakers_hint(self) -> None:
        hinted = speaker.SpeakerDiarizationJob(
            backend="sherpa_onnx",
            audio_path="/tmp/demo.wav",
            segmentation_model="/tmp/seg.onnx",
            embedding_model="/tmp/embed.onnx",
            expected_speakers=3,
            cluster_threshold=0.5,
            min_duration_on=0.3,
            min_duration_off=0.5,
            pyannote_model="pyannote/speaker-diarization-community-1",
        )
        automatic = speaker.SpeakerDiarizationJob(
            backend="sherpa_onnx",
            audio_path="/tmp/demo.wav",
            segmentation_model="/tmp/seg.onnx",
            embedding_model="/tmp/embed.onnx",
            expected_speakers=0,
            cluster_threshold=0.5,
            min_duration_on=0.3,
            min_duration_off=0.5,
            pyannote_model="pyannote/speaker-diarization-community-1",
        )

        self.assertEqual(3, speaker._cluster_count(hinted))
        self.assertEqual(-1, speaker._cluster_count(automatic))


def _sample_config(root: Path, *, speaker_backend: str = "sherpa_onnx") -> AppConfig:
    model_path = root / "ggml-large-v3.bin"
    model_path.write_bytes(b"fake-model")
    segmentation_model = root / "seg.onnx"
    segmentation_model.write_bytes(b"seg")
    embedding_model = root / "embed.onnx"
    embedding_model.write_bytes(b"embed")
    return AppConfig(
        audio=AudioConfig(),
        importer=ImportConfig(),
        refine=RefineConfig(),
        whisper=WhisperConfig(
            binary="/Users/demo/whisper-server",
            model=model_path,
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
        speaker=SpeakerConfig(
            enabled=True,
            backend=speaker_backend,
            segmentation_model=segmentation_model,
            embedding_model=embedding_model,
            cluster_threshold=0.5,
            pyannote_model="pyannote/speaker-diarization-community-1",
            pyannote_auth_token="hf-token",
        ),
        root_dir=root,
    )


def _sample_workspace(root: Path) -> SessionWorkspace:
    session_root = root / ".live-note" / "sessions" / "speaker-session"
    workspace = SessionWorkspace.create(
        session_root,
        SessionMetadata(
            session_id="speaker-session",
            title="多人讨论",
            kind="meeting",
            input_mode="file",
            source_label="meeting.wav",
            source_ref="file://meeting.wav",
            language="zh",
            started_at="2026-03-19T10:00:00+00:00",
            transcript_note_path="Sessions/Transcripts/2026-03-19/demo.md",
            structured_note_path="Sessions/Summaries/2026-03-19/demo.md",
            session_dir=str(session_root),
            status="transcript_only",
            transcript_source="refined",
            refine_status="disabled",
            execution_target="remote",
            remote_session_id="speaker-session",
            speaker_status="pending",
        ),
    )
    workspace.record_segment_text("seg-00001", 0, 1200, "第一位。")
    workspace.record_segment_text("seg-00002", 1200, 2400, "第二位。")
    return workspace
