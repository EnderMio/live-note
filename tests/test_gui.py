from __future__ import annotations

import queue
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
    _apply_visual_theme,
    _bind_mousewheel_scrolling,
    _build_execution_target_hint,
    _build_vertical_scroller,
    _default_gui_metrics,
    _default_gui_palette,
    _language_code_to_display,
    _normalize_language_value,
    _optional_language_override,
    _primary_remote_task,
    _summary_supports_refine,
    _wrap_action_rows,
)
from live_note.app.services import DoctorCheck
from live_note.app.task_queue import QueueLoadResult, build_task_record


class GuiLanguageTests(unittest.TestCase):
    def test_execution_target_helper_module_exports_same_hint_logic(self) -> None:
        from live_note.app.gui_execution_target import build_execution_target_hint

        self.assertEqual(
            "当前转写：远端服务（172.21.0.159，已连接）",
            build_execution_target_hint(True, "http://172.21.0.159:8765", "OK"),
        )

    def test_execution_target_helper_module_marks_unknown_remote_as_pending(self) -> None:
        from live_note.app.gui_execution_target import build_execution_target_hint

        self.assertEqual(
            "当前转写：远端服务（172.21.0.159，待检测）",
            build_execution_target_hint(True, "http://172.21.0.159:8765", None),
        )

    def test_execution_target_helper_module_normalizes_blank_host(self) -> None:
        from live_note.app.gui_execution_target import display_remote_host

        self.assertEqual("未配置", display_remote_host("   "))

    def test_language_helper_module_normalizes_mixed_language_label(self) -> None:
        from live_note.app.gui_language import normalize_language_value

        self.assertEqual("auto", normalize_language_value("自动识别 / 中英混合 / 多语言（auto）"))

    def test_language_helper_module_allows_default_override_to_none(self) -> None:
        from live_note.app.gui_language import optional_language_override

        self.assertIsNone(optional_language_override("沿用默认设置"))

    def test_language_helper_module_maps_language_code_to_display(self) -> None:
        from live_note.app.gui_language import language_code_to_display

        self.assertEqual("中文（zh）", language_code_to_display("zh", allow_blank=False))

    def test_normalize_language_value_maps_mixed_language_label_to_auto(self) -> None:
        self.assertEqual("auto", _normalize_language_value("自动识别 / 中英混合 / 多语言（auto）"))

    def test_normalize_language_value_allows_custom_language_code(self) -> None:
        self.assertEqual("fr", _normalize_language_value("fr"))

    def test_optional_language_override_uses_none_for_default_label(self) -> None:
        self.assertIsNone(_optional_language_override("沿用默认设置"))

    def test_language_code_to_display_returns_known_label(self) -> None:
        self.assertEqual("中文（zh）", _language_code_to_display("zh", allow_blank=False))

    def test_build_execution_target_hint_uses_local_when_remote_disabled(self) -> None:
        self.assertEqual("当前转写：本机", _build_execution_target_hint(False, "", None))

    def test_build_execution_target_hint_marks_remote_as_connected(self) -> None:
        self.assertEqual(
            "当前转写：远端服务（172.21.0.159，已连接）",
            _build_execution_target_hint(True, "http://172.21.0.159:8765", "OK"),
        )

    def test_build_execution_target_hint_marks_remote_as_unreachable(self) -> None:
        self.assertEqual(
            "当前转写：远端服务（172.21.0.159，未连通）",
            _build_execution_target_hint(True, "http://172.21.0.159:8765", "FAIL"),
        )


