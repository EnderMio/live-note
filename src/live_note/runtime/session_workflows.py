from __future__ import annotations

import logging
import shutil
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path

from live_note.audio.convert import AudioImportError
from live_note.config import AppConfig
from live_note.domain import SegmentState, SessionMetadata
from live_note.llm import OpenAiCompatibleClient
from live_note.obsidian.client import ObsidianClient
from live_note.runtime.session_mutations import (
    create_workspace_session,
    require_runtime_session,
    update_workspace_session,
)
from live_note.runtime.session_outputs import publish_final_outputs, try_sync_note
from live_note.runtime.types import ProgressCallback
from live_note.runtime.workflow_support import (
    _attach_console_logging,
    _emit_progress,
    _read_wav_pcm16,
    _recover_session_segments,
    _run_live_refinement,
    _runtime_whisper_config,
    _wav_duration_ms,
    _wav_sample_rate,
    _write_wav,
    create_session_metadata,
    reconstruct_session_live_audio,
    recover_live_session_from_spool,
)
from live_note.session_workspace import SessionWorkspace, build_workspace

__all__ = [
    "finalize_session",
    "merge_sessions",
    "postprocess_session",
    "refine_session",
    "republish_session",
    "retranscribe_session",
    "sync_session_notes",
]


@dataclass(frozen=True, slots=True)
class MergeSourceSession:
    workspace: SessionWorkspace
    metadata: SessionMetadata
    states: list[SegmentState]
    duration_ms: int


def apply_speaker_labels(*args, **kwargs):
    from live_note.remote.speaker import apply_speaker_labels as _apply_speaker_labels

    return _apply_speaker_labels(*args, **kwargs)


def postprocess_session(
    config: AppConfig,
    session_id: str,
    on_progress: ProgressCallback | None = None,
    *,
    speaker_enabled: bool | None = None,
    recover_from_spool: bool = False,
) -> int:
    workspace = build_workspace(config.root_dir, session_id)
    metadata = require_runtime_session(config.root_dir, session_id)
    logger = workspace.session_logger()
    _attach_console_logging()
    resolved_config = config
    if speaker_enabled is not None:
        resolved_config = replace(
            config,
            speaker=replace(config.speaker, enabled=bool(speaker_enabled)),
        )
    obsidian = ObsidianClient(resolved_config.obsidian)
    llm_client = OpenAiCompatibleClient(resolved_config.llm)

    if recover_from_spool:
        metadata = recover_live_session_from_spool(
            config=resolved_config,
            workspace=workspace,
            metadata=metadata,
            logger=logger,
            on_progress=on_progress,
        )
    if resolved_config.refine.enabled and resolved_config.refine.auto_after_live:
        previous_source = metadata.transcript_source
        try:
            metadata = _run_live_refinement(
                config=resolved_config,
                workspace=workspace,
                metadata=update_workspace_session(
                    config.root_dir,
                    workspace,
                    event_kind="refine_started",
                    refine_status="refining",
                ),
                logger=logger,
                on_progress=on_progress,
            )
        except Exception as exc:
            logger.error("自动离线精修失败，将保留实时草稿: %s", exc)
            _emit_progress(
                on_progress,
                "error",
                f"自动离线精修失败：{exc}",
                session_id=metadata.session_id,
                error=str(exc),
            )
            metadata = update_workspace_session(
                config.root_dir,
                workspace,
                event_kind="refine_failed",
                transcript_source=previous_source,
                refine_status="failed",
            )
    if resolved_config.speaker.enabled:
        metadata = apply_speaker_labels(
            resolved_config,
            workspace,
            metadata,
            on_progress=on_progress,
        )
    publish_final_outputs(
        workspace,
        metadata,
        obsidian,
        llm_client,
        logger,
        on_progress=on_progress,
    )
    _emit_progress(
        on_progress,
        "done",
        "会话已完成。",
        session_id=metadata.session_id,
    )
    return 0


def finalize_session(
    config: AppConfig,
    session_id: str,
    on_progress: ProgressCallback | None = None,
) -> int:
    workspace = build_workspace(config.root_dir, session_id)
    metadata = require_runtime_session(config.root_dir, session_id)
    logger = workspace.session_logger()
    _attach_console_logging()
    _emit_progress(
        on_progress,
        "recovering",
        f"正在补全会话 {session_id}",
        session_id=metadata.session_id,
    )

    obsidian = ObsidianClient(config.obsidian)
    llm_client = OpenAiCompatibleClient(config.llm)

    missing = [
        state for state in workspace.rebuild_segment_states() if state.wav_path and not state.text
    ]
    if missing:
        whisper_config = _runtime_whisper_config(config.whisper, metadata.language)
        _recover_session_segments(
            workspace=workspace,
            metadata=metadata,
            whisper_config=whisper_config,
            logger=logger,
            states=missing,
            on_progress=on_progress,
            verb="补转写",
            status="transcribing",
            seed_entries=workspace.transcript_entries(),
        )

    publish_final_outputs(
        workspace,
        metadata,
        obsidian,
        llm_client,
        logger,
        on_progress=on_progress,
    )
    _emit_progress(
        on_progress,
        "done",
        "会话补写完成。",
        session_id=metadata.session_id,
    )
    return 0


