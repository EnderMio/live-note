from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from live_note.runtime.remote_session_projections import list_remote_session_projections
from live_note.runtime.store import ControlDb, SessionProjectionRepo

from .history_queries import list_session_history


@dataclass(frozen=True, slots=True)
class SessionSummary:
    session_id: str
    title: str
    kind: str
    input_mode: str
    started_at: str
    status: str
    runtime_status: str
    display_status: str
    segment_count: int
    transcribed_count: int
    failed_count: int
    latest_error: str | None
    transcript_source: str
    refine_status: str
    execution_target: str
    speaker_status: str
    session_dir: Path
    transcript_file: Path
    structured_file: Path


def list_session_summaries(root_dir: Path) -> list[SessionSummary]:
    items_by_session_id: dict[str, SessionSummary] = {}
    stats_by_session_id = {
        record.session_id: record
        for record in SessionProjectionRepo(ControlDb.for_root(root_dir)).list_all()
    }
    for record in list_session_history(ControlDb.for_root(root_dir)):
        root = Path(record.session_dir)
        stats = stats_by_session_id.get(record.session_id)
        items_by_session_id[record.session_id] = SessionSummary(
            session_id=record.session_id,
            title=record.title,
            kind=record.kind,
            input_mode=record.input_mode,
            started_at=record.started_at,
            status=record.status,
            runtime_status=record.runtime_status,
            display_status=record.display_status,
            segment_count=stats.segment_count if stats is not None else 0,
            transcribed_count=stats.transcribed_count if stats is not None else 0,
            failed_count=stats.failed_count if stats is not None else 0,
            latest_error=stats.latest_error if stats is not None else None,
            transcript_source=record.transcript_source,
            refine_status=record.refine_status,
            execution_target=record.execution_target,
            speaker_status=record.speaker_status,
            session_dir=root,
            transcript_file=root / "transcript.md",
            structured_file=root / "structured.md",
        )
    for record in list_remote_session_projections(root_dir):
        root = Path(record.session_dir)
        stats = stats_by_session_id.get(record.session_id)
        items_by_session_id[record.session_id] = SessionSummary(
            session_id=record.session_id,
            title=record.title,
            kind=record.kind,
            input_mode=record.input_mode,
            started_at=record.started_at,
            status=record.status,
            runtime_status=record.runtime_status or record.status,
            display_status=record.status,
            segment_count=stats.segment_count if stats is not None else 0,
            transcribed_count=stats.transcribed_count if stats is not None else 0,
            failed_count=stats.failed_count if stats is not None else 0,
            latest_error=stats.latest_error if stats is not None else None,
            transcript_source=record.transcript_source,
            refine_status=record.refine_status,
            execution_target=record.execution_target,
            speaker_status=record.speaker_status,
            session_dir=root,
            transcript_file=root / "transcript.md",
            structured_file=root / "structured.md",
        )
    return sorted(items_by_session_id.values(), key=lambda item: item.started_at, reverse=True)
