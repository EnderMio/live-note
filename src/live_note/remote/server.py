from __future__ import annotations

from pathlib import Path
from typing import Any

from live_note.app.journal import SessionWorkspace
from live_note.config import load_config
from live_note.remote.protocol import entry_to_dict, metadata_to_dict

from .api import build_remote_app


def build_session_artifacts_payload(workspace: SessionWorkspace) -> dict[str, Any]:
    metadata = workspace.read_session()
    return {
        "session_id": metadata.session_id,
        "metadata": metadata_to_dict(metadata),
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


def create_remote_app(config):
    return build_remote_app(config)


def serve_remote_app(config_path: Path) -> int:
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError("缺少 uvicorn 依赖。先运行 pip install -e .") from exc

    config = load_config(config_path)
    uvicorn.run(
        build_remote_app(config),
        host=config.serve.host,
        port=config.serve.port,
        log_level="info",
    )
    return 0
