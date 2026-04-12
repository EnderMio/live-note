from __future__ import annotations

import logging
import queue
import shutil
import wave
from collections.abc import Iterable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from live_note.audio.convert import AudioImportError, split_wav_file
from live_note.audio.segmentation import SegmentWindow, SpeechSegmenter
from live_note.config import AppConfig
from live_note.domain import PendingSegment, SegmentState, SessionMetadata, TranscriptEntry
from live_note.obsidian.client import ObsidianClient
from live_note.obsidian.renderer import build_transcript_note
from live_note.runtime.domain.session_state import SessionStatus
from live_note.runtime.ingest import iter_spool_frames, write_spool_to_wav
from live_note.runtime.session_mutations import require_runtime_session, update_workspace_session
from live_note.runtime.session_outputs import publish_failure_outputs
from live_note.runtime.types import ProgressCallback, ProgressEvent
from live_note.session_workspace import SessionWorkspace, session_root, workspace_root_dir
from live_note.transcribe.text import (
    build_transcription_prompt,
    normalize_transcript_text,
    should_admit_transcript_prompt,
)
from live_note.transcribe.whisper import (
    WhisperError,
    WhisperInferenceClient,
    WhisperServerProcess,
    with_language_override,
    with_runtime_port,
)
from live_note.utils import iso_now, slugify_filename

SPLIT_FALLBACK_SECONDS = 15
SPLIT_FALLBACK_ERROR = "疑似字幕/幻觉，自动恢复失败"
FRAME_STOP = object()
FRAME_PAUSE = object()
FRAME_RESUME = object()
SEGMENT_STOP = object()
SEGMENT_CONTEXT_RESET = object()


@dataclass(frozen=True, slots=True)
class SessionAudioWriter:
    path: Path
    sample_rate: int
    _handle: wave.Wave_write | None = None

    def __enter__(self) -> SessionAudioWriter:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = wave.open(str(self.path), "wb")
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(self.sample_rate)
        object.__setattr__(self, "_handle", handle)
        return self

    def write(self, pcm16: bytes) -> None:
        if self._handle is None:
            raise RuntimeError("SessionAudioWriter 尚未打开。")
        self._handle.writeframes(pcm16)

    def close(self) -> None:
        if self._handle is None:
            return
        self._handle.close()
        object.__setattr__(self, "_handle", None)

    def __exit__(self, exc_type, exc, exc_tb) -> None:
        self.close()
        return None


def create_session_metadata(
    config: AppConfig,
    title: str,
    kind: str,
    language: str,
    input_mode: str,
    source_label: str,
    source_ref: str,
) -> SessionMetadata:
    now = datetime.now(UTC)
    session_id = f"{now.strftime('%Y%m%d-%H%M%S')}-{slugify_filename(title)}"
    date_fragment = now.strftime("%Y-%m-%d")
    file_stem = f"{slugify_filename(title)}-{now.strftime('%H%M%S')}"
    workspace_dir = session_root(config.root_dir) / session_id
    transcript_note_path = f"{config.obsidian.transcript_dir}/{date_fragment}/{file_stem}.md"
    structured_note_path = f"{config.obsidian.structured_dir}/{date_fragment}/{file_stem}.md"
    if input_mode == "live":
        transcript_source = "live"
        refine_status = "pending" if config.refine.enabled else "disabled"
    else:
        transcript_source = "refined"
        refine_status = "disabled"
    return SessionMetadata(
        session_id=session_id,
        title=title,
        kind=kind,
        input_mode=input_mode,
        source_label=source_label,
        source_ref=source_ref,
        language=language,
        started_at=iso_now(),
        transcript_note_path=transcript_note_path,
        structured_note_path=structured_note_path,
        session_dir=str(workspace_dir),
        status=SessionStatus.STARTING.value,
        transcript_source=transcript_source,
        refine_status=refine_status,
    )


def _runtime_whisper_config(config: Any, language: str | None) -> Any:
    return with_runtime_port(with_language_override(config, language))