class GuiThemeTests(unittest.TestCase):
    def test_theme_helper_module_exports_default_palette(self) -> None:
        from live_note.app.gui_theme import default_gui_palette

        palette = default_gui_palette()

        self.assertEqual("#EEF2F7", palette.app_bg)
        self.assertEqual("#2563EB", palette.accent)

    def test_theme_helper_module_exports_default_metrics(self) -> None:
        from live_note.app.gui_theme import default_gui_metrics

        metrics = default_gui_metrics()

        self.assertEqual((20, 12), metrics.header_padding)
        self.assertEqual(12, metrics.section_gap)

    def test_theme_helper_module_exports_apply_visual_theme(self) -> None:
        from live_note.app.gui_theme import apply_visual_theme

        root = MagicMock()
        style = MagicMock()

        with patch("live_note.app.gui_theme.ttk.Style", return_value=style):
            apply_visual_theme(root)

        root.configure.assert_called_once()
        self.assertEqual("#EEF2F7", root.configure.call_args.kwargs["bg"])
        style.theme_use.assert_called_once_with("clam")

    def test_default_gui_palette_is_light_and_neutral(self) -> None:
        palette = _default_gui_palette()

        self.assertEqual("#EEF2F7", palette.app_bg)
        self.assertEqual("#FBFCFE", palette.surface_bg)
        self.assertEqual("#1F2937", palette.text_primary)
        self.assertEqual("#2563EB", palette.accent)

    def test_default_gui_metrics_compact_layout_spacing(self) -> None:
        metrics = _default_gui_metrics()

        self.assertEqual((20, 12), metrics.header_padding)
        self.assertEqual(14, metrics.page_padding)
        self.assertEqual(12, metrics.section_gap)
        self.assertEqual(11, metrics.log_height)

    def test_apply_visual_theme_configures_root_and_ttk_styles(self) -> None:
        root = MagicMock()
        style = MagicMock()

        with patch("live_note.app.gui.ttk.Style", return_value=style):
            _apply_visual_theme(root)

        root.configure.assert_called_once()
        self.assertEqual("#EEF2F7", root.configure.call_args.kwargs["bg"])
        style.theme_use.assert_called_once_with("clam")
        configured_styles = [call.args[0] for call in style.configure.call_args_list]
        self.assertIn("App.TNotebook", configured_styles)
        self.assertIn("App.TNotebook.Tab", configured_styles)
        self.assertIn("Section.TLabelframe", configured_styles)
        self.assertIn("App.Treeview", configured_styles)

    def test_scroll_helper_module_blocks_outer_canvas_when_inner_matches(self) -> None:
        from live_note.app.gui_scroll import bind_mousewheel_scrolling

        root = MagicMock()
        outer_canvas = MagicMock()
        inner_canvas = MagicMock()
        outer_canvas.widgetName = "canvas"
        inner_canvas.widgetName = "canvas"
        outer_content = SimpleNamespace(master=outer_canvas)
        inner_content = SimpleNamespace(master=inner_canvas)
        inner_canvas.master = outer_content

        outer_canvas.winfo_containing.return_value = inner_content
        inner_canvas.winfo_containing.return_value = inner_content

        bind_mousewheel_scrolling(root, outer_canvas)
        bind_mousewheel_scrolling(root, inner_canvas)

        handlers = [
            call.args[1] for call in root.bind_all.call_args_list if call.args[0] == "<MouseWheel>"
        ]
        self.assertEqual(2, len(handlers))

        event = SimpleNamespace(x_root=120, y_root=80, delta=-120, num=None)
        for handler in handlers:
            handler(event)

        inner_canvas.yview_scroll.assert_called_once_with(1, "units")
        outer_canvas.yview_scroll.assert_not_called()

    def test_bind_mousewheel_scrolling_does_not_scroll_outer_canvas_when_inner_matches(
        self,
    ) -> None:
        root = MagicMock()
        outer_canvas = MagicMock()
        inner_canvas = MagicMock()
        outer_canvas.widgetName = "canvas"
        inner_canvas.widgetName = "canvas"
        outer_content = SimpleNamespace(master=outer_canvas)
        inner_content = SimpleNamespace(master=inner_canvas)
        inner_canvas.master = outer_content

        outer_canvas.winfo_containing.return_value = inner_content
        inner_canvas.winfo_containing.return_value = inner_content

        _bind_mousewheel_scrolling(root, outer_canvas)
        _bind_mousewheel_scrolling(root, inner_canvas)

        handlers = [
            call.args[1] for call in root.bind_all.call_args_list if call.args[0] == "<MouseWheel>"
        ]
        self.assertEqual(2, len(handlers))

        event = SimpleNamespace(x_root=120, y_root=80, delta=-120, num=None)
        for handler in handlers:
            handler(event)

        inner_canvas.yview_scroll.assert_called_once_with(1, "units")
        outer_canvas.yview_scroll.assert_not_called()

    def test_bind_mousewheel_scrolling_does_not_scroll_outer_canvas_when_inner_treeview_matches(
        self,
    ) -> None:
        root = MagicMock()
        outer_canvas = MagicMock()
        outer_canvas.widgetName = "canvas"
        content = SimpleNamespace(master=outer_canvas)
        treeview = SimpleNamespace(master=content, widgetName="ttk::treeview")

        outer_canvas.winfo_containing.return_value = treeview

        _bind_mousewheel_scrolling(root, outer_canvas)

        handlers = [
            call.args[1] for call in root.bind_all.call_args_list if call.args[0] == "<MouseWheel>"
        ]
        self.assertEqual(1, len(handlers))

        event = SimpleNamespace(x_root=120, y_root=80, delta=-120, num=None)
        handlers[0](event)

        outer_canvas.yview_scroll.assert_not_called()

    def test_bind_mousewheel_scrolling_ignores_unresolvable_popup_widget(self) -> None:
        root = MagicMock()
        outer_canvas = MagicMock()
        outer_canvas.widgetName = "canvas"
        outer_canvas.winfo_containing.side_effect = KeyError("popdown")

        _bind_mousewheel_scrolling(root, outer_canvas)

        handlers = [
            call.args[1] for call in root.bind_all.call_args_list if call.args[0] == "<MouseWheel>"
        ]
        self.assertEqual(1, len(handlers))

        event = SimpleNamespace(x_root=120, y_root=80, delta=-120, num=None)

        try:
            handlers[0](event)
        except KeyError as exc:
            self.fail(f"mousewheel handler leaked popup lookup failure: {exc!r}")

        outer_canvas.yview_scroll.assert_not_called()


class GuiRemoteTaskTests(unittest.TestCase):
    def test_remote_helper_module_exports_primary_task_logic(self) -> None:
        from live_note.app.gui_remote import primary_remote_task

        running = SimpleNamespace(
            status="running",
            attachment_state="attached",
            message="正在转写片段 4/10",
            updated_at="2026-03-23T15:00:00+08:00",
        )
        queued = SimpleNamespace(
            status="queued",
            attachment_state="attached",
            message="已加入远端队列。",
            updated_at="2026-03-23T15:01:00+08:00",
        )

        self.assertIs(running, primary_remote_task([queued, running]))

    def test_remote_helper_module_exports_status_text_logic(self) -> None:
        from live_note.app.gui_remote import remote_task_status_text

        record = SimpleNamespace(
            status="completed",
            attachment_state="attached",
            result_version=4,
            last_synced_result_version=1,
            last_error=None,
        )

        self.assertEqual("待同步", remote_task_status_text(record))

    def test_remote_helper_module_exports_requires_sync_logic(self) -> None:
        from live_note.app.gui_remote import remote_task_requires_sync

        record = SimpleNamespace(
            remote_task_id="task-1",
            session_id="session-1",
            status="completed",
            attachment_state="attached",
            result_version=4,
            last_synced_result_version=4,
            last_error="同步失败",
        )

        self.assertTrue(remote_task_requires_sync(record))

    def test_primary_remote_task_prefers_running_over_newer_queued_item(self) -> None:
        running = SimpleNamespace(
            status="running",
            attachment_state="attached",
            message="正在转写片段 4/10",
            updated_at="2026-03-23T15:00:00+08:00",
        )
        queued = SimpleNamespace(
            status="queued",
            attachment_state="attached",
            message="已加入远端队列。",
            updated_at="2026-03-23T15:01:00+08:00",
        )

        primary = _primary_remote_task([queued, running])

        self.assertIs(primary, running)


