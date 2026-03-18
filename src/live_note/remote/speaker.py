from __future__ import annotations

import wave
from array import array
from dataclasses import replace
from typing import Any

from live_note.app.events import ProgressCallback, ProgressEvent
from live_note.app.journal import SessionWorkspace
from live_note.config import AppConfig
from live_note.domain import SessionMetadata, TranscriptEntry


def apply_speaker_labels(
    config: AppConfig,
    workspace: SessionWorkspace,
    metadata: SessionMetadata,
    *,
    on_progress: ProgressCallback | None = None,
) -> SessionMetadata:
    if not config.speaker.enabled:
        return workspace.update_session(speaker_status="disabled")
    if config.speaker.segmentation_model is None or config.speaker.embedding_model is None:
        return workspace.update_session(speaker_status="failed")
    if not workspace.session_live_wav.exists():
        return workspace.update_session(speaker_status="failed")

    try:
        import sherpa_onnx
    except ImportError:
        return workspace.update_session(speaker_status="failed")

    _emit_progress(on_progress, "speaker", "正在进行说话人区分。", session_id=metadata.session_id)
    diarization = sherpa_onnx.OfflineSpeakerDiarization(
        sherpa_onnx.OfflineSpeakerDiarizationConfig(
            segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                    model=str(config.speaker.segmentation_model)
                )
            ),
            embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=str(config.speaker.embedding_model)
            ),
            clustering=sherpa_onnx.FastClusteringConfig(
                num_clusters=-1,
                threshold=config.speaker.cluster_threshold,
            ),
            min_duration_on=config.speaker.min_duration_on,
            min_duration_off=config.speaker.min_duration_off,
        )
    )
    samples, sample_rate = _read_wave_f32(workspace.session_live_wav)
    if sample_rate != diarization.sample_rate:
        raise RuntimeError(
            f"speaker diarization 采样率不匹配：需要 {diarization.sample_rate}Hz，"
            f"当前为 {sample_rate}Hz。"
        )
    results = diarization.process(samples).sort_by_start_time()
    entries = workspace.transcript_entries()
    if not entries:
        return workspace.update_session(speaker_status="done")

    for entry in _with_speaker_labels(entries, results):
        workspace.record_segment_text(
            entry.segment_id,
            entry.started_ms,
            entry.ended_ms,
            entry.text,
            speaker_label=entry.speaker_label,
        )
    return workspace.update_session(speaker_status="done")


def _with_speaker_labels(
    entries: list[TranscriptEntry],
    results: list[Any],
) -> list[TranscriptEntry]:
    labeled: list[TranscriptEntry] = []
    for entry in entries:
        speaker_label = _match_speaker(entry, results)
        labeled.append(replace(entry, speaker_label=speaker_label))
    return labeled


def _match_speaker(entry: TranscriptEntry, results: list[Any]) -> str | None:
    midpoint = (entry.started_ms + entry.ended_ms) / 2000
    best_overlap = 0.0
    best_speaker: str | None = None
    for result in results:
        start = float(result.start)
        end = float(result.end)
        if start <= midpoint <= end:
            return f"Speaker {int(result.speaker) + 1}"
        overlap = max(0.0, min(end, entry.ended_ms / 1000) - max(start, entry.started_ms / 1000))
        if overlap > best_overlap:
            best_overlap = overlap
            best_speaker = f"Speaker {int(result.speaker) + 1}"
    return best_speaker


def _read_wave_f32(path) -> tuple[Any, int]:
    with wave.open(str(path), "rb") as handle:
        if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise RuntimeError("speaker diarization 仅支持单声道 16-bit WAV。")
        sample_rate = handle.getframerate()
        pcm16 = handle.readframes(handle.getnframes())
    samples = array("h")
    samples.frombytes(pcm16)
    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("speaker diarization 需要 numpy。") from exc
    return np.asarray(samples, dtype="float32") / 32768.0, sample_rate


def _emit_progress(
    callback: ProgressCallback | None,
    stage: str,
    message: str,
    *,
    session_id: str | None = None,
) -> None:
    if callback is None:
        return
    callback(
        ProgressEvent(
            stage=stage,
            message=message,
            session_id=session_id,
        )
    )