def retranscribe_session(
    config: AppConfig,
    session_id: str,
    on_progress: ProgressCallback | None = None,
) -> int:
    workspace = build_workspace(config.root_dir, session_id)
    metadata = require_runtime_session(config.root_dir, session_id)
    logger = workspace.session_logger()
    _attach_console_logging()
    _emit_progress(
        on_progress,
        "recovering",
        f"正在重转写会话 {session_id}",
        session_id=metadata.session_id,
    )

    obsidian = ObsidianClient(config.obsidian)
    llm_client = OpenAiCompatibleClient(config.llm)
    whisper_config = _runtime_whisper_config(config.whisper, metadata.language)
    states = [state for state in workspace.rebuild_segment_states() if state.wav_path]
    _recover_session_segments(
        workspace=workspace,
        metadata=metadata,
        whisper_config=whisper_config,
        logger=logger,
        states=states,
        on_progress=on_progress,
        verb="重转写",
        status="retranscribing",
        seed_entries=[],
    )

    publish_final_outputs(
        workspace,
        metadata,
        obsidian,
        llm_client,
        logger,
        on_progress=on_progress,
    )
    _emit_progress(
        on_progress,
        "done",
        "会话重转写完成。",
        session_id=metadata.session_id,
    )
    return 0


def refine_session(
    config: AppConfig,
    session_id: str,
    on_progress: ProgressCallback | None = None,
) -> int:
    workspace = build_workspace(config.root_dir, session_id)
    metadata = require_runtime_session(config.root_dir, session_id)
    if metadata.input_mode != "live":
        raise RuntimeError("只有实时录音会话支持离线精修。")
    logger = workspace.session_logger()
    if not workspace.session_live_wav.exists():
        _emit_progress(
            on_progress,
            "refining",
            "未找到整场录音，正在尝试用分段音频回拼。",
            session_id=metadata.session_id,
        )
        if not reconstruct_session_live_audio(workspace):
            raise RuntimeError(
                "当前会话没有 session.live.wav，且无法从分段音频回拼整场录音，无法执行离线精修。"
            )
        logger.info("已从分段音频回拼 session.live.wav。")

    _attach_console_logging()
    _emit_progress(
        on_progress,
        "refining",
        f"正在离线精修会话 {session_id}",
        session_id=metadata.session_id,
    )

    obsidian = ObsidianClient(config.obsidian)
    llm_client = OpenAiCompatibleClient(config.llm)
    previous_source = metadata.transcript_source
    metadata = update_workspace_session(
        config.root_dir,
        workspace,
        event_kind="refine_started",
        refine_status="refining",
    )
    try:
        metadata = _run_live_refinement(
            config=config,
            workspace=workspace,
            metadata=metadata,
            logger=logger,
            on_progress=on_progress,
        )
    except Exception as exc:
        logger.error("离线精修失败: %s", exc)
        _emit_progress(
            on_progress,
            "error",
            f"离线精修失败：{exc}",
            session_id=metadata.session_id,
            error=str(exc),
        )
        metadata = update_workspace_session(
            config.root_dir,
            workspace,
            event_kind="refine_failed",
            transcript_source=previous_source,
            refine_status="failed",
        )

    publish_final_outputs(
        workspace,
        metadata,
        obsidian,
        llm_client,
        logger,
        on_progress=on_progress,
    )
    _emit_progress(
        on_progress,
        "done",
        "离线精修已完成。",
        session_id=metadata.session_id,
    )
    return 0


