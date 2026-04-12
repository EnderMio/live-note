from __future__ import annotations

from pathlib import Path

from live_note.config import load_config

from .api import build_remote_app


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
        ws_ping_interval=config.serve.ws_ping_interval_seconds,
        ws_ping_timeout=config.serve.ws_ping_timeout_seconds,
    )
    return 0
