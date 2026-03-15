from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from live_note.config import AudioConfig
from live_note.domain import AudioFrame


class VadLike(Protocol):
    def is_speech(self, pcm16: bytes, sample_rate: int) -> bool: ...


@dataclass(frozen=True, slots=True)
class SegmentWindow:
    started_ms: int
    ended_ms: int
    pcm16: bytes


class SegmentationError(RuntimeError):
    pass


class SpeechSegmenter:
    def __init__(self, config: AudioConfig, vad: VadLike | None = None):
        self.config = config
        self.vad = vad or self._load_vad()
        self._started_ms: int | None = None
        self._ended_ms: int | None = None
        self._last_speech_ms: int | None = None
        self._buffer = bytearray()

    def feed(self, frame: AudioFrame) -> list[SegmentWindow]:
        is_speech = self.vad.is_speech(frame.pcm16, self.config.sample_rate)
        if self._started_ms is None:
            if not is_speech:
                return []
            self._started_ms = frame.started_ms
        self._ended_ms = frame.ended_ms
        self._buffer.extend(frame.pcm16)
        if is_speech:
            self._last_speech_ms = frame.ended_ms
        if self._should_finalize():
            return [self._finalize()]
        return []

    def flush(self) -> list[SegmentWindow]:
        if self._started_ms is None or not self._buffer:
            return []
        return [self._finalize()]

    def _should_finalize(self) -> bool:
        if self._started_ms is None or self._ended_ms is None:
            return False
        duration = self._ended_ms - self._started_ms
        if duration >= self.config.max_segment_ms:
            return True
        if duration < self.config.min_segment_ms or self._last_speech_ms is None:
            return False
        trailing_silence = self._ended_ms - self._last_speech_ms
        return trailing_silence >= self.config.silence_ms

    def _finalize(self) -> SegmentWindow:
        if self._started_ms is None or self._ended_ms is None:
            raise SegmentationError("没有可输出的音频片段。")
        segment = SegmentWindow(
            started_ms=self._started_ms,
            ended_ms=self._ended_ms,
            pcm16=bytes(self._buffer),
        )
        self._started_ms = None
        self._ended_ms = None
        self._last_speech_ms = None
        self._buffer.clear()
        return segment

    def _load_vad(self) -> VadLike:
        try:
            import webrtcvad
        except ModuleNotFoundError as exc:
            raise SegmentationError("缺少 webrtcvad-wheels 依赖。先运行 pip install -e .") from exc
        return webrtcvad.Vad(self.config.vad_aggressiveness)
