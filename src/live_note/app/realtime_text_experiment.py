from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from live_note.utils import compact_text

if TYPE_CHECKING:
    from live_note.app.realtime_text_arms import RealtimeTextArmRecord
    from live_note.app.realtime_text_replay import ReplayCheckpointRecord

if __name__ == "__main__":
    sys.modules.setdefault("live_note.app.realtime_text_experiment", sys.modules[__name__])

MATCH_LAG_BOUND_MS = 8_000
_NORMALIZED_TEXT_PATTERN = re.compile(r"[，。！？!?,.、：:；;“”\"'`·\s]+")
_METRIC_FAILURE_LATENCY_MS = 12_001
_ASSISTANCE_ITEM_TYPE_ORDER = (
    "topic",
    "decision",
    "action_item",
    "open_question",
)


class Arm(Enum):
    A0_CURRENT_LIVE_TEXT_BASELINE = "A0 current_live_text_baseline"
    A1_FINALIZED_SEGMENT_WINDOW = "A1 finalized_segment_window"
    A2_STABILIZED_ROLLING_WINDOW = "A2 stabilized_rolling_window"
    A3_MINI_REFINE_RECENT_WINDOW = "A3 mini_refine_recent_window"
    A4_FUNASR_PHASE2 = "A4 funasr_phase2"


PHASE1_ARMS = (
    Arm.A0_CURRENT_LIVE_TEXT_BASELINE,
    Arm.A1_FINALIZED_SEGMENT_WINDOW,
    Arm.A2_STABILIZED_ROLLING_WINDOW,
    Arm.A3_MINI_REFINE_RECENT_WINDOW,
)


class AssistanceItemType(Enum):
    TOPIC = "topic"
    DECISION = "decision"
    ACTION_ITEM = "action_item"
    OPEN_QUESTION = "open_question"


@dataclass(frozen=True)
class AssistanceItem:
    item_type: AssistanceItemType
    text: str
    emitted_ts_ms: int = 0
    retracted_ts_ms: int | None = None


@dataclass(frozen=True)
class GoldLabel:
    item_type: AssistanceItemType
    text: str
    first_evidence_ts_ms: int


@dataclass(frozen=True)
class ExperimentMetrics:
    lba_f1: float
    precision: float
    recall: float
    p50_latency_ms: int
    p95_latency_ms: int
    unsupported_rate_per_minute: float
    retraction_rate: float
    coverage: float
    wer: float
    cer: float


@dataclass(frozen=True)
class RealtimeTextMetrics:
    true_positives: int
    false_positives: int
    false_negatives: int
    lba_f1: float
    precision: float
    recall: float
    p50_latency_ms: int | None
    p95_latency_ms: int | None
    unsupported_item_rate_per_minute: float
    retraction_rate: float
    usable_checkpoint_coverage: float
    wer: float | None
    cer: float | None


@dataclass(frozen=True)
class ArmEvaluation:
    arm: Arm
    status: str
    arm_verdict: str
    metrics: ExperimentMetrics | None
    replay_checkpoint_count: int
    usable_checkpoint_count: int
    fixture_count: int
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class RealtimeTextExperimentReport:
    fixtures_root: str
    canonical_verdict: Verdict
    arms: Mapping[Arm, ArmEvaluation]


class Verdict(Enum):
    REJECT_ALL_ARMS = "REJECT_ALL_ARMS"
    PROMOTE_BEST_ARM_TO_SHADOW = "PROMOTE_BEST_ARM_TO_SHADOW"
    READY_FOR_REALTIME_ASSISTANCE_PILOT = "READY_FOR_REALTIME_ASSISTANCE_PILOT"


LBA_F1_THRESHOLD = 0.80
PRECISION_THRESHOLD = 0.85
P95_LATENCY_THRESHOLD_MS = 12_000
UNSUPPORTED_RATE_THRESHOLD_PER_MINUTE = 0.10
RETRACTION_RATE_THRESHOLD = 0.10
COVERAGE_THRESHOLD = 0.70
PILOT_LBA_F1_MARGIN = 0.02
PILOT_P95_LATENCY_IMPROVEMENT = 0.30


