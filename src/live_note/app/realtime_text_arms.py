from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from live_note.app.realtime_text_experiment import Arm
from live_note.app.realtime_text_replay import ReplayCheckpointRecord, ReplayFinalTruth

A2_CHECKPOINT_INTERVAL_MS = 8_000
A2_FREEZE_STREAK = 2
A3_TRAILING_WINDOW_MS = 15_000
A3_MIN_NEW_AUDIO_MS = 8_000


class MiniRefineRecentWindowDecodeAdapter(Protocol):
    def decode_recent_window(
        self,
        *,
        fixture_id: str,
        source_records: list[ReplayCheckpointRecord],
        window_start_ts_ms: int,
        window_end_ts_ms: int,
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class _ReplayCheckpointMiniRefineDecodeAdapter:
    def decode_recent_window(
        self,
        *,
        fixture_id: str,
        source_records: list[ReplayCheckpointRecord],
        window_start_ts_ms: int,
        window_end_ts_ms: int,
    ) -> str:
        del fixture_id
        window_records = [
            record
            for record in source_records
            if window_start_ts_ms <= record.checkpoint_ts_ms <= window_end_ts_ms
        ]
        if not window_records:
            return ""
        return window_records[-1].checkpoint_text


@dataclass(frozen=True, slots=True)
class RealtimeTextArmRecord:
    fixture_id: str
    checkpoint_id: str
    checkpoint_ts_ms: int
    checkpoint_text: str
    arm: Arm
    metadata: dict[str, object]
    final_truth: ReplayFinalTruth


@dataclass(slots=True)
class _RollingChunkState:
    current_text: str
    stable_streak: int
    frozen: bool


def build_realtime_text_arm_records(
    records: list[ReplayCheckpointRecord],
    arm: Arm,
    *,
    mini_refine_decode_adapter: MiniRefineRecentWindowDecodeAdapter | None = None,
) -> list[RealtimeTextArmRecord]:
    if arm is Arm.A0_CURRENT_LIVE_TEXT_BASELINE:
        return _build_a0_current_live_text_baseline(records)
    if arm is Arm.A1_FINALIZED_SEGMENT_WINDOW:
        return _build_a1_finalized_segment_window(records)
    if arm is Arm.A2_STABILIZED_ROLLING_WINDOW:
        return _build_a2_stabilized_rolling_window(records)
    if arm is Arm.A3_MINI_REFINE_RECENT_WINDOW:
        return _build_a3_mini_refine_recent_window(
            records,
            decode_adapter=mini_refine_decode_adapter
            if mini_refine_decode_adapter is not None
            else _ReplayCheckpointMiniRefineDecodeAdapter(),
        )
    raise NotImplementedError(f"arm not implemented yet: {arm.value}")


def _build_a0_current_live_text_baseline(
    records: list[ReplayCheckpointRecord],
) -> list[RealtimeTextArmRecord]:
    arm_records: list[RealtimeTextArmRecord] = []
    for fixture_records in _group_records_by_fixture(records).values():
        live_draft_records = [
            record for record in fixture_records if record.checkpoint_source == "live_draft"
        ]
        if not live_draft_records:
            continue
        canonical_final_records = [
            record for record in fixture_records if record.checkpoint_source == "canonical_final"
        ]
        degenerate = _live_draft_collapses_to_canonical_final(
            live_draft_records,
            canonical_final_records,
        )
        for record in live_draft_records:
            arm_records.append(
                RealtimeTextArmRecord(
                    fixture_id=record.fixture_id,
                    checkpoint_id=f"{record.checkpoint_id}:{Arm.A0_CURRENT_LIVE_TEXT_BASELINE.name}",
                    checkpoint_ts_ms=record.checkpoint_ts_ms,
                    checkpoint_text=record.checkpoint_text,
                    arm=Arm.A0_CURRENT_LIVE_TEXT_BASELINE,
                    metadata={
                        "source": "live_draft",
                        "degenerate": degenerate,
                    },
                    final_truth=record.final_truth,
                )
            )
    return arm_records


def _build_a1_finalized_segment_window(
    records: list[ReplayCheckpointRecord],
) -> list[RealtimeTextArmRecord]:
    arm_records: list[RealtimeTextArmRecord] = []
    for fixture_records in _group_records_by_fixture(records).values():
        canonical_final_records = [
            record for record in fixture_records if record.checkpoint_source == "canonical_final"
        ]
        if not canonical_final_records:
            continue

        window_segments: list[str] = []
        window_started_ts_ms: int | None = None
        previous_checkpoint_text = ""
        for record in canonical_final_records:
            finalized_segment_text = _extract_finalized_segment_text(
                previous_checkpoint_text,
                record.checkpoint_text,
            )
            previous_checkpoint_text = record.checkpoint_text
            if not finalized_segment_text:
                continue
            if window_started_ts_ms is None:
                window_started_ts_ms = record.checkpoint_ts_ms
            window_segments.append(finalized_segment_text)
            window_duration_ms = record.checkpoint_ts_ms - window_started_ts_ms
            if len(window_segments) < 3 and window_duration_ms < 20_000:
                continue
            arm_records.append(
                RealtimeTextArmRecord(
                    fixture_id=record.fixture_id,
                    checkpoint_id=f"{record.checkpoint_id}:{Arm.A1_FINALIZED_SEGMENT_WINDOW.name}",
                    checkpoint_ts_ms=record.checkpoint_ts_ms,
                    checkpoint_text="\n".join(window_segments),
                    arm=Arm.A1_FINALIZED_SEGMENT_WINDOW,
                    metadata={
                        "source": "canonical_final",
                        "segment_count": len(window_segments),
                        "window_duration_ms": window_duration_ms,
                    },
                    final_truth=record.final_truth,
                )
            )
            window_segments = []
            window_started_ts_ms = None
    return arm_records


def _build_a2_stabilized_rolling_window(
    records: list[ReplayCheckpointRecord],
) -> list[RealtimeTextArmRecord]:
    arm_records: list[RealtimeTextArmRecord] = []
    for fixture_records in _group_records_by_fixture(records).values():
        source_records = _select_a2_source_records(fixture_records)
        if not source_records:
            continue

        source_records = sorted(
            source_records,
            key=lambda item: (item.checkpoint_ts_ms, item.checkpoint_id),
        )
        chunk_states: list[_RollingChunkState] = []
        churn_count = 0
        emitted_index = 0
        record_index = 0
        next_emit_ts_ms = source_records[0].checkpoint_ts_ms + A2_CHECKPOINT_INTERVAL_MS
        final_checkpoint_ts_ms = source_records[-1].checkpoint_ts_ms

        while next_emit_ts_ms <= final_checkpoint_ts_ms:
            while (
                record_index + 1 < len(source_records)
                and source_records[record_index + 1].checkpoint_ts_ms <= next_emit_ts_ms
            ):
                record_index += 1
            checkpoint_record = source_records[record_index]

            churn_count += _update_rolling_chunk_states(
                chunk_states,
                _split_checkpoint_chunks(checkpoint_record.checkpoint_text),
            )

            emitted_index += 1
            arm_records.append(
                RealtimeTextArmRecord(
                    fixture_id=checkpoint_record.fixture_id,
                    checkpoint_id=(
                        f"{checkpoint_record.fixture_id}:a2:{emitted_index}:{next_emit_ts_ms}"
                    ),
                    checkpoint_ts_ms=next_emit_ts_ms,
                    checkpoint_text=_render_rolling_chunk_text(chunk_states),
                    arm=Arm.A2_STABILIZED_ROLLING_WINDOW,
                    metadata={
                        "source": source_records[0].checkpoint_source,
                        "checkpoint_interval_ms": A2_CHECKPOINT_INTERVAL_MS,
                        "freeze_streak": A2_FREEZE_STREAK,
                        "frozen_chunk_count": sum(1 for chunk in chunk_states if chunk.frozen),
                        "churn_count": churn_count,
                    },
                    final_truth=checkpoint_record.final_truth,
                )
            )

            next_emit_ts_ms += A2_CHECKPOINT_INTERVAL_MS

    return arm_records


def _build_a3_mini_refine_recent_window(
    records: list[ReplayCheckpointRecord],
    *,
    decode_adapter: MiniRefineRecentWindowDecodeAdapter,
) -> list[RealtimeTextArmRecord]:
    arm_records: list[RealtimeTextArmRecord] = []
    for fixture_records in _group_records_by_fixture(records).values():
        source_records = _select_a2_source_records(fixture_records)
        if not source_records:
            continue

        source_records = sorted(
            source_records,
            key=lambda item: (item.checkpoint_ts_ms, item.checkpoint_id),
        )
        emitted_index = 0
        first_checkpoint_ts_ms = source_records[0].checkpoint_ts_ms
        last_emitted_checkpoint_ts_ms = first_checkpoint_ts_ms

        for source_record in source_records[1:]:
            checkpoint_ts_ms = source_record.checkpoint_ts_ms
            if checkpoint_ts_ms - last_emitted_checkpoint_ts_ms < A3_MIN_NEW_AUDIO_MS:
                continue

            window_start_ts_ms = max(
                first_checkpoint_ts_ms, checkpoint_ts_ms - A3_TRAILING_WINDOW_MS
            )
            checkpoint_text = decode_adapter.decode_recent_window(
                fixture_id=source_record.fixture_id,
                source_records=source_records,
                window_start_ts_ms=window_start_ts_ms,
                window_end_ts_ms=checkpoint_ts_ms,
            ).strip()

            emitted_index += 1
            arm_records.append(
                RealtimeTextArmRecord(
                    fixture_id=source_record.fixture_id,
                    checkpoint_id=f"{source_record.fixture_id}:a3:{emitted_index}:{checkpoint_ts_ms}",
                    checkpoint_ts_ms=checkpoint_ts_ms,
                    checkpoint_text=checkpoint_text,
                    arm=Arm.A3_MINI_REFINE_RECENT_WINDOW,
                    metadata={
                        "source": source_records[0].checkpoint_source,
                        "trailing_window_ms": A3_TRAILING_WINDOW_MS,
                        "min_new_audio_ms": A3_MIN_NEW_AUDIO_MS,
                        "window_start_ts_ms": window_start_ts_ms,
                        "window_end_ts_ms": checkpoint_ts_ms,
                        "immutable_after_emit": True,
                    },
                    final_truth=source_record.final_truth,
                )
            )
            last_emitted_checkpoint_ts_ms = checkpoint_ts_ms
    return arm_records


def _group_records_by_fixture(
    records: list[ReplayCheckpointRecord],
) -> dict[str, list[ReplayCheckpointRecord]]:
    records_by_fixture: dict[str, list[ReplayCheckpointRecord]] = {}
    for record in records:
        records_by_fixture.setdefault(record.fixture_id, []).append(record)
    return records_by_fixture


def _select_a2_source_records(
    fixture_records: list[ReplayCheckpointRecord],
) -> list[ReplayCheckpointRecord]:
    live_draft_records = [
        record for record in fixture_records if record.checkpoint_source == "live_draft"
    ]
    if live_draft_records:
        return live_draft_records
    return [record for record in fixture_records if record.checkpoint_source == "canonical_final"]


def _split_checkpoint_chunks(checkpoint_text: str) -> list[str]:
    return [line.strip() for line in checkpoint_text.splitlines() if line.strip()]


def _update_rolling_chunk_states(
    chunk_states: list[_RollingChunkState],
    incoming_chunks: list[str],
) -> int:
    churn = 0
    total_chunks = max(len(chunk_states), len(incoming_chunks))
    for chunk_index in range(total_chunks):
        incoming_text = incoming_chunks[chunk_index] if chunk_index < len(incoming_chunks) else ""
        if chunk_index >= len(chunk_states):
            chunk_states.append(
                _RollingChunkState(
                    current_text=incoming_text,
                    stable_streak=1 if incoming_text else 0,
                    frozen=False,
                )
            )
            continue

        state = chunk_states[chunk_index]
        if state.frozen:
            continue

        if incoming_text == state.current_text:
            if incoming_text:
                state.stable_streak += 1
        else:
            if incoming_text or state.current_text:
                churn += 1
            state.current_text = incoming_text
            state.stable_streak = 1 if incoming_text else 0

        if state.current_text and state.stable_streak >= A2_FREEZE_STREAK:
            state.frozen = True

    return churn


def _render_rolling_chunk_text(chunk_states: list[_RollingChunkState]) -> str:
    return "\n".join(chunk.current_text for chunk in chunk_states if chunk.current_text)


def _extract_finalized_segment_text(previous_checkpoint_text: str, checkpoint_text: str) -> str:
    previous_lines = previous_checkpoint_text.splitlines()
    current_lines = checkpoint_text.splitlines()
    if (
        len(current_lines) >= len(previous_lines)
        and current_lines[: len(previous_lines)] == previous_lines
    ):
        return "\n".join(current_lines[len(previous_lines) :]).strip()
    return checkpoint_text.strip()


def _live_draft_collapses_to_canonical_final(
    live_draft_records: list[ReplayCheckpointRecord],
    canonical_final_records: list[ReplayCheckpointRecord],
) -> bool:
    if not live_draft_records:
        return False
    if live_draft_records[0].final_truth.execution_target != "local":
        return False
    if len(live_draft_records) != len(canonical_final_records):
        return False
    return all(
        live_record.checkpoint_ts_ms == final_record.checkpoint_ts_ms
        and live_record.checkpoint_text == final_record.checkpoint_text
        for live_record, final_record in zip(
            live_draft_records, canonical_final_records, strict=True
        )
    )
