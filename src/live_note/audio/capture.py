from __future__ import annotations

import atexit
import queue
import threading
import time
from array import array
from collections.abc import Callable
from dataclasses import dataclass

from live_note.config import AudioConfig
from live_note.domain import AudioFrame


class AudioCaptureError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class InputDevice:
    index: int
    name: str
    max_input_channels: int
    default_samplerate: float


@dataclass(frozen=True, slots=True)
class InputLevel:
    normalized: float
    peak: float
    clipping: bool


def _load_sounddevice():
    try:
        import sounddevice as sd
    except ModuleNotFoundError as exc:
        raise AudioCaptureError("缺少 sounddevice 依赖。先运行 pip install -e .") from exc
    _disable_sounddevice_atexit(sd)
    return sd


def _disable_sounddevice_atexit(sd) -> None:
    if getattr(sd, "_live_note_atexit_disabled", False):
        return
    exit_handler = getattr(sd, "_exit_handler", None)
    if exit_handler is not None:
        try:
            atexit.unregister(exit_handler)
        except Exception:
            pass
    setattr(sd, "_live_note_atexit_disabled", True)


def list_input_devices() -> list[InputDevice]:
    sd = _load_sounddevice()
    devices: list[InputDevice] = []
    for index, raw_device in enumerate(sd.query_devices()):
        max_channels = int(raw_device["max_input_channels"])
        if max_channels <= 0:
            continue
        devices.append(
            InputDevice(
                index=index,
                name=str(raw_device["name"]),
                max_input_channels=max_channels,
                default_samplerate=float(raw_device["default_samplerate"]),
            )
        )
    return devices


def resolve_input_device(source: str) -> InputDevice:
    devices = list_input_devices()
    if source.isdigit():
        index = int(source)
        for device in devices:
            if device.index == index:
                return device
    lowered = source.lower()
    for device in devices:
        if lowered in device.name.lower():
            return device
    raise AudioCaptureError(f"找不到输入设备: {source}")


def downmix_pcm16(data: bytes, channels: int) -> bytes:
    if channels <= 1:
        return data
    samples = array("h")
    samples.frombytes(data)
    mixed = array("h")
    for start in range(0, len(samples), channels):
        window = samples[start : start + channels]
        mixed.append(int(sum(window) / len(window)))
    return mixed.tobytes()


def measure_input_level(pcm16: bytes) -> InputLevel:
    if not pcm16:
        return InputLevel(normalized=0.0, peak=0.0, clipping=False)
    samples = array("h")
    samples.frombytes(pcm16)
    if not samples:
        return InputLevel(normalized=0.0, peak=0.0, clipping=False)
    peak_sample = max(abs(sample) for sample in samples)
    peak = min(peak_sample / 32767.0, 1.0)
    return InputLevel(
        normalized=peak,
        peak=peak,
        clipping=peak_sample >= 32700,
    )


def describe_input_level(level: InputLevel) -> str:
    if level.clipping or level.peak >= 0.98:
        return "Clipping"
    if level.normalized < 0.015:
        return "No signal"
    if level.normalized < 0.18:
        return "Low"
    if level.normalized < 0.72:
        return "OK"
    return "High"


class AudioCaptureService:
    def __init__(
        self,
        config: AudioConfig,
        device: InputDevice,
        frame_queue: queue.Queue[AudioFrame | object],
    ):
        self.config = config
        self.device = device
        self.frame_queue = frame_queue
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: Exception | None = None
        self._level_callback: Callable[[InputLevel], None] | None = None
        self._smoothed_level = 0.0

    @property
    def error(self) -> Exception | None:
        return self._error

    @property
    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="audio-capture", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def pause(self) -> None:
        self._pause_event.set()

    def resume(self) -> None:
        self._pause_event.clear()

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout=timeout)

    def set_level_callback(self, callback: Callable[[InputLevel], None] | None) -> None:
        self._level_callback = callback

    @property
    def is_paused(self) -> bool:
        return self._pause_event.is_set()

    def _run(self) -> None:
        sd = _load_sounddevice()
        blocksize = int(self.config.sample_rate * self.config.frame_duration_ms / 1000)
        channels = min(max(1, self.device.max_input_channels), 2)
        next_started_ms = 0

        def callback(indata, frames, time_info, status) -> None:
            nonlocal next_started_ms
            if status:
                self._error = AudioCaptureError(str(status))
            if self._pause_event.is_set():
                self._smoothed_level = 0.0
                self._emit_input_level(b"")
                if self._stop_event.is_set():
                    raise sd.CallbackStop()
                return
            mono_pcm16 = downmix_pcm16(bytes(indata), channels)
            self._emit_input_level(mono_pcm16)
            duration_ms = int(frames * 1000 / self.config.sample_rate)
            frame = AudioFrame(
                started_ms=next_started_ms,
                ended_ms=next_started_ms + duration_ms,
                pcm16=mono_pcm16,
            )
            next_started_ms = frame.ended_ms
            try:
                self.frame_queue.put_nowait(frame)
            except queue.Full:
                self._error = AudioCaptureError("音频帧队列已满，处理速度跟不上采集速度。")
                self._stop_event.set()
                callback_abort = getattr(sd, "CallbackAbort", None)
                if callback_abort is not None:
                    raise callback_abort()
                callback_stop = getattr(sd, "CallbackStop", None)
                if callback_stop is not None:
                    raise callback_stop()
                return
            if self._stop_event.is_set():
                raise sd.CallbackStop()

        try:
            with sd.RawInputStream(
                samplerate=self.config.sample_rate,
                blocksize=blocksize,
                device=self.device.index,
                channels=channels,
                dtype="int16",
                callback=callback,
            ):
                while not self._stop_event.is_set():
                    time.sleep(0.1)
        except Exception as exc:  # pragma: no cover - 真实设备异常不稳定
            if self._error is None:
                self._error = exc

    def _emit_input_level(self, pcm16: bytes) -> None:
        if self._level_callback is None:
            return
        measured = measure_input_level(pcm16)
        if measured.normalized >= self._smoothed_level:
            smoothed = measured.normalized
        else:
            smoothed = max(measured.normalized, self._smoothed_level * 0.85)
        self._smoothed_level = smoothed
        self._level_callback(
            InputLevel(
                normalized=smoothed,
                peak=measured.peak,
                clipping=measured.clipping,
            )
        )
