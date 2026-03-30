from __future__ import annotations

import queue
import sys
import unittest
from array import array
from types import SimpleNamespace
from unittest.mock import patch

from live_note.audio.capture import (
    AudioCaptureError,
    AudioCaptureService,
    InputDevice,
    _load_sounddevice,
)
from live_note.config import AudioConfig


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

    def test_audio_capture_emits_input_level_updates_for_non_silent_audio(self) -> None:
        frame_queue: queue.Queue[object] = queue.Queue(maxsize=4)
        service = AudioCaptureService(
            AudioConfig(sample_rate=16000, frame_duration_ms=30),
            InputDevice(index=1, name="Mic", max_input_channels=1, default_samplerate=16000.0),
            frame_queue,
        )
        updates = []

        class _FakeRawInputStream:
            def __init__(self, **kwargs) -> None:
                self._callback = kwargs["callback"]

            def __enter__(self):
                samples = array("h", [0, 16384, -16384] * 160).tobytes()
                self._callback(samples, 480, None, None)
                service.stop()
                return self

            def __exit__(self, exc_type, exc, tb) -> bool:
                return False

        fake_sd = SimpleNamespace(RawInputStream=_FakeRawInputStream)

        with patch("live_note.audio.capture._load_sounddevice", return_value=fake_sd):
            service.set_level_callback(updates.append)
            service._run()

        self.assertEqual(1, len(updates))
        self.assertGreater(updates[0].normalized, 0.45)
        self.assertGreater(updates[0].peak, 0.45)
        self.assertFalse(updates[0].clipping)

    def test_audio_capture_emits_zero_input_level_update_when_paused(self) -> None:
        frame_queue: queue.Queue[object] = queue.Queue(maxsize=4)
        service = AudioCaptureService(
            AudioConfig(sample_rate=16000, frame_duration_ms=30),
            InputDevice(index=1, name="Mic", max_input_channels=1, default_samplerate=16000.0),
            frame_queue,
        )
        updates = []

        class _FakeRawInputStream:
            def __init__(self, **kwargs) -> None:
                self._callback = kwargs["callback"]

            def __enter__(self):
                loud_samples = array("h", [0, 16384, -16384] * 160).tobytes()
                self._callback(loud_samples, 480, None, None)
                service.pause()
                self._callback(loud_samples, 480, None, None)
                service.stop()
                return self

            def __exit__(self, exc_type, exc, tb) -> bool:
                return False

        fake_sd = SimpleNamespace(RawInputStream=_FakeRawInputStream)

        with patch("live_note.audio.capture._load_sounddevice", return_value=fake_sd):
            service.set_level_callback(updates.append)
            service._run()

        self.assertEqual(2, len(updates))
        self.assertGreater(updates[0].normalized, 0.45)
        self.assertEqual(0.0, updates[1].normalized)
        self.assertEqual(0.0, updates[1].peak)
        self.assertFalse(updates[1].clipping)

    def test_audio_capture_aborts_callback_immediately_when_frame_queue_is_full(self) -> None:
        frame_queue: queue.Queue[object] = queue.Queue(maxsize=1)
        frame_queue.put(object())
        service = AudioCaptureService(
            AudioConfig(sample_rate=16000, frame_duration_ms=30),
            InputDevice(index=1, name="Mic", max_input_channels=1, default_samplerate=16000.0),
            frame_queue,
        )
        aborts: list[str] = []

        class _CallbackAbort(Exception):
            pass

        class _CallbackStop(Exception):
            pass

        class _FakeRawInputStream:
            def __init__(self, **kwargs) -> None:
                self._callback = kwargs["callback"]

            def __enter__(self):
                try:
                    self._callback(b"\x00\x00" * 480, 480, None, None)
                except _CallbackAbort:
                    aborts.append("abort")
                except _CallbackStop:
                    aborts.append("stop")
                service.stop()
                return self

            def __exit__(self, exc_type, exc, tb) -> bool:
                return False

        fake_sd = SimpleNamespace(
            RawInputStream=_FakeRawInputStream,
            CallbackAbort=_CallbackAbort,
            CallbackStop=_CallbackStop,
        )

        with patch("live_note.audio.capture._load_sounddevice", return_value=fake_sd):
            service._run()

        self.assertIsInstance(service.error, AudioCaptureError)
        self.assertRegex(str(service.error), "音频帧队列已满")
        self.assertEqual(["abort"], aborts)