class GuiQueueLifecycleHelperTests(unittest.TestCase):
    def test_queue_lifecycle_helper_returns_next_record_when_gui_is_idle(self) -> None:
        from live_note.app.gui_queue_lifecycle import next_queued_record_to_start

        record = build_task_record(
            task_id="task-0001",
            action="session_action",
            label="重转写并重写",
            payload={"action": "retranscribe", "session_id": "session-1"},
            created_at="2026-03-24T00:00:00+00:00",
        )
        runtime = SimpleNamespace(next_queued=Mock(return_value=record))

        result = next_queued_record_to_start(
            runtime,
            queue_worker=None,
            busy=False,
            background_tasks={},
            config_exists=True,
        )

        self.assertIs(record, result)
        runtime.next_queued.assert_called_once_with()

    def test_queue_lifecycle_helper_posts_done_event_on_success(self) -> None:
        from live_note.app.gui_queue_lifecycle import run_queue_task_worker

        record = build_task_record(
            task_id="task-0001",
            action="session_action",
            label="重转写并重写",
            payload={"action": "retranscribe", "session_id": "session-1"},
            created_at="2026-03-24T00:00:00+00:00",
        )
        service = SimpleNamespace(run_queue_task=Mock(return_value=0))
        remove_record = Mock()
        event_queue = queue.Queue()
        cancel_event = threading.Event()
        on_progress = Mock()

        run_queue_task_worker(
            service,
            record,
            on_progress=on_progress,
            cancel_event=cancel_event,
            remove_record=remove_record,
            event_queue=event_queue,
        )

        service.run_queue_task.assert_called_once_with(
            record,
            on_progress=on_progress,
            cancel_event=cancel_event,
        )
        remove_record.assert_called_once_with(record.task_id)
        self.assertEqual(
            ("task_done", "queue", record.task_id, record.label, 0),
            event_queue.get_nowait(),
        )


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

    def test_refresh_remote_tasks_populates_tree_and_status(self) -> None:
        gui = LiveNoteGui.__new__(LiveNoteGui)
        gui.service = SimpleNamespace(
            list_remote_task_summaries=lambda: SimpleNamespace(
                remote_available=True,
                availability_message=None,
                tasks=[
                    SimpleNamespace(
                        remote_task_id="task-1",
                        label="文件导入",
                        action="import",
                        session_id="remote-1",
                        status="running",
                        stage="transcribing",
                        message="正在转写片段 1/2",
                        current=1,
                        total=2,
                        updated_at="2026-03-19T10:00:00+00:00",
                        attachment_state="attached",
                        can_cancel=True,
                        result_version=1,
                        last_synced_result_version=0,
                        last_error=None,
                    )
                ],
            )
        )
        gui.remote_task_tree = MagicMock()
        gui.remote_task_tree.get_children.return_value = ["old"]
        gui.remote_task_status_var = MagicMock()
        gui.remote_task_progress = MagicMock()
        gui._on_remote_task_select = Mock()

        gui._refresh_remote_tasks()

        gui.remote_task_tree.delete.assert_called_once_with("old")
        gui.remote_task_tree.insert.assert_called_once()
        gui.remote_task_status_var.set.assert_called_once()
        gui.remote_task_progress.configure.assert_called()
        self.assertIn("task-1", gui.remote_task_rows)

    def test_refresh_remote_tasks_treats_lost_running_task_as_inactive(self) -> None:
        gui = LiveNoteGui.__new__(LiveNoteGui)
        gui.service = SimpleNamespace(
            list_remote_task_summaries=lambda: SimpleNamespace(
                remote_available=True,
                availability_message=None,
                tasks=[
                    SimpleNamespace(
                        remote_task_id="task-lost-1",
                        label="文件导入",
                        action="import",
                        session_id="remote-1",
                        status="running",
                        stage="speaker",
                        message="正在分析说话人特征。",
                        current=1,
                        total=3,
                        updated_at="2026-03-19T10:00:00+00:00",
                        attachment_state="lost",
                        can_cancel=True,
                        result_version=1,
                        last_synced_result_version=0,
                        last_error="服务端已重置，任务无法恢复。",
                    )
                ],
            )
        )
        gui.remote_task_tree = MagicMock()
        gui.remote_task_tree.get_children.return_value = []
        gui.remote_task_status_var = MagicMock()
        gui.remote_task_progress = MagicMock()
        gui._on_remote_task_select = Mock()

        gui._refresh_remote_tasks()

        gui.remote_task_status_var.set.assert_called_once()
        message = gui.remote_task_status_var.set.call_args.args[0]
        self.assertIn("没有活动任务", message)
        gui.remote_task_progress.start.assert_not_called()

    def test_cancel_selected_remote_task_requests_service_and_refreshes(self) -> None:
        gui = LiveNoteGui.__new__(LiveNoteGui)
        gui.remote_task_tree = SimpleNamespace(selection=lambda: ("task-1",))
        gui.remote_task_rows = {"task-1": SimpleNamespace(remote_task_id="task-1", can_cancel=True)}
        gui.service = SimpleNamespace(cancel_remote_task=Mock())
        gui._refresh_remote_tasks = Mock()
        gui._append_log = Mock()

        gui._cancel_selected_remote_task()

        gui.service.cancel_remote_task.assert_called_once_with("task-1")
        gui._refresh_remote_tasks.assert_called_once_with()

    def test_remote_task_select_turns_secondary_action_into_retry_sync_when_needed(self) -> None:
        gui = LiveNoteGui.__new__(LiveNoteGui)
        gui.remote_task_tree = SimpleNamespace(selection=lambda: ("task-1",))
        gui.remote_task_rows = {
            "task-1": SimpleNamespace(
                remote_task_id="task-1",
                can_cancel=False,
                session_id="remote-1",
                attachment_state="attached",
                status="completed",
                result_version=4,
                last_synced_result_version=1,
                last_error="同步失败",
            )
        }
        gui.open_remote_task_button = MagicMock()
        gui.cancel_remote_task_button = MagicMock()

        gui._on_remote_task_select(None)

        gui.open_remote_task_button.configure.assert_called_with(state="normal")
        gui.cancel_remote_task_button.configure.assert_called_with(
            text="重试同步",
            command=gui._retry_selected_remote_task_sync,
            state="normal",
        )

    def test_retry_selected_remote_task_sync_calls_service_and_refreshes(self) -> None:
        gui = LiveNoteGui.__new__(LiveNoteGui)
        gui.remote_task_tree = SimpleNamespace(selection=lambda: ("task-1",))
        gui.remote_task_rows = {
            "task-1": SimpleNamespace(remote_task_id="task-1", session_id="remote-1")
        }
        gui.service = SimpleNamespace(sync_remote_task=Mock())
        gui._refresh_remote_tasks = Mock()
        gui._append_log = Mock()

        gui._retry_selected_remote_task_sync()

        gui.service.sync_remote_task.assert_called_once_with("task-1")
        gui._refresh_remote_tasks.assert_called_once_with()


