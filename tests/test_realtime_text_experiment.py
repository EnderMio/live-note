from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from live_note.app.realtime_text_arms import RealtimeTextArmRecord
from live_note.app.realtime_text_experiment import (
    Arm,
    AssistanceItem,
    AssistanceItemType,
    ExperimentMetrics,
    GoldLabel,
    Verdict,
    _build_arm_items,
    _build_gold_labels,
    compute_realtime_text_metrics,
    decide_experiment_verdict,
    generate_realtime_text_report,
)
from live_note.app.realtime_text_replay import ReplayFinalTruth, load_replay_checkpoints

FIXTURES_ROOT = Path(__file__).resolve().parent / "fixtures" / "realtime_text_eval"
RUNNER_COMMAND = (
    "python -m live_note.app.realtime_text_experiment --fixtures "
    "tests/fixtures/realtime_text_eval --output "
    ".sisyphus/evidence/final/realtime-text-report"
)


def make_metrics(
    *,
    lba_f1: float,
    precision: float = 0.90,
    recall: float = 0.82,
    p50_latency_ms: int = 4000,
    p95_latency_ms: int = 10000,
    unsupported_rate_per_minute: float = 0.05,
    retraction_rate: float = 0.08,
    coverage: float = 0.75,
    wer: float = 0.30,
    cer: float = 0.20,
) -> ExperimentMetrics:
    return ExperimentMetrics(
        lba_f1=lba_f1,
        precision=precision,
        recall=recall,
        p50_latency_ms=p50_latency_ms,
        p95_latency_ms=p95_latency_ms,
        unsupported_rate_per_minute=unsupported_rate_per_minute,
        retraction_rate=retraction_rate,
        coverage=coverage,
        wer=wer,
        cer=cer,
    )