def merge_sessions(
    config: AppConfig,
    session_ids: list[str],
    *,
    title: str | None = None,
    on_progress: ProgressCallback | None = None,
) -> int:
    normalized_ids = _normalize_merge_session_ids(session_ids)
    if len(normalized_ids) < 2:
        raise RuntimeError("至少需要两条不同的会话才能执行合并。")

    sources = sorted(
        (_load_merge_source(config.root_dir, session_id) for session_id in normalized_ids),
        key=lambda item: (item.metadata.started_at, item.metadata.session_id),
    )
    metadata = _build_merged_session_metadata(config, sources, title=title)
    workspace = create_workspace_session(config.root_dir, metadata)
    logger = workspace.session_logger()
    _attach_console_logging()
    _emit_progress(
        on_progress,
        "merging",
        f"正在合并 {len(sources)} 条会话。",
        session_id=metadata.session_id,
    )

    _merge_source_sessions(
        workspace=workspace,
        metadata=metadata,
        sources=sources,
        logger=logger,
        on_progress=on_progress,
    )
    workspace.refresh_projection()

    if _can_merge_live_audio(sources):
        try:
            _merge_session_live_audio(sources, workspace.session_live_wav)
        except AudioImportError as exc:
            logger.warning("跳过合并后的 session.live.wav: %s", exc)
            if metadata.refine_status == "pending":
                metadata = update_workspace_session(
                    config.root_dir,
                    workspace,
                    event_kind="merge_refine_disabled",
                    refine_status="disabled",
                )
        else:
            logger.info("已生成合并后的 session.live.wav。")

    obsidian = ObsidianClient(config.obsidian)
    llm_client = OpenAiCompatibleClient(config.llm)
    publish_final_outputs(
        workspace,
        metadata,
        obsidian,
        llm_client,
        logger,
        on_progress=on_progress,
    )
    _emit_progress(
        on_progress,
        "done",
        f"已生成合并会话：{metadata.title}",
        session_id=metadata.session_id,
    )
    return 0


def republish_session(
    config: AppConfig,
    session_id: str,
    on_progress: ProgressCallback | None = None,
) -> int:
    workspace = build_workspace(config.root_dir, session_id)
    metadata = require_runtime_session(config.root_dir, session_id)
    logger = workspace.session_logger()
    _attach_console_logging()
    _emit_progress(
        on_progress,
        "publishing",
        f"正在重新生成会话 {session_id} 的输出",
        session_id=metadata.session_id,
    )
    obsidian = ObsidianClient(config.obsidian)
    llm_client = OpenAiCompatibleClient(config.llm)
    publish_final_outputs(
        workspace,
        metadata,
        obsidian,
        llm_client,
        logger,
        on_progress=on_progress,
    )
    _emit_progress(
        on_progress,
        "done",
        "已重新生成原文与整理稿。",
        session_id=metadata.session_id,
    )
    return 0


def sync_session_notes(
    config: AppConfig,
    session_id: str,
    on_progress: ProgressCallback | None = None,
) -> int:
    workspace = build_workspace(config.root_dir, session_id)
    metadata = require_runtime_session(config.root_dir, session_id)
    logger = workspace.session_logger()
    _attach_console_logging()
    obsidian = ObsidianClient(config.obsidian)
    if not obsidian.is_enabled():
        _emit_progress(
            on_progress,
            "done",
            "Obsidian 同步已关闭，跳过重新同步。",
            session_id=metadata.session_id,
        )
        return 0
    _emit_progress(
        on_progress,
        "syncing",
        f"正在重新同步会话 {session_id}",
        session_id=metadata.session_id,
    )
    if workspace.transcript_md.exists():
        try_sync_note(
            obsidian,
            metadata.transcript_note_path,
            workspace.transcript_md.read_text(encoding="utf-8"),
            logger,
            "原文笔记",
        )
    if workspace.structured_md.exists():
        try_sync_note(
            obsidian,
            metadata.structured_note_path,
            workspace.structured_md.read_text(encoding="utf-8"),
            logger,
            "整理笔记",
        )
    _emit_progress(
        on_progress,
        "done",
        "会话笔记已重新同步。",
        session_id=metadata.session_id,
    )
    return 0


