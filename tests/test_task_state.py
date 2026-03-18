from __future__ import annotations

import unittest

from live_note.app.events import ProgressEvent
from live_note.app.task_state import GuiTaskState


class GuiTaskStateTests(unittest.TestCase):
    def test_detach_live_moves_current_task_to_background(self) -> None:
        state = GuiTaskState()
        state.start_live(task_id="task-1", label="实时录音")

        detached_task_id = state.detach_live(session_id="session-1")

        self.assertEqual("task-1", detached_task_id)
        self.assertFalse(state.busy)
        self.assertIsNone(state.current_task_id)
        self.assertEqual({"task-1": "实时录音"}, state.background_tasks)
        self.assertEqual({"task-1": "session-1"}, state.background_task_sessions)

    def test_is_foreground_event_binds_current_session_then_routes_background(self) -> None:
        state = GuiTaskState()
        state.start_live(task_id="task-1", label="实时录音")

        foreground = state.is_foreground_event(
            ProgressEvent(stage="listening", message="前台", session_id="session-1")
        )
        self.assertTrue(foreground)
        self.assertEqual("session-1", state.current_task_session_id)

        state.detach_live(session_id="session-1")
        background = state.is_foreground_event(
            ProgressEvent(stage="transcribing", message="后台", session_id="session-1")
        )

        self.assertFalse(background)

    def test_finish_queue_clears_matching_running_queue_task(self) -> None:
        state = GuiTaskState()
        state.mark_queue_running(task_id="task-q1", label="文件导入")

        state.finish_queue("task-q1")

        self.assertIsNone(state.queue_current_task_id)
        self.assertIsNone(state.queue_current_task_label)