class RealtimeTextExperimentTests(unittest.TestCase):
    def test_contract_exports_phase1_arms_and_deferred_phase2_arm(self) -> None:
        self.assertEqual("A0_CURRENT_LIVE_TEXT_BASELINE", Arm.A0_CURRENT_LIVE_TEXT_BASELINE.name)
        self.assertEqual("A1_FINALIZED_SEGMENT_WINDOW", Arm.A1_FINALIZED_SEGMENT_WINDOW.name)
        self.assertEqual("A2_STABILIZED_ROLLING_WINDOW", Arm.A2_STABILIZED_ROLLING_WINDOW.name)
        self.assertEqual("A3_MINI_REFINE_RECENT_WINDOW", Arm.A3_MINI_REFINE_RECENT_WINDOW.name)
        self.assertEqual("A4_FUNASR_PHASE2", Arm.A4_FUNASR_PHASE2.name)

    def test_contract_uses_assistance_and_gold_label_schema(self) -> None:
        item = AssistanceItem(
            item_type=AssistanceItemType.ACTION_ITEM,
            text="下周前提交实验报告",
        )
        label = GoldLabel(
            item_type=AssistanceItemType.OPEN_QUESTION,
            text="是否要扩到 A4 phase-2？",
            first_evidence_ts_ms=12000,
        )

        self.assertEqual(AssistanceItemType.ACTION_ITEM, item.item_type)
        self.assertEqual("下周前提交实验报告", item.text)
        self.assertEqual(12000, label.first_evidence_ts_ms)

    def test_verdict_rejects_all_arms_below_lba_f1_threshold(self) -> None:
        metrics_by_arm = {
            Arm.A0_CURRENT_LIVE_TEXT_BASELINE: make_metrics(lba_f1=0.79),
            Arm.A1_FINALIZED_SEGMENT_WINDOW: make_metrics(lba_f1=0.79),
            Arm.A2_STABILIZED_ROLLING_WINDOW: make_metrics(lba_f1=0.79),
            Arm.A3_MINI_REFINE_RECENT_WINDOW: make_metrics(lba_f1=0.79),
        }

        verdict = decide_experiment_verdict(metrics_by_arm)

        self.assertEqual(Verdict.REJECT_ALL_ARMS, verdict)

    def test_verdict_rejects_arm_when_any_absolute_threshold_fails(self) -> None:
        metrics_by_arm = {
            Arm.A0_CURRENT_LIVE_TEXT_BASELINE: make_metrics(lba_f1=0.81, coverage=0.69),
            Arm.A1_FINALIZED_SEGMENT_WINDOW: make_metrics(lba_f1=0.80),
            Arm.A2_STABILIZED_ROLLING_WINDOW: make_metrics(lba_f1=0.79),
            Arm.A3_MINI_REFINE_RECENT_WINDOW: make_metrics(lba_f1=0.78),
        }

        verdict = decide_experiment_verdict(metrics_by_arm)

        self.assertEqual(Verdict.PROMOTE_BEST_ARM_TO_SHADOW, verdict)

    def test_verdict_marks_pilot_when_arm_beats_a1_or_matches_with_latency_gain(self) -> None:
        pareto_metrics = {
            Arm.A0_CURRENT_LIVE_TEXT_BASELINE: make_metrics(lba_f1=0.74),
            Arm.A1_FINALIZED_SEGMENT_WINDOW: make_metrics(lba_f1=0.84, p95_latency_ms=10000),
            Arm.A2_STABILIZED_ROLLING_WINDOW: make_metrics(lba_f1=0.86, p95_latency_ms=9800),
            Arm.A3_MINI_REFINE_RECENT_WINDOW: make_metrics(lba_f1=0.82, p95_latency_ms=9200),
        }
        latency_tradeoff_metrics = {
            Arm.A0_CURRENT_LIVE_TEXT_BASELINE: make_metrics(lba_f1=0.75),
            Arm.A1_FINALIZED_SEGMENT_WINDOW: make_metrics(lba_f1=0.86, p95_latency_ms=10000),
            Arm.A2_STABILIZED_ROLLING_WINDOW: make_metrics(lba_f1=0.84, p95_latency_ms=6900),
            Arm.A3_MINI_REFINE_RECENT_WINDOW: make_metrics(lba_f1=0.81, p95_latency_ms=9100),
        }

        pareto_verdict = decide_experiment_verdict(pareto_metrics)
        latency_tradeoff_verdict = decide_experiment_verdict(latency_tradeoff_metrics)

        self.assertEqual(Verdict.READY_FOR_REALTIME_ASSISTANCE_PILOT, pareto_verdict)
        self.assertEqual(Verdict.READY_FOR_REALTIME_ASSISTANCE_PILOT, latency_tradeoff_verdict)

    def test_verdict_promotes_shadow_when_best_arm_passes_but_not_relative_winner(self) -> None:
        metrics_by_arm = {
            Arm.A0_CURRENT_LIVE_TEXT_BASELINE: make_metrics(lba_f1=0.74),
            Arm.A1_FINALIZED_SEGMENT_WINDOW: make_metrics(lba_f1=0.86, p95_latency_ms=10000),
            Arm.A2_STABILIZED_ROLLING_WINDOW: make_metrics(lba_f1=0.84, p95_latency_ms=7100),
            Arm.A3_MINI_REFINE_RECENT_WINDOW: make_metrics(lba_f1=0.82, p95_latency_ms=9500),
        }

        verdict = decide_experiment_verdict(metrics_by_arm)

        self.assertEqual(Verdict.PROMOTE_BEST_ARM_TO_SHADOW, verdict)

    def test_scorer_counts_hit_only_within_eight_second_bound(self) -> None:
        metrics = compute_realtime_text_metrics(
            gold_labels=[
                GoldLabel(
                    item_type=AssistanceItemType.DECISION,
                    text="Ship the beta this week.",
                    first_evidence_ts_ms=1_000,
                ),
                GoldLabel(
                    item_type=AssistanceItemType.ACTION_ITEM,
                    text="Assign QA owner",
                    first_evidence_ts_ms=5_000,
                ),
            ],
            arm_items=[
                AssistanceItem(
                    item_type=AssistanceItemType.DECISION,
                    text="ship the beta this week",
                    emitted_ts_ms=8_999,
                ),
                AssistanceItem(
                    item_type=AssistanceItemType.DECISION,
                    text="ship the beta this week",
                    emitted_ts_ms=9_001,
                ),
                AssistanceItem(
                    item_type=AssistanceItemType.ACTION_ITEM,
                    text="Assign QA owner!",
                    emitted_ts_ms=13_000,
                ),
            ],
            checkpoint_count=10,
            usable_checkpoint_count=7,
            session_duration_ms=60_000,
        )

        self.assertEqual(2, metrics.true_positives)
        self.assertEqual(1, metrics.false_positives)
        self.assertEqual(0, metrics.false_negatives)
        self.assertAlmostEqual(2 / 3, metrics.precision)
        self.assertAlmostEqual(1.0, metrics.recall)
        self.assertAlmostEqual(0.8, metrics.lba_f1)
        self.assertEqual(7_999, metrics.p50_latency_ms)
        self.assertEqual(8_000, metrics.p95_latency_ms)
        self.assertAlmostEqual(1.0, metrics.unsupported_item_rate_per_minute)
        self.assertAlmostEqual(0.0, metrics.retraction_rate)
        self.assertAlmostEqual(0.7, metrics.usable_checkpoint_coverage)

    def test_scorer_computes_secondary_metrics_and_diagnostic_error_rates(self) -> None:
        metrics = compute_realtime_text_metrics(
            gold_labels=[
                GoldLabel(
                    item_type=AssistanceItemType.TOPIC,
                    text="Budget review",
                    first_evidence_ts_ms=1_000,
                ),
                GoldLabel(
                    item_type=AssistanceItemType.OPEN_QUESTION,
                    text="Need vendor quote",
                    first_evidence_ts_ms=3_000,
                ),
            ],
            arm_items=[
                AssistanceItem(
                    item_type=AssistanceItemType.TOPIC,
                    text="budget review",
                    emitted_ts_ms=3_000,
                ),
                AssistanceItem(
                    item_type=AssistanceItemType.OPEN_QUESTION,
                    text="Need vendor quote",
                    emitted_ts_ms=8_000,
                    retracted_ts_ms=9_000,
                ),
                AssistanceItem(
                    item_type=AssistanceItemType.DECISION,
                    text="approve spend",
                    emitted_ts_ms=4_000,
                ),
            ],
            checkpoint_count=5,
            usable_checkpoint_count=4,
            session_duration_ms=120_000,
            finalized_reference_text="budget review need vendor quote",
            arm_reference_text="budget review need vendor quoat",
        )

        self.assertEqual(2, metrics.true_positives)
        self.assertEqual(1, metrics.false_positives)
        self.assertEqual(0, metrics.false_negatives)
        self.assertAlmostEqual(2 / 3, metrics.precision)
        self.assertAlmostEqual(1.0, metrics.recall)
        self.assertAlmostEqual(0.8, metrics.lba_f1)
        self.assertEqual(3_500, metrics.p50_latency_ms)
        self.assertEqual(5_000, metrics.p95_latency_ms)
        self.assertAlmostEqual(0.5, metrics.unsupported_item_rate_per_minute)
        self.assertAlmostEqual(1 / 3, metrics.retraction_rate)
        self.assertAlmostEqual(0.8, metrics.usable_checkpoint_coverage)
        self.assertGreater(metrics.wer, 0.0)
        self.assertGreater(metrics.cer, 0.0)

    def test_build_gold_labels_falls_back_to_live_only_authoritative_truth(self) -> None:
        replay_records = load_replay_checkpoints(FIXTURES_ROOT)
        fixture_records = [
            record
            for record in replay_records
            if record.fixture_id == "local_live_refine_failure_preserves_live_draft"
        ]

        gold_labels = _build_gold_labels(fixture_records)

        self.assertEqual(
            ["Draft intro line.", "Draft fallback action."], [label.text for label in gold_labels]
        )
        self.assertEqual([1_200, 2_600], [label.first_evidence_ts_ms for label in gold_labels])

    def test_build_arm_items_marks_retracted_lines_from_snapshot_evolution(self) -> None:
        final_truth = ReplayFinalTruth(
            fixture_id="fixture-retraction",
            transcript_text="Agenda line\nAction item",
            transcript_status="transcript_only",
            structured_status="pending",
            transcript_source="live",
            refine_status="failed",
            execution_target="local",
        )
        gold_labels = [
            GoldLabel(
                item_type=AssistanceItemType.TOPIC,
                text="Agenda line",
                first_evidence_ts_ms=1_000,
            ),
            GoldLabel(
                item_type=AssistanceItemType.ACTION_ITEM,
                text="Action item",
                first_evidence_ts_ms=2_000,
            ),
        ]
        arm_records = [
            RealtimeTextArmRecord(
                fixture_id="fixture-retraction",
                checkpoint_id="fixture-retraction:a0:1",
                checkpoint_ts_ms=1_000,
                checkpoint_text="Agenda line\nAction item",
                arm=Arm.A0_CURRENT_LIVE_TEXT_BASELINE,
                metadata={"source": "live_draft"},
                final_truth=final_truth,
            ),
            RealtimeTextArmRecord(
                fixture_id="fixture-retraction",
                checkpoint_id="fixture-retraction:a0:2",
                checkpoint_ts_ms=4_000,
                checkpoint_text="Action item",
                arm=Arm.A0_CURRENT_LIVE_TEXT_BASELINE,
                metadata={"source": "live_draft"},
                final_truth=final_truth,
            ),
        ]

        arm_items = _build_arm_items(
            arm=Arm.A0_CURRENT_LIVE_TEXT_BASELINE,
            arm_records=arm_records,
            gold_labels=gold_labels,
        )

        agenda_items = [item for item in arm_items if item.text == "Agenda line"]
        self.assertEqual(1, len(agenda_items))
        self.assertEqual(4_000, agenda_items[0].retracted_ts_ms)

        metrics = compute_realtime_text_metrics(
            gold_labels=gold_labels,
            arm_items=arm_items,
            checkpoint_count=2,
            usable_checkpoint_count=2,
            session_duration_ms=4_000,
        )
        self.assertGreater(metrics.retraction_rate, 0.0)

    def test_runner_writes_json_and_markdown_reports_with_deferred_a4(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_prefix = Path(tmpdir) / "realtime-text-report"

            report = generate_realtime_text_report(
                fixtures_root=FIXTURES_ROOT,
                output_prefix=output_prefix,
            )

            json_report = json.loads(output_prefix.with_suffix(".json").read_text(encoding="utf-8"))
            markdown_report = output_prefix.with_suffix(".md").read_text(encoding="utf-8")

        self.assertEqual(report.canonical_verdict.value, json_report["canonical_verdict"])
        self.assertEqual(
            {arm.name for arm in Arm},
            set(json_report["arms"].keys()),
        )
        self.assertEqual("deferred_phase2", json_report["arms"]["A4_FUNASR_PHASE2"]["status"])
        self.assertEqual("not_run", json_report["arms"]["A4_FUNASR_PHASE2"]["arm_verdict"])
        self.assertIn("A0_CURRENT_LIVE_TEXT_BASELINE", markdown_report)
        self.assertIn("A3_MINI_REFINE_RECENT_WINDOW", markdown_report)
        self.assertIn("A4_FUNASR_PHASE2", markdown_report)
        self.assertIn("deferred phase-2", markdown_report)
        self.assertIn(report.canonical_verdict.value, markdown_report)

    def test_runbook_commands_match_runner_interface(self) -> None:
        runbook_path = (
            Path(__file__).resolve().parent.parent
            / "docs"
            / "experiments"
            / "realtime-text-source-discovery.md"
        )
        template_path = (
            Path(__file__).resolve().parent.parent
            / "docs"
            / "experiments"
            / "templates"
            / "realtime-text-decision.md"
        )

        runbook_text = runbook_path.read_text(encoding="utf-8")
        template_text = template_path.read_text(encoding="utf-8")

        self.assertIn(RUNNER_COMMAND, runbook_text)
        self.assertIn("A0 current_live_text_baseline", runbook_text)
        self.assertIn("A4 funasr_phase2", runbook_text)
        self.assertIn("REJECT_ALL_ARMS", template_text)
        self.assertIn("PROMOTE_BEST_ARM_TO_SHADOW", template_text)
        self.assertIn("READY_FOR_REALTIME_ASSISTANCE_PILOT", template_text)

    def test_cli_module_entrypoint_writes_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_prefix = Path(tmpdir) / "realtime-text-report"

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "live_note.app.realtime_text_experiment",
                    "--fixtures",
                    str(FIXTURES_ROOT),
                    "--output",
                    str(output_prefix),
                ],
                capture_output=True,
                check=False,
                text=True,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertTrue(output_prefix.with_suffix(".json").exists())
            self.assertTrue(output_prefix.with_suffix(".md").exists())


if __name__ == "__main__":
    unittest.main()
