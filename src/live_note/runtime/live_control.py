from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from live_note.runtime.domain.commands import CommandRecord
from live_note.runtime.store import LogRepo
from live_note.utils import iso_now

LIVE_TASK_STARTED = "live_task_started"
LIVE_TASK_STOP_REQUESTED = "live_task_stop_requested"
LIVE_TASK_PAUSE_REQUESTED = "live_task_pause_requested"
LIVE_TASK_RESUME_REQUESTED = "live_task_resume_requested"

_LIVE_CONTROL_COMMAND_KINDS = frozenset(
    {
        LIVE_TASK_STARTED,
        LIVE_TASK_STOP_REQUESTED,
        LIVE_TASK_PAUSE_REQUESTED,
        LIVE_TASK_RESUME_REQUESTED,
    }
)


@dataclass(frozen=True, slots=True)
class LiveControlState:
    task_id: str
    is_paused: bool = False
    stop_requested: bool = False
    last_sequence: int = 0


def append_live_control_command(
    logs: LogRepo,
    *,
    task_id: str,
    kind: str,
    session_id: str | None = None,
    created_at: str | None = None,
    payload: dict[str, object] | None = None,
) -> CommandRecord:
    normalized_task_id = task_id.strip()
    normalized_kind = kind.strip()
    if not normalized_task_id:
        raise ValueError("live control task_id cannot be empty")
    if normalized_kind not in _LIVE_CONTROL_COMMAND_KINDS:
        raise ValueError(f"invalid live control command: {kind}")
    changed_at = created_at or iso_now()
    return logs.append_command(
        CommandRecord(
            command_id=f"{normalized_kind}:{normalized_task_id}:{uuid4().hex[:12]}",
            kind=normalized_kind,
            session_id=session_id,
            task_id=normalized_task_id,
            created_at=changed_at,
            payload=dict(payload or {}),
        )
    )


def get_live_control_state(logs: LogRepo, task_id: str) -> LiveControlState:
    return derive_live_control_state(task_id, logs.list_commands(task_id=task_id))


def derive_live_control_state(
    task_id: str,
    commands: list[CommandRecord],
) -> LiveControlState:
    if not task_id:
        return LiveControlState(task_id="")
    reset_sequence = 0
    for command in commands:
        if command.kind == LIVE_TASK_STARTED and command.sequence is not None:
            reset_sequence = max(reset_sequence, int(command.sequence))
    paused = False
    stop_requested = False
    last_sequence = reset_sequence
    for command in commands:
        sequence = int(command.sequence or 0)
        if sequence < reset_sequence:
            continue
        last_sequence = max(last_sequence, sequence)
        if command.kind == LIVE_TASK_PAUSE_REQUESTED:
            paused = True
            continue
        if command.kind == LIVE_TASK_RESUME_REQUESTED:
            paused = False
            continue
        if command.kind == LIVE_TASK_STOP_REQUESTED:
            stop_requested = True
    return LiveControlState(
        task_id=task_id,
        is_paused=paused,
        stop_requested=stop_requested,
        last_sequence=last_sequence,
    )