def _normalize_merge_session_ids(session_ids: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for session_id in session_ids:
        trimmed = session_id.strip()
        if not trimmed or trimmed in seen:
            continue
        normalized.append(trimmed)
        seen.add(trimmed)
    return normalized


def _load_merge_source(config_root: Path, session_id: str) -> MergeSourceSession:
    workspace = build_workspace(config_root, session_id)
    metadata = require_runtime_session(config_root, session_id)
    states = workspace.rebuild_segment_states()
    return MergeSourceSession(
        workspace=workspace,
        metadata=metadata,
        states=states,
        duration_ms=_session_duration_ms(workspace, states),
    )


def _build_merged_session_metadata(
    config: AppConfig,
    sources: list[MergeSourceSession],
    *,
    title: str | None,
) -> SessionMetadata:
    resolved_title = title.strip() if title and title.strip() else _default_merged_title(sources)
    input_mode = _shared_value([item.metadata.input_mode for item in sources], default="merged")
    kind = _shared_value([item.metadata.kind for item in sources], default="generic")
    language = _shared_value([item.metadata.language for item in sources], default="auto")
    transcript_source = _resolve_merged_transcript_source(sources)
    refine_status = _resolve_merged_refine_status(config, sources, input_mode=input_mode)
    base = create_session_metadata(
        config=config,
        title=resolved_title,
        kind=kind,
        language=language,
        input_mode=input_mode,
        source_label=f"合并自 {len(sources)} 条会话",
        source_ref=",".join(item.metadata.session_id for item in sources),
    )
    return replace(
        base,
        started_at=sources[0].metadata.started_at,
        status="merged",
        transcript_source=transcript_source,
        refine_status=refine_status,
    )


def _default_merged_title(sources: list[MergeSourceSession]) -> str:
    titles = _dedupe_preserving_order(item.metadata.title for item in sources)
    if len(titles) == 1:
        base = titles[0]
    elif len(titles) == 2:
        base = " + ".join(titles)
    else:
        base = f"{titles[0]} 等 {len(titles)} 段"
    return f"{base}（合并）"


def _dedupe_preserving_order(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        result.append(value)
        seen.add(value)
    return result


def _shared_value(values: list[str], *, default: str) -> str:
    unique = {value for value in values if value}
    if len(unique) == 1:
        return unique.pop()
    return default


def _resolve_merged_transcript_source(sources: list[MergeSourceSession]) -> str:
    if all(item.metadata.transcript_source == "refined" for item in sources):
        return "refined"
    return "live"


def _resolve_merged_refine_status(
    config: AppConfig,
    sources: list[MergeSourceSession],
    *,
    input_mode: str,
) -> str:
    if all(item.metadata.refine_status == "done" for item in sources):
        return "done"
    if input_mode == "live" and _can_merge_live_audio(sources):
        return "pending" if config.refine.enabled else "disabled"
    return "disabled"


def _merge_source_sessions(
    workspace: SessionWorkspace,
    metadata: SessionMetadata,
    sources: list[MergeSourceSession],
    logger: logging.Logger,
    on_progress: ProgressCallback | None,
) -> None:
    offset_ms = 0
    counter = 0
    for index, source in enumerate(sources, start=1):
        _emit_progress(
            on_progress,
            "merging",
            f"正在合并会话 {index}/{len(sources)}：{source.metadata.title}",
            session_id=metadata.session_id,
            current=index,
            total=len(sources),
        )
        for state in source.states:
            counter += 1
            segment_id = f"seg-{counter:05d}"
            started_ms = state.started_ms + offset_ms
            ended_ms = state.ended_ms + offset_ms
            copied_wav = _copy_segment_wav_if_present(
                source=source,
                state=state,
                target_path=workspace.next_wav_path(segment_id),
                logger=logger,
            )
            if copied_wav is not None:
                workspace.record_segment_created(
                    segment_id,
                    started_ms,
                    ended_ms,
                    copied_wav,
                    refresh_projection=False,
                )
            if state.text:
                workspace.record_segment_text(
                    segment_id,
                    started_ms,
                    ended_ms,
                    state.text,
                    refresh_projection=False,
                )
            elif state.error:
                workspace.record_segment_error(
                    segment_id,
                    started_ms,
                    ended_ms,
                    state.error,
                    refresh_projection=False,
                )
        offset_ms += source.duration_ms


def _copy_segment_wav_if_present(
    *,
    source: MergeSourceSession,
    state: SegmentState,
    target_path: Path,
    logger: logging.Logger,
) -> Path | None:
    if state.wav_path is None:
        return None
    if not state.wav_path.exists():
        logger.warning(
            "合并会话 %s 时找不到片段音频 %s，将仅保留文本。",
            source.metadata.session_id,
            state.wav_path,
        )
        return None
    target_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(state.wav_path, target_path)
    return target_path


def _session_duration_ms(workspace: SessionWorkspace, states: list[SegmentState]) -> int:
    if states:
        return max(state.ended_ms for state in states)
    if workspace.session_live_wav.exists():
        try:
            return _wav_duration_ms(workspace.session_live_wav)
        except AudioImportError:
            return 0
    return 0


def _can_merge_live_audio(sources: list[MergeSourceSession]) -> bool:
    if not sources:
        return False
    sample_rate: int | None = None
    for item in sources:
        wav_path = item.workspace.session_live_wav
        if item.metadata.input_mode != "live" or not wav_path.exists():
            return False
        try:
            current_rate = _wav_sample_rate(wav_path)
        except AudioImportError:
            return False
        if sample_rate is None:
            sample_rate = current_rate
        elif current_rate != sample_rate:
            return False
    return True


def _merge_session_live_audio(
    sources: list[MergeSourceSession],
    output_path: Path,
) -> None:
    parts: list[bytes] = []
    sample_rate: int | None = None
    for source in sources:
        pcm16, current_rate = _read_wav_pcm16(source.workspace.session_live_wav)
        if sample_rate is None:
            sample_rate = current_rate
        elif current_rate != sample_rate:
            raise AudioImportError("无法合并采样率不同的整场录音。")
        parts.append(pcm16)
    if sample_rate is None:
        return
    _write_wav(output_path, sample_rate, b"".join(parts))