def decide_experiment_verdict(metrics_by_arm: Mapping[Arm, ExperimentMetrics]) -> Verdict:
    passing_arms = {
        arm: metrics_by_arm[arm]
        for arm in PHASE1_ARMS
        if arm in metrics_by_arm and _passes_absolute_thresholds(metrics_by_arm[arm])
    }
    if not passing_arms:
        return Verdict.REJECT_ALL_ARMS

    a1_metrics = metrics_by_arm.get(Arm.A1_FINALIZED_SEGMENT_WINDOW)
    if a1_metrics is None:
        return Verdict.PROMOTE_BEST_ARM_TO_SHADOW

    qualified_for_pilot = any(
        arm is not Arm.A1_FINALIZED_SEGMENT_WINDOW
        and (
            _is_pareto_better_than_a1(metrics, a1_metrics)
            or _matches_a1_with_latency_gain(metrics, a1_metrics)
        )
        for arm, metrics in passing_arms.items()
    )
    if qualified_for_pilot:
        return Verdict.READY_FOR_REALTIME_ASSISTANCE_PILOT

    return Verdict.PROMOTE_BEST_ARM_TO_SHADOW


def generate_realtime_text_report(
    *,
    fixtures_root: Path,
    output_prefix: Path,
) -> RealtimeTextExperimentReport:
    from live_note.app.realtime_text_replay import load_replay_checkpoints

    replay_records = load_replay_checkpoints(fixtures_root)
    phase1_evaluations = {
        arm: _evaluate_arm(replay_records=replay_records, arm=arm) for arm in PHASE1_ARMS
    }
    canonical_verdict = decide_experiment_verdict(
        {
            arm: evaluation.metrics
            for arm, evaluation in phase1_evaluations.items()
            if evaluation.metrics is not None
        }
    )
    arms: dict[Arm, ArmEvaluation] = dict(phase1_evaluations)
    arms[Arm.A4_FUNASR_PHASE2] = ArmEvaluation(
        arm=Arm.A4_FUNASR_PHASE2,
        status="deferred_phase2",
        arm_verdict="not_run",
        metrics=None,
        replay_checkpoint_count=0,
        usable_checkpoint_count=0,
        fixture_count=0,
        notes=(
            "Deferred to phase-2; report reserves visibility without counting this arm as failed.",
        ),
    )
    report = RealtimeTextExperimentReport(
        fixtures_root=str(fixtures_root),
        canonical_verdict=canonical_verdict,
        arms=arms,
    )
    _write_json_report(report=report, output_path=output_prefix.with_suffix(".json"))
    _write_markdown_report(report=report, output_path=output_prefix.with_suffix(".md"))
    return report


def compute_realtime_text_metrics(
    *,
    gold_labels: list[GoldLabel],
    arm_items: list[AssistanceItem],
    checkpoint_count: int,
    usable_checkpoint_count: int,
    session_duration_ms: int,
    finalized_reference_text: str | None = None,
    arm_reference_text: str | None = None,
) -> RealtimeTextMetrics:
    matched_gold_indexes: set[int] = set()
    true_positives = 0
    false_positives = 0
    latencies_ms: list[int] = []

    for arm_item in sorted(
        arm_items, key=lambda item: (item.emitted_ts_ms, item.item_type.value, item.text)
    ):
        matched_index = _match_gold_label(
            arm_item=arm_item,
            gold_labels=gold_labels,
            matched_gold_indexes=matched_gold_indexes,
        )
        if matched_index is None:
            false_positives += 1
            continue
        matched_gold_indexes.add(matched_index)
        true_positives += 1
        latencies_ms.append(
            arm_item.emitted_ts_ms - gold_labels[matched_index].first_evidence_ts_ms
        )

    false_negatives = len(gold_labels) - len(matched_gold_indexes)
    precision = _safe_divide(true_positives, true_positives + false_positives)
    recall = _safe_divide(true_positives, true_positives + false_negatives)
    lba_f1 = _safe_divide(2 * precision * recall, precision + recall)

    return RealtimeTextMetrics(
        true_positives=true_positives,
        false_positives=false_positives,
        false_negatives=false_negatives,
        lba_f1=lba_f1,
        precision=precision,
        recall=recall,
        p50_latency_ms=_median_latency(latencies_ms),
        p95_latency_ms=_percentile_latency(latencies_ms, 0.95),
        unsupported_item_rate_per_minute=_safe_divide(
            false_positives * 60_000, session_duration_ms
        ),
        retraction_rate=_safe_divide(
            sum(1 for item in arm_items if item.retracted_ts_ms is not None),
            len(arm_items),
        ),
        usable_checkpoint_coverage=_safe_divide(usable_checkpoint_count, checkpoint_count),
        wer=_word_error_rate(finalized_reference_text, arm_reference_text),
        cer=_character_error_rate(finalized_reference_text, arm_reference_text),
    )


