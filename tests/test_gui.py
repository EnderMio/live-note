from __future__ import annotations

import tempfile
import threading
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, Mock, patch

from live_note.app.events import ProgressEvent
from live_note.app.gui import (
    LiveNoteGui,
    _build_vertical_scroller,
    _language_code_to_display,
    _normalize_language_value,
    _optional_language_override,
    _summary_supports_refine,
    _wrap_action_rows,
)
from live_note.app.task_queue import QueueLoadResult, build_task_record


class GuiLanguageTests(unittest.TestCase):
    def test_normalize_language_value_maps_mixed_language_label_to_auto(self) -> None:
        self.assertEqual("auto", _normalize_language_value("自动识别 / 中英混合 / 多语言（auto）"))

    def test_normalize_language_value_allows_custom_language_code(self) -> None:
        self.assertEqual("fr", _normalize_language_value("fr"))

    def test_optional_language_override_uses_none_for_default_label(self) -> None:
        self.assertIsNone(_optional_language_override("沿用默认设置"))

    def test_language_code_to_display_returns_known_label(self) -> None:
        self.assertEqual("中文（zh）", _language_code_to_display("zh", allow_blank=False))


class GuiHistoryTests(unittest.TestCase):
    def test_selected_summary_skips_prompt_when_prompt_disabled(self) -> None:
        gui = LiveNoteGui.__new__(LiveNoteGui)
        gui.history_tree = SimpleNamespace(selection=lambda: ())
        gui.history_rows = {}

        with patch("live_note.app.gui.messagebox.showinfo") as showinfo_mock:
            summary = gui._selected_summary(prompt=False)

        self.assertIsNone(summary)
        showinfo_mock.assert_not_called()

    def test_selected_summary_prompts_when_requested(self) -> None:
        gui = LiveNoteGui.__new__(LiveNoteGui)
        gui.history_tree = SimpleNamespace(selection=lambda: ())
        gui.history_rows = {}

        with patch("live_note.app.gui.messagebox.showinfo") as showinfo_mock:
            summary = gui._selected_summary()

        self.assertIsNone(summary)
        showinfo_mock.assert_called_once_with("请选择会话", "请先从历史列表中选择一条会话。")

    def test_selected_summaries_require_multiple_items_when_requested(self) -> None:
        gui = LiveNoteGui.__new__(LiveNoteGui)
        gui.history_tree = SimpleNamespace(selection=lambda: ("one",))
        gui.history_rows = {"one": SimpleNamespace(session_id="one")}

        with patch("live_note.app.gui.messagebox.showinfo") as showinfo_mock:
            summaries = gui._selected_summaries(min_count=2)

        self.assertIsNone(summaries)
        showinfo_mock.assert_called_once_with("请选择会话", "请先从历史列表中选择至少 2 条会话。")

    def test_selected_summaries_return_all_selected_rows(self) -> None:
        gui = LiveNoteGui.__new__(LiveNoteGui)
        first = SimpleNamespace(session_id="one")
        second = SimpleNamespace(session_id="two")
        gui.history_tree = SimpleNamespace(selection=lambda: ("one", "two"))
        gui.history_rows = {"one": first, "two": second}

        summaries = gui._selected_summaries(prompt=False, min_count=2)

        self.assertEqual([first, second], summaries)


