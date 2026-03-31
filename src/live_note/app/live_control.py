from __future__ import annotations

import time
from collections.abc import Callable
from typing import Protocol


class Scheduler(Protocol):
    def after(self, delay_ms: int, callback: Callable[[], None]) -> str: ...

    def after_cancel(self, after_id: str) -> None: ...


class LiveRunner(Protocol):
    @property
    def is_paused(self) -> bool: ...

    def request_stop(self) -> None: ...

    def request_pause(self) -> None: ...

    def request_resume(self) -> None: ...


def parse_auto_stop_seconds(raw_value: str) -> int | None:
    trimmed = raw_value.strip()
    if not trimmed:
        return None
    try:
        minutes = float(trimmed)
    except ValueError as exc:
        raise ValueError("自动停止（分钟）需要填写数字，例如 15 或 1.5。") from exc
    if minutes <= 0:
        return None
    return max(1, round(minutes * 60))


class LiveRecordingController:
    def __init__(
        self,
        *,
        scheduler: Scheduler,
        on_auto_stop: Callable[[], None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.scheduler = scheduler
        self.on_auto_stop = on_auto_stop
        self.monotonic = monotonic
        self.runner: LiveRunner | None = None
        self._auto_stop_after_id: str | None = None
        self._auto_stop_deadline: float | None = None
        self._auto_stop_remaining_seconds: float | None = None
        self._auto_stop_paused = False
        self._stop_requested = False

    @property
    def auto_stop_deadline(self) -> float | None:
        return self._auto_stop_deadline

    @property
    def remaining_auto_stop_seconds(self) -> float | None:
        return self._auto_stop_remaining_seconds

    @property
    def is_stopping(self) -> bool:
        return self._stop_requested

    def bind_runner(self, runner: LiveRunner | None) -> None:
        self.runner = runner
        self._stop_requested = False

    def clear(self) -> None:
        self.cancel_auto_stop()
        self.runner = None
        self._stop_requested = False

    def arm_auto_stop(self, seconds: int | None) -> None:
        self.cancel_auto_stop()
        if seconds is None:
            return
        self._auto_stop_remaining_seconds = float(seconds)
        self._auto_stop_deadline = self.monotonic() + seconds
        self._auto_stop_paused = False
        self._schedule_tick()

    def request_stop(self) -> bool:
        if self.runner is None:
            return False
        self.cancel_auto_stop()
        self._stop_requested = True
        self.runner.request_stop()
        return True

    def request_pause(self) -> bool:
        if self.runner is None or getattr(self.runner, "is_paused", False):
            return False
        self.runner.request_pause()
        self.pause_auto_stop()
        return True

    def request_resume(self) -> bool:
        if self.runner is None or not getattr(self.runner, "is_paused", False):
            return False
        self.runner.request_resume()
        self.resume_auto_stop()
        return True

    def cancel_auto_stop(self) -> None:
        self._clear_scheduled_tick()
        self._auto_stop_deadline = None
        self._auto_stop_remaining_seconds = None
        self._auto_stop_paused = False

    def pause_auto_stop(self) -> None:
        deadline = self._auto_stop_deadline
        if deadline is None:
            return
        self._auto_stop_remaining_seconds = max(0.0, deadline - self.monotonic())
        self._auto_stop_deadline = None
        self._auto_stop_paused = True
        self._clear_scheduled_tick()

    def resume_auto_stop(self) -> None:
        remaining_seconds = self._auto_stop_remaining_seconds
        if not self._auto_stop_paused or remaining_seconds is None or remaining_seconds <= 0:
            return
        self._auto_stop_deadline = self.monotonic() + remaining_seconds
        self._auto_stop_paused = False
        self._schedule_tick()

    def _schedule_tick(self) -> None:
        self._clear_scheduled_tick()
        if self._auto_stop_deadline is None or self.runner is None:
            return
        self._auto_stop_after_id = self.scheduler.after(1000, self._on_tick)

    def _on_tick(self) -> None:
        self._auto_stop_after_id = None
        deadline = self._auto_stop_deadline
        if deadline is None or self.runner is None:
            return
        remaining_seconds = deadline - self.monotonic()
        if remaining_seconds <= 0:
            if self.on_auto_stop is not None:
                self.on_auto_stop()
            return
        self._auto_stop_remaining_seconds = remaining_seconds
        self._schedule_tick()

    def _clear_scheduled_tick(self) -> None:
        if self._auto_stop_after_id is None:
            return
        self.scheduler.after_cancel(self._auto_stop_after_id)
        self._auto_stop_after_id = None
