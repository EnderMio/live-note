from __future__ import annotations

from dataclasses import dataclass

from live_note.domain import SessionMetadata


@dataclass(frozen=True, slots=True)
class RemoteSessionProjectionRecord:
    session_id: str
    title: str
    kind: str
    input_mode: str
    source_label: str
    source_ref: str
    language: str
    started_at: str
    transcript_note_path: str
    structured_note_path: str
    session_dir: str
    status: str
    runtime_status: str | None
    transcript_source: str
    refine_status: str
    execution_target: str
    remote_session_id: str | None
    speaker_status: str
    remote_updated_at: str | None = None
    last_seen_at: str | None = None
    artifacts_synced_at: str | None = None

    def to_metadata(self) -> SessionMetadata:
        return SessionMetadata(
            session_id=self.session_id,
            title=self.title,
            kind=self.kind,
            input_mode=self.input_mode,
            source_label=self.source_label,
            source_ref=self.source_ref,
            language=self.language,
            started_at=self.started_at,
            transcript_note_path=self.transcript_note_path,
            structured_note_path=self.structured_note_path,
            session_dir=self.session_dir,
            status=self.status,
            transcript_source=self.transcript_source,
            refine_status=self.refine_status,
            execution_target=self.execution_target,
            remote_session_id=self.remote_session_id,
            speaker_status=self.speaker_status,
        )
