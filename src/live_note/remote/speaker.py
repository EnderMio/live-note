from __future__ import annotations

import importlib.util
import multiprocessing
import queue
import re
import wave
from array import array
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from live_note.app.events import ProgressCallback, ProgressEvent
from live_note.app.journal import SessionWorkspace
from live_note.app.task_errors import TaskCancelledError
from live_note.config import AppConfig
from live_note.domain import SessionMetadata, TranscriptEntry
from live_note.utils import compact_text

_SPEAKER_JOURNAL_TMP = "segments.speaker.jsonl.tmp"
_TURN_MERGE_GAP_MS = 250
_STRONG_SPLIT_CHARS = frozenset("。！？!?；;…")
_SOFT_SPLIT_CHARS = frozenset("，,、：:")
_MID_BREAK_PATTERN = re.compile(r"[\s，,、：:。！？!?；;…]")


@dataclass(frozen=True, slots=True)
class SpeakerDiarizationJob:
    backend: str
    audio_path: str
    segmentation_model: str | None
    embedding_model: str | None
    expected_speakers: int
    cluster_threshold: float
    min_duration_on: float
    min_duration_off: float
    pyannote_model: str
    pyannote_auth_token: str | None = None


@dataclass(frozen=True, slots=True)
class SpeakerTurn:
    started_ms: int
    ended_ms: int
    speaker_id: int


@dataclass(frozen=True, slots=True)
class _TurnSpan:
    started_ms: int
    ended_ms: int
    speaker_label: str | None


@dataclass(frozen=True, slots=True)
class _TimedTextPiece:
    text: str
    started_ms: int
    ended_ms: int


def apply_speaker_labels(
    config: AppConfig,
    workspace: SessionWorkspace,
    metadata: SessionMetadata,
    *,
    audio_path=None,
    on_progress: ProgressCallback | None = None,
    cancel_callback: Callable[[], None] | None = None,
) -> SessionMetadata:
    source_path = audio_path or workspace.session_live_wav
    if not config.speaker.enabled:
        return workspace.update_session(speaker_status="disabled")
    if not source_path.exists():
        return workspace.update_session(speaker_status="failed")
    if not _backend_configured(config):
        return workspace.update_session(speaker_status="failed")
    if not _backend_dependency_available(config.speaker.backend):
        return workspace.update_session(speaker_status="failed")
    _check_cancel(cancel_callback)
    workspace.update_session(speaker_status="running")
    _emit_progress(
        on_progress,
        "speaker",
        "正在分析说话人特征。",
        session_id=metadata.session_id,
        current=1,
        total=3,
    )
    try:
        turns = _run_diarization_job(
            _build_diarization_job(config, source_path),
            cancel_callback=cancel_callback,
        )
    except TaskCancelledError:
        raise
    except Exception as exc:
        _emit_progress(
            on_progress,
            "speaker",
            "说话人区分失败，已跳过。",
            session_id=metadata.session_id,
            current=3,
            total=3,
            error=str(exc),
        )
        return workspace.update_session(speaker_status="failed")

    _check_cancel(cancel_callback)
    _emit_progress(
        on_progress,
        "speaker",
        "正在匹配说话人标签。",
        session_id=metadata.session_id,
        current=2,
        total=3,
    )
    entries = workspace.transcript_entries()
    if not entries:
        _emit_progress(
            on_progress,
            "speaker",
            "说话人区分已完成。",
            session_id=metadata.session_id,
            current=3,
            total=3,
        )
        return workspace.update_session(speaker_status="done")

    aligned_entries = _with_speaker_labels(entries, turns)
    _rewrite_canonical_transcript(
        workspace,
        aligned_entries,
        cancel_callback=cancel_callback,
    )
    for entry in aligned_entries:
        _check_cancel(cancel_callback)
    _emit_progress(
        on_progress,
        "speaker",
        "说话人区分已完成。",
        session_id=metadata.session_id,
        current=3,
        total=3,
    )
    return workspace.update_session(speaker_status="done")


