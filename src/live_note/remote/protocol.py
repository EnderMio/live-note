from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from live_note.app.events import ProgressEvent
from live_note.domain import SessionMetadata, TranscriptEntry


@dataclass(frozen=True, slots=True)
class LiveStartRequest:
    title: str
    kind: str
    language: str
    source_label: str
    source_ref: str
    auto_refine_after_live: bool | None = None
    speaker_enabled: bool | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> LiveStartRequest:
        auto_refine_after_live = payload.get("auto_refine_after_live")
        if auto_refine_after_live is not None:
            auto_refine_after_live = _optional_bool(auto_refine_after_live)
        speaker_enabled = payload.get("speaker_enabled")
        if speaker_enabled is not None:
            speaker_enabled = _optional_bool(speaker_enabled)
        return cls(
            title=str(payload.get("title", "")).strip(),
            kind=str(payload.get("kind", "generic")).strip() or "generic",
            language=str(payload.get("language", "auto")).strip() or "auto",
            source_label=str(payload.get("source_label", "remote-audio")).strip()
            or "remote-audio",
            source_ref=str(payload.get("source_ref", "remote")).strip() or "remote",
            auto_refine_after_live=auto_refine_after_live,
            speaker_enabled=speaker_enabled,
        )


def metadata_to_dict(metadata: SessionMetadata) -> dict[str, Any]:
    return asdict(metadata)


def metadata_from_dict(payload: dict[str, Any]) -> SessionMetadata:
    data = dict(payload)
    session_id = str(data.get("session_id", "")).strip()
    data.setdefault("transcript_note_path", "")
    data.setdefault("structured_note_path", "")
    data.setdefault("session_dir", "")
    data.setdefault("status", "pending")
    data.setdefault("transcript_source", "live")
    data.setdefault("refine_status", "disabled")
    data.setdefault("execution_target", "remote")
    data.setdefault("remote_session_id", session_id or None)
    data.setdefault("speaker_status", "disabled")
    return SessionMetadata(**data)


def entry_to_dict(entry: TranscriptEntry) -> dict[str, Any]:
    return asdict(entry)


def entry_from_dict(payload: dict[str, Any]) -> TranscriptEntry:
    return TranscriptEntry(**payload)


def progress_to_payload(event: ProgressEvent) -> dict[str, Any]:
    return {
        "type": "progress",
        "stage": event.stage,
        "message": event.message,
        "session_id": event.session_id,
        "current": event.current,
        "total": event.total,
        "error": event.error,
    }


def progress_from_payload(payload: dict[str, Any]) -> ProgressEvent:
    return ProgressEvent(
        stage=str(payload["stage"]),
        message=str(payload["message"]),
        session_id=_optional_string(payload.get("session_id")),
        current=_optional_int(payload.get("current")),
        total=_optional_int(payload.get("total")),
        error=_optional_string(payload.get("error")),
    )


def websocket_url(base_url: str, path: str) -> str:
    if base_url.startswith("https://"):
        return f"wss://{base_url.removeprefix('https://')}{path}"
    if base_url.startswith("http://"):
        return f"ws://{base_url.removeprefix('http://')}{path}"
    if base_url.startswith("wss://") or base_url.startswith("ws://"):
        return f"{base_url.rstrip('/')}{path}"
    return f"ws://{base_url.rstrip('/')}{path}"


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return bool(value)