def _open_session_audio_writer(
    path: Path,
    sample_rate: int,
    *,
    enabled: bool,
):
    if not enabled:
        return nullcontext(None)
    return SessionAudioWriter(path, sample_rate)


def _enqueue_with_retry(target_queue: queue.Queue[Any], item: Any) -> None:
    while True:
        try:
            target_queue.put(item, timeout=0.5)
            return
        except queue.Full:
            continue


def _attach_console_logging() -> None:
    logger = logging.getLogger("live_note")
    logger.setLevel(logging.INFO)
    if any(getattr(handler, "_live_note_console", False) for handler in logger.handlers):
        return
    handler = _console_handler()
    setattr(handler, "_live_note_console", True)
    logger.addHandler(handler)


def _console_handler() -> logging.Handler:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    return handler


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


def _mark_session_failed(
    *,
    workspace: SessionWorkspace,
    obsidian: ObsidianClient,
    logger: logging.Logger,
    label: str,
    exc: BaseException,
    on_progress: ProgressCallback | None,
) -> None:
    root_dir = workspace_root_dir(workspace.root)
    session_id = workspace.root.name
    try:
        metadata = publish_failure_outputs(
            workspace=workspace,
            metadata=require_runtime_session(root_dir, session_id),
            obsidian=obsidian,
            logger=logger,
            reason=str(exc),
        )
    except Exception:
        metadata = require_runtime_session(root_dir, session_id)
    logger.exception("%s失败: %s", label, exc)
    _emit_progress(
        on_progress,
        "error",
        f"{label}失败：{exc}",
        session_id=metadata.session_id,
        error=str(exc),
    )


def _wav_duration_ms(path: Path) -> int:
    try:
        with wave.open(str(path), "rb") as handle:
            sample_rate = handle.getframerate()
            frame_count = handle.getnframes()
    except (EOFError, OSError, wave.Error) as exc:
        raise AudioImportError(f"WAV 文件损坏或不可读: {path}") from exc
    if sample_rate <= 0:
        return 0
    return round(frame_count * 1000 / sample_rate)


def _wav_sample_rate(path: Path) -> int:
    try:
        with wave.open(str(path), "rb") as handle:
            sample_rate = handle.getframerate()
    except (EOFError, OSError, wave.Error) as exc:
        raise AudioImportError(f"WAV 文件损坏或不可读: {path}") from exc
    if sample_rate <= 0:
        raise AudioImportError(f"WAV 采样率不合法: {path}")
    return sample_rate


def _read_wav_pcm16(path: Path) -> tuple[bytes, int]:
    try:
        with wave.open(str(path), "rb") as handle:
            channels = handle.getnchannels()
            sample_width = handle.getsampwidth()
            sample_rate = handle.getframerate()
            if channels != 1 or sample_width != 2:
                raise AudioImportError(f"WAV 格式不受支持: {path}")
            return handle.readframes(handle.getnframes()), sample_rate
    except AudioImportError:
        raise
    except (EOFError, OSError, wave.Error) as exc:
        raise AudioImportError(f"WAV 文件损坏或不可读: {path}") from exc


def _write_wav(path: Path, sample_rate: int, pcm16: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm16)


def can_reconstruct_session_live_audio(workspace: SessionWorkspace) -> bool:
    try:
        states = workspace.rebuild_segment_states()
    except Exception:
        return False
    if not states:
        return False
    sample_rate: int | None = None
    for state in states:
        if state.wav_path is None or not state.wav_path.exists():
            return False
        try:
            current_rate = _wav_sample_rate(state.wav_path)
        except AudioImportError:
            return False
        if sample_rate is None:
            sample_rate = current_rate
        elif current_rate != sample_rate:
            return False
    return True


