from __future__ import annotations

import queue
import threading
import time
from array import array
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


def _load_sounddevice():
    try:
        import sounddevice as sd
    except ModuleNotFoundError as exc:
        raise AudioCaptureError("缺少 sounddevice 依赖。先运行 pip install -e .") from exc
    return sd


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
        self._thread: threading.Thread | None = None
        self._error: Exception | None = None

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

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        sd = _load_sounddevice()
        blocksize = int(self.config.sample_rate * self.config.frame_duration_ms / 1000)
        channels = min(max(1, self.device.max_input_channels), 2)
        next_started_ms = 0

        def callback(indata, frames, time_info, status) -> None:
            nonlocal next_started_ms
            if status:
                self._error = AudioCaptureError(str(status))
            mono_pcm16 = downmix_pcm16(bytes(indata), channels)
            duration_ms = int(frames * 1000 / self.config.sample_rate)
            frame = AudioFrame(
                started_ms=next_started_ms,
                ended_ms=next_started_ms + duration_ms,
                pcm16=mono_pcm16,
            )
            next_started_ms = frame.ended_ms
            try:
                self.frame_queue.put(frame, timeout=0.5)
            except queue.Full:
                self._error = AudioCaptureError("音频帧队列已满，处理速度跟不上采集速度。")
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
            self._error = exc
