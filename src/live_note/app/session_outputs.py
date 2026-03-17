from __future__ import annotations

import logging

from live_note.domain import SessionMetadata, TranscriptEntry
from live_note.llm import LlmError, OpenAiCompatibleClient
from live_note.obsidian.client import ObsidianClient, ObsidianError
from live_note.obsidian.renderer import (
    build_structured_failure_note,
    build_structured_note,
    build_structured_pending_note,
    build_transcript_note,
)
from live_note.review import detect_review_items

from .events import ProgressCallback, ProgressEvent
from .journal import SessionWorkspace


def write_initial_transcript(
    workspace: SessionWorkspace,
    metadata: SessionMetadata,
    obsidian: ObsidianClient,
    logger: logging.Logger,
    *,
    status: str,
) -> None:
    initial_note = build_transcript_note(metadata, [], status=status)
    workspace.write_transcript(initial_note)
    try_sync_note(obsidian, metadata.transcript_note_path, initial_note, logger, "原文初始笔记")


def publish_final_outputs(
    workspace: SessionWorkspace,
    metadata: SessionMetadata,
    obsidian: ObsidianClient,
    llm_client: OpenAiCompatibleClient,
    logger: logging.Logger,
    *,
    on_progress: ProgressCallback | None = None,
) -> None:
    final_entries = workspace.transcript_entries()
    review_items = detect_review_items(final_entries, metadata.language)
    session_audio_path = "session.live.wav" if workspace.session_live_wav.exists() else None
    structured_body, structured_status = build_structured_output(
        llm_client=llm_client,
        metadata=metadata,
        entries=final_entries,
        transcript_note_path=metadata.transcript_note_path,
    )
    _emit_progress(
        on_progress,
        "publishing",
        "正在生成最终原文。",
        session_id=metadata.session_id,
    )
    final_transcript = build_transcript_note(
        metadata,
        final_entries,
        status=structured_status,
        review_items=review_items,
        session_audio_path=session_audio_path,
    )
    workspace.write_transcript(final_transcript)
    try_sync_note(
        obsidian,
        metadata.transcript_note_path,
        final_transcript,
        logger,
        "原文最终笔记",
    )

    _emit_progress(
        on_progress,
        "summarizing",
        "正在生成整理稿。",
        session_id=metadata.session_id,
    )
    workspace.write_structured(structured_body)
    try_sync_note(obsidian, metadata.structured_note_path, structured_body, logger, "整理笔记")
    workspace.update_session(status=structured_status)


def build_structured_output(
    llm_client: OpenAiCompatibleClient,
    metadata: SessionMetadata,
    entries: list[TranscriptEntry],
    transcript_note_path: str,
) -> tuple[str, str]:
    if not entries:
        body = build_structured_failure_note(
            metadata,
            transcript_note_path=transcript_note_path,
            reason="当前会话没有可用的转写文本。",
        )
        return body, "structured_failed"

    if not llm_client.is_enabled():
        body = build_structured_pending_note(
            metadata,
            transcript_note_path=transcript_note_path,
            reason="当前会话未启用自动整理，请按需手动补写。",
        )
        return body, "transcript_only"

    if not llm_client.is_configured():
        body = build_structured_pending_note(
            metadata,
            transcript_note_path=transcript_note_path,
            reason="LLM 已启用但配置不完整，先保留可手动补写的整理模板。",
        )
        return body, "transcript_only"

    try:
        llm_markdown = llm_client.generate_structured_note(metadata, entries)
        body = build_structured_note(
            metadata,
            llm_markdown=llm_markdown,
            transcript_note_path=transcript_note_path,
            status="finalized",
        )
        return body, "finalized"
    except LlmError as exc:
        body = build_structured_failure_note(
            metadata,
            transcript_note_path=transcript_note_path,
            reason=str(exc),
        )
        return body, "structured_failed"


def try_sync_note(
    obsidian: ObsidianClient,
    path: str,
    content: str,
    logger: logging.Logger,
    label: str,
) -> None:
    try:
        obsidian.put_note(path, content)
    except ObsidianError as exc:
        logger.warning("%s 同步失败，将保留在本地 journal 中: %s", label, exc)


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