def reconstruct_session_live_audio(workspace: SessionWorkspace) -> bool:
    if workspace.session_live_wav.exists():
        return True
    if not can_reconstruct_session_live_audio(workspace):
        return False

    states = workspace.rebuild_segment_states()
    parts: list[bytes] = []
    sample_rate: int | None = None
    current_frame = 0
    final_frame = 0

    for state in states:
        if state.wav_path is None:
            return False
        pcm16, current_rate = _read_wav_pcm16(state.wav_path)
        if sample_rate is None:
            sample_rate = current_rate
        elif current_rate != sample_rate:
            return False
        start_frame = round(state.started_ms * current_rate / 1000)
        end_frame = round(state.ended_ms * current_rate / 1000)
        if start_frame > current_frame:
            parts.append(b"\x00\x00" * (start_frame - current_frame))
            current_frame = start_frame
        parts.append(pcm16)
        current_frame += len(pcm16) // 2
        final_frame = max(final_frame, end_frame)

    if sample_rate is None:
        return False
    if final_frame > current_frame:
        parts.append(b"\x00\x00" * (final_frame - current_frame))

    _write_wav(workspace.session_live_wav, sample_rate, b"".join(parts))
    return True


def reset_live_recovery_workspace(
    workspace: SessionWorkspace,
    metadata: SessionMetadata,
) -> None:
    workspace.segments_jsonl.write_text("", encoding="utf-8")
    workspace.segments_live_jsonl.write_text("", encoding="utf-8")
    workspace.transcript_md.write_text(
        build_transcript_note(metadata, [], status=SessionStatus.INGESTING.value),
        encoding="utf-8",
    )
    workspace.structured_md.unlink(missing_ok=True)
    workspace.session_live_wav.unlink(missing_ok=True)
    for wav_path in workspace.segments_dir.glob("*.wav"):
        wav_path.unlink(missing_ok=True)
    workspace.refresh_projection()


def recover_live_session_from_spool(
    *,
    config: AppConfig,
    workspace: SessionWorkspace,
    metadata: SessionMetadata,
    logger: logging.Logger,
    on_progress: ProgressCallback | None = None,
) -> SessionMetadata:
    if metadata.input_mode != "live":
        raise RuntimeError("只有实时录音会话支持从 ingest journal 恢复。")
    if not workspace.live_ingest_pcm.exists():
        raise FileNotFoundError("缺少 live.ingest.pcm，无法恢复实时会话。")

    reset_live_recovery_workspace(workspace, metadata)
    _emit_progress(
        on_progress,
        "recovering",
        f"正在从 ingest journal 恢复会话 {metadata.session_id}",
        session_id=metadata.session_id,
    )
    write_spool_to_wav(
        session_dir=workspace.root,
        output_path=workspace.session_live_wav,
        sample_rate=config.audio.sample_rate,
    )

    whisper_config = _runtime_whisper_config(config.whisper, metadata.language)
    whisper_client = WhisperInferenceClient(whisper_config)
    segmenter = SpeechSegmenter(config.audio)
    obsidian = ObsidianClient(config.obsidian)
    entries: list[TranscriptEntry] = []
    prompt_entries: list[TranscriptEntry] = []

    with WhisperServerProcess(whisper_config, workspace.logs_txt):
        counter = 0

        def emit_segments(segments: Iterable[SegmentWindow]) -> None:
            nonlocal counter
            for segment in segments:
                counter += 1
                pending = _persist_live_segment(
                    counter=counter,
                    segment=segment,
                    workspace=workspace,
                    sample_rate=config.audio.sample_rate,
                )
                success = _process_segment(
                    pending=pending,
                    workspace=workspace,
                    metadata=metadata,
                    obsidian=obsidian,
                    whisper_client=whisper_client,
                    logger=logger,
                    entries=entries,
                    context_entries=prompt_entries,
                    live_status=SessionStatus.INGESTING.value,
                    sample_rate=config.audio.sample_rate,
                )
                stage = "segment_transcribed" if success else "segment_failed"
                message = (
                    f"恢复片段 {pending.segment_id} 完成"
                    if success
                    else f"恢复片段 {pending.segment_id} 失败"
                )
                _emit_progress(
                    on_progress,
                    stage,
                    message,
                    session_id=metadata.session_id,
                    current=counter,
                )

        for frame in iter_spool_frames(workspace.root):
            emit_segments(segmenter.feed(frame))
        emit_segments(segmenter.flush())
    workspace.refresh_projection()

    return require_runtime_session(workspace_root_dir(workspace.root), metadata.session_id)