class GuiTaskTests(unittest.TestCase):
    def test_summary_supports_refine_when_live_segments_can_be_reconstructed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_dir = Path(temp_dir)
            segments_dir = session_dir / "segments"
            segments_dir.mkdir(parents=True, exist_ok=True)
            wav_path = segments_dir / "seg-00001.wav"
            with wave.open(str(wav_path), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(16000)
                handle.writeframes(b"\x00\x00" * 16000)
            (session_dir / "segments.jsonl").write_text(
                (
                    '{"kind":"segment_created","segment_id":"seg-00001","started_ms":0,'
                    '"ended_ms":1000,"created_at":"2026-03-17T00:00:00+00:00",'
                    '"wav_path":"segments/seg-00001.wav","text":null,"error":null}\n'
                ),
                encoding="utf-8",
            )

            summary = SimpleNamespace(input_mode="live", session_dir=session_dir)

            self.assertTrue(_summary_supports_refine(summary))

    def test_apply_branding_sets_window_icon_when_logo_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            logo_path = Path(temp_dir) / "logo.png"
            logo_path.write_bytes(b"png")
            gui = LiveNoteGui.__new__(LiveNoteGui)
            gui.root = MagicMock()
            image = MagicMock()
            header_image = MagicMock()
            image.subsample.return_value = header_image

            with (
                patch("live_note.app.gui.brand_logo_png_path", return_value=logo_path),
                patch("live_note.app.gui.tk.PhotoImage", return_value=image),
            ):
                gui._apply_branding()

        gui.root.iconphoto.assert_called_once_with(True, image)
        self.assertIs(gui.window_logo_image, image)
        self.assertIs(gui.header_logo_image, header_image)

    def test_handle_progress_updates_history_progress_for_determinate_event(self) -> None:
        gui = LiveNoteGui.__new__(LiveNoteGui)
        gui.current_task_id = "task-0001"
        gui.current_task_session_id = "session-1"
        gui.current_live_task_id = None
        gui.background_task_sessions = {}
        gui.status_var = SimpleNamespace(set=Mock())
        gui.task_progress_var = SimpleNamespace(set=Mock())
        gui.history_progress = MagicMock()
        gui.progress = MagicMock()
        gui._append_log = Mock()

        gui._handle_progress(
            ProgressEvent(
                stage="merging",
                message="正在合并会话 2/3：课程记录",
                session_id="session-1",
                current=2,
                total=3,
            )
        )

        gui.status_var.set.assert_called_once_with("正在合并会话 2/3：课程记录")
        gui.task_progress_var.set.assert_called_once_with("正在合并会话 2/3：课程记录")
        gui.history_progress.stop.assert_called_once()
        gui.history_progress.configure.assert_called_once_with(mode="determinate")
        self.assertEqual(gui.history_progress.__setitem__.call_args_list, [(("value", 67),)])

    def test_update_idle_status_resets_history_progress_when_no_tasks(self) -> None:
        gui = LiveNoteGui.__new__(LiveNoteGui)
        gui.busy = False
        gui.background_tasks = {}
        gui.status_var = SimpleNamespace(set=Mock())
        gui.task_progress_var = SimpleNamespace(set=Mock())
        gui.history_progress = MagicMock()

        gui._update_idle_status()

        gui.status_var.set.assert_called_once_with("准备就绪")
        gui.task_progress_var.set.assert_called_once_with("当前没有任务。")
        gui.history_progress.stop.assert_called_once()
        gui.history_progress.configure.assert_called_once_with(mode="determinate", value=0)

    def test_handle_progress_detaches_live_task_after_capture_finished(self) -> None:
        gui = LiveNoteGui.__new__(LiveNoteGui)
        gui.current_task_id = "task-0001"
        gui.current_task_session_id = "session-1"
        gui.current_live_task_id = "task-0001"
        gui.background_task_sessions = {}
        gui._append_log = Mock()
        gui._detach_live_task = Mock()
        gui._refresh_history = Mock()

        gui._handle_progress(
            ProgressEvent(
                stage="capture_finished",
                message="录音已停止，后台继续转写、精修和整理。",
                session_id="session-1",
            )
        )

        gui._append_log.assert_called_once_with("录音已停止，后台继续转写、精修和整理。")
        gui._detach_live_task.assert_called_once_with("session-1")
        gui._refresh_history.assert_called_once()

    def test_find_background_task_by_session_falls_back_to_unknown_slot(self) -> None:
        gui = LiveNoteGui.__new__(LiveNoteGui)
        gui.background_task_sessions = {
            "task-0001": "session-1",
            "task-0002": None,
        }

        self.assertEqual("task-0001", gui._find_background_task_by_session("session-1"))
        self.assertEqual("task-0002", gui._find_background_task_by_session("session-2"))

    def test_retry_refine_skips_session_without_session_live_wav(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            gui = LiveNoteGui.__new__(LiveNoteGui)
            gui._selected_summary = Mock(
                return_value=SimpleNamespace(
                    session_id="session-1",
                    input_mode="live",
                    session_dir=Path(temp_dir),
                )
            )
            gui._ensure_ready_for_run = Mock(return_value=True)
            gui._run_background = Mock()

            with patch("live_note.app.gui.messagebox.showinfo") as showinfo_mock:
                gui._retry_refine()

        gui._run_background.assert_not_called()
        showinfo_mock.assert_called_once_with(
            "无法离线精修",
            "所选会话没有可用的整场录音（session.live.wav），无法执行离线精修并重写。",
        )

    def test_update_history_action_states_disables_refine_button_without_session_audio(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            gui = LiveNoteGui.__new__(LiveNoteGui)
            gui.retry_refine_button = MagicMock()

            gui._update_history_action_states(
                [
                    SimpleNamespace(
                        input_mode="live",
                        session_dir=Path(temp_dir),
                    )
                ]
            )

        gui.retry_refine_button.configure.assert_called_once_with(state="disabled")

    def test_retry_retranscribe_enqueues_queue_task(self) -> None:
        gui = LiveNoteGui.__new__(LiveNoteGui)
        gui._selected_summary = Mock(return_value=SimpleNamespace(session_id="session-1"))
        gui._ensure_queue_ready = Mock(return_value=True)
        gui._enqueue_queue_task = Mock()

        gui._retry_retranscribe()

        gui._enqueue_queue_task.assert_called_once_with(
            label="重转写并重写",
            action="session_action",
            payload={"action": "retranscribe", "session_id": "session-1"},
        )

    def test_maybe_start_next_queue_task_starts_first_record_when_idle(self) -> None:
        gui = LiveNoteGui.__new__(LiveNoteGui)
        gui.queue_records = [
            build_task_record(
                task_id="task-0001",
                action="session_action",
                label="重转写并重写",
                payload={"action": "retranscribe", "session_id": "session-1"},
                created_at="2026-03-16T10:00:00+00:00",
            )
        ]
        gui.queue_lock = threading.Lock()
        gui.queue_worker = None
        gui.busy = False
        gui.background_tasks = {}
        gui._start_queue_task = Mock()

        gui._maybe_start_next_queue_task()

        gui._start_queue_task.assert_called_once_with(gui.queue_records[0])

    def test_maybe_start_next_queue_task_skips_when_live_or_background_busy(self) -> None:
        record = build_task_record(
            task_id="task-0001",
            action="session_action",
            label="重转写并重写",
            payload={"action": "retranscribe", "session_id": "session-1"},
            created_at="2026-03-16T10:00:00+00:00",
        )

        gui = LiveNoteGui.__new__(LiveNoteGui)
        gui.queue_records = [record]
        gui.queue_lock = threading.Lock()
        gui.queue_worker = None
        gui.busy = True
        gui.background_tasks = {}
        gui.status_var = SimpleNamespace(set=Mock())
        gui.task_progress_var = SimpleNamespace(set=Mock())
        gui.history_progress = MagicMock()
        gui._start_queue_task = Mock()
        gui._maybe_start_next_queue_task()
        gui._start_queue_task.assert_not_called()

        gui.busy = False
        gui.background_tasks = {"live-task": "实时录音"}
        gui._maybe_start_next_queue_task()
        gui._start_queue_task.assert_not_called()

    def test_load_task_queue_state_keeps_queued_and_collects_interrupted_messages(self) -> None:
        queued = build_task_record(
            task_id="task-0001",
            action="session_action",
            label="重转写并重写",
            payload={"action": "retranscribe", "session_id": "session-1"},
            created_at="2026-03-16T10:00:00+00:00",
        )
        interrupted = build_task_record(
            task_id="task-0002",
            action="import",
            label="导入文件",
            payload={"file_path": "~/demo.mp3", "kind": "generic"},
            created_at="2026-03-16T10:01:00+00:00",
            status="running",
            started_at="2026-03-16T10:02:00+00:00",
        )
        gui = LiveNoteGui.__new__(LiveNoteGui)
        gui.queue_store = SimpleNamespace(
            load=Mock(
                return_value=QueueLoadResult(
                    active=[queued],
                    interrupted=[interrupted],
                    warnings=["队列文件损坏，已忽略。"],
                )
            ),
            save=Mock(),
        )
        gui.queue_lock = threading.Lock()
        gui.queue_records = []
        gui.queue_worker = None
        gui.busy = False
        gui.background_tasks = {}
        gui.queue_tree = MagicMock()
        gui._refresh_queue_tree = Mock()
        gui._append_log = Mock()
        gui._maybe_start_next_queue_task = Mock()

        gui._load_task_queue_state()

        self.assertEqual([queued], gui.queue_records)
        gui._refresh_queue_tree.assert_called_once()
        gui._append_log.assert_any_call("队列文件损坏，已忽略。")
        gui._append_log.assert_any_call("上次未完成的任务已标记为中断：导入文件")
        gui._maybe_start_next_queue_task.assert_called_once()

    def test_load_task_queue_state_persists_clean_queue_after_interrupted_recovery(self) -> None:
        queued = build_task_record(
            task_id="task-0007",
            action="session_action",
            label="重转写并重写",
            payload={"action": "retranscribe", "session_id": "session-1"},
            created_at="2026-03-16T10:00:00+00:00",
        )
        interrupted = build_task_record(
            task_id="task-0003",
            action="import",
            label="导入文件",
            payload={"file_path": "~/demo.mp3", "kind": "generic"},
            created_at="2026-03-16T10:01:00+00:00",
            status="running",
            started_at="2026-03-16T10:02:00+00:00",
        )
        gui = LiveNoteGui.__new__(LiveNoteGui)
        gui.queue_store = SimpleNamespace(
            load=Mock(
                return_value=QueueLoadResult(
                    active=[queued],
                    interrupted=[interrupted],
                    warnings=[],
                )
            ),
            save=Mock(),
        )
        gui.queue_lock = threading.Lock()
        gui.queue_records = []
        gui.queue_worker = None
        gui.task_sequence = 0
        gui.busy = False
        gui.background_tasks = {}
        gui.queue_tree = MagicMock()
        gui._refresh_queue_tree = Mock()
        gui._append_log = Mock()
        gui._maybe_start_next_queue_task = Mock()

        gui._load_task_queue_state()

        gui.queue_store.save.assert_called_once_with([queued])
        self.assertEqual("task-0008", gui._next_task_id())


class GuiLayoutTests(unittest.TestCase):
    def test_wrap_action_rows_moves_items_to_new_row_when_width_is_tight(self) -> None:
        layout = _wrap_action_rows(available_width=280, item_widths=[100, 100, 100], gap=8)

        self.assertEqual([(0, 0), (0, 1), (1, 0)], layout)

    def test_build_vertical_scroller_wraps_content_with_canvas_and_scrollbar(self) -> None:
        parent = MagicMock()
        root = MagicMock()
        canvas = MagicMock()
        scrollbar = MagicMock()
        content = MagicMock()
        canvas.create_window.return_value = "settings-window"
        parent.winfo_toplevel.return_value = root

        with (
            patch("live_note.app.gui.tk.Canvas", return_value=canvas),
            patch("live_note.app.gui.ttk.Scrollbar", return_value=scrollbar),
            patch("live_note.app.gui.ttk.Frame", return_value=content),
        ):
            result = _build_vertical_scroller(parent)

        self.assertIs(result, content)
        parent.columnconfigure.assert_called_once_with(0, weight=1)
        parent.rowconfigure.assert_called_once_with(0, weight=1)
        canvas.grid.assert_called_once_with(row=0, column=0, sticky="nsew")
        scrollbar.grid.assert_called_once_with(row=0, column=1, sticky="ns")
        canvas.configure.assert_any_call(yscrollcommand=scrollbar.set)
        canvas.create_window.assert_called_once_with((0, 0), window=content, anchor="nw")
        canvas.bind.assert_any_call("<Configure>", ANY)
        content.bind.assert_any_call("<Configure>", ANY)
        root.bind_all.assert_any_call("<MouseWheel>", ANY, add="+")
        root.bind_all.assert_any_call("<Button-4>", ANY, add="+")
        root.bind_all.assert_any_call("<Button-5>", ANY, add="+")