def _evaluate_arm(
    *,
    replay_records: list[ReplayCheckpointRecord],
    arm: Arm,
) -> ArmEvaluation:
    from live_note.app.realtime_text_arms import build_realtime_text_arm_records

    arm_records = build_realtime_text_arm_records(replay_records, arm)
    fixture_ids = sorted({record.fixture_id for record in replay_records})
    shifted_gold_labels: list[GoldLabel] = []
    shifted_arm_items: list[AssistanceItem] = []
    finalized_reference_parts: list[str] = []
    arm_reference_parts: list[str] = []
    replay_checkpoint_count = 0
    usable_checkpoint_count = 0
    session_duration_ms_total = 0
    offset_ms = 0

    replay_by_fixture = _group_replay_records_by_fixture(replay_records)
    arm_by_fixture = _group_arm_records_by_fixture(arm_records)
    for fixture_id in fixture_ids:
        fixture_replay_records = replay_by_fixture.get(fixture_id, [])
        fixture_arm_records = arm_by_fixture.get(fixture_id, [])
        source_records = _select_source_records_for_arm(fixture_replay_records, arm)
        gold_labels = _build_gold_labels(fixture_replay_records)
        arm_items = _build_arm_items(
            arm=arm,
            arm_records=fixture_arm_records,
            gold_labels=gold_labels,
        )
        session_duration_ms = _fixture_session_duration_ms(source_records)
        replay_checkpoint_count += len(source_records)
        usable_checkpoint_count += sum(
            1 for record in fixture_arm_records if record.checkpoint_text.strip()
        )
        session_duration_ms_total += session_duration_ms
        finalized_reference_parts.append(_fixture_finalized_reference_text(fixture_replay_records))
        arm_reference_parts.append(_fixture_arm_reference_text(fixture_arm_records))

        shifted_gold_labels.extend(
            GoldLabel(
                item_type=label.item_type,
                text=label.text,
                first_evidence_ts_ms=label.first_evidence_ts_ms + offset_ms,
            )
            for label in gold_labels
        )
        shifted_arm_items.extend(
            AssistanceItem(
                item_type=item.item_type,
                text=item.text,
                emitted_ts_ms=item.emitted_ts_ms + offset_ms,
                retracted_ts_ms=(
                    item.retracted_ts_ms + offset_ms if item.retracted_ts_ms is not None else None
                ),
            )
            for item in arm_items
        )
        offset_ms += session_duration_ms + MATCH_LAG_BOUND_MS + 1_000

    realtime_metrics = compute_realtime_text_metrics(
        gold_labels=shifted_gold_labels,
        arm_items=shifted_arm_items,
        checkpoint_count=replay_checkpoint_count,
        usable_checkpoint_count=usable_checkpoint_count,
        session_duration_ms=max(session_duration_ms_total, 1),
        finalized_reference_text="\n".join(part for part in finalized_reference_parts if part),
        arm_reference_text="\n".join(part for part in arm_reference_parts if part),
    )
    metrics = _to_experiment_metrics(realtime_metrics)
    return ArmEvaluation(
        arm=arm,
        status="evaluated",
        arm_verdict=_determine_arm_verdict(arm=arm, metrics=metrics),
        metrics=metrics,
        replay_checkpoint_count=replay_checkpoint_count,
        usable_checkpoint_count=usable_checkpoint_count,
        fixture_count=len(fixture_ids),
        notes=(),
    )