def _admitted_prompt_entries(entries: list[TranscriptEntry]) -> list[TranscriptEntry]:
    return [entry for entry in entries if should_admit_transcript_prompt(entry.text)]


def _transcribe_segment_text(
    whisper_client: WhisperInferenceClient,
    wav_path: Path,
    language: str,
    entries: list[TranscriptEntry],
    *,
    prompt_entries: list[TranscriptEntry] | None = None,
    pcm16: bytes | None = None,
    sample_rate: int | None = None,
    disable_prompt: bool = False,
) -> str:
    prompt = None
    if not disable_prompt:
        prompt_source = entries if prompt_entries is None else prompt_entries
        prompt = build_transcription_prompt(language, prompt_source)
    raw_text = whisper_client.transcribe(wav_path, prompt=prompt)
    if not raw_text.strip():
        return ""

    resolved_pcm16 = pcm16
    resolved_sample_rate = sample_rate
    if resolved_pcm16 is None or resolved_sample_rate is None:
        resolved_pcm16, resolved_sample_rate = _read_wav_pcm16(wav_path)

    return normalize_transcript_text(
        raw_text,
        language,
        pcm16=resolved_pcm16,
        sample_rate=resolved_sample_rate,
    )


def _should_run_single_split_fallback(pending: PendingSegment) -> bool:
    return (pending.ended_ms - pending.started_ms) > SPLIT_FALLBACK_SECONDS * 1000


def _transcribe_with_single_split_fallback(
    *,
    pending: PendingSegment,
    whisper_client: WhisperInferenceClient,
    language: str,
    entries: list[TranscriptEntry],
) -> str:
    child_output_dir = pending.wav_path.parent / f"{pending.segment_id}-split-fallback"
    child_chunks = split_wav_file(
        input_path=pending.wav_path,
        output_dir=child_output_dir,
        chunk_seconds=SPLIT_FALLBACK_SECONDS,
    )

    accepted_children: list[tuple[int, int, str]] = []
    for child in sorted(child_chunks, key=lambda chunk: (chunk.started_ms, chunk.ended_ms)):
        child_text = _transcribe_segment_text(
            whisper_client=whisper_client,
            wav_path=child.wav_path,
            language=language,
            entries=entries,
            disable_prompt=True,
        )
        if child_text and should_admit_transcript_prompt(child_text):
            accepted_children.append((child.started_ms, child.ended_ms, child_text))

    if not accepted_children:
        raise WhisperError(SPLIT_FALLBACK_ERROR)

    return " ".join(text for _, _, text in accepted_children)


def _transcribe_with_suspicious_recovery_ladder(
    *,
    pending: PendingSegment,
    whisper_client: WhisperInferenceClient,
    language: str,
    entries: list[TranscriptEntry],
    prompt_entries: list[TranscriptEntry],
    pcm16: bytes | None = None,
    sample_rate: int | None = None,
) -> str:
    text = _transcribe_segment_text(
        whisper_client=whisper_client,
        wav_path=pending.wav_path,
        language=language,
        entries=entries,
        prompt_entries=prompt_entries,
        pcm16=pcm16,
        sample_rate=sample_rate,
    )
    if not text or should_admit_transcript_prompt(text):
        return text

    retry_text = _transcribe_segment_text(
        whisper_client=whisper_client,
        wav_path=pending.wav_path,
        language=language,
        entries=entries,
        prompt_entries=prompt_entries,
        pcm16=pcm16,
        sample_rate=sample_rate,
        disable_prompt=True,
    )
    if retry_text and should_admit_transcript_prompt(retry_text):
        return retry_text
    if _should_run_single_split_fallback(pending):
        return _transcribe_with_single_split_fallback(
            pending=pending,
            whisper_client=whisper_client,
            language=language,
            entries=entries,
        )
    raise WhisperError(SPLIT_FALLBACK_ERROR)


