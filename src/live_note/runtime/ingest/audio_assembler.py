from __future__ import annotations

import wave
from pathlib import Path

from live_note.utils import ensure_parent

from .spool_reader import iter_spool_frames


def write_spool_to_wav(
    *,
    session_dir: Path,
    output_path: Path,
    sample_rate: int,
) -> int:
    ensure_parent(output_path)
    frame_count = 0
    with wave.open(str(output_path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        for frame in iter_spool_frames(session_dir):
            handle.writeframes(frame.pcm16)
            frame_count += 1
    return frame_count
