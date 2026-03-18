from __future__ import annotations

import json
import logging
import shutil
import tomllib
from collections.abc import Iterable
from dataclasses import asdict
from pathlib import Path

from live_note.domain import SegmentEvent, SegmentState, SessionMetadata, TranscriptEntry
from live_note.utils import ensure_parent, iso_now


class SessionWorkspace:
    def __init__(self, root: Path):
        self.root = root
        self.segments_dir = root / "segments"
        self.refined_dir = root / "refined"
        self.session_toml = root / "session.toml"
        self.segments_jsonl = root / "segments.jsonl"
        self.segments_live_jsonl = root / "segments.live.jsonl"
        self.refined_segments_tmp_jsonl = root / "segments.refined.jsonl.tmp"
        self.transcript_md = root / "transcript.md"
        self.structured_md = root / "structured.md"
        self.logs_txt = root / "logs.txt"
        self.session_live_wav = root / "session.live.wav"

    @classmethod
    def create(cls, root: Path, metadata: SessionMetadata) -> SessionWorkspace:
        workspace = cls(root)
        workspace.root.mkdir(parents=True, exist_ok=True)
        workspace.segments_dir.mkdir(parents=True, exist_ok=True)
        workspace.write_session(metadata)
        return workspace

    @classmethod
    def load(cls, root: Path) -> SessionWorkspace:
        if not root.exists():
            raise FileNotFoundError(f"会话目录不存在: {root}")
        return cls(root)

    def write_session(self, metadata: SessionMetadata) -> None:
        self.session_toml.write_text(_render_toml(asdict(metadata)), encoding="utf-8")

    def read_session(self) -> SessionMetadata:
        with self.session_toml.open("rb") as handle:
            data = tomllib.load(handle)
        return SessionMetadata(**data)

    def update_status(self, status: str) -> SessionMetadata:
        return self.update_session(status=status)

    def update_session(self, **changes: str) -> SessionMetadata:
        metadata = self.read_session()
        updated = SessionMetadata(**(asdict(metadata) | changes))
        self.write_session(updated)
        return updated

    def append_event(self, event: SegmentEvent, journal_path: Path | None = None) -> None:
        target = journal_path or self.segments_jsonl
        ensure_parent(target)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(event), ensure_ascii=False))
            handle.write("\n")

    def load_events(self, journal_path: Path | None = None) -> list[SegmentEvent]:
        target = journal_path or self.segments_jsonl
        if not target.exists():
            return []
        events: list[SegmentEvent] = []
        for line in target.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            events.append(SegmentEvent(**json.loads(line)))
        return events

    def rebuild_segment_states(self, journal_path: Path | None = None) -> list[SegmentState]:
        states: dict[str, SegmentState] = {}
        for event in self.load_events(journal_path=journal_path):
            current = states.get(
                event.segment_id,
                SegmentState(
                    segment_id=event.segment_id,
                    started_ms=event.started_ms,
                    ended_ms=event.ended_ms,
                    wav_path=None,
                    text=None,
                    error=None,
                ),
            )
            wav_path = current.wav_path
            if event.wav_path:
                wav_path = self.root / event.wav_path
            text = current.text
            error = current.error
            if event.kind == "segment_transcribed":
                text = event.text
                error = None
                if event.speaker_label is not None:
                    current = SegmentState(
                        segment_id=current.segment_id,
                        started_ms=current.started_ms,
                        ended_ms=current.ended_ms,
                        wav_path=current.wav_path,
                        text=current.text,
                        error=current.error,
                        speaker_label=event.speaker_label,
                    )
            elif event.kind == "segment_failed":
                error = event.error
            else:
                if event.text is not None:
                    text = event.text
                if event.error is not None:
                    error = event.error
            states[event.segment_id] = SegmentState(
                segment_id=event.segment_id,
                started_ms=event.started_ms,
                ended_ms=event.ended_ms,
                wav_path=wav_path,
                text=text,
                error=error,
                speaker_label=(
                    event.speaker_label
                    if event.speaker_label is not None
                    else current.speaker_label
                ),
            )
        return sorted(states.values(), key=lambda item: (item.started_ms, item.segment_id))

    def transcript_entries(self, journal_path: Path | None = None) -> list[TranscriptEntry]:
        entries = [
            TranscriptEntry(
                segment_id=state.segment_id,
                started_ms=state.started_ms,
                ended_ms=state.ended_ms,
                text=state.text,
                speaker_label=state.speaker_label,
            )
            for state in self.rebuild_segment_states(journal_path=journal_path)
            if state.text
        ]
        return sorted(entries, key=lambda item: (item.started_ms, item.segment_id))

    def write_transcript(self, content: str) -> None:
        self.transcript_md.write_text(content, encoding="utf-8")

    def write_structured(self, content: str) -> None:
        self.structured_md.write_text(content, encoding="utf-8")

    def session_logger(self) -> logging.Logger:
        logger = logging.getLogger(f"live_note.session.{self.root.name}")
        logger.setLevel(logging.INFO)
        if logger.handlers:
            return logger
        handler = logging.FileHandler(self.logs_txt, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        logger.propagate = True
        return logger

    def next_wav_path(self, segment_id: str) -> Path:
        return self.segments_dir / f"{segment_id}.wav"

    def next_refined_wav_path(self, segment_id: str) -> Path:
        return self.refined_dir / f"{segment_id}.wav"

    def replace_canonical_journal(self, source_path: Path) -> None:
        if self.segments_jsonl.exists() and not self.segments_live_jsonl.exists():
            shutil.copy2(self.segments_jsonl, self.segments_live_jsonl)
        source_path.replace(self.segments_jsonl)

    def record_segment_created(
        self,
        segment_id: str,
        started_ms: int,
        ended_ms: int,
        wav_path: Path,
        journal_path: Path | None = None,
    ) -> None:
        relative = wav_path.relative_to(self.root)
        self.append_event(
            SegmentEvent(
                kind="segment_created",
                segment_id=segment_id,
                started_ms=started_ms,
                ended_ms=ended_ms,
                wav_path=str(relative),
                created_at=iso_now(),
            ),
            journal_path=journal_path,
        )

    def record_segment_text(
        self,
        segment_id: str,
        started_ms: int,
        ended_ms: int,
        text: str,
        speaker_label: str | None = None,
        journal_path: Path | None = None,
    ) -> None:
        self.append_event(
            SegmentEvent(
                kind="segment_transcribed",
                segment_id=segment_id,
                started_ms=started_ms,
                ended_ms=ended_ms,
                text=text,
                created_at=iso_now(),
                speaker_label=speaker_label,
            ),
            journal_path=journal_path,
        )

    def record_segment_error(
        self,
        segment_id: str,
        started_ms: int,
        ended_ms: int,
        error: str,
        journal_path: Path | None = None,
    ) -> None:
        self.append_event(
            SegmentEvent(
                kind="segment_failed",
                segment_id=segment_id,
                started_ms=started_ms,
                ended_ms=ended_ms,
                error=error,
                created_at=iso_now(),
            ),
            journal_path=journal_path,
        )


def _render_toml(values: dict[str, object]) -> str:
    rendered = []
    for key, value in values.items():
        if value is None:
            rendered.append(f"{key} = \"\"")
            continue
        if isinstance(value, bool):
            rendered.append(f"{key} = {'true' if value else 'false'}")
            continue
        if isinstance(value, int | float):
            rendered.append(f"{key} = {value}")
            continue
        escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
        rendered.append(f'{key} = "{escaped}"')
    return "\n".join(rendered) + "\n"


def build_workspace(config_root: Path, session_id: str) -> SessionWorkspace:
    return SessionWorkspace.load(config_root / ".live-note" / "sessions" / session_id)


def session_root(config_root: Path) -> Path:
    return config_root / ".live-note" / "sessions"


def list_sessions(config_root: Path) -> Iterable[Path]:
    root = session_root(config_root)
    if not root.exists():
        return []
    return sorted(path for path in root.iterdir() if path.is_dir())