def _persist_live_segment(
    counter: int,
    segment: SegmentWindow,
    workspace: SessionWorkspace,
    sample_rate: int,
) -> PendingSegment:
    segment_id = f"seg-{counter:05d}"
    wav_path = workspace.next_wav_path(segment_id)
    _write_wav(wav_path, sample_rate, segment.pcm16)
    workspace.record_segment_created(segment_id, segment.started_ms, segment.ended_ms, wav_path)
    return PendingSegment(
        segment_id=segment_id,
        started_ms=segment.started_ms,
        ended_ms=segment.ended_ms,
        pcm16=segment.pcm16,
        wav_path=wav_path,
    )


def _process_segment(
    pending: PendingSegment,
    workspace: SessionWorkspace,
    metadata: SessionMetadata,
    obsidian: ObsidianClient,
    whisper_client: WhisperInferenceClient,
    logger: logging.Logger,
    entries: list[TranscriptEntry],
    live_status: str,
    context_entries: list[TranscriptEntry] | None = None,
    sample_rate: int | None = None,
    guard_prompt_admission: bool = False,
) -> bool:
    try:
        prompt_entries = context_entries if context_entries is not None else entries
        if guard_prompt_admission:
            prompt_entries = _admitted_prompt_entries(prompt_entries)
        if guard_prompt_admission:
            text = _transcribe_with_suspicious_recovery_ladder(
                pending=pending,
                whisper_client=whisper_client,
                language=metadata.language,
                entries=entries,
                prompt_entries=prompt_entries,
                pcm16=pending.pcm16,
                sample_rate=sample_rate,
            )
        else:
            text = _transcribe_segment_text(
                whisper_client=whisper_client,
                wav_path=pending.wav_path,
                language=metadata.language,
                entries=entries,
                prompt_entries=prompt_entries,
                pcm16=pending.pcm16,
                sample_rate=sample_rate,
            )
        if not text:
            raise WhisperError("whisper-server 返回空转写结果。")
        workspace.record_segment_text(
            pending.segment_id,
            pending.started_ms,
            pending.ended_ms,
            text,
        )
        entry = TranscriptEntry(
            segment_id=pending.segment_id,
            started_ms=pending.started_ms,
            ended_ms=pending.ended_ms,
            text=text,
        )
        entries.append(entry)
        if context_entries is not None:
            context_entries.append(entry)
        content = build_transcript_note(metadata, list(entries), status=live_status)
        workspace.write_transcript(content)
        return True
    except Exception as exc:
        workspace.record_segment_error(
            pending.segment_id,
            pending.started_ms,
            pending.ended_ms,
            str(exc),
        )
        logger.error("片段 %s 处理失败: %s", pending.segment_id, exc)
        return False


def _recover_session_segments(
    workspace: SessionWorkspace,
    metadata: SessionMetadata,
    whisper_config: Any,
    logger: logging.Logger,
    states: list[SegmentState],
    on_progress: ProgressCallback | None,
    verb: str,
    status: str,
    seed_entries: list[TranscriptEntry],
) -> None:
    if not states:
        return

    whisper_client = WhisperInferenceClient(whisper_config)
    entries = list(seed_entries)
    with WhisperServerProcess(whisper_config, workspace.logs_txt):
        for index, state in enumerate(states, start=1):
            assert state.wav_path is not None
            try:
                _emit_progress(
                    on_progress,
                    status,
                    f"正在{verb}片段 {index}/{len(states)}",
                    session_id=metadata.session_id,
                    current=index,
                    total=len(states),
                )
                pending = PendingSegment(
                    segment_id=state.segment_id,
                    started_ms=state.started_ms,
                    ended_ms=state.ended_ms,
                    pcm16=None,
                    wav_path=state.wav_path,
                )
                text = _transcribe_with_suspicious_recovery_ladder(
                    pending=pending,
                    whisper_client=whisper_client,
                    language=metadata.language,
                    entries=entries,
                    prompt_entries=_admitted_prompt_entries(entries),
                )
                if not text:
                    raise WhisperError(f"{verb}结果为空。")
                workspace.record_segment_text(
                    state.segment_id,
                    state.started_ms,
                    state.ended_ms,
                    text,
                )
                entries.append(
                    TranscriptEntry(
                        segment_id=state.segment_id,
                        started_ms=state.started_ms,
                        ended_ms=state.ended_ms,
                        text=text,
                    )
                )
            except Exception as exc:
                workspace.record_segment_error(
                    state.segment_id,
                    state.started_ms,
                    state.ended_ms,
                    str(exc),
                )
                logger.error("%s失败 %s: %s", verb, state.segment_id, exc)
                _emit_progress(
                    on_progress,
                    "segment_failed",
                    f"{verb}失败：{state.segment_id}",
                    session_id=metadata.session_id,
                    current=index,
                    total=len(states),
                    error=str(exc),
                )
    workspace.refresh_projection()


