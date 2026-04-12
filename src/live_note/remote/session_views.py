from __future__ import annotations

from dataclasses import dataclass

from live_note.config import AppConfig
from live_note.runtime.store import ControlDb, SessionProjectionRepo
from live_note.runtime.supervisors.runtime_host import RuntimeHost
from live_note.session_workspace import build_workspace

from .protocol import entry_to_dict, metadata_to_dict


@dataclass(frozen=True, slots=True)
class RemoteSessionViews:
    config: AppConfig
    server_id: str

    def health_payload(self) -> dict[str, object]:
        return {
            "status": "ok",
            "service": "live-note-remote",
            "speaker_enabled": self.config.speaker.enabled,
            "funasr_enabled": self.config.funasr.enabled,
            "supports_imports": True,
            "supports_tasks": True,
            "server_id": self.server_id,
            "realtime_backend": "funasr" if self.config.funasr.enabled else "whisper_cpp",
            "remote_enabled": self.config.remote.enabled,
        }

    def list_sessions_payload(self) -> list[dict[str, object]]:
        host = RuntimeHost.for_root(self.config.root_dir)
        stats_by_session_id = {
            record.session_id: record
            for record in SessionProjectionRepo(ControlDb.for_root(self.config.root_dir)).list_all()
        }
        items: list[dict[str, object]] = []
        for record in host.sessions.list_all():
            projection = stats_by_session_id.get(record.session_id)
            items.append(
                {
                    "session_id": record.session_id,
                    "title": record.title,
                    "kind": record.kind,
                    "status": record.display_status,
                    "runtime_status": record.runtime_status,
                    "started_at": record.started_at,
                    "updated_at": record.updated_at or record.started_at,
                    "execution_target": record.execution_target,
                    "speaker_status": record.speaker_status,
                    "entry_count": projection.transcribed_count if projection is not None else 0,
                }
            )
        return items

    def session_payload(self, session_id: str) -> dict[str, object]:
        host = RuntimeHost.for_root(self.config.root_dir)
        record = host.sessions.get(session_id)
        if record is None:
            raise FileNotFoundError(f"未找到会话: {session_id}")
        return {
            **metadata_to_dict(record.to_metadata()),
            "runtime_status": record.runtime_status,
            "updated_at": record.updated_at or record.started_at,
        }

    def artifacts_payload(self, session_id: str) -> dict[str, object]:
        host = RuntimeHost.for_root(self.config.root_dir)
        record = host.sessions.get(session_id)
        if record is None:
            raise FileNotFoundError(f"未找到会话: {session_id}")
        workspace = build_workspace(self.config.root_dir, session_id)
        return {
            "session_id": session_id,
            "metadata": metadata_to_dict(record.to_metadata()),
            "runtime_status": record.runtime_status,
            "updated_at": record.updated_at or record.started_at,
            "entries": [entry_to_dict(item) for item in workspace.transcript_entries()],
            "has_session_audio": workspace.session_live_wav.exists(),
            "transcript_content": (
                workspace.transcript_md.read_text(encoding="utf-8")
                if workspace.transcript_md.exists()
                else ""
            ),
            "structured_content": (
                workspace.structured_md.read_text(encoding="utf-8")
                if workspace.structured_md.exists()
                else ""
            ),
        }
