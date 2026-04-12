from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import replace
from typing import Protocol

from live_note.config import AppConfig
from live_note.runtime.domain.task_state import TaskRecord
from live_note.runtime.types import LiveRunner, ProgressCallback


class TaskRunner(Protocol):
    def run(self) -> int: ...


class LiveTaskRunner(LiveRunner, Protocol):
    def run(self) -> int: ...


def build_local_live_runner(
    *,
    config: AppConfig,
    title: str,
    source: str,
    kind: str,
    language: str | None,
    on_progress: ProgressCallback | None,
    auto_refine_after_live: bool | None,
) -> LiveTaskRunner:
    from live_note.runtime.local_runners import LocalLiveRunner

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
    return LocalLiveRunner(**kwargs)


def build_local_import_runner(
    *,
    config: AppConfig,
    file_path: str,
    title: str | None,
    kind: str,
    language: str | None,
    on_progress: ProgressCallback | None,
    cancel_event: threading.Event | None,
) -> TaskRunner:
    from live_note.runtime.local_runners import LocalImportRunner

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
    return LocalImportRunner(**kwargs)


def run_local_postprocess(
    *,
    config: AppConfig,
    session_id: str,
    on_progress: ProgressCallback | None,
    speaker_enabled: bool | None,
    recover_from_spool: bool,
) -> int:
    from live_note.runtime.session_workflows import postprocess_session

    return postprocess_session(
        config,
        session_id,
        on_progress=on_progress,
        speaker_enabled=speaker_enabled,
        recover_from_spool=recover_from_spool,
    )


class TaskRunnerFactory:
    def __init__(
        self,
        *,
        load_config: Callable[[], AppConfig],
    ) -> None:
        self._load_config = load_config

    def build_live_runner(
        self,
        *,
        title: str,
        source: str,
        kind: str,
        language: str | None,
        on_progress: ProgressCallback | None,
        auto_refine_after_live: bool | None,
        speaker_enabled: bool | None,
    ) -> LiveTaskRunner:
        config = self._load_config()
        if speaker_enabled is not None:
            config = replace(config, speaker=replace(config.speaker, enabled=bool(speaker_enabled)))
        if getattr(getattr(config, "remote", None), "enabled", False):
            from live_note.remote.live_runner import RemoteLiveRunner

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
            return RemoteLiveRunner(**kwargs)
        return build_local_live_runner(
            config=config,
            title=title,
            source=source,
            kind=kind,
            language=language,
            on_progress=on_progress,
            auto_refine_after_live=auto_refine_after_live,
        )

    def build_import_runner(
        self,
        *,
        file_path: str,
        title: str | None,
        kind: str,
        language: str | None,
        on_progress: ProgressCallback | None,
        cancel_event: threading.Event | None,
        speaker_enabled: bool | None,
    ) -> TaskRunner:
        config = self._load_config()
        if speaker_enabled is not None:
            config = replace(config, speaker=replace(config.speaker, enabled=bool(speaker_enabled)))
        if getattr(getattr(config, "remote", None), "enabled", False):
            raise RuntimeError("remote import 必须直接提交远端任务，不能进入本地 runtime queue。")
        return build_local_import_runner(
            config=config,
            file_path=file_path,
            title=title,
            kind=kind,
            language=language,
            on_progress=on_progress,
            cancel_event=cancel_event,
        )

    def run_task_record(
        self,
        record: TaskRecord,
        *,
        on_progress: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
        on_live_runner: Callable[[LiveTaskRunner | None], None] | None = None,
    ) -> int:
        return self.run_task_action(
            action=record.action,
            payload=record.payload,
            on_progress=on_progress,
            cancel_event=cancel_event,
            on_live_runner=on_live_runner,
        )

    def run_task_action(
        self,
        *,
        action: str,
        payload: dict[str, object],
        on_progress: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
        on_live_runner: Callable[[LiveTaskRunner | None], None] | None = None,
    ) -> int:
        if action == "live":
            language = payload.get("language")
            auto_refine_after_live = payload.get("auto_refine_after_live")
            speaker_enabled = payload.get("speaker_enabled")
            runner = self.build_live_runner(
                title=str(payload["title"]),
                source=str(payload["source"]),
                kind=str(payload.get("kind") or "generic"),
                language=language if isinstance(language, str) else None,
                on_progress=on_progress,
                auto_refine_after_live=(
                    bool(auto_refine_after_live)
                    if isinstance(auto_refine_after_live, bool)
                    else None
                ),
                speaker_enabled=(
                    bool(speaker_enabled) if isinstance(speaker_enabled, bool) else None
                ),
            )
            if callable(on_live_runner):
                on_live_runner(runner)
            try:
                return runner.run()
            finally:
                if callable(on_live_runner):
                    on_live_runner(None)
        if action == "import":
            language = payload.get("language")
            speaker_enabled = payload.get("speaker_enabled")
            kwargs = dict(
                file_path=str(payload["file_path"]),
                title=payload.get("title") or None,
                kind=str(payload.get("kind") or "generic"),
                language=language if isinstance(language, str) else None,
                on_progress=on_progress,
                speaker_enabled=(
                    bool(speaker_enabled) if isinstance(speaker_enabled, bool) else None
                ),
            )
            if cancel_event is not None:
                kwargs["cancel_event"] = cancel_event
            return self.build_import_runner(**kwargs).run()
        from live_note.runtime.session_workflows import (
            finalize_session,
            merge_sessions,
            refine_session,
            republish_session,
            retranscribe_session,
            sync_session_notes,
        )

        config = self._load_config()
        if action == "merge":
            return merge_sessions(
                config,
                [str(item) for item in payload.get("session_ids", [])],
                title=payload.get("title") if isinstance(payload.get("title"), str) else None,
                on_progress=on_progress,
            )
        if action == "postprocess":
            session_id = str(payload["session_id"])
            speaker_enabled = payload.get("speaker_enabled")
            recover_from_spool = payload.get("recover_from_spool")
            return run_local_postprocess(
                config=config,
                session_id=session_id,
                on_progress=on_progress,
                speaker_enabled=(
                    bool(speaker_enabled) if isinstance(speaker_enabled, bool) else None
                ),
                recover_from_spool=bool(recover_from_spool),
            )
        if action == "retranscribe":
            return retranscribe_session(config, str(payload["session_id"]), on_progress=on_progress)
        if action == "refine":
            return refine_session(config, str(payload["session_id"]), on_progress=on_progress)
        if action == "republish":
            return republish_session(config, str(payload["session_id"]), on_progress=on_progress)
        if action == "resync_notes":
            return sync_session_notes(config, str(payload["session_id"]), on_progress=on_progress)
        if action == "finalize":
            return finalize_session(config, str(payload["session_id"]), on_progress=on_progress)
        raise RuntimeError(f"未知任务类型：{action}")
