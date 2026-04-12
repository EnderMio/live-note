from __future__ import annotations

from pathlib import Path
from typing import Protocol


class QueueTaskLike(Protocol):
    action: str
    label: str
    payload: dict[str, object]


def queue_target_text(record: QueueTaskLike) -> str:
    payload = record.payload
    if record.action == "import":
        return Path(str(payload.get("file_path", ""))).name or "本地文件"
    if record.action == "merge":
        session_ids = payload.get("session_ids", [])
        if isinstance(session_ids, list):
            return f"{len(session_ids)} 条会话"
        return "多条会话"
    if payload.get("session_id") is not None:
        return str(payload.get("session_id") or "会话")
    return record.label