def _with_speaker_labels(
    entries: list[TranscriptEntry],
    turns: list[SpeakerTurn],
) -> list[TranscriptEntry]:
    normalized_labels = _normalized_speaker_labels(turns)
    labeled: list[TranscriptEntry] = []
    for entry in entries:
        labeled.extend(_split_entry_into_turns(entry, turns, normalized_labels))
    return labeled


def _split_entry_into_turns(
    entry: TranscriptEntry,
    turns: list[SpeakerTurn],
    normalized_labels: dict[int, str],
) -> list[TranscriptEntry]:
    turn_spans = _turn_spans_for_entry(entry, turns, normalized_labels)
    if not turn_spans:
        return [replace(entry, speaker_label=None)]
    if len(turn_spans) == 1:
        return [replace(entry, speaker_label=turn_spans[0].speaker_label)]

    timed_pieces = _timed_text_pieces(
        entry.text,
        entry.started_ms,
        entry.ended_ms,
        target_count=len(turn_spans),
    )
    if len(timed_pieces) <= 1:
        return [replace(entry, speaker_label=_match_speaker(entry, turns, normalized_labels))]

    text_by_turn: dict[int, list[str]] = defaultdict(list)
    for piece in timed_pieces:
        turn_index = _best_turn_index(piece, turn_spans)
        if turn_index is None:
            continue
        text_by_turn[turn_index].append(piece.text)

    split_entries: list[TranscriptEntry] = []
    for turn_index, turn_span in enumerate(turn_spans, start=1):
        text = _join_text_pieces(text_by_turn.get(turn_index - 1, []))
        if not text:
            continue
        split_entries.append(
            TranscriptEntry(
                segment_id=f"{entry.segment_id}-utt-{turn_index:03d}",
                started_ms=turn_span.started_ms,
                ended_ms=turn_span.ended_ms,
                text=text,
                speaker_label=turn_span.speaker_label,
            )
        )
    if len(split_entries) <= 1:
        return [replace(entry, speaker_label=_match_speaker(entry, turns, normalized_labels))]
    return split_entries


def _turn_spans_for_entry(
    entry: TranscriptEntry,
    turns: list[SpeakerTurn],
    normalized_labels: dict[int, str],
) -> list[_TurnSpan]:
    spans: list[_TurnSpan] = []
    for turn in turns:
        overlap_start = max(entry.started_ms, turn.started_ms)
        overlap_end = min(entry.ended_ms, turn.ended_ms)
        if overlap_end <= overlap_start:
            continue
        spans.append(
            _TurnSpan(
                started_ms=overlap_start,
                ended_ms=overlap_end,
                speaker_label=normalized_labels.get(turn.speaker_id),
            )
        )
    if not spans:
        return []
    spans.sort(key=lambda item: (item.started_ms, item.ended_ms))
    merged: list[_TurnSpan] = []
    for span in spans:
        if (
            merged
            and merged[-1].speaker_label == span.speaker_label
            and span.started_ms <= merged[-1].ended_ms + _TURN_MERGE_GAP_MS
        ):
            previous = merged[-1]
            merged[-1] = replace(previous, ended_ms=max(previous.ended_ms, span.ended_ms))
        else:
            merged.append(span)
    if len(merged) == 1:
        only = merged[0]
        return [
            _TurnSpan(
                started_ms=entry.started_ms,
                ended_ms=entry.ended_ms,
                speaker_label=only.speaker_label,
            )
        ]
    stretched: list[_TurnSpan] = []
    cursor = entry.started_ms
    for index, span in enumerate(merged):
        if index == len(merged) - 1:
            end = entry.ended_ms
        else:
            next_span = merged[index + 1]
            end = max(cursor + 1, int(round((span.ended_ms + next_span.started_ms) / 2)))
        stretched.append(
            _TurnSpan(
                started_ms=cursor,
                ended_ms=end,
                speaker_label=span.speaker_label,
            )
        )
        cursor = end
    return stretched