def _select_source_records_for_arm(
    replay_records: list[ReplayCheckpointRecord],
    arm: Arm,
) -> list[ReplayCheckpointRecord]:
    if arm is Arm.A0_CURRENT_LIVE_TEXT_BASELINE:
        return [record for record in replay_records if record.checkpoint_source == "live_draft"]
    if arm is Arm.A1_FINALIZED_SEGMENT_WINDOW:
        return [
            record for record in replay_records if record.checkpoint_source == "canonical_final"
        ]
    live_draft_records = [
        record for record in replay_records if record.checkpoint_source == "live_draft"
    ]
    if live_draft_records:
        return live_draft_records
    return [record for record in replay_records if record.checkpoint_source == "canonical_final"]


def _build_gold_labels(replay_records: list[ReplayCheckpointRecord]) -> list[GoldLabel]:
    gold_label_source_records = _select_gold_label_source_records(replay_records)
    if not gold_label_source_records:
        return []
    transcript_text = gold_label_source_records[-1].final_truth.transcript_text
    final_lines = _split_checkpoint_lines(transcript_text)
    normalized_occurrences: dict[str, int] = {}
    gold_labels: list[GoldLabel] = []
    for line in final_lines:
        normalized_line = _normalize_item_text(line)
        occurrence = normalized_occurrences.get(normalized_line, 0) + 1
        normalized_occurrences[normalized_line] = occurrence
        gold_labels.append(
            GoldLabel(
                item_type=_infer_assistance_item_type(line),
                text=line,
                first_evidence_ts_ms=_first_evidence_ts_ms(
                    source_records=gold_label_source_records,
                    target_line=line,
                    occurrence=occurrence,
                ),
            )
        )
    return gold_labels


def _build_arm_items(
    *,
    arm: Arm,
    arm_records: list[RealtimeTextArmRecord],
    gold_labels: list[GoldLabel],
) -> list[AssistanceItem]:
    sorted_records = sorted(
        arm_records, key=lambda item: (item.checkpoint_ts_ms, item.checkpoint_id)
    )
    item_type_by_text = {
        _normalize_item_text(label.text): label.item_type
        for label in gold_labels
        if label.text.strip()
    }
    if arm is Arm.A1_FINALIZED_SEGMENT_WINDOW:
        return _build_append_only_arm_items(
            arm_records=sorted_records,
            item_type_by_text=item_type_by_text,
        )

    return _build_snapshot_arm_items(
        arm_records=sorted_records,
        item_type_by_text=item_type_by_text,
    )


def _build_append_only_arm_items(
    *,
    arm_records: list[RealtimeTextArmRecord],
    item_type_by_text: Mapping[str, AssistanceItemType],
) -> list[AssistanceItem]:
    arm_items: list[AssistanceItem] = []
    for record in arm_records:
        for line in _split_checkpoint_lines(record.checkpoint_text):
            normalized_line = _normalize_item_text(line)
            arm_items.append(
                AssistanceItem(
                    item_type=item_type_by_text.get(
                        normalized_line,
                        _infer_assistance_item_type(line),
                    ),
                    text=line,
                    emitted_ts_ms=record.checkpoint_ts_ms,
                )
            )
    return arm_items


