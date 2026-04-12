from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from live_note.runtime.domain.commands import CommandRecord
from live_note.runtime.live_control import (
    LIVE_TASK_PAUSE_REQUESTED,
    LIVE_TASK_RESUME_REQUESTED,
    LIVE_TASK_STARTED,
    LIVE_TASK_STOP_REQUESTED,
    append_live_control_command,
    derive_live_control_state,
)
from live_note.runtime.read_model import get_live_task_control
from live_note.runtime.store import LogRepo
from live_note.runtime.supervisors import RuntimeHost


class RuntimeLiveControlTests(unittest.TestCase):
    def test_live_control_state_resets_at_latest_start_marker(self) -> None:
        commands = [
            CommandRecord(
                command_id="cmd-1",
                kind=LIVE_TASK_STARTED,
                task_id="task-1",
                created_at="2026-04-10T00:00:00+00:00",
                sequence=1,
            ),
            CommandRecord(
                command_id="cmd-2",
                kind=LIVE_TASK_PAUSE_REQUESTED,
                task_id="task-1",
                created_at="2026-04-10T00:00:01+00:00",
                sequence=2,
            ),
            CommandRecord(
                command_id="cmd-3",
                kind=LIVE_TASK_STARTED,
                task_id="task-1",
                created_at="2026-04-10T00:10:00+00:00",
                sequence=3,
            ),
            CommandRecord(
                command_id="cmd-4",
                kind=LIVE_TASK_STOP_REQUESTED,
                task_id="task-1",
                created_at="2026-04-10T00:10:01+00:00",
                sequence=4,
            ),
        ]

        state = derive_live_control_state("task-1", commands)

        self.assertFalse(state.is_paused)
        self.assertTrue(state.stop_requested)
        self.assertEqual(4, state.last_sequence)

    def test_runtime_projection_tracks_live_control_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            host = RuntimeHost.for_root(root)
            host.task_supervisor.submit(
                task_id="task-1",
                action="live",
                label="实时录音",
                payload={},
                resource_keys=("live",),
            )

            append_live_control_command(
                LogRepo(host.db),
                task_id="task-1",
                kind=LIVE_TASK_STARTED,
                created_at="2026-04-10T00:00:00+00:00",
            )
            append_live_control_command(
                LogRepo(host.db),
                task_id="task-1",
                kind=LIVE_TASK_PAUSE_REQUESTED,
                created_at="2026-04-10T00:00:01+00:00",
            )
            paused = get_live_task_control(host.db, "task-1")

            append_live_control_command(
                LogRepo(host.db),
                task_id="task-1",
                kind=LIVE_TASK_RESUME_REQUESTED,
                created_at="2026-04-10T00:00:02+00:00",
            )
            resumed = get_live_task_control(host.db, "task-1")

            append_live_control_command(
                LogRepo(host.db),
                task_id="task-1",
                kind=LIVE_TASK_STOP_REQUESTED,
                created_at="2026-04-10T00:00:03+00:00",
            )
            stopped = get_live_task_control(host.db, "task-1")

        self.assertTrue(paused.is_paused)
        self.assertFalse(paused.stop_requested)
        self.assertFalse(resumed.is_paused)
        self.assertFalse(resumed.stop_requested)
        self.assertTrue(stopped.stop_requested)
        self.assertEqual(4, stopped.last_sequence)
