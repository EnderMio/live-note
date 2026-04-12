from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from live_note.session_workspace import SessionWorkspace
from live_note.domain import SegmentEvent, SegmentState, TranscriptEntry


@dataclass(frozen=True, slots=True)
class ReplayFinalTruth:
    fixture_id: str
    transcript_text: str
    transcript_status: str
    structured_status: str
    transcript_source: str
    refine_status: str
    execution_target: str


@dataclass(frozen=True, slots=True)
class ReplayCheckpointRecord:
    fixture_id: str
    checkpoint_id: str
    checkpoint_ts_ms: int
    checkpoint_source: str
    checkpoint_text: str
    final_truth: ReplayFinalTruth


@dataclass(frozen=True, slots=True)
class ReplayFixtureManifest:
    transcript_source: str
    refine_status: str
    execution_target: str


def load_replay_checkpoints(fixtures_root: Path) -> list[ReplayCheckpointRecord]:
    records: list[ReplayCheckpointRecord] = []
    for fixture_dir in sorted(path for path in fixtures_root.iterdir() if path.is_dir()):
        workspace = SessionWorkspace.load(fixture_dir)
        manifest = _load_fixture_manifest(fixture_dir)
        final_truth = _build_final_truth(fixture_dir.name, workspace, manifest)
        records.extend(_load_fixture_records(fixture_dir.name, workspace, final_truth))
    return records


def _build_final_truth(
    fixture_id: str,
    workspace: SessionWorkspace,
    manifest: ReplayFixtureManifest,
) -> ReplayFinalTruth:
    transcript_status = _read_note_field(workspace.transcript_md, "status") or "unknown"
    structured_status = _read_note_field(workspace.structured_md, "status") or "unknown"
    transcript_text = _join_entries(workspace.transcript_entries())
    return ReplayFinalTruth(
        fixture_id=fixture_id,
        transcript_text=transcript_text,
        transcript_status=transcript_status,
        structured_status=structured_status,
        transcript_source=(
            _read_note_field(workspace.transcript_md, "transcript_source")
            or manifest.transcript_source
        ),
        refine_status=(
            _read_note_field(workspace.transcript_md, "refine_status") or manifest.refine_status
        ),
        execution_target=manifest.execution_target,
    )


def _load_fixture_records(
    fixture_id: str,
    workspace: SessionWorkspace,
    final_truth: ReplayFinalTruth,
) -> list[ReplayCheckpointRecord]:
    records: list[ReplayCheckpointRecord] = []
    if workspace.segments_live_jsonl.exists():
        records.extend(
            _build_records_from_events(
                fixture_id=fixture_id,
                events=workspace.load_events(journal_path=workspace.segments_live_jsonl),
                checkpoint_source="live_draft",
                final_truth=final_truth,
            )
        )
    canonical_source = "live_draft" if final_truth.transcript_source == "live" else "canonical_final"
    records.extend(
        _build_records_from_events(
            fixture_id=fixture_id,
            events=workspace.load_events(),
            checkpoint_source=canonical_source,
            final_truth=final_truth,
        )
    )
    return records


def _build_records_from_events(
    *,
    fixture_id: str,
    events: list[SegmentEvent],
    checkpoint_source: str,
    final_truth: ReplayFinalTruth,
) -> list[ReplayCheckpointRecord]:
    prefix: list[SegmentEvent] = []
    records: list[ReplayCheckpointRecord] = []
    checkpoint_index = 0
    for event in events:
        prefix.append(event)
        if event.kind != "segment_transcribed":
            continue
        checkpoint_index += 1
        checkpoint_text = _join_entries(_entries_from_events(prefix))
        records.append(
            ReplayCheckpointRecord(
                fixture_id=fixture_id,
                checkpoint_id=f"{fixture_id}:{checkpoint_source}:{checkpoint_index}",
                checkpoint_ts_ms=event.ended_ms,
                checkpoint_source=checkpoint_source,
                checkpoint_text=checkpoint_text,
                final_truth=final_truth,
            )
        )
    return records


def _entries_from_events(events: list[SegmentEvent]) -> list[TranscriptEntry]:
    states: dict[str, SegmentState] = {}
    for event in events:
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
        text = current.text
        error = current.error
        speaker_label = current.speaker_label
        if event.kind == "segment_transcribed":
            text = event.text
            error = None
            if event.speaker_label is not None:
                speaker_label = event.speaker_label
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
            wav_path=None,
            text=text,
            error=error,
            speaker_label=speaker_label,
        )
    entries = [
        TranscriptEntry(
            segment_id=state.segment_id,
            started_ms=state.started_ms,
            ended_ms=state.ended_ms,
            text=state.text,
            speaker_label=state.speaker_label,
        )
        for state in states.values()
        if state.text
    ]
    return sorted(entries, key=lambda item: (item.started_ms, item.segment_id))


def _join_entries(entries: list[TranscriptEntry]) -> str:
    return "\n".join(entry.text.strip() for entry in entries if entry.text.strip())


def _load_fixture_manifest(fixture_dir: Path) -> ReplayFixtureManifest:
    transcript_source = _read_note_field(fixture_dir / "transcript.md", "transcript_source")
    refine_status = _read_note_field(fixture_dir / "transcript.md", "refine_status")
    execution_target = "local"
    session_toml = fixture_dir / "session.toml"
    if session_toml.exists():
        with session_toml.open("rb") as handle:
            data = tomllib.load(handle)
        transcript_source = _fixture_string(data.get("transcript_source")) or transcript_source
        refine_status = _fixture_string(data.get("refine_status")) or refine_status
        execution_target = _fixture_string(data.get("execution_target")) or execution_target
    return ReplayFixtureManifest(
        transcript_source=transcript_source or "live",
        refine_status=refine_status or "disabled",
        execution_target=execution_target,
    )


def _read_note_field(note_path: Path, key: str) -> str | None:
    in_frontmatter = False
    for line in note_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped == "---":
            if in_frontmatter:
                break
            in_frontmatter = True
            continue
        if not in_frontmatter or not stripped or ":" not in stripped:
            continue
        field, value = stripped.split(":", 1)
        if field.strip() == key:
            return value.strip().strip('"')
    return None


def _fixture_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None
