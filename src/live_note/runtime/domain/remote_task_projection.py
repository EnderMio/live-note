from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RemoteTaskProjectionRecord:
    projection_id: str
    action: str
    label: str
    status: str
    stage: str
    message: str
    updated_at: str
    created_at: str
    attachment_state: str
    remote_task_id: str | None = None
    server_id: str | None = None
    session_id: str | None = None
    request_id: str | None = None
    last_synced_result_version: int = 0
    result_version: int = 0
    last_seen_at: str | None = None
    artifacts_synced_at: str | None = None
    last_error: str | None = None
    current: int | None = None
    total: int | None = None
    can_cancel: bool = False