def _run_live_refinement(
    config: AppConfig,
    workspace: SessionWorkspace,
    metadata: SessionMetadata,
    logger: logging.Logger,
    on_progress: ProgressCallback | None = None,
) -> SessionMetadata:
    if metadata.input_mode != "live":
        raise RuntimeError("只有实时录音会话支持离线精修。")
    if not workspace.session_live_wav.exists():
        raise FileNotFoundError("找不到 session.live.wav，无法离线精修。")

    whisper_config = _runtime_whisper_config(config.whisper, metadata.language)
    if workspace.refined_dir.exists():
        shutil.rmtree(workspace.refined_dir)
    workspace.refined_dir.mkdir(parents=True, exist_ok=True)
    if workspace.refined_segments_tmp_jsonl.exists():
        workspace.refined_segments_tmp_jsonl.unlink()

    try:
        chunks = split_wav_file(
            input_path=workspace.session_live_wav,
            output_dir=workspace.refined_dir,
            chunk_seconds=config.importer.chunk_seconds,
        )
        if not chunks:
            raise AudioImportError("整场录音为空，无法离线精修。")

        entries: list[TranscriptEntry] = []
        whisper_client = WhisperInferenceClient(whisper_config)
        with WhisperServerProcess(whisper_config, workspace.logs_txt):
            for index, chunk in enumerate(chunks, start=1):
                _emit_progress(
                    on_progress,
                    "refining",
                    f"正在离线精修片段 {index}/{len(chunks)}",
                    session_id=metadata.session_id,
                    current=index,
                    total=len(chunks),
                )
                workspace.record_segment_created(
                    chunk.segment_id,
                    chunk.started_ms,
                    chunk.ended_ms,
                    chunk.wav_path,
                    journal_path=workspace.refined_segments_tmp_jsonl,
                )
                text = _transcribe_segment_text(
                    whisper_client=whisper_client,
                    wav_path=chunk.wav_path,
                    language=metadata.language,
                    entries=entries,
                )
                if not text:
                    workspace.record_segment_error(
                        chunk.segment_id,
                        chunk.started_ms,
                        chunk.ended_ms,
                        "离线精修结果为空。",
                        journal_path=workspace.refined_segments_tmp_jsonl,
                    )
                    logger.info("离线精修跳过空片段 %s", chunk.segment_id)
                    continue
                workspace.record_segment_text(
                    chunk.segment_id,
                    chunk.started_ms,
                    chunk.ended_ms,
                    text,
                    journal_path=workspace.refined_segments_tmp_jsonl,
                )
                entries.append(
                    TranscriptEntry(
                        segment_id=chunk.segment_id,
                        started_ms=chunk.started_ms,
                        ended_ms=chunk.ended_ms,
                        text=text,
                    )
                )
        if not entries:
            raise WhisperError("离线精修结果为空。")
    except Exception:
        logger.exception("离线精修失败")
        raise
    else:
        workspace.replace_canonical_journal(workspace.refined_segments_tmp_jsonl)
        return update_workspace_session(
            workspace_root_dir(workspace.root),
            workspace,
            event_kind="refine_completed",
            transcript_source="refined",
            refine_status="done",
        )
    finally:
        if workspace.refined_segments_tmp_jsonl.exists():
            workspace.refined_segments_tmp_jsonl.unlink()