def _build_snapshot_arm_items(
    *,
    arm_records: list[RealtimeTextArmRecord],
    item_type_by_text: Mapping[str, AssistanceItemType],
) -> list[AssistanceItem]:
    arm_items: list[AssistanceItem] = []
    active_item_indexes_by_text: dict[str, list[int]] = {}
    previous_snapshot_counts: dict[str, int] = {}
    previous_snapshot_lines: dict[str, list[str]] = {}
    for record in arm_records:
        current_snapshot_lines: dict[str, list[str]] = {}
        for line in _split_checkpoint_lines(record.checkpoint_text):
            normalized_line = _normalize_item_text(line)
            current_snapshot_lines.setdefault(normalized_line, []).append(line)

        current_snapshot_counts = {
            normalized_line: len(lines) for normalized_line, lines in current_snapshot_lines.items()
        }

        for normalized_line, previous_count in previous_snapshot_counts.items():
            current_count = current_snapshot_counts.get(normalized_line, 0)
            if current_count >= previous_count:
                continue
            active_indexes = active_item_indexes_by_text.get(normalized_line, [])
            for active_index in active_indexes[current_count:previous_count]:
                active_item = arm_items[active_index]
                arm_items[active_index] = AssistanceItem(
                    item_type=active_item.item_type,
                    text=active_item.text,
                    emitted_ts_ms=active_item.emitted_ts_ms,
                    retracted_ts_ms=record.checkpoint_ts_ms,
                )
            active_item_indexes_by_text[normalized_line] = active_indexes[:current_count]

        for normalized_line, current_lines in current_snapshot_lines.items():
            previous_count = previous_snapshot_counts.get(normalized_line, 0)
            current_count = len(current_lines)
            if current_count <= previous_count:
                continue
            active_indexes = active_item_indexes_by_text.setdefault(normalized_line, [])
            for line in current_lines[previous_count:]:
                arm_items.append(
                    AssistanceItem(
                        item_type=item_type_by_text.get(
                            normalized_line,
                            _infer_assistance_item_type(line),
                        ),
                        text=line,
                        emitted_ts_ms=record.checkpoint_ts_ms,
                    )
                )
                active_indexes.append(len(arm_items) - 1)

        previous_snapshot_counts = current_snapshot_counts
        previous_snapshot_lines = current_snapshot_lines

    del previous_snapshot_lines
    return arm_items


def _first_evidence_ts_ms(
    *,
    source_records: Sequence[ReplayCheckpointRecord],
    target_line: str,
    occurrence: int,
) -> int:
    target_normalized = _normalize_item_text(target_line)
    for record in source_records:
        matching_lines = [
            line
            for line in _split_checkpoint_lines(record.checkpoint_text)
            if _normalize_item_text(line) == target_normalized
        ]
        if len(matching_lines) >= occurrence:
            return record.checkpoint_ts_ms
    return source_records[-1].checkpoint_ts_ms


def _select_gold_label_source_records(
    replay_records: Sequence[ReplayCheckpointRecord],
) -> list[ReplayCheckpointRecord]:
    canonical_final_records = sorted(
        [record for record in replay_records if record.checkpoint_source == "canonical_final"],
        key=lambda item: (item.checkpoint_ts_ms, item.checkpoint_id),
    )
    if canonical_final_records:
        return canonical_final_records
    return sorted(
        replay_records,
        key=lambda item: (item.checkpoint_ts_ms, item.checkpoint_id),
    )


def _infer_assistance_item_type(text: str) -> AssistanceItemType:
    normalized_text = _normalize_item_text(text)
    if not normalized_text:
        return AssistanceItemType.TOPIC
    stable_index = sum(ord(character) for character in normalized_text) % len(
        _ASSISTANCE_ITEM_TYPE_ORDER
    )
    item_type_name = _ASSISTANCE_ITEM_TYPE_ORDER[stable_index]
    return AssistanceItemType(item_type_name)


def _group_replay_records_by_fixture(
    replay_records: Sequence[ReplayCheckpointRecord],
) -> dict[str, list[ReplayCheckpointRecord]]:
    grouped: dict[str, list[ReplayCheckpointRecord]] = {}
    for record in replay_records:
        grouped.setdefault(record.fixture_id, []).append(record)
    return grouped


def _group_arm_records_by_fixture(
    arm_records: Sequence[RealtimeTextArmRecord],
) -> dict[str, list[RealtimeTextArmRecord]]:
    grouped: dict[str, list[RealtimeTextArmRecord]] = {}
    for record in arm_records:
        grouped.setdefault(record.fixture_id, []).append(record)
    return grouped


