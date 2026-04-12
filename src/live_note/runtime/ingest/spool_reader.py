from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from live_note.domain import AudioFrame

from .audio_spool import read_audio_frames


def iter_spool_frames(session_dir: Path) -> Iterator[AudioFrame]:
    yield from read_audio_frames(session_dir)
