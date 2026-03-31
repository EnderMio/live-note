from __future__ import annotations

import unittest
from collections import defaultdict
from pathlib import Path

FIXTURES_ROOT = Path(__file__).parent / "fixtures" / "realtime_text_eval"


class RealtimeTextReplayTests(unittest.TestCase):
    def _load_module(self):
        try:
            from live_note.app import realtime_text_replay
        except ModuleNotFoundError as exc:
            self.fail(f"missing replay extractor module: {exc}")
        return realtime_text_replay

    def _records_by_fixture(self):
        module = self._load_module()
        records = module.load_replay_checkpoints(FIXTURES_ROOT)
        grouped: dict[str, list[object]] = defaultdict(list)
        for record in records:
            grouped[record.fixture_id].append(record)
        return grouped

    def test_replay_loads_all_sanitized_fixture_classes(self) -> None:
        grouped = self._records_by_fixture()

        self.assertEqual(
            {
                "local_live_refine_failure_preserves_live_draft",
                "local_live_refine_success",
                "remote_live_delayed_offline_final",
                "structured_failed",
                "suspicious_recovery_interrupted",
                "transcript_only",
            },
            set(grouped),
        )

    def test_replay_uses_live_backup_when_refine_failed(self) -> None:
        records = self._records_by_fixture()["local_live_refine_failure_preserves_live_draft"]

        self.assertEqual(
            ["live_draft", "live_draft"], [record.checkpoint_source for record in records]
        )
        self.assertEqual("Draft intro line.\nDraft fallback action.", records[-1].checkpoint_text)
        self.assertEqual("live", records[-1].final_truth.transcript_source)
        self.assertEqual("failed", records[-1].final_truth.refine_status)
        self.assertEqual(
            "Draft intro line.\nDraft fallback action.",
            records[-1].final_truth.transcript_text,
        )

    def test_replay_attaches_refined_truth_for_delayed_final_case(self) -> None:
        records = self._records_by_fixture()["remote_live_delayed_offline_final"]
        live_records = [record for record in records if record.checkpoint_source == "live_draft"]
        final_records = [
            record for record in records if record.checkpoint_source == "canonical_final"
        ]

        self.assertEqual(
            "Remote draft opening.\nRemote draft wrap-up.", live_records[-1].checkpoint_text
        )
        self.assertEqual(
            "Remote final opening.\nRemote final wrap-up with offline pass.",
            final_records[-1].checkpoint_text,
        )
        self.assertEqual(
            "Remote final opening.\nRemote final wrap-up with offline pass.",
            live_records[-1].final_truth.transcript_text,
        )
        self.assertEqual("remote", live_records[-1].final_truth.execution_target)
        self.assertEqual("done", live_records[-1].final_truth.refine_status)

    def test_replay_distinguishes_transcript_only_and_structured_failed(self) -> None:
        grouped = self._records_by_fixture()
        transcript_only_truth = grouped["transcript_only"][-1].final_truth
        structured_failed_truth = grouped["structured_failed"][-1].final_truth

        self.assertEqual("transcript_only", transcript_only_truth.transcript_status)
        self.assertEqual("transcript_only", transcript_only_truth.structured_status)
        self.assertEqual("structured_failed", structured_failed_truth.transcript_status)
        self.assertEqual("structured_failed", structured_failed_truth.structured_status)
        self.assertNotEqual(
            transcript_only_truth.structured_status, structured_failed_truth.structured_status
        )


if __name__ == "__main__":
    unittest.main()
