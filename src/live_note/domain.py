from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AudioFrame:
    started_ms: int
    ended_ms: int
    pcm16: bytes


@dataclass(frozen=True, slots=True)
class PendingSegment:
    segment_id: str
    started_ms: int
    ended_ms: int
    pcm16: bytes | None
    wav_path: Path


@dataclass(frozen=True, slots=True)
class TranscriptEntry:
    segment_id: str
    started_ms: int
    ended_ms: int
    text: str


@dataclass(frozen=True, slots=True)
class ReviewItem:
    started_ms: int
    ended_ms: int
    reason_labels: tuple[str, ...]
    excerpt: str


@dataclass(frozen=True, slots=True)
class SegmentEvent:
    kind: str
    segment_id: str
    started_ms: int
    ended_ms: int
    created_at: str
    wav_path: str | None = None
    text: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class SegmentState:
    segment_id: str
    started_ms: int
    ended_ms: int
    wav_path: Path | None
    text: str | None
    error: str | None


@dataclass(frozen=True, slots=True)
class SessionMetadata:
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
    transcript_source: str = "live"
    refine_status: str = "disabled"

    @property
    def note_stem(self) -> str:
        return Path(self.transcript_note_path).stem