def _fixture_session_duration_ms(source_records: Sequence[ReplayCheckpointRecord]) -> int:
    if not source_records:
        return 1
    return max(record.checkpoint_ts_ms for record in source_records)


def _fixture_finalized_reference_text(replay_records: Sequence[ReplayCheckpointRecord]) -> str:
    canonical_final_records = [
        record for record in replay_records if record.checkpoint_source == "canonical_final"
    ]
    if not canonical_final_records:
        return ""
    return canonical_final_records[-1].final_truth.transcript_text


def _fixture_arm_reference_text(arm_records: Sequence[RealtimeTextArmRecord]) -> str:
    if not arm_records:
        return ""
    return arm_records[-1].checkpoint_text


def _to_experiment_metrics(realtime_metrics: RealtimeTextMetrics) -> ExperimentMetrics:
    return ExperimentMetrics(
        lba_f1=realtime_metrics.lba_f1,
        precision=realtime_metrics.precision,
        recall=realtime_metrics.recall,
        p50_latency_ms=(
            realtime_metrics.p50_latency_ms
            if realtime_metrics.p50_latency_ms is not None
            else _METRIC_FAILURE_LATENCY_MS
        ),
        p95_latency_ms=(
            realtime_metrics.p95_latency_ms
            if realtime_metrics.p95_latency_ms is not None
            else _METRIC_FAILURE_LATENCY_MS
        ),
        unsupported_rate_per_minute=realtime_metrics.unsupported_item_rate_per_minute,
        retraction_rate=realtime_metrics.retraction_rate,
        coverage=realtime_metrics.usable_checkpoint_coverage,
        wer=realtime_metrics.wer if realtime_metrics.wer is not None else 1.0,
        cer=realtime_metrics.cer if realtime_metrics.cer is not None else 1.0,
    )


def _determine_arm_verdict(*, arm: Arm, metrics: ExperimentMetrics) -> str:
    if arm is Arm.A4_FUNASR_PHASE2:
        return "not_run"
    if not _passes_absolute_thresholds(metrics):
        return "failed_thresholds"
    return "passes_thresholds"


