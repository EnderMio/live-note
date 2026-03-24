from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class SessionSummary:
    session_id: str
    title: str
    kind: str
    input_mode: str
    started_at: str
    status: str
    segment_count: int
    transcribed_count: int
    failed_count: int
    latest_error: str | None
    transcript_source: str
    refine_status: str
    execution_target: str
    speaker_status: str
    session_dir: Path
    transcript_file: Path
    structured_file: Path


class SessionQueryService:
    def __init__(
        self,
        *,
        load_config: Callable[[], Any],
        iter_session_roots: Callable[[Path], Iterable[Path]],
        workspace_loader: Any,
    ):
        self._load_config = load_config
        self._iter_session_roots = iter_session_roots
        self._workspace_loader = workspace_loader

    def list_session_summaries(self) -> list[SessionSummary]:
        try:
            config = self._load_config()
        except Exception:
            return []

        items: list[SessionSummary] = []
        for root in self._iter_session_roots(config.root_dir):
            try:
                workspace = self._workspace_loader.load(root)
                metadata = workspace.read_session()
                states = workspace.rebuild_segment_states()
            except Exception as exc:
                items.append(_build_broken_session_summary(root, exc))
                continue

            latest_error = next((state.error for state in reversed(states) if state.error), None)
            items.append(
                SessionSummary(
                    session_id=metadata.session_id,
                    title=metadata.title,
                    kind=metadata.kind,
                    input_mode=metadata.input_mode,
                    started_at=metadata.started_at,
                    status=metadata.status,
                    segment_count=len(states),
                    transcribed_count=sum(1 for state in states if state.text),
                    failed_count=sum(1 for state in states if state.error),
                    latest_error=latest_error,
                    transcript_source=metadata.transcript_source,
                    refine_status=metadata.refine_status,
                    execution_target=metadata.execution_target,
                    speaker_status=metadata.speaker_status,
                    session_dir=workspace.root,
                    transcript_file=workspace.transcript_md,
                    structured_file=workspace.structured_md,
                )
            )
        return sorted(items, key=lambda item: item.started_at, reverse=True)


def _build_broken_session_summary(root: Path, exc: Exception) -> SessionSummary:
    return SessionSummary(
        session_id=root.name,
        title=f"{root.name}（损坏会话）",
        kind="broken",
        input_mode="broken",
        started_at="",
        status="broken",
        segment_count=0,
        transcribed_count=0,
        failed_count=1,
        latest_error=str(exc),
        transcript_source="unknown",
        refine_status="unknown",
        execution_target="unknown",
        speaker_status="unknown",
        session_dir=root,
        transcript_file=root / "transcript.md",
        structured_file=root / "structured.md",
    )