def _timed_text_pieces(
    text: str,
    started_ms: int,
    ended_ms: int,
    *,
    target_count: int,
) -> list[_TimedTextPiece]:
    pieces = _split_text_pieces(text, target_count)
    if not pieces:
        return []
    if len(pieces) == 1:
        return [
            _TimedTextPiece(
                text=pieces[0],
                started_ms=started_ms,
                ended_ms=ended_ms,
            )
        ]
    weights = [max(1, _piece_weight(item)) for item in pieces]
    duration = max(1, ended_ms - started_ms)
    total_weight = max(1, sum(weights))
    boundaries = [started_ms]
    cumulative = 0
    for index, weight in enumerate(weights[:-1], start=1):
        cumulative += weight
        boundary = started_ms + round(duration * cumulative / total_weight)
        min_boundary = boundaries[-1] + 1
        max_boundary = ended_ms - (len(weights) - index)
        boundaries.append(max(min_boundary, min(boundary, max_boundary)))
    boundaries.append(ended_ms)
    return [
        _TimedTextPiece(
            text=piece,
            started_ms=boundaries[index],
            ended_ms=boundaries[index + 1],
        )
        for index, piece in enumerate(pieces)
    ]


def _split_text_pieces(text: str, target_count: int) -> list[str]:
    condensed = compact_text(text)
    if not condensed:
        return []
    pieces = _split_by_chars(condensed, _STRONG_SPLIT_CHARS)
    if len(pieces) < target_count:
        pieces = _expand_split_by_chars(pieces, target_count, _SOFT_SPLIT_CHARS)
    if len(pieces) < target_count:
        pieces = _expand_split_by_length(pieces, target_count)
    return [item.strip() for item in pieces if item.strip()]


def _split_by_chars(text: str, delimiters: set[str] | frozenset[str]) -> list[str]:
    pieces: list[str] = []
    buffer: list[str] = []
    for char in text:
        buffer.append(char)
        if char in delimiters:
            piece = "".join(buffer).strip()
            if piece:
                pieces.append(piece)
            buffer.clear()
    tail = "".join(buffer).strip()
    if tail:
        pieces.append(tail)
    return pieces or [text.strip()]


def _expand_split_by_chars(
    pieces: list[str],
    target_count: int,
    delimiters: set[str] | frozenset[str],
) -> list[str]:
    expanded = list(pieces)
    while len(expanded) < target_count:
        split_index = _best_piece_index_for_char_split(expanded, delimiters)
        if split_index is None:
            break
        replacement = _split_by_chars(expanded[split_index], delimiters)
        if len(replacement) <= 1:
            break
        expanded[split_index : split_index + 1] = replacement
    return expanded


def _best_piece_index_for_char_split(
    pieces: list[str],
    delimiters: set[str] | frozenset[str],
) -> int | None:
    candidates = [
        index for index, piece in enumerate(pieces) if any(char in delimiters for char in piece)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda index: _piece_weight(pieces[index]))


def _expand_split_by_length(pieces: list[str], target_count: int) -> list[str]:
    expanded = list(pieces)
    while len(expanded) < target_count:
        split_index = max(range(len(expanded)), key=lambda index: _piece_weight(expanded[index]))
        if _piece_weight(expanded[split_index]) < 2:
            break
        left, right = _split_piece_by_length(expanded[split_index])
        if not left or not right:
            break
        expanded[split_index : split_index + 1] = [left, right]
    return expanded


def _split_piece_by_length(piece: str) -> tuple[str, str]:
    condensed = piece.strip()
    if len(condensed) < 2:
        return condensed, ""
    midpoint = len(condensed) // 2
    split_at = None
    for delta in range(len(condensed)):
        left = midpoint - delta
        right = midpoint + delta
        for candidate in (left, right):
            if candidate <= 0 or candidate >= len(condensed):
                continue
            if _MID_BREAK_PATTERN.match(condensed[candidate - 1]):
                split_at = candidate
                break
        if split_at is not None:
            break
    if split_at is None:
        split_at = midpoint
    left = condensed[:split_at].strip()
    right = condensed[split_at:].strip()
    return left, right


