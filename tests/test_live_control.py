from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from live_note.app.live_control import LiveRecordingController, parse_auto_stop_seconds


class _FakeScheduler:
    def __init__(self) -> None:
        self.calls: dict[str, tuple[int, object]] = {}
        self.sequence = 0

    def after(self, delay_ms: int, callback: object) -> str:
        self.sequence += 1
        after_id = f"after-{self.sequence}"
        self.calls[after_id] = (delay_ms, callback)
        return after_id

    def after_cancel(self, after_id: str) -> None:
        self.calls.pop(after_id, None)


class LiveControlTests(unittest.TestCase):
    def test_parse_auto_stop_seconds_accepts_decimal_minutes(self) -> None:
        self.assertEqual(90, parse_auto_stop_seconds("1.5"))

    def test_parse_auto_stop_seconds_treats_blank_as_disabled(self) -> None:
        self.assertIsNone(parse_auto_stop_seconds(" "))

    def test_request_stop_clears_timer_and_stops_runner(self) -> None:
        scheduler = _FakeScheduler()
        on_elapsed = Mock()
        runner = SimpleNamespace(request_stop=Mock(), request_pause=Mock(), request_resume=Mock())
        controller = LiveRecordingController(
            scheduler=scheduler,
            monotonic=lambda: 10.0,
            on_auto_stop=on_elapsed,
        )
        controller.bind_runner(runner)
        controller.arm_auto_stop(30)

        stopped = controller.request_stop()

        self.assertTrue(stopped)
        runner.request_stop.assert_called_once_with()
        self.assertEqual({}, scheduler.calls)

    def test_pause_and_resume_preserve_remaining_auto_stop_time(self) -> None:
        scheduler = _FakeScheduler()
        monotonic_values = iter([100.0, 112.0, 120.0])
        controller = LiveRecordingController(
            scheduler=scheduler,
            monotonic=lambda: next(monotonic_values),
            on_auto_stop=Mock(),
        )
        controller.bind_runner(
            SimpleNamespace(
                is_paused=False,
                request_pause=Mock(),
                request_resume=Mock(),
            )
        )
        controller.arm_auto_stop(30)

        paused = controller.request_pause()
        self.assertTrue(paused)
        self.assertAlmostEqual(18.0, controller.remaining_auto_stop_seconds or 0.0)
        self.assertEqual({}, scheduler.calls)

        controller.runner.is_paused = True
        resumed = controller.request_resume()

        self.assertTrue(resumed)
        self.assertIsNotNone(controller.auto_stop_deadline)
        self.assertEqual(1, len(scheduler.calls))

    def test_tick_triggers_auto_stop_callback_when_deadline_reached(self) -> None:
        scheduler = _FakeScheduler()
        on_elapsed = Mock()
        monotonic_values = iter([10.0, 16.1])
        controller = LiveRecordingController(
            scheduler=scheduler,
            monotonic=lambda: next(monotonic_values),
            on_auto_stop=on_elapsed,
        )
        controller.bind_runner(SimpleNamespace())
        controller.arm_auto_stop(5)

        scheduled_callback = next(iter(scheduler.calls.values()))[1]
        scheduled_callback()

        on_elapsed.assert_called_once_with()

