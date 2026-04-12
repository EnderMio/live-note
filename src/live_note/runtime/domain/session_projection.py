from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SessionProjectionRecord:
    session_id: str
    segment_count: int
    transcribed_count: int
    failed_count: int
    latest_error: str | None
    updated_at: str