def _piece_weight(text: str) -> int:
    return sum(1 for char in text if not char.isspace())


def _best_turn_index(piece: _TimedTextPiece, spans: list[_TurnSpan]) -> int | None:
    best_index: int | None = None
    best_overlap = -1
    for index, span in enumerate(spans):
        overlap = max(
            0,
            min(piece.ended_ms, span.ended_ms) - max(piece.started_ms, span.started_ms),
        )
        if overlap > best_overlap:
            best_overlap = overlap
            best_index = index
    return best_index


def _join_text_pieces(pieces: list[str]) -> str:
    if not pieces:
        return ""
    combined = pieces[0].strip()
    for piece in pieces[1:]:
        current = piece.strip()
        if not current:
            continue
        if combined and combined[-1].isalnum() and current[0].isalnum():
            combined = f"{combined} {current}"
        else:
            combined = f"{combined}{current}"
    return combined.strip()


def _rewrite_canonical_transcript(
    workspace: SessionWorkspace,
    entries: list[TranscriptEntry],
    *,
    cancel_callback: Callable[[], None] | None = None,
) -> None:
    source_states = workspace.rebuild_segment_states()
    temp_journal = workspace.root / _SPEAKER_JOURNAL_TMP
    if temp_journal.exists():
        temp_journal.unlink()
    try:
        _check_cancel(cancel_callback)
        for state in source_states:
            _check_cancel(cancel_callback)
            if state.wav_path is not None:
                workspace.record_segment_created(
                    state.segment_id,
                    state.started_ms,
                    state.ended_ms,
                    state.wav_path,
                    journal_path=temp_journal,
                )
            if state.error:
                workspace.record_segment_error(
                    state.segment_id,
                    state.started_ms,
                    state.ended_ms,
                    state.error,
                    journal_path=temp_journal,
                )
        for entry in entries:
            _check_cancel(cancel_callback)
            workspace.record_segment_text(
                entry.segment_id,
                entry.started_ms,
                entry.ended_ms,
                entry.text,
                speaker_label=entry.speaker_label,
                journal_path=temp_journal,
            )
        _check_cancel(cancel_callback)
        workspace.replace_canonical_journal(temp_journal)
    finally:
        if temp_journal.exists():
            temp_journal.unlink()


def _match_speaker(
    entry: TranscriptEntry,
    turns: list[SpeakerTurn],
    normalized_labels: dict[int, str] | None = None,
) -> str | None:
    if not turns:
        return None
    label_map = normalized_labels or _normalized_speaker_labels(turns)
    midpoint_ms = (entry.started_ms + entry.ended_ms) / 2
    overlap_by_speaker: dict[int, int] = defaultdict(int)
    midpoint_hits: set[int] = set()
    first_seen_index: dict[int, int] = {}
    for index, turn in enumerate(turns):
        overlap_ms = max(
            0,
            min(turn.ended_ms, entry.ended_ms) - max(turn.started_ms, entry.started_ms),
        )
        if overlap_ms <= 0:
            continue
        overlap_by_speaker[turn.speaker_id] += overlap_ms
        first_seen_index.setdefault(turn.speaker_id, index)
        if turn.started_ms <= midpoint_ms <= turn.ended_ms:
            midpoint_hits.add(turn.speaker_id)
    if not overlap_by_speaker:
        return None
    speaker_id = max(
        overlap_by_speaker,
        key=lambda item: (
            overlap_by_speaker[item],
            1 if item in midpoint_hits else 0,
            -first_seen_index.get(item, 0),
        ),
    )
    return label_map.get(speaker_id)