class GuiTaskTests(unittest.TestCase):
    def test_parse_live_auto_stop_seconds_accepts_decimal_minutes(self) -> None:
        gui = LiveNoteGui.__new__(LiveNoteGui)
        gui.live_stop_after_minutes_var = SimpleNamespace(get=lambda: "1.5")

        self.assertEqual(90, gui._parse_live_auto_stop_seconds())

    def test_parse_live_auto_stop_seconds_treats_blank_as_disabled(self) -> None:
        gui = LiveNoteGui.__new__(LiveNoteGui)
        gui.live_stop_after_minutes_var = SimpleNamespace(get=lambda: " ")

        self.assertIsNone(gui._parse_live_auto_stop_seconds())

    def test_build_settings_tab_includes_remote_client_section(self) -> None:
        gui = LiveNoteGui.__new__(LiveNoteGui)
        for name in [
            "ffmpeg_var",
            "whisper_binary_var",
            "whisper_model_var",
            "whisper_host_var",
            "whisper_port_var",
            "whisper_threads_var",
            "whisper_language_var",
            "whisper_translate_var",
            "save_session_wav_var",
            "refine_enabled_var",
            "refine_auto_after_live_var",
            "obsidian_enabled_var",
            "obsidian_base_url_var",
            "obsidian_transcript_dir_var",
            "obsidian_structured_dir_var",
            "obsidian_verify_ssl_var",
            "obsidian_api_key_var",
            "llm_enabled_var",
            "llm_base_url_var",
            "llm_model_var",
            "llm_stream_var",
            "llm_wire_api_var",
            "llm_requires_openai_auth_var",
            "llm_api_key_var",
            "remote_enabled_var",
            "remote_base_url_var",
            "remote_api_token_var",
            "remote_live_chunk_ms_var",
            "funasr_enabled_var",
            "funasr_base_url_var",
            "funasr_mode_var",
            "funasr_use_itn_var",
            "speaker_enabled_var",
            "speaker_backend_var",
            "speaker_segmentation_model_var",
            "speaker_embedding_model_var",
            "speaker_cluster_threshold_var",
            "speaker_pyannote_model_var",
        ]:
            setattr(gui, name, object())
        gui._autodetect_settings = Mock()
        gui._save_settings = Mock()
        gui._refresh_doctor_checks = Mock()
        gui._open_path = Mock()
        gui.service = SimpleNamespace(
            config_path=Path("/tmp/config.toml"),
            env_path=Path("/tmp/.env"),
        )

        content = MagicMock()
        frame_texts: list[str] = []

        def widget_factory(*_args, **kwargs):
            widget = MagicMock()
            widget.grid = Mock()
            widget.columnconfigure = Mock()
            widget.heading = Mock()
            widget.column = Mock()
            return widget

        def label_frame_factory(*_args, **kwargs):
            frame_texts.append(kwargs.get("text", ""))
            return widget_factory()

        with (
            patch("live_note.app.gui._build_vertical_scroller", return_value=content),
            patch("live_note.app.gui.ttk.Frame", side_effect=widget_factory),
            patch("live_note.app.gui.ttk.Button", side_effect=widget_factory),
            patch("live_note.app.gui.ttk.Checkbutton", side_effect=widget_factory),
            patch("live_note.app.gui.ttk.Label", side_effect=widget_factory),
            patch("live_note.app.gui.ttk.Treeview", side_effect=widget_factory),
            patch("live_note.app.gui.ttk.LabelFrame", side_effect=label_frame_factory),
            patch("live_note.app.gui._entry_row"),
            patch("live_note.app.gui._entry_row_with_button"),
            patch("live_note.app.gui._combobox_row"),
            patch("live_note.app.gui._language_row"),
        ):
            gui._build_settings_tab(MagicMock())

        self.assertIn("远端转写", frame_texts)
        self.assertIn("说话人区分", frame_texts)

    def test_build_history_tab_wraps_content_in_vertical_scroller(self) -> None:
        gui = LiveNoteGui.__new__(LiveNoteGui)
        gui.root = SimpleNamespace(after_idle=Mock())
        gui.task_progress_var = object()
        gui.remote_task_status_var = object()
        gui.history_detail_var = object()
        gui._theme_metrics = Mock(return_value=_default_gui_metrics())

        content = MagicMock()
        label_frame_parents: list[object] = []

        def widget_factory(*_args, **_kwargs):
            widget = MagicMock()
            widget.grid = Mock()
            widget.columnconfigure = Mock()
            widget.rowconfigure = Mock()
            widget.bind = Mock()
            widget.heading = Mock()
            widget.column = Mock()
            return widget

        def label_frame_factory(parent, *_args, **_kwargs):
            label_frame_parents.append(parent)
            return widget_factory()

        with (
            patch("live_note.app.gui._build_vertical_scroller", return_value=content) as scroller,
            patch("live_note.app.gui.ttk.Frame", side_effect=widget_factory),
            patch("live_note.app.gui.ttk.Button", side_effect=widget_factory),
            patch("live_note.app.gui.ttk.Label", side_effect=widget_factory),
            patch("live_note.app.gui.ttk.Progressbar", side_effect=widget_factory),
            patch("live_note.app.gui.ttk.Treeview", side_effect=widget_factory),
            patch("live_note.app.gui.ttk.LabelFrame", side_effect=label_frame_factory),
        ):
            gui._build_history_tab(MagicMock())

        scroller.assert_called_once()
        self.assertTrue(label_frame_parents)
        self.assertIs(label_frame_parents[0], content)

    def test_relayout_history_actions_flushes_layout_after_regridding_buttons(self) -> None:
        gui = LiveNoteGui.__new__(LiveNoteGui)
        frame = MagicMock()
        frame.winfo_width.return_value = 320
        frame.grid_columnconfigure = Mock()
        frame.update_idletasks = Mock()
        first = MagicMock()
        second = MagicMock()
        first.winfo_reqwidth.return_value = 120
        second.winfo_reqwidth.return_value = 140
        first.grid_forget = Mock()
        second.grid_forget = Mock()
        first.grid = Mock()
        second.grid = Mock()
        gui.history_actions_frame = frame
        gui.history_action_buttons = [first, second]

        gui._relayout_history_actions()

        frame.update_idletasks.assert_called_once_with()

    def test_load_settings_updates_visible_speaker_backend_fields(self) -> None:
        gui = LiveNoteGui.__new__(LiveNoteGui)
        for name in [
            "ffmpeg_var",
            "whisper_binary_var",
            "whisper_model_var",
            "whisper_host_var",
            "whisper_port_var",
            "whisper_threads_var",
            "live_language_var",
            "import_language_var",
            "whisper_language_var",
            "whisper_translate_var",
            "save_session_wav_var",
            "refine_enabled_var",
            "refine_auto_after_live_var",
            "live_auto_refine_var",
            "obsidian_enabled_var",
            "obsidian_base_url_var",
            "obsidian_transcript_dir_var",
            "obsidian_structured_dir_var",
            "obsidian_verify_ssl_var",
            "obsidian_api_key_var",
            "llm_enabled_var",
            "llm_base_url_var",
            "llm_model_var",
            "llm_stream_var",
            "llm_wire_api_var",
            "llm_requires_openai_auth_var",
            "llm_api_key_var",
            "remote_enabled_var",
            "remote_base_url_var",
            "remote_api_token_var",
            "remote_live_chunk_ms_var",
            "serve_host_var",
            "serve_port_var",
            "serve_api_token_var",
            "funasr_enabled_var",
            "funasr_base_url_var",
            "funasr_mode_var",
            "funasr_use_itn_var",
            "live_speaker_enabled_var",
            "import_speaker_enabled_var",
            "speaker_enabled_var",
            "speaker_segmentation_model_var",
            "speaker_embedding_model_var",
            "speaker_cluster_threshold_var",
        ]:
            setattr(gui, name, MagicMock())
        gui.speaker_backend_var = MagicMock()
        gui.speaker_pyannote_model_var = MagicMock()
        gui._update_live_auto_refine_state = Mock()
        gui._update_execution_target_hint = Mock()

        gui._load_settings(
            SimpleNamespace(
                ffmpeg_binary="/opt/homebrew/bin/ffmpeg",
                whisper_binary="/Users/demo/whisper-server",
                whisper_model="/Users/demo/model.bin",
                whisper_host="127.0.0.1",
                whisper_port=8178,
                whisper_threads=4,
                whisper_language="auto",
                whisper_translate=False,
                save_session_wav=True,
                refine_enabled=True,
                refine_auto_after_live=True,
                obsidian_enabled=False,
                obsidian_base_url="https://127.0.0.1:27124",
                obsidian_transcript_dir="Sessions/Transcripts",
                obsidian_structured_dir="Sessions/Summaries",
                obsidian_verify_ssl=False,
                obsidian_api_key="",
                llm_enabled=False,
                llm_base_url="https://api.openai.com/v1",
                llm_model="gpt-4.1-mini",
                llm_stream=False,
                llm_wire_api="chat_completions",
                llm_requires_openai_auth=False,
                llm_api_key="",
                remote_enabled=False,
                remote_base_url="http://127.0.0.1:8765",
                remote_api_token="",
                remote_live_chunk_ms=240,
                serve_host="127.0.0.1",
                serve_port=8765,
                serve_api_token="",
                funasr_enabled=False,
                funasr_base_url="ws://127.0.0.1:10095",
                funasr_mode="2pass",
                funasr_use_itn=True,
                speaker_enabled=True,
                speaker_backend="pyannote",
                speaker_segmentation_model="",
                speaker_embedding_model="",
                speaker_cluster_threshold=0.5,
                speaker_pyannote_model="pyannote/speaker-diarization-community-1",
            )
        )

        gui.speaker_backend_var.set.assert_called_once_with("pyannote")
        gui.speaker_pyannote_model_var.set.assert_called_once_with(
            "pyannote/speaker-diarization-community-1"
        )

    def test_current_settings_reads_visible_speaker_backend_fields(self) -> None:
        gui = LiveNoteGui.__new__(LiveNoteGui)
        gui.ffmpeg_var = SimpleNamespace(get=lambda: "/opt/homebrew/bin/ffmpeg")
        gui.whisper_binary_var = SimpleNamespace(get=lambda: "/Users/demo/whisper-server")
        gui.whisper_model_var = SimpleNamespace(get=lambda: "/Users/demo/model.bin")
        gui.whisper_host_var = SimpleNamespace(get=lambda: "127.0.0.1")
        gui.whisper_port_var = SimpleNamespace(get=lambda: "8178")
        gui.whisper_threads_var = SimpleNamespace(get=lambda: "4")
        gui.whisper_language_var = SimpleNamespace(
            get=lambda: "自动识别 / 中英混合 / 多语言（auto）"
        )
        gui.whisper_translate_var = SimpleNamespace(get=lambda: False)
        gui.save_session_wav_var = SimpleNamespace(get=lambda: True)
        gui.refine_enabled_var = SimpleNamespace(get=lambda: True)
        gui.refine_auto_after_live_var = SimpleNamespace(get=lambda: True)
        gui.obsidian_enabled_var = SimpleNamespace(get=lambda: False)
        gui.obsidian_base_url_var = SimpleNamespace(get=lambda: "https://127.0.0.1:27124")
        gui.obsidian_transcript_dir_var = SimpleNamespace(get=lambda: "Sessions/Transcripts")
        gui.obsidian_structured_dir_var = SimpleNamespace(get=lambda: "Sessions/Summaries")
        gui.obsidian_verify_ssl_var = SimpleNamespace(get=lambda: False)
        gui.obsidian_api_key_var = SimpleNamespace(get=lambda: "")
        gui.llm_enabled_var = SimpleNamespace(get=lambda: False)
        gui.llm_base_url_var = SimpleNamespace(get=lambda: "https://api.openai.com/v1")
        gui.llm_model_var = SimpleNamespace(get=lambda: "gpt-4.1-mini")
        gui.llm_stream_var = SimpleNamespace(get=lambda: False)
        gui.llm_wire_api_var = SimpleNamespace(get=lambda: "chat_completions")
        gui.llm_requires_openai_auth_var = SimpleNamespace(get=lambda: False)
        gui.remote_enabled_var = SimpleNamespace(get=lambda: False)
        gui.remote_base_url_var = SimpleNamespace(get=lambda: "http://127.0.0.1:8765")
        gui.remote_api_token_var = SimpleNamespace(get=lambda: "")
        gui.remote_live_chunk_ms_var = SimpleNamespace(get=lambda: "240")
        gui.serve_host_var = SimpleNamespace(get=lambda: "127.0.0.1")
        gui.serve_port_var = SimpleNamespace(get=lambda: "8765")
        gui.serve_api_token_var = SimpleNamespace(get=lambda: "")
        gui.funasr_enabled_var = SimpleNamespace(get=lambda: False)
        gui.funasr_base_url_var = SimpleNamespace(get=lambda: "ws://127.0.0.1:10095")
        gui.funasr_mode_var = SimpleNamespace(get=lambda: "2pass")
        gui.funasr_use_itn_var = SimpleNamespace(get=lambda: True)
        gui.speaker_enabled_var = SimpleNamespace(get=lambda: True)
        gui.speaker_backend_var = SimpleNamespace(get=lambda: "pyannote")
        gui.speaker_segmentation_model_var = SimpleNamespace(get=lambda: "")
        gui.speaker_embedding_model_var = SimpleNamespace(get=lambda: "")
        gui.speaker_cluster_threshold_var = SimpleNamespace(get=lambda: "0.5")
        gui.speaker_pyannote_model_var = SimpleNamespace(
            get=lambda: "pyannote/speaker-diarization-community-1"
        )
        gui.llm_api_key_var = SimpleNamespace(get=lambda: "")

        draft = gui._current_settings()

        self.assertEqual("pyannote", draft.speaker_backend)
        self.assertEqual(
            "pyannote/speaker-diarization-community-1",
            draft.speaker_pyannote_model,
        )

    def test_refresh_doctor_checks_updates_execution_target_hint(self) -> None:
        gui = LiveNoteGui.__new__(LiveNoteGui)
        gui.service = SimpleNamespace(
            doctor_checks=lambda: [
                DoctorCheck(
                    "remote_health",
                    "OK",
                    "连通 http://172.21.0.159:8765 | live-note-remote",
                )
            ]
        )
        gui.doctor_tree = MagicMock()
        gui.remote_enabled_var = SimpleNamespace(get=lambda: True)
        gui.remote_base_url_var = SimpleNamespace(get=lambda: "http://172.21.0.159:8765")
        gui.execution_target_var = SimpleNamespace(set=Mock())

        gui._refresh_doctor_checks()

        gui.execution_target_var.set.assert_called_once_with(
            "当前转写：远端服务（172.21.0.159，已连接）"
        )

    def test_start_live_session_logs_current_execution_target(self) -> None:
        gui = LiveNoteGui.__new__(LiveNoteGui)
        runner = Mock()
        gui._ensure_ready_for_run = Mock(return_value=True)
        gui.live_title_var = SimpleNamespace(get=lambda: "产品周会")
        gui.live_devices = [SimpleNamespace(index=2)]
        gui.live_device_combo = SimpleNamespace(current=lambda: 0)
        gui.service = SimpleNamespace(create_live_coordinator=Mock(return_value=runner))
        gui.live_kind_var = SimpleNamespace(get=lambda: "meeting")
        gui.live_language_var = SimpleNamespace(get=lambda: "沿用默认设置")
        gui.live_auto_refine_var = SimpleNamespace(get=lambda: True)
        gui.live_speaker_enabled_var = SimpleNamespace(get=lambda: False)
        gui.live_stop_after_minutes_var = SimpleNamespace(get=lambda: "")
        gui.refine_enabled_var = SimpleNamespace(get=lambda: True)
        gui.save_session_wav_var = SimpleNamespace(get=lambda: True)
        gui._progress_callback = Mock(return_value=Mock())
        gui._next_task_id = Mock(return_value="task-1")
        gui._start_live_task = Mock()
        gui._arm_live_auto_stop = Mock()
        gui.stop_live_button = MagicMock()
        gui.pause_live_button = MagicMock()
        gui.execution_target_var = SimpleNamespace(
            get=lambda: "当前转写：远端服务（172.21.0.159，已连接）"
        )
        gui._append_log = Mock()

        gui._start_live_session()

        gui._append_log.assert_called_once_with("当前转写：远端服务（172.21.0.159，已连接）")
        gui._arm_live_auto_stop.assert_called_once_with(None)

    def test_start_live_session_passes_auto_refine_choice(self) -> None:
        gui = LiveNoteGui.__new__(LiveNoteGui)
        runner = Mock()
        gui._ensure_ready_for_run = Mock(return_value=True)
        gui.live_title_var = SimpleNamespace(get=lambda: "产品周会")
        gui.live_devices = [SimpleNamespace(index=2)]
        gui.live_device_combo = SimpleNamespace(current=lambda: 0)
        gui.service = SimpleNamespace(create_live_coordinator=Mock(return_value=runner))
        gui.live_kind_var = SimpleNamespace(get=lambda: "meeting")
        gui.live_language_var = SimpleNamespace(get=lambda: "沿用默认设置")
        gui.live_auto_refine_var = SimpleNamespace(get=lambda: False)
        gui.live_speaker_enabled_var = SimpleNamespace(get=lambda: True)
        gui.live_stop_after_minutes_var = SimpleNamespace(get=lambda: "15")
        gui.refine_enabled_var = SimpleNamespace(get=lambda: True)
        gui.save_session_wav_var = SimpleNamespace(get=lambda: True)
        gui._progress_callback = Mock(return_value=Mock())
        gui._next_task_id = Mock(return_value="task-1")
        gui._start_live_task = Mock()
        gui._arm_live_auto_stop = Mock()
        gui.stop_live_button = MagicMock()
        gui.pause_live_button = MagicMock()
        gui.execution_target_var = SimpleNamespace(get=lambda: "当前转写：本机")
        gui._append_log = Mock()

        gui._start_live_session()

        gui.service.create_live_coordinator.assert_called_once_with(
            title="产品周会",
            source="2",
            kind="meeting",
            language=None,
            on_progress=ANY,
            auto_refine_after_live=False,
            speaker_enabled=True,
        )
        gui._arm_live_auto_stop.assert_called_once_with(900)

    def test_start_import_enqueues_speaker_choice(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            media_path = Path(temp_dir) / "demo.mp3"
            media_path.write_bytes(b"fake-audio")
            gui = LiveNoteGui.__new__(LiveNoteGui)
            gui._ensure_queue_ready = Mock(return_value=True)
            gui.import_file_var = SimpleNamespace(get=lambda: str(media_path))
            gui.import_title_var = SimpleNamespace(get=lambda: "课程录音")
            gui.import_kind_var = SimpleNamespace(get=lambda: "lecture")
            gui.import_language_var = SimpleNamespace(get=lambda: "沿用默认设置")
            gui.import_speaker_enabled_var = SimpleNamespace(get=lambda: True)
            gui._enqueue_queue_task = Mock()

            gui._start_import()

        gui._enqueue_queue_task.assert_called_once()
        payload = gui._enqueue_queue_task.call_args.kwargs["payload"]
        self.assertTrue(payload["speaker_enabled"])

    def test_toggle_live_pause_pauses_and_resumes_auto_stop(self) -> None:
        gui = LiveNoteGui.__new__(LiveNoteGui)
        runner = SimpleNamespace(is_paused=False, request_pause=Mock(), request_resume=Mock())
        gui.current_live_runner = runner
        gui.pause_live_button = MagicMock()
        gui._append_log = Mock()
        gui._pause_live_auto_stop = Mock()
        gui._resume_live_auto_stop = Mock()

        gui._toggle_live_pause()

        runner.request_pause.assert_called_once_with()
        gui._pause_live_auto_stop.assert_called_once_with()

        runner = SimpleNamespace(is_paused=True, request_pause=Mock(), request_resume=Mock())
        gui.current_live_runner = runner
        gui.pause_live_button = MagicMock()
        gui._append_log = Mock()
        gui._pause_live_auto_stop = Mock()
        gui._resume_live_auto_stop = Mock()

        gui._toggle_live_pause()

        runner.request_resume.assert_called_once_with()
        gui._resume_live_auto_stop.assert_called_once_with()

    def test_request_live_stop_cancels_auto_stop_timer(self) -> None:
        gui = LiveNoteGui.__new__(LiveNoteGui)
        gui.current_live_runner = SimpleNamespace(request_stop=Mock())
        gui.stop_live_button = MagicMock()
        gui.pause_live_button = MagicMock()
        gui._cancel_live_auto_stop = Mock()
        gui._append_log = Mock()

        gui._request_live_stop("已到自动停止时间，等待当前片段收尾。")

        gui.current_live_runner.request_stop.assert_called_once_with()
        gui._cancel_live_auto_stop.assert_called_once_with()
        gui._append_log.assert_called_once_with("已到自动停止时间，等待当前片段收尾。")

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

    def test_handle_queue_progress_updates_primary_progress_for_determinate_event(self) -> None:
        gui = LiveNoteGui.__new__(LiveNoteGui)
        gui.queue_current_task_id = "task-queue-1"
        gui.busy = False
        gui.status_var = SimpleNamespace(set=Mock())
        gui.task_progress_var = SimpleNamespace(set=Mock())
        gui.history_progress = MagicMock()
        gui.progress = MagicMock()
        gui._append_log = Mock()

        gui._handle_progress(
            ProgressEvent(
                stage="transcribing",
                message="正在转写片段 2/4",
                current=2,
                total=4,
                source="queue",
                task_id="task-queue-1",
            )
        )

        gui.status_var.set.assert_called_once_with("正在转写片段 2/4")
        gui.progress.stop.assert_called_once()
        gui.progress.configure.assert_called_once_with(mode="determinate")
        self.assertEqual(gui.progress.__setitem__.call_args_list, [(("value", 50),)])

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
            gui.merge_sessions_button = MagicMock()

            gui._update_history_action_states(
                [
                    SimpleNamespace(
                        input_mode="live",
                        session_dir=Path(temp_dir),
                        execution_target="local",
                    )
                ]
            )

        gui.retry_refine_button.configure.assert_called_once_with(state="disabled")

    def test_update_history_action_states_disables_merge_for_remote_sessions(self) -> None:
        gui = LiveNoteGui.__new__(LiveNoteGui)
        gui.retry_refine_button = MagicMock()
        gui.merge_sessions_button = MagicMock()

        gui._update_history_action_states(
            [
                SimpleNamespace(
                    input_mode="file",
                    session_dir=Path("/tmp/a"),
                    execution_target="remote",
                ),
                SimpleNamespace(
                    input_mode="file",
                    session_dir=Path("/tmp/b"),
                    execution_target="local",
                ),
            ]
        )

        gui.merge_sessions_button.configure.assert_called_once_with(state="disabled")

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

    def test_merge_selected_sessions_rejects_remote_sessions(self) -> None:
        gui = LiveNoteGui.__new__(LiveNoteGui)
        gui._selected_summaries = Mock(
            return_value=[
                SimpleNamespace(session_id="session-1", execution_target="remote"),
                SimpleNamespace(session_id="session-2", execution_target="remote"),
            ]
        )
        gui._ensure_queue_ready = Mock(return_value=True)
        gui._enqueue_queue_task = Mock()

        with patch("live_note.app.gui.messagebox.showinfo") as showinfo_mock:
            gui._merge_selected_sessions()

        gui._enqueue_queue_task.assert_not_called()
        showinfo_mock.assert_called_once_with(
            "暂不支持",
            "远端会话暂不支持在桌面端直接合并，请先分别完成整理或后续在服务端处理。",
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

    def test_set_live_progress_state_uses_lower_frequency_animation(self) -> None:
        gui = LiveNoteGui.__new__(LiveNoteGui)
        gui.progress = MagicMock()

        gui._set_live_progress_state("正在准备", active=True)

        gui.progress.configure.assert_called_once_with(mode="indeterminate")
        gui.progress.start.assert_called_once_with(96)

    def test_set_queue_progress_state_uses_lower_frequency_animation(self) -> None:
        gui = LiveNoteGui.__new__(LiveNoteGui)
        gui.task_progress_var = SimpleNamespace(set=Mock())
        gui.history_progress = MagicMock()

        gui._set_queue_progress_state("正在处理队列", active=True)

        gui.task_progress_var.set.assert_called_once_with("正在处理队列")
        gui.history_progress.configure.assert_called_once_with(mode="indeterminate")
        gui.history_progress.start.assert_called_once_with(96)

    def test_queue_select_enables_cancel_for_running_import_task(self) -> None:
        running_import = build_task_record(
            task_id="task-0001",
            action="import",
            label="导入文件",
            payload={"file_path": "~/demo.mp3", "kind": "generic"},
            created_at="2026-03-19T10:00:00+00:00",
            status="running",
            started_at="2026-03-19T10:01:00+00:00",
        )
        gui = LiveNoteGui.__new__(LiveNoteGui)
        gui.cancel_queue_button = MagicMock()
        gui.queue_tree = SimpleNamespace(selection=lambda: ("task-0001",))
        gui.queue_current_task_id = "task-0001"
        gui._queue_record = Mock(return_value=running_import)

        gui._on_queue_select(None)

        gui.cancel_queue_button.configure.assert_called_once_with(state="normal")

    def test_cancel_selected_queue_task_requests_running_import_cancel(self) -> None:
        running_import = build_task_record(
            task_id="task-0001",
            action="import",
            label="导入文件",
            payload={"file_path": "~/demo.mp3", "kind": "generic"},
            created_at="2026-03-19T10:00:00+00:00",
            status="running",
            started_at="2026-03-19T10:01:00+00:00",
        )
        gui = LiveNoteGui.__new__(LiveNoteGui)
        gui.queue_tree = SimpleNamespace(selection=lambda: ("task-0001",))
        gui.queue_current_task_id = "task-0001"
        gui._queue_record = Mock(return_value=running_import)
        gui._queue_runtime = Mock(return_value=SimpleNamespace(cancel=Mock(return_value=0)))
        gui._request_running_queue_import_cancel = Mock(return_value=True)
        gui._sync_queue_compat_state = Mock()
        gui._append_log = Mock()
        gui._refresh_queue_tree = Mock()
        gui._update_idle_status = Mock()

        gui._cancel_selected_queue_task()

        gui._request_running_queue_import_cancel.assert_called_once_with("task-0001")
        gui._append_log.assert_called_once_with("已请求取消当前导入任务。")
        gui._refresh_queue_tree.assert_called_once_with()

    def test_poll_events_uses_faster_interval_while_busy(self) -> None:
        gui = LiveNoteGui.__new__(LiveNoteGui)
        gui.event_queue = queue.Queue()
        gui.root = MagicMock()
        gui.busy = True
        gui.queue_worker = None
        gui.background_tasks = {}

        gui._poll_events()

        gui.root.after.assert_called_once_with(120, gui._poll_events)

    def test_poll_events_uses_slower_interval_when_idle(self) -> None:
        gui = LiveNoteGui.__new__(LiveNoteGui)
        gui.event_queue = queue.Queue()
        gui.root = MagicMock()
        gui.busy = False
        gui.queue_worker = None
        gui.background_tasks = {}

        gui._poll_events()

        gui.root.after.assert_called_once_with(400, gui._poll_events)

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
    def test_layout_helper_module_wraps_action_rows_when_width_is_tight(self) -> None:
        from live_note.app.gui_layout import wrap_action_rows

        layout = wrap_action_rows(available_width=280, item_widths=[100, 100, 100], gap=8)

        self.assertEqual([(0, 0), (0, 1), (1, 0)], layout)

    def test_wrap_action_rows_moves_items_to_new_row_when_width_is_tight(self) -> None:
        layout = _wrap_action_rows(available_width=280, item_widths=[100, 100, 100], gap=8)

        self.assertEqual([(0, 0), (0, 1), (1, 0)], layout)


class GuiQueueDisplayTests(unittest.TestCase):
    def test_queue_display_helper_module_formats_import_target_text(self) -> None:
        from live_note.app.gui_queue_display import queue_target_text

        record = build_task_record(
            task_id="task-0001",
            action="import",
            label="导入文件",
            payload={"file_path": "~/Recordings/demo.mp3", "kind": "generic"},
            created_at="2026-03-24T00:00:00+00:00",
        )

        self.assertEqual("demo.mp3", queue_target_text(record))

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
