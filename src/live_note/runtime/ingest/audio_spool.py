from __future__ import annotations

import struct
from collections.abc import Iterator
from pathlib import Path

from live_note.domain import AudioFrame
from live_note.utils import ensure_parent

_HEADER = struct.Struct("<qqI")


def audio_spool_path(session_dir: Path) -> Path:
    return session_dir / "live.ingest.pcm"


def append_audio_frame(session_dir: Path, frame: AudioFrame) -> None:
    path = audio_spool_path(session_dir)
    ensure_parent(path)
    with path.open("ab") as handle:
        handle.write(_HEADER.pack(frame.started_ms, frame.ended_ms, len(frame.pcm16)))
        handle.write(frame.pcm16)


def read_audio_frames(session_dir: Path) -> Iterator[AudioFrame]:
    path = audio_spool_path(session_dir)
    if not path.exists():
        return
    with path.open("rb") as handle:
        while True:
            header = handle.read(_HEADER.size)
            if not header:
                return
            started_ms, ended_ms, size = _HEADER.unpack(header)
            pcm16 = handle.read(size)
            if len(pcm16) != size:
                raise RuntimeError(f"ingest spool truncated: {path}")
            yield AudioFrame(
                started_ms=started_ms,
                ended_ms=ended_ms,
                pcm16=pcm16,
            )
