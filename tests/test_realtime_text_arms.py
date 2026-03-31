from __future__ import annotations

import unittest

from live_note.app.realtime_text_experiment import Arm
from live_note.app.realtime_text_replay import ReplayCheckpointRecord, ReplayFinalTruth


def make_final_truth(*, fixture_id: str, execution_target: str) -> ReplayFinalTruth:
    return ReplayFinalTruth(
        fixture_id=fixture_id,
        transcript_text="final transcript",
        transcript_status="done",
        structured_status="done",
        transcript_source="live",
        refine_status="disabled",
        execution_target=execution_target,
    )


def make_record(
    *,
    fixture_id: str,
    checkpoint_source: str,
    checkpoint_index: int,
    checkpoint_ts_ms: int,
    checkpoint_text: str,
    execution_target: str,
) -> ReplayCheckpointRecord:
    return ReplayCheckpointRecord(
        fixture_id=fixture_id,
        checkpoint_id=f"{fixture_id}:{checkpoint_source}:{checkpoint_index}",
        checkpoint_ts_ms=checkpoint_ts_ms,
        checkpoint_source=checkpoint_source,
        checkpoint_text=checkpoint_text,
        final_truth=make_final_truth(fixture_id=fixture_id, execution_target=execution_target),
    )


class StubMiniRefineDecodeAdapter:
    def __init__(self, outputs: list[str]) -> None:
        self._outputs = outputs
        self.calls: list[tuple[str, int, int]] = []

    def decode_recent_window(
        self,
        *,
        fixture_id: str,
        source_records: list[ReplayCheckpointRecord],
        window_start_ts_ms: int,
        window_end_ts_ms: int,
    ) -> str:
        del source_records
        self.calls.append((fixture_id, window_start_ts_ms, window_end_ts_ms))
        output_index = len(self.calls) - 1
        if output_index >= len(self._outputs):
            raise AssertionError("unexpected decode call")
        return self._outputs[output_index]