def _normalized_speaker_labels(turns: list[SpeakerTurn]) -> dict[int, str]:
    totals: dict[int, int] = defaultdict(int)
    first_seen_index: dict[int, int] = {}
    for index, turn in enumerate(turns):
        totals[turn.speaker_id] += max(1, turn.ended_ms - turn.started_ms)
        first_seen_index.setdefault(turn.speaker_id, index)
    ordered_ids = sorted(
        totals,
        key=lambda item: (-totals[item], first_seen_index[item], item),
    )
    return {
        speaker_id: f"Speaker {index}"
        for index, speaker_id in enumerate(ordered_ids, start=1)
    }


def _build_diarization_job(config: AppConfig, source_path) -> SpeakerDiarizationJob:
    return SpeakerDiarizationJob(
        backend=config.speaker.backend,
        audio_path=str(source_path),
        segmentation_model=(
            str(config.speaker.segmentation_model) if config.speaker.segmentation_model else None
        ),
        embedding_model=(
            str(config.speaker.embedding_model) if config.speaker.embedding_model else None
        ),
        expected_speakers=config.speaker.expected_speakers,
        cluster_threshold=config.speaker.cluster_threshold,
        min_duration_on=config.speaker.min_duration_on,
        min_duration_off=config.speaker.min_duration_off,
        pyannote_model=config.speaker.pyannote_model,
        pyannote_auth_token=config.speaker.pyannote_auth_token,
    )


def _run_diarization_job(
    job: SpeakerDiarizationJob,
    *,
    cancel_callback: Callable[[], None] | None = None,
) -> list[SpeakerTurn]:
    process, result_queue = _launch_diarization_worker(job)
    try:
        while True:
            _check_cancel(cancel_callback)
            try:
                payload = result_queue.get(timeout=0.2)
            except queue.Empty:
                if not process.is_alive():
                    exit_code = getattr(process, "exitcode", None)
                    raise RuntimeError(
                        "speaker diarization 子进程异常退出。"
                        if exit_code is None
                        else f"speaker diarization 子进程异常退出，exit={exit_code}。"
                    )
                continue
            status = str(payload.get("status") or "").strip().lower()
            if status == "ok":
                return [SpeakerTurn(**item) for item in payload.get("turns", [])]
            error = str(payload.get("error") or "speaker diarization 失败。")
            raise RuntimeError(error)
    except BaseException:
        _terminate_worker(process)
        raise
    finally:
        _finalize_worker(process, result_queue)


def _launch_diarization_worker(job: SpeakerDiarizationJob):
    ctx = multiprocessing.get_context("spawn")
    result_queue = ctx.Queue()
    process = ctx.Process(
        target=_diarization_worker_main,
        args=(job, result_queue),
        daemon=True,
        name="live-note-speaker",
    )
    process.start()
    return process, result_queue


def _diarization_worker_main(job: SpeakerDiarizationJob, result_queue) -> None:
    try:
        turns = _execute_diarization_job(job)
        result_queue.put(
            {
                "status": "ok",
                "turns": [
                    {
                        "started_ms": item.started_ms,
                        "ended_ms": item.ended_ms,
                        "speaker_id": item.speaker_id,
                    }
                    for item in turns
                ],
            }
        )
    except BaseException as exc:
        result_queue.put({"status": "error", "error": str(exc)})


def _terminate_worker(process) -> None:
    if not process.is_alive():
        return
    process.terminate()
    process.join(timeout=5)
    if process.is_alive() and hasattr(process, "kill"):
        process.kill()
        process.join(timeout=1)


def _finalize_worker(process, result_queue) -> None:
    process.join(timeout=0)
    close = getattr(result_queue, "close", None)
    if callable(close):
        close()
    join_thread = getattr(result_queue, "join_thread", None)
    if callable(join_thread):
        join_thread()


def _check_cancel(cancel_callback: Callable[[], None] | None) -> None:
    if cancel_callback is None:
        return
    cancel_callback()


def _cluster_count(job: SpeakerDiarizationJob) -> int:
    return job.expected_speakers if job.expected_speakers > 0 else -1


