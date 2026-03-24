from __future__ import annotations

from collections.abc import Callable

from live_note.audio.capture import (
    InputDevice,
)
from live_note.audio.capture import (
    list_input_devices as capture_list_input_devices,
)


class InputDeviceService:
    def __init__(
        self,
        *,
        list_input_devices: Callable[[], list[InputDevice]] = capture_list_input_devices,
    ):
        self._list_input_devices = list_input_devices

    def list_input_devices(self) -> list[InputDevice]:
        return self._list_input_devices()
