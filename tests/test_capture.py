from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from live_note.audio.capture import AudioCaptureError, _load_sounddevice


class CaptureTests(unittest.TestCase):
    def test_load_sounddevice_unregisters_module_exit_handler_only_once(self) -> None:
        handler = object()
        fake_sd = SimpleNamespace(_exit_handler=handler)

        with (
            patch.dict(sys.modules, {"sounddevice": fake_sd}),
            patch("live_note.audio.capture.atexit.unregister") as unregister_mock,
        ):
            first = _load_sounddevice()
            second = _load_sounddevice()

        self.assertIs(first, fake_sd)
        self.assertIs(second, fake_sd)
        unregister_mock.assert_called_once_with(handler)
        self.assertTrue(fake_sd._live_note_atexit_disabled)

    def test_load_sounddevice_raises_clear_error_when_module_is_missing(self) -> None:
        original_import = __import__

        def fake_import(name: str, globals=None, locals=None, fromlist=(), level=0):
            if name == "sounddevice":
                raise ModuleNotFoundError("No module named 'sounddevice'")
            return original_import(name, globals, locals, fromlist, level)

        with patch("builtins.__import__", side_effect=fake_import):
            with self.assertRaisesRegex(AudioCaptureError, "缺少 sounddevice 依赖"):
                _load_sounddevice()
