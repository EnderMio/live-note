from __future__ import annotations

from pathlib import Path

from live_note.domain import SessionMetadata
from live_note.runtime.remote_session_projections import get_remote_session_projection
from live_note.runtime.supervisors.runtime_host import RuntimeHost
from live_note.session_workspace import SessionWorkspace


def create_session(
    root_dir: Path,
    metadata: SessionMetadata,
) -> SessionMetadata:
    return RuntimeHost.for_root(root_dir).session_supervisor.create_or_load(metadata).to_metadata()


def create_workspace_session(
    root_dir: Path,
    metadata: SessionMetadata,
) -> SessionWorkspace:
    workspace = SessionWorkspace.create(Path(metadata.session_dir), metadata)
    workspace.write_session(create_session(root_dir, metadata))
    return workspace


def apply_workspace_session_command(
    root_dir: Path,
    workspace: SessionWorkspace,
    command_kind: str,
    *,
    payload: dict[str, object] | None = None,
) -> SessionMetadata:
    session_id = workspace.root.name
    host = RuntimeHost.for_root(root_dir)
    if host.sessions.get(session_id) is None:
        raise FileNotFoundError(f"session not found in control db: {session_id}")
    metadata = host.session_supervisor.apply_command(
        session_id,
        command_kind,
        payload=payload,
    ).to_metadata()
    workspace.write_session(metadata)
    return metadata


def update_workspace_session(
    root_dir: Path,
    workspace: SessionWorkspace,
    *,
    event_kind: str,
    **changes: object,
) -> SessionMetadata:
    if not changes:
        return require_runtime_session(root_dir, workspace.root.name)
    session_id = workspace.root.name
    host = RuntimeHost.for_root(root_dir)
    if host.sessions.get(session_id) is None:
        raise FileNotFoundError(f"session not found in control db: {session_id}")
    metadata = host.session_supervisor.apply_metadata_changes(
        session_id,
        changes,
        event_kind=event_kind,
    ).to_metadata()
    workspace.write_session(metadata)
    return metadata


def get_runtime_session(root_dir: Path, session_id: str) -> SessionMetadata | None:
    record = RuntimeHost.for_root(root_dir).sessions.get(session_id)
    if record is not None:
        return record.to_metadata()
    projection = get_remote_session_projection(root_dir, session_id)
    if projection is not None:
        return projection.to_metadata()
    return None


def require_runtime_session(root_dir: Path, session_id: str) -> SessionMetadata:
    metadata = get_runtime_session(root_dir, session_id)
    if metadata is None:
        raise FileNotFoundError(f"session not found in control db: {session_id}")
    return metadata
