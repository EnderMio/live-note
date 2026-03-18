from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .coordinator import can_reconstruct_session_live_audio
from .journal import SessionWorkspace


class SessionSummaryLike(Protocol):
    session_id: str
    title: str
    input_mode: str
    session_dir: Path
    segment_count: int
    transcribed_count: int
    transcript_source: str
    refine_status: str
    execution_target: str
    speaker_status: str
    latest_error: str | None


@dataclass(frozen=True, slots=True)
class TaskRequest:
    label: str
    action: str
    payload: dict[str, object]


def build_import_task_request(
    *,
    file_path: Path,
    title: str | None,
    kind: str,
    language: str | None,
) -> TaskRequest:
    return TaskRequest(
        label="文件导入",
        action="import",
        payload={
            "file_path": str(file_path),
            "title": title or None,
            "kind": kind,
            "language": language,
        },
    )


def build_session_task_request(
    *,
    label: str,
    operation: str,
    session_id: str,
) -> TaskRequest:
    return TaskRequest(
        label=label,
        action="session_action",
        payload={
            "action": operation,
            "session_id": session_id,
        },
    )


def build_merge_task_request(
    *,
    session_ids: list[str],
    title: str | None = None,
) -> TaskRequest:
    return TaskRequest(
        label="合并会话",
        action="merge",
        payload={
            "session_ids": session_ids,
            "title": title,
        },
    )


def supports_refine(summary: object) -> bool:
    input_mode = getattr(summary, "input_mode", None)
    session_dir = getattr(summary, "session_dir", None)
    if input_mode != "live" or not isinstance(session_dir, Path):
        return False
    if (session_dir / "session.live.wav").exists():
        return True
    try:
        workspace = SessionWorkspace.load(session_dir)
    except Exception:
        return False
    return can_reconstruct_session_live_audio(workspace)


def can_merge_summaries(summaries: list[object] | None) -> bool:
    return bool(
        summaries
        and len(summaries) >= 2
        and all(getattr(summary, "execution_target", "local") == "local" for summary in summaries)
    )


def build_history_detail(summaries: list[SessionSummaryLike] | None) -> str | None:
    if not summaries:
        return None
    if len(summaries) > 1:
        titles = " / ".join(summary.title for summary in summaries[:3])
        if len(summaries) > 3:
            titles = f"{titles} / ..."
        if not can_merge_summaries(list(summaries)):
            return f"已选择 {len(summaries)} 条会话：{titles}。当前选择包含远端会话，不能直接合并。"
        return f"已选择 {len(summaries)} 条会话：{titles}。可执行“合并所选会话”，原始会话会保留。"
    summary = summaries[0]
    return (
        f"Session ID: {summary.session_id} | 已转写 {summary.transcribed_count}/"
        f"{summary.segment_count} | 来源: {summary.transcript_source} | "
        f"精修: {summary.refine_status} | 运行: {summary.execution_target} | "
        f"说话人: {summary.speaker_status} | 最近错误: {summary.latest_error or '无'}"
    )
