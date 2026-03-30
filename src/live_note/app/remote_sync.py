from __future__ import annotations

from dataclasses import replace

from live_note.config import AppConfig
from live_note.domain import SessionMetadata, TranscriptEntry
from live_note.llm import OpenAiCompatibleClient
from live_note.obsidian.client import ObsidianClient
from live_note.obsidian.renderer import build_transcript_note

from .coordinator import create_session_metadata
from .events import ProgressCallback
from .journal import SessionWorkspace, session_root
from .session_outputs import publish_final_outputs, try_sync_note, write_initial_transcript


def ensure_remote_workspace(
    config: AppConfig,
    remote_metadata: SessionMetadata,
) -> SessionWorkspace:
    metadata = build_local_remote_metadata(config, remote_metadata)
    workspace_root = session_root(config.root_dir) / metadata.session_id
    if workspace_root.exists():
        workspace = SessionWorkspace.load(workspace_root)
        workspace.write_session(metadata)
        return workspace
    workspace = SessionWorkspace.create(workspace_root, metadata)
    logger = workspace.session_logger()
    write_initial_transcript(
        workspace,
        metadata,
        ObsidianClient(config.obsidian),
        logger,
        status=metadata.status,
    )
    return workspace


def sync_remote_transcript_snapshot(
    config: AppConfig,
    remote_metadata: SessionMetadata,
    entries: list[TranscriptEntry],
) -> SessionMetadata:
    workspace = ensure_remote_workspace(config, remote_metadata)
    metadata = build_local_remote_metadata(config, remote_metadata)
    workspace.write_session(metadata)
    workspace.segments_jsonl.write_text("", encoding="utf-8")
    for entry in entries:
        workspace.record_segment_text(
            entry.segment_id,
            entry.started_ms,
            entry.ended_ms,
            entry.text,
            speaker_label=entry.speaker_label,
        )
    transcript = build_transcript_note(metadata, entries, status=metadata.status)
    workspace.write_transcript(transcript)
    return workspace.read_session()


def apply_remote_artifacts(
    config: AppConfig,
    remote_metadata: SessionMetadata,
    entries: list[TranscriptEntry],
    *,
    transcript_content: str | None = None,
    structured_content: str | None = None,
    on_progress: ProgressCallback | None = None,
) -> SessionMetadata:
    workspace = ensure_remote_workspace(config, remote_metadata)
    metadata = build_local_remote_metadata(config, remote_metadata)
    workspace.write_session(metadata)
    workspace.segments_jsonl.write_text("", encoding="utf-8")
    for entry in entries:
        workspace.record_segment_text(
            entry.segment_id,
            entry.started_ms,
            entry.ended_ms,
            entry.text,
            speaker_label=entry.speaker_label,
        )
    logger = workspace.session_logger()
    obsidian = ObsidianClient(config.obsidian)
    if transcript_content is not None or structured_content is not None:
        transcript = transcript_content or build_transcript_note(
            metadata,
            entries,
            status=metadata.status,
        )
        workspace.write_transcript(transcript)
        try_sync_note(
            obsidian,
            metadata.transcript_note_path,
            transcript,
            logger,
            "远端原文最终笔记",
        )
        if structured_content:
            workspace.write_structured(structured_content)
            try_sync_note(
                obsidian,
                metadata.structured_note_path,
                structured_content,
                logger,
                "远端整理笔记",
            )
    else:
        publish_final_outputs(
            workspace,
            metadata,
            obsidian,
            OpenAiCompatibleClient(config.llm),
            logger,
            on_progress=on_progress,
        )
    return workspace.read_session()


def build_local_remote_metadata(
    config: AppConfig,
    remote_metadata: SessionMetadata,
) -> SessionMetadata:
    base = create_session_metadata(
        config=config,
        title=remote_metadata.title,
        kind=remote_metadata.kind,
        language=remote_metadata.language,
        input_mode=remote_metadata.input_mode,
        source_label=remote_metadata.source_label,
        source_ref=remote_metadata.source_ref,
    )
    local_session_dir = session_root(config.root_dir) / remote_metadata.session_id
    transcript_note_path = _preferred_note_path(
        remote_metadata.transcript_note_path,
        fallback=base.transcript_note_path,
    )
    structured_note_path = _preferred_note_path(
        remote_metadata.structured_note_path,
        fallback=base.structured_note_path,
    )
    return replace(
        base,
        session_id=remote_metadata.session_id,
        started_at=remote_metadata.started_at,
        transcript_note_path=transcript_note_path,
        structured_note_path=structured_note_path,
        session_dir=str(local_session_dir),
        status=remote_metadata.status,
        transcript_source=remote_metadata.transcript_source,
        refine_status=remote_metadata.refine_status,
        execution_target="remote",
        remote_session_id=remote_metadata.remote_session_id or remote_metadata.session_id,
        speaker_status=remote_metadata.speaker_status,
    )


def _preferred_note_path(path: str, *, fallback: str) -> str:
    normalized = path.strip()
    if normalized:
        return normalized
    return fallback
