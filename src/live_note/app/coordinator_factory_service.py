from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from live_note.config import AppConfig

from .events import ProgressCallback


class CoordinatorFactoryService:
    def __init__(
        self,
        *,
        load_config: Callable[[], AppConfig],
        local_live_factory: Callable[..., Any],
        remote_live_factory: Callable[..., Any],
        local_import_factory: Callable[..., Any],
        remote_import_factory: Callable[..., Any],
    ):
        self._load_config = load_config
        self._local_live_factory = local_live_factory
        self._remote_live_factory = remote_live_factory
        self._local_import_factory = local_import_factory
        self._remote_import_factory = remote_import_factory

    def create_live_coordinator(
        self,
        *,
        title: str,
        source: str,
        kind: str,
        language: str | None,
        on_progress: ProgressCallback | None,
        auto_refine_after_live: bool | None,
        speaker_enabled: bool | None,
    ) -> Any:
        config = self._load_config()
        if speaker_enabled is not None:
            config = replace(config, speaker=replace(config.speaker, enabled=bool(speaker_enabled)))
        coordinator_cls = (
            self._remote_live_factory
            if getattr(config.remote, "enabled", False)
            else self._local_live_factory
        )
        kwargs = {
            "config": config,
            "title": title,
            "source": source,
            "kind": kind,
            "language": language,
            "on_progress": on_progress,
        }
        if auto_refine_after_live is not None:
            kwargs["auto_refine_after_live"] = auto_refine_after_live
        return coordinator_cls(**kwargs)

    def create_import_coordinator(
        self,
        *,
        file_path: str,
        title: str | None,
        kind: str,
        language: str | None,
        on_progress: ProgressCallback | None,
        cancel_event: threading.Event | None,
        speaker_enabled: bool | None,
    ) -> Any:
        config = self._load_config()
        if speaker_enabled is not None:
            config = replace(config, speaker=replace(config.speaker, enabled=bool(speaker_enabled)))
        coordinator_cls = (
            self._remote_import_factory
            if getattr(getattr(config, "remote", None), "enabled", False)
            else self._local_import_factory
        )
        kwargs = {
            "config": config,
            "file_path": file_path,
            "title": title,
            "kind": kind,
            "language": language,
            "on_progress": on_progress,
        }
        if cancel_event is not None:
            kwargs["cancel_event"] = cancel_event
        return coordinator_cls(**kwargs)