def _write_json_report(*, report: RealtimeTextExperimentReport, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(_report_to_json_payload(report), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _write_markdown_report(*, report: RealtimeTextExperimentReport, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Realtime Text Source Discovery Report",
        "",
        f"- Fixtures root: `{report.fixtures_root}`",
        f"- Canonical verdict: `{report.canonical_verdict.value}`",
        (
            f"- Deferred arm: `{Arm.A4_FUNASR_PHASE2.name}` "
            "is deferred phase-2 and recorded as not-run."
        ),
        "",
        (
            "| Arm | Status | Arm Verdict | LBA-F1 | Precision | Recall | "
            "P95 Latency (ms) | Unsupported/min | Retraction | Coverage | WER | CER |"
        ),
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for arm in Arm:
        evaluation = report.arms[arm]
        metrics = evaluation.metrics
        lines.append(
            (
                "| {arm} | {status} | {arm_verdict} | {lba_f1} | {precision} | "
                "{recall} | {p95} | {unsupported} | {retraction} | {coverage} | "
                "{wer} | {cer} |"
            ).format(
                arm=arm.name,
                status=evaluation.status,
                arm_verdict=evaluation.arm_verdict,
                lba_f1=_format_metric(metrics.lba_f1 if metrics is not None else None),
                precision=_format_metric(metrics.precision if metrics is not None else None),
                recall=_format_metric(metrics.recall if metrics is not None else None),
                p95=_format_latency(metrics.p95_latency_ms if metrics is not None else None),
                unsupported=_format_metric(
                    metrics.unsupported_rate_per_minute if metrics is not None else None
                ),
                retraction=_format_metric(metrics.retraction_rate if metrics is not None else None),
                coverage=_format_metric(metrics.coverage if metrics is not None else None),
                wer=_format_metric(metrics.wer if metrics is not None else None),
                cer=_format_metric(metrics.cer if metrics is not None else None),
            )
        )
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _report_to_json_payload(report: RealtimeTextExperimentReport) -> dict[str, object]:
    return {
        "fixtures_root": report.fixtures_root,
        "canonical_verdict": report.canonical_verdict.value,
        "arms": {
            arm.name: {
                "status": evaluation.status,
                "arm_verdict": evaluation.arm_verdict,
                "fixture_count": evaluation.fixture_count,
                "replay_checkpoint_count": evaluation.replay_checkpoint_count,
                "usable_checkpoint_count": evaluation.usable_checkpoint_count,
                "notes": list(evaluation.notes),
                "metrics": (
                    {
                        "lba_f1": evaluation.metrics.lba_f1,
                        "precision": evaluation.metrics.precision,
                        "recall": evaluation.metrics.recall,
                        "p50_latency_ms": evaluation.metrics.p50_latency_ms,
                        "p95_latency_ms": evaluation.metrics.p95_latency_ms,
                        "unsupported_rate_per_minute": (
                            evaluation.metrics.unsupported_rate_per_minute
                        ),
                        "retraction_rate": evaluation.metrics.retraction_rate,
                        "coverage": evaluation.metrics.coverage,
                        "wer": evaluation.metrics.wer,
                        "cer": evaluation.metrics.cer,
                    }
                    if evaluation.metrics is not None
                    else None
                ),
            }
            for arm, evaluation in report.arms.items()
        },
    }


def _passes_absolute_thresholds(metrics: ExperimentMetrics) -> bool:
    return (
        metrics.lba_f1 >= LBA_F1_THRESHOLD
        and metrics.precision >= PRECISION_THRESHOLD
        and metrics.p95_latency_ms <= P95_LATENCY_THRESHOLD_MS
        and metrics.unsupported_rate_per_minute <= UNSUPPORTED_RATE_THRESHOLD_PER_MINUTE
        and metrics.retraction_rate <= RETRACTION_RATE_THRESHOLD
        and metrics.coverage >= COVERAGE_THRESHOLD
    )


def _is_pareto_better_than_a1(candidate: ExperimentMetrics, a1: ExperimentMetrics) -> bool:
    return (
        candidate.lba_f1 >= a1.lba_f1
        and candidate.precision >= a1.precision
        and candidate.recall >= a1.recall
        and candidate.p95_latency_ms <= a1.p95_latency_ms
        and candidate.unsupported_rate_per_minute <= a1.unsupported_rate_per_minute
        and candidate.retraction_rate <= a1.retraction_rate
        and candidate.coverage >= a1.coverage
        and (
            candidate.lba_f1 > a1.lba_f1
            or candidate.precision > a1.precision
            or candidate.recall > a1.recall
            or candidate.p95_latency_ms < a1.p95_latency_ms
            or candidate.unsupported_rate_per_minute < a1.unsupported_rate_per_minute
            or candidate.retraction_rate < a1.retraction_rate
            or candidate.coverage > a1.coverage
        )
    )


def _matches_a1_with_latency_gain(candidate: ExperimentMetrics, a1: ExperimentMetrics) -> bool:
    if a1.p95_latency_ms <= 0:
        return False
    return (
        candidate.lba_f1 >= a1.lba_f1 - PILOT_LBA_F1_MARGIN
        and (a1.p95_latency_ms - candidate.p95_latency_ms) / a1.p95_latency_ms
        >= PILOT_P95_LATENCY_IMPROVEMENT
    )


def _match_gold_label(
    *,
    arm_item: AssistanceItem,
    gold_labels: list[GoldLabel],
    matched_gold_indexes: set[int],
) -> int | None:
    arm_type = arm_item.item_type.value
    arm_text = _normalize_item_text(arm_item.text)
    for index, gold_label in enumerate(gold_labels):
        if index in matched_gold_indexes:
            continue
        if gold_label.item_type.value != arm_type:
            continue
        if _normalize_item_text(gold_label.text) != arm_text:
            continue
        if arm_item.emitted_ts_ms < gold_label.first_evidence_ts_ms:
            continue
        if arm_item.emitted_ts_ms > gold_label.first_evidence_ts_ms + MATCH_LAG_BOUND_MS:
            continue
        return index
    return None


def _normalize_item_text(text: str) -> str:
    return _NORMALIZED_TEXT_PATTERN.sub("", compact_text(text)).lower()


def _split_checkpoint_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _median_latency(latencies_ms: list[int]) -> int | None:
    if not latencies_ms:
        return None
    ordered = sorted(latencies_ms)
    midpoint = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) // 2


def _percentile_latency(latencies_ms: list[int], percentile: float) -> int | None:
    if not latencies_ms:
        return None
    ordered = sorted(latencies_ms)
    rank = max(math.ceil(percentile * len(ordered)) - 1, 0)
    return ordered[min(rank, len(ordered) - 1)]


def _word_error_rate(reference_text: str | None, hypothesis_text: str | None) -> float | None:
    if reference_text is None or hypothesis_text is None:
        return None
    reference_tokens = compact_text(reference_text).split()
    hypothesis_tokens = compact_text(hypothesis_text).split()
    if not reference_tokens:
        return 0.0 if not hypothesis_tokens else 1.0
    return _levenshtein_distance(reference_tokens, hypothesis_tokens) / len(reference_tokens)


def _character_error_rate(reference_text: str | None, hypothesis_text: str | None) -> float | None:
    if reference_text is None or hypothesis_text is None:
        return None
    reference_chars = list(compact_text(reference_text).replace(" ", ""))
    hypothesis_chars = list(compact_text(hypothesis_text).replace(" ", ""))
    if not reference_chars:
        return 0.0 if not hypothesis_chars else 1.0
    return _levenshtein_distance(reference_chars, hypothesis_chars) / len(reference_chars)


def _levenshtein_distance(reference: list[str], hypothesis: list[str]) -> int:
    if not reference:
        return len(hypothesis)
    if not hypothesis:
        return len(reference)
    previous_row = list(range(len(hypothesis) + 1))
    for reference_index, reference_value in enumerate(reference, start=1):
        current_row = [reference_index]
        for hypothesis_index, hypothesis_value in enumerate(hypothesis, start=1):
            insertion = current_row[hypothesis_index - 1] + 1
            deletion = previous_row[hypothesis_index] + 1
            substitution = previous_row[hypothesis_index - 1] + (
                reference_value != hypothesis_value
            )
            current_row.append(min(insertion, deletion, substitution))
        previous_row = current_row
    return previous_row[-1]


def _safe_divide(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def _format_metric(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.3f}"


def _format_latency(value: int | None) -> str:
    if value is None:
        return "-"
    return str(value)


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay realtime text fixtures and emit the experiment report.",
    )
    parser.add_argument(
        "--fixtures",
        type=Path,
        required=True,
        help="Path to the deterministic replay fixtures directory.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Output prefix for the .json and .md reports.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_argument_parser()
    args = parser.parse_args(argv)
    report = generate_realtime_text_report(
        fixtures_root=args.fixtures,
        output_prefix=args.output,
    )
    print(f"Wrote {args.output.with_suffix('.json')}")
    print(f"Wrote {args.output.with_suffix('.md')}")
    print(f"Canonical verdict: {report.canonical_verdict.value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "Arm",
    "ArmEvaluation",
    "AssistanceItem",
    "AssistanceItemType",
    "COVERAGE_THRESHOLD",
    "ExperimentMetrics",
    "GoldLabel",
    "LBA_F1_THRESHOLD",
    "MATCH_LAG_BOUND_MS",
    "P95_LATENCY_THRESHOLD_MS",
    "PHASE1_ARMS",
    "PRECISION_THRESHOLD",
    "PILOT_LBA_F1_MARGIN",
    "PILOT_P95_LATENCY_IMPROVEMENT",
    "RealtimeTextExperimentReport",
    "RealtimeTextMetrics",
    "RETRACTION_RATE_THRESHOLD",
    "UNSUPPORTED_RATE_THRESHOLD_PER_MINUTE",
    "Verdict",
    "compute_realtime_text_metrics",
    "decide_experiment_verdict",
    "generate_realtime_text_report",
    "main",
]