def _execute_diarization_job(job: SpeakerDiarizationJob) -> list[SpeakerTurn]:
    if job.backend == "pyannote":
        return _execute_pyannote_diarization_job(job)
    try:
        import sherpa_onnx
    except ImportError as exc:
        raise RuntimeError("缺少 sherpa_onnx 依赖。") from exc

    diarization = sherpa_onnx.OfflineSpeakerDiarization(
        sherpa_onnx.OfflineSpeakerDiarizationConfig(
            segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                    model=str(job.segmentation_model)
                )
            ),
            embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(job.embedding_model)),
            clustering=sherpa_onnx.FastClusteringConfig(
                num_clusters=_cluster_count(job),
                threshold=job.cluster_threshold,
            ),
            min_duration_on=job.min_duration_on,
            min_duration_off=job.min_duration_off,
        )
    )
    samples, sample_rate = _read_wave_f32(job.audio_path)
    if sample_rate != diarization.sample_rate:
        raise RuntimeError(
            f"speaker diarization 采样率不匹配：需要 {diarization.sample_rate}Hz，"
            f"当前为 {sample_rate}Hz。"
        )
    results = diarization.process(samples).sort_by_start_time()
    turns: list[SpeakerTurn] = []
    for result in results:
        turns.append(
            SpeakerTurn(
                started_ms=max(0, int(round(float(result.start) * 1000))),
                ended_ms=max(1, int(round(float(result.end) * 1000))),
                speaker_id=int(result.speaker),
            )
        )
    return turns


def _execute_pyannote_diarization_job(job: SpeakerDiarizationJob) -> list[SpeakerTurn]:
    try:
        from pyannote.audio import Pipeline
    except ImportError as exc:
        raise RuntimeError("缺少 pyannote.audio 依赖。") from exc

    pipeline = _load_pyannote_pipeline(Pipeline, job)
    diarization_kwargs: dict[str, Any] = {}
    if job.expected_speakers > 0:
        diarization_kwargs["num_speakers"] = job.expected_speakers
    diarization = pipeline(job.audio_path, **diarization_kwargs)
    speaker_ids: dict[str, int] = {}
    turns: list[SpeakerTurn] = []
    for turn, _, label in diarization.itertracks(yield_label=True):
        normalized_label = str(label)
        speaker_id = speaker_ids.setdefault(normalized_label, len(speaker_ids) + 1)
        turns.append(
            SpeakerTurn(
                started_ms=max(0, int(round(float(turn.start) * 1000))),
                ended_ms=max(1, int(round(float(turn.end) * 1000))),
                speaker_id=speaker_id,
            )
        )
    return sorted(turns, key=lambda item: (item.started_ms, item.ended_ms, item.speaker_id))


def _load_pyannote_pipeline(pipeline_cls, job: SpeakerDiarizationJob):
    kwargs: dict[str, Any] = {}
    if job.pyannote_auth_token:
        kwargs["token"] = job.pyannote_auth_token
    try:
        return pipeline_cls.from_pretrained(job.pyannote_model, **kwargs)
    except TypeError:
        if not job.pyannote_auth_token:
            raise
        return pipeline_cls.from_pretrained(
            job.pyannote_model,
            use_auth_token=job.pyannote_auth_token,
        )


def _backend_configured(config: AppConfig) -> bool:
    if config.speaker.backend == "pyannote":
        return bool(config.speaker.pyannote_model.strip())
    return (
        config.speaker.segmentation_model is not None
        and config.speaker.embedding_model is not None
    )


def _backend_dependency_available(backend: str) -> bool:
    if backend == "pyannote":
        return importlib.util.find_spec("pyannote.audio") is not None
    return importlib.util.find_spec("sherpa_onnx") is not None


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
    current: int | None = None,
    total: int | None = None,
    error: str | None = None,
) -> None:
    if callback is None:
        return
    callback(
        ProgressEvent(
            stage=stage,
            message=message,
            session_id=session_id,
            current=current,
            total=total,
            error=error,
        )
    )


def future_result(future) -> Any:
    return future.result()
