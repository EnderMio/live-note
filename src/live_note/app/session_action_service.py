from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from live_note.config import AppConfig

from .events import ProgressCallback


class SessionActionService:
    def __init__(
        self,
        *,
        load_config: Callable[[], AppConfig],
        remote_tasks_path: Callable[[], Path],
        workspace_loader: object,
        remote_client_factory: Callable[[object], object],
        upsert_pending_remote_task: Callable[..., object],
        upsert_remote_task_payload: Callable[..., object],
        merge_sessions: Callable[..., int],
        republish_session: Callable[..., int],
        sync_session_notes: Callable[..., int],
        retranscribe_session: Callable[..., int],
        refine_session: Callable[..., int],
        finalize_session: Callable[..., int],
    ):
        self._load_config = load_config
        self._remote_tasks_path = remote_tasks_path
        self._workspace_loader = workspace_loader
        self._remote_client_factory = remote_client_factory
        self._upsert_pending_remote_task = upsert_pending_remote_task
        self._upsert_remote_task_payload = upsert_remote_task_payload
        self._merge_sessions = merge_sessions
        self._republish_session = republish_session
        self._sync_session_notes = sync_session_notes
        self._retranscribe_session = retranscribe_session
        self._refine_session = refine_session
        self._finalize_session = finalize_session

    def merge(
        self,
        session_ids: list[str],
        *,
        title: str | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> int:
        return self._merge_sessions(
            self._load_config(),
            session_ids,
            title=title,
            on_progress=on_progress,
        )

    def republish(
        self,
        session_id: str,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> int:
        return self._republish_session(self._load_config(), session_id, on_progress=on_progress)

    def finalize(self, session_id: str, *, on_progress: ProgressCallback | None = None) -> int:
        return self._finalize_session(self._load_config(), session_id, on_progress=on_progress)

    def resync_notes(
        self,
        session_id: str,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> int:
        return self._sync_session_notes(self._load_config(), session_id, on_progress=on_progress)

    def retranscribe(self, session_id: str, *, on_progress: ProgressCallback | None = None) -> int:
        config = self._load_config()
        workspace = self._workspace_loader.load(
            config.root_dir / ".live-note" / "sessions" / session_id
        )
        metadata = workspace.read_session()
        if metadata.execution_target == "remote":
            remote_session_id = metadata.remote_session_id or metadata.session_id
            request_id = f"retranscribe-{uuid4().hex[:12]}"
            self._upsert_pending_remote_task(
                self._remote_tasks_path(),
                action="retranscribe",
                label="重转写并重写",
                request_id=request_id,
                session_id=remote_session_id,
            )
            client = self._remote_client_factory(config.remote)
            payload = client.retranscribe(remote_session_id, request_id=request_id)
            self._upsert_remote_task_payload(
                self._remote_tasks_path(),
                payload,
                fallback_request_id=request_id,
                fallback_session_id=remote_session_id,
                fallback_label="重转写并重写",
            )
            return 0
        return self._retranscribe_session(config, session_id, on_progress=on_progress)

    def refine(self, session_id: str, *, on_progress: ProgressCallback | None = None) -> int:
        config = self._load_config()
        workspace = self._workspace_loader.load(
            config.root_dir / ".live-note" / "sessions" / session_id
        )
        metadata = workspace.read_session()
        if metadata.execution_target == "remote":
            remote_session_id = metadata.remote_session_id or metadata.session_id
            request_id = f"refine-{uuid4().hex[:12]}"
            self._upsert_pending_remote_task(
                self._remote_tasks_path(),
                action="refine",
                label="离线精修并重写",
                request_id=request_id,
                session_id=remote_session_id,
            )
            client = self._remote_client_factory(config.remote)
            payload = client.refine(remote_session_id, request_id=request_id)
            self._upsert_remote_task_payload(
                self._remote_tasks_path(),
                payload,
                fallback_request_id=request_id,
                fallback_session_id=remote_session_id,
                fallback_label="离线精修并重写",
            )
            return 0
        return self._refine_session(config, session_id, on_progress=on_progress)
