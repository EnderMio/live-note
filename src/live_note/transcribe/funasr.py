from __future__ import annotations

import json
from collections.abc import Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any

from live_note.config import FunAsrConfig


class FunAsrError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class FunAsrMessage:
    text: str
    mode: str
    is_final: bool
    wav_name: str | None
    raw_payload: dict[str, Any]
    timestamp_ms: tuple[tuple[int, int], ...] = ()
    sentence_spans_ms: tuple[tuple[int, int], ...] = ()

    @property
    def bounds_ms(self) -> tuple[int, int] | None:
        if self.sentence_spans_ms:
            start = self.sentence_spans_ms[0][0]
            end = self.sentence_spans_ms[-1][1]
            return (start, max(end, start + 1))
        if self.timestamp_ms:
            start = self.timestamp_ms[0][0]
            end = self.timestamp_ms[-1][1]
            return (start, max(end, start + 1))
        return None


class FunAsrLiveConnection(AbstractContextManager["FunAsrLiveConnection"]):
    def __init__(self, websocket: Any, config: FunAsrConfig) -> None:
        self._websocket = websocket
        self._config = config

    def start_stream(
        self,
        *,
        wav_name: str,
        sample_rate: int,
        chunk_size: Sequence[int] = (5, 10, 5),
        chunk_interval: int = 10,
        encoder_chunk_look_back: int = 4,
        decoder_chunk_look_back: int = 0,
    ) -> None:
        self._websocket.send(
            json.dumps(
                {
                    "mode": self._config.mode,
                    "chunk_size": [int(item) for item in chunk_size],
                    "chunk_interval": int(chunk_interval),
                    "encoder_chunk_look_back": int(encoder_chunk_look_back),
                    "decoder_chunk_look_back": int(decoder_chunk_look_back),
                    "audio_fs": int(sample_rate),
                    "wav_name": wav_name,
                    "is_speaking": True,
                    "itn": bool(self._config.use_itn),
                },
                ensure_ascii=False,
            )
        )

    def send_audio(self, pcm16: bytes) -> None:
        self._websocket.send(pcm16)

    def send_stop(self) -> None:
        self._websocket.send(json.dumps({"is_speaking": False}, ensure_ascii=False))

    def recv_message(self, timeout: float | None = None) -> FunAsrMessage:
        try:
            payload = self._websocket.recv(timeout=timeout)
        except TimeoutError:
            raise
        except Exception as exc:
            raise FunAsrError(f"FunASR 连接已关闭：{exc}") from exc
        if payload is None:
            raise FunAsrError("FunASR 连接已关闭。")
        if isinstance(payload, bytes):
            raise FunAsrError("FunASR 返回了意外的二进制消息。")
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise FunAsrError("FunASR 返回了无效 JSON。") from exc
        if not isinstance(data, dict):
            raise FunAsrError("FunASR 返回了无效消息格式。")
        return FunAsrMessage(
            text=str(data.get("text") or ""),
            mode=str(data.get("mode") or self._config.mode),
            is_final=bool(data.get("is_final", False)),
            wav_name=str(data["wav_name"]) if data.get("wav_name") is not None else None,
            raw_payload=data,
            timestamp_ms=_parse_timestamp_pairs(data.get("timestamp")),
            sentence_spans_ms=_parse_sentence_spans(data.get("stamp_sents")),
        )

    def close(self) -> None:
        close = getattr(self._websocket, "close", None)
        if close is not None:
            close()

    def __exit__(self, exc_type, exc, exc_tb) -> None:
        self.close()
        return None


class FunAsrWebSocketClient:
    def __init__(self, config: FunAsrConfig) -> None:
        self._config = config

    def connect_live(self) -> FunAsrLiveConnection:
        try:
            from websockets.sync.client import connect
        except ImportError as exc:
            raise FunAsrError("缺少 websockets 依赖，先运行 pip install -e .") from exc
        websocket = connect(
            _websocket_url(self._config.base_url),
            open_timeout=10,
            close_timeout=10,
            ping_interval=None,
            subprotocols=["binary"],
        )
        return FunAsrLiveConnection(websocket, self._config)


def _websocket_url(base_url: str) -> str:
    if base_url.startswith("https://"):
        return f"wss://{base_url.removeprefix('https://')}"
    if base_url.startswith("http://"):
        return f"ws://{base_url.removeprefix('http://')}"
    if base_url.startswith("wss://") or base_url.startswith("ws://"):
        return base_url
    return f"ws://{base_url}"


def _parse_timestamp_pairs(value: Any) -> tuple[tuple[int, int], ...]:
    raw = _parse_json_like(value)
    if not isinstance(raw, list):
        return ()
    pairs: list[tuple[int, int]] = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        start = _to_milliseconds(item[0])
        end = _to_milliseconds(item[1])
        if start is None or end is None:
            continue
        start_ms = min(start, end)
        end_ms = max(start, end)
        pairs.append((start_ms, max(end_ms, start_ms + 1)))
    return tuple(sorted(pairs, key=lambda item: (item[0], item[1])))


def _parse_sentence_spans(value: Any) -> tuple[tuple[int, int], ...]:
    raw = _parse_json_like(value)
    if not isinstance(raw, list):
        return ()
    spans: list[tuple[int, int]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        start = _to_milliseconds(item.get("start"))
        end = _to_milliseconds(item.get("end"))
        if start is None or end is None:
            continue
        start_ms = min(start, end)
        end_ms = max(start, end)
        spans.append((start_ms, max(end_ms, start_ms + 1)))
    return tuple(sorted(spans, key=lambda item: (item[0], item[1])))


def _parse_json_like(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return value


def _to_milliseconds(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(round(float(value)))
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(round(float(text)))
    except ValueError:
        return None