class RealtimeTextArmsTests(unittest.TestCase):
    def test_a0_marks_local_backend_degenerate_when_identical_to_segment_final(self) -> None:
        from live_note.app.realtime_text_arms import build_realtime_text_arm_records

        records = [
            make_record(
                fixture_id="local-case",
                checkpoint_source="live_draft",
                checkpoint_index=1,
                checkpoint_ts_ms=1_000,
                checkpoint_text="Draft intro line.",
                execution_target="local",
            ),
            make_record(
                fixture_id="local-case",
                checkpoint_source="canonical_final",
                checkpoint_index=1,
                checkpoint_ts_ms=1_000,
                checkpoint_text="Draft intro line.",
                execution_target="local",
            ),
            make_record(
                fixture_id="local-case",
                checkpoint_source="live_draft",
                checkpoint_index=2,
                checkpoint_ts_ms=2_000,
                checkpoint_text="Draft intro line.\nDraft action item.",
                execution_target="local",
            ),
            make_record(
                fixture_id="local-case",
                checkpoint_source="canonical_final",
                checkpoint_index=2,
                checkpoint_ts_ms=2_000,
                checkpoint_text="Draft intro line.\nDraft action item.",
                execution_target="local",
            ),
        ]

        arm_records = build_realtime_text_arm_records(
            records,
            Arm.A0_CURRENT_LIVE_TEXT_BASELINE,
        )

        self.assertEqual(
            ["Draft intro line.", "Draft intro line.\nDraft action item."],
            [record.checkpoint_text for record in arm_records],
        )
        self.assertEqual(
            ["live_draft", "live_draft"],
            [str(record.metadata["source"]) for record in arm_records],
        )
        self.assertEqual(
            [True, True],
            [bool(record.metadata["degenerate"]) for record in arm_records],
        )

    def test_a0_uses_remote_merged_draft_output(self) -> None:
        from live_note.app.realtime_text_arms import build_realtime_text_arm_records

        records = [
            make_record(
                fixture_id="remote-case",
                checkpoint_source="live_draft",
                checkpoint_index=1,
                checkpoint_ts_ms=1_000,
                checkpoint_text="Remote merged draft",
                execution_target="remote",
            ),
            make_record(
                fixture_id="remote-case",
                checkpoint_source="canonical_final",
                checkpoint_index=1,
                checkpoint_ts_ms=1_000,
                checkpoint_text="Remote final segment",
                execution_target="remote",
            ),
            make_record(
                fixture_id="remote-case",
                checkpoint_source="live_draft",
                checkpoint_index=2,
                checkpoint_ts_ms=2_000,
                checkpoint_text="Remote merged draft extended",
                execution_target="remote",
            ),
            make_record(
                fixture_id="remote-case",
                checkpoint_source="canonical_final",
                checkpoint_index=2,
                checkpoint_ts_ms=2_000,
                checkpoint_text="Remote final segment refined",
                execution_target="remote",
            ),
        ]

        arm_records = build_realtime_text_arm_records(
            records,
            Arm.A0_CURRENT_LIVE_TEXT_BASELINE,
        )

        self.assertEqual(
            ["Remote merged draft", "Remote merged draft extended"],
            [record.checkpoint_text for record in arm_records],
        )
        self.assertEqual(
            [False, False],
            [bool(record.metadata["degenerate"]) for record in arm_records],
        )
        self.assertEqual(
            ["live_draft", "live_draft"],
            [str(record.metadata["source"]) for record in arm_records],
        )

    def test_a1_emits_checkpoint_when_third_finalized_segment_closes_window(self) -> None:
        from live_note.app.realtime_text_arms import build_realtime_text_arm_records

        records = [
            make_record(
                fixture_id="a1-third-close",
                checkpoint_source="canonical_final",
                checkpoint_index=1,
                checkpoint_ts_ms=4_000,
                checkpoint_text="Speaker 1: Kickoff",
                execution_target="remote",
            ),
            make_record(
                fixture_id="a1-third-close",
                checkpoint_source="canonical_final",
                checkpoint_index=2,
                checkpoint_ts_ms=9_000,
                checkpoint_text="Speaker 1: Kickoff\nSpeaker 2: Budget update",
                execution_target="remote",
            ),
            make_record(
                fixture_id="a1-third-close",
                checkpoint_source="canonical_final",
                checkpoint_index=3,
                checkpoint_ts_ms=14_000,
                checkpoint_text=(
                    "Speaker 1: Kickoff\nSpeaker 2: Budget update\nSpeaker 1: Ship Friday"
                ),
                execution_target="remote",
            ),
        ]

        arm_records = build_realtime_text_arm_records(
            records,
            Arm.A1_FINALIZED_SEGMENT_WINDOW,
        )

        self.assertEqual(1, len(arm_records))
        self.assertEqual(14_000, arm_records[0].checkpoint_ts_ms)
        self.assertEqual(
            "Speaker 1: Kickoff\nSpeaker 2: Budget update\nSpeaker 1: Ship Friday",
            arm_records[0].checkpoint_text,
        )
        self.assertEqual("canonical_final", arm_records[0].metadata["source"])
        self.assertEqual(3, arm_records[0].metadata["segment_count"])
        self.assertEqual(10_000, arm_records[0].metadata["window_duration_ms"])

    def test_a1_emits_checkpoint_when_finalized_window_reaches_twenty_seconds(self) -> None:
        from live_note.app.realtime_text_arms import build_realtime_text_arm_records

        records = [
            make_record(
                fixture_id="a1-time-close",
                checkpoint_source="canonical_final",
                checkpoint_index=1,
                checkpoint_ts_ms=5_000,
                checkpoint_text="Intro",
                execution_target="remote",
            ),
            make_record(
                fixture_id="a1-time-close",
                checkpoint_source="canonical_final",
                checkpoint_index=2,
                checkpoint_ts_ms=26_000,
                checkpoint_text="Intro\nLong follow-up",
                execution_target="remote",
            ),
        ]

        arm_records = build_realtime_text_arm_records(
            records,
            Arm.A1_FINALIZED_SEGMENT_WINDOW,
        )

        self.assertEqual(1, len(arm_records))
        self.assertEqual(26_000, arm_records[0].checkpoint_ts_ms)
        self.assertEqual("Intro\nLong follow-up", arm_records[0].checkpoint_text)
        self.assertEqual(2, arm_records[0].metadata["segment_count"])
        self.assertEqual(21_000, arm_records[0].metadata["window_duration_ms"])

    def test_a1_ignores_partial_live_draft_records(self) -> None:
        from live_note.app.realtime_text_arms import build_realtime_text_arm_records

        records = [
            make_record(
                fixture_id="a1-ignore-partial",
                checkpoint_source="live_draft",
                checkpoint_index=1,
                checkpoint_ts_ms=2_000,
                checkpoint_text="Speaker 1: Partial opening",
                execution_target="remote",
            ),
            make_record(
                fixture_id="a1-ignore-partial",
                checkpoint_source="canonical_final",
                checkpoint_index=1,
                checkpoint_ts_ms=4_000,
                checkpoint_text="Speaker 1: Final opening",
                execution_target="remote",
            ),
            make_record(
                fixture_id="a1-ignore-partial",
                checkpoint_source="live_draft",
                checkpoint_index=2,
                checkpoint_ts_ms=6_000,
                checkpoint_text="Speaker 1: Partial opening\nSpeaker 2: Partial reply",
                execution_target="remote",
            ),
            make_record(
                fixture_id="a1-ignore-partial",
                checkpoint_source="canonical_final",
                checkpoint_index=2,
                checkpoint_ts_ms=9_000,
                checkpoint_text="Speaker 1: Final opening\nSpeaker 2: Final reply",
                execution_target="remote",
            ),
            make_record(
                fixture_id="a1-ignore-partial",
                checkpoint_source="canonical_final",
                checkpoint_index=3,
                checkpoint_ts_ms=12_000,
                checkpoint_text=(
                    "Speaker 1: Final opening\nSpeaker 2: Final reply\nSpeaker 1: Final decision"
                ),
                execution_target="remote",
            ),
        ]

        arm_records = build_realtime_text_arm_records(
            records,
            Arm.A1_FINALIZED_SEGMENT_WINDOW,
        )

        self.assertEqual(1, len(arm_records))
        self.assertEqual(
            "Speaker 1: Final opening\nSpeaker 2: Final reply\nSpeaker 1: Final decision",
            arm_records[0].checkpoint_text,
        )
        self.assertEqual("canonical_final", arm_records[0].metadata["source"])

    def test_a2_never_retracts_frozen_text(self) -> None:
        from live_note.app.realtime_text_arms import build_realtime_text_arm_records

        records = [
            make_record(
                fixture_id="a2-no-retract",
                checkpoint_source="live_draft",
                checkpoint_index=1,
                checkpoint_ts_ms=0,
                checkpoint_text="Alpha",
                execution_target="remote",
            ),
            make_record(
                fixture_id="a2-no-retract",
                checkpoint_source="live_draft",
                checkpoint_index=2,
                checkpoint_ts_ms=8_000,
                checkpoint_text="Alpha",
                execution_target="remote",
            ),
            make_record(
                fixture_id="a2-no-retract",
                checkpoint_source="live_draft",
                checkpoint_index=3,
                checkpoint_ts_ms=16_000,
                checkpoint_text="Alpha",
                execution_target="remote",
            ),
            make_record(
                fixture_id="a2-no-retract",
                checkpoint_source="live_draft",
                checkpoint_index=4,
                checkpoint_ts_ms=24_000,
                checkpoint_text="Beta",
                execution_target="remote",
            ),
            make_record(
                fixture_id="a2-no-retract",
                checkpoint_source="live_draft",
                checkpoint_index=5,
                checkpoint_ts_ms=32_000,
                checkpoint_text="Beta\nNew tail",
                execution_target="remote",
            ),
        ]

        arm_records = build_realtime_text_arm_records(
            records,
            Arm.A2_STABILIZED_ROLLING_WINDOW,
        )

        self.assertEqual([8_000, 16_000, 24_000, 32_000], [r.checkpoint_ts_ms for r in arm_records])
        self.assertEqual(
            ["Alpha", "Alpha", "Alpha", "Alpha\nNew tail"],
            [record.checkpoint_text for record in arm_records],
        )
        self.assertEqual(
            [0, 1, 1, 1], [record.metadata["frozen_chunk_count"] for record in arm_records]
        )

    def test_a2_tracks_churn_before_freeze(self) -> None:
        from live_note.app.realtime_text_arms import build_realtime_text_arm_records

        records = [
            make_record(
                fixture_id="a2-churn",
                checkpoint_source="live_draft",
                checkpoint_index=1,
                checkpoint_ts_ms=0,
                checkpoint_text="Draft one",
                execution_target="remote",
            ),
            make_record(
                fixture_id="a2-churn",
                checkpoint_source="live_draft",
                checkpoint_index=2,
                checkpoint_ts_ms=8_000,
                checkpoint_text="Draft one",
                execution_target="remote",
            ),
            make_record(
                fixture_id="a2-churn",
                checkpoint_source="live_draft",
                checkpoint_index=3,
                checkpoint_ts_ms=16_000,
                checkpoint_text="Draft one revised",
                execution_target="remote",
            ),
            make_record(
                fixture_id="a2-churn",
                checkpoint_source="live_draft",
                checkpoint_index=4,
                checkpoint_ts_ms=24_000,
                checkpoint_text="Draft one revised",
                execution_target="remote",
            ),
            make_record(
                fixture_id="a2-churn",
                checkpoint_source="live_draft",
                checkpoint_index=5,
                checkpoint_ts_ms=32_000,
                checkpoint_text="Different rewrite",
                execution_target="remote",
            ),
        ]

        arm_records = build_realtime_text_arm_records(
            records,
            Arm.A2_STABILIZED_ROLLING_WINDOW,
        )

        self.assertEqual(
            ["Draft one", "Draft one revised", "Draft one revised", "Draft one revised"],
            [record.checkpoint_text for record in arm_records],
        )
        self.assertEqual([0, 1, 1, 1], [record.metadata["churn_count"] for record in arm_records])
        self.assertEqual(
            [0, 0, 1, 1], [record.metadata["frozen_chunk_count"] for record in arm_records]
        )

    def test_a3_skips_decode_until_eight_seconds_of_new_audio_arrive(self) -> None:
        from live_note.app.realtime_text_arms import build_realtime_text_arm_records

        records = [
            make_record(
                fixture_id="a3-budget",
                checkpoint_source="live_draft",
                checkpoint_index=1,
                checkpoint_ts_ms=0,
                checkpoint_text="Draft at 0s",
                execution_target="remote",
            ),
            make_record(
                fixture_id="a3-budget",
                checkpoint_source="live_draft",
                checkpoint_index=2,
                checkpoint_ts_ms=7_000,
                checkpoint_text="Draft at 7s",
                execution_target="remote",
            ),
            make_record(
                fixture_id="a3-budget",
                checkpoint_source="live_draft",
                checkpoint_index=3,
                checkpoint_ts_ms=8_000,
                checkpoint_text="Draft at 8s",
                execution_target="remote",
            ),
            make_record(
                fixture_id="a3-budget",
                checkpoint_source="live_draft",
                checkpoint_index=4,
                checkpoint_ts_ms=15_000,
                checkpoint_text="Draft at 15s",
                execution_target="remote",
            ),
            make_record(
                fixture_id="a3-budget",
                checkpoint_source="live_draft",
                checkpoint_index=5,
                checkpoint_ts_ms=16_000,
                checkpoint_text="Draft at 16s",
                execution_target="remote",
            ),
        ]
        adapter = StubMiniRefineDecodeAdapter(["Mini decode @8s", "Mini decode @16s"])

        arm_records = build_realtime_text_arm_records(
            records,
            Arm.A3_MINI_REFINE_RECENT_WINDOW,
            mini_refine_decode_adapter=adapter,
        )

        self.assertEqual([8_000, 16_000], [record.checkpoint_ts_ms for record in arm_records])
        self.assertEqual(
            ["Mini decode @8s", "Mini decode @16s"],
            [record.checkpoint_text for record in arm_records],
        )
        self.assertEqual(
            [
                ("a3-budget", 0, 8_000),
                ("a3-budget", 1_000, 16_000),
            ],
            adapter.calls,
        )

    def test_a3_emitted_checkpoint_is_immutable(self) -> None:
        from live_note.app.realtime_text_arms import build_realtime_text_arm_records

        records = [
            make_record(
                fixture_id="a3-immutable",
                checkpoint_source="live_draft",
                checkpoint_index=1,
                checkpoint_ts_ms=0,
                checkpoint_text="Draft at 0s",
                execution_target="remote",
            ),
            make_record(
                fixture_id="a3-immutable",
                checkpoint_source="live_draft",
                checkpoint_index=2,
                checkpoint_ts_ms=8_000,
                checkpoint_text="Draft at 8s",
                execution_target="remote",
            ),
            make_record(
                fixture_id="a3-immutable",
                checkpoint_source="live_draft",
                checkpoint_index=3,
                checkpoint_ts_ms=16_000,
                checkpoint_text="Draft at 16s",
                execution_target="remote",
            ),
            make_record(
                fixture_id="a3-immutable",
                checkpoint_source="live_draft",
                checkpoint_index=4,
                checkpoint_ts_ms=24_000,
                checkpoint_text="Draft at 24s",
                execution_target="remote",
            ),
        ]
        adapter = StubMiniRefineDecodeAdapter(
            [
                "Checkpoint 1",
                "Checkpoint 2 rewritten",
                "Checkpoint 3 latest",
            ]
        )

        arm_records = build_realtime_text_arm_records(
            records,
            Arm.A3_MINI_REFINE_RECENT_WINDOW,
            mini_refine_decode_adapter=adapter,
        )

        self.assertEqual(3, len(arm_records))
        self.assertEqual(
            ["Checkpoint 1", "Checkpoint 2 rewritten", "Checkpoint 3 latest"],
            [record.checkpoint_text for record in arm_records],
        )
        self.assertEqual("Checkpoint 1", arm_records[0].checkpoint_text)
        self.assertEqual("Checkpoint 2 rewritten", arm_records[1].checkpoint_text)
        self.assertEqual("Checkpoint 3 latest", arm_records[2].checkpoint_text)


if __name__ == "__main__":
    unittest.main()
