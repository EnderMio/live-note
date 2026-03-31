from __future__ import annotations

import queue
import threading
import tkinter as tk
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from tkinter import Tk, filedialog, messagebox, ttk

from live_note.audio.capture import InputDevice
from live_note.branding import brand_logo_png_path
from live_note.utils import iso_now

from .events import ProgressEvent
from .gui_execution_target import (
    build_execution_target_hint as _build_execution_target_hint,
)
from .gui_language import language_code_to_display as _language_code_to_display
from .gui_language import normalize_language_value as _normalize_language_value
from .gui_language import optional_language_override as _optional_language_override
from .gui_layout import wrap_action_rows as _wrap_action_rows
from .gui_queue_display import queue_target_text as _queue_target_text
from .gui_queue_lifecycle import next_queued_record_to_start, run_queue_task_worker
from .gui_remote import (
    primary_remote_task as _primary_remote_task,
)
from .gui_remote import (
    remote_task_requires_sync as _remote_task_requires_sync,
)
from .gui_remote import (
    remote_task_status_text as _remote_task_status_text,
)
from .gui_scroll import bind_mousewheel_scrolling as _bind_mousewheel_scrolling
from .gui_theme import GuiMetrics, GuiPalette
from .gui_theme import apply_visual_theme as _apply_visual_theme
from .gui_theme import default_gui_metrics as _default_gui_metrics
from .gui_theme import default_gui_palette as _default_gui_palette
from .live_control import LiveRecordingController, parse_auto_stop_seconds
from .services import AppService, SessionSummary, SettingsDraft
from .session_actions import (
    build_history_detail,
    build_import_task_request,
    build_merge_task_request,
    build_session_task_request,
    can_merge_summaries,
    supports_refine,
)
from .task_queue import QueuedTaskRecord, QueueLoadResult, TaskQueueStore
from .task_queue_runtime import TaskQueueRuntime
from .task_state import GuiTaskState

KIND_CHOICES = ["generic", "meeting", "lecture"]
LLM_WIRE_API_CHOICES = ["chat_completions", "responses"]
SPEAKER_BACKEND_CHOICES = ["sherpa_onnx", "pyannote"]
SESSION_LANGUAGE_CHOICES = [
    "沿用默认设置",
    "自动识别 / 中英混合 / 多语言（auto）",
    "中文（zh）",
    "英文（en）",
    "日文（ja）",
    "韩文（ko）",
]
DEFAULT_LANGUAGE_CHOICES = SESSION_LANGUAGE_CHOICES[1:]
PROGRESS_ANIMATION_INTERVAL_MS = 96
POLL_ACTIVE_INTERVAL_MS = 120
POLL_IDLE_INTERVAL_MS = 400
REMOTE_TASK_POLL_ACTIVE_MS = 1500
REMOTE_TASK_POLL_BACKGROUND_MS = 5000
REMOTE_TASK_POLL_IDLE_MS = 15000


class _NoopScheduler:
    def after(self, _delay_ms: int, _callback: Callable[[], None]) -> str:
        return "noop"

    def after_cancel(self, _after_id: str) -> None:
        return None


def launch_gui(config_path: Path | None = None) -> int:
    try:
        root = Tk()
    except tk.TclError as exc:
        print(f"无法启动桌面界面: {exc}")
        return 1
    app = LiveNoteGui(root, AppService(config_path))
    app.run()
    return 0


class LiveNoteGui:
    def __init__(self, root: Tk, service: AppService):
        self.root = root
        self.service = service
        self.event_queue: queue.Queue[object] = queue.Queue()
        self.current_worker: threading.Thread | None = None
        self._task_state = GuiTaskState()
        self.queue_runtime = TaskQueueRuntime(TaskQueueStore(self.service.task_queue_path()))
        self.queue_worker: threading.Thread | None = None
        self.live_devices: list[InputDevice] = []
        self.history_rows: dict[str, SessionSummary] = {}
        self.remote_task_rows: dict[str, object] = {}

        self.status_var = tk.StringVar(value="准备就绪")
        self.execution_target_var = tk.StringVar(value="当前转写：本机")
        self.history_detail_var = tk.StringVar(value="选择一条历史会话查看详情。")
        self.task_progress_var = tk.StringVar(value="当前没有任务。")
        self.remote_task_status_var = tk.StringVar(value="当前没有远端任务。")

        self.live_title_var = tk.StringVar()
        self.live_kind_var = tk.StringVar(value="generic")
        self.live_language_var = tk.StringVar(value="沿用默认设置")
        self.live_auto_refine_var = tk.BooleanVar(value=True)
        self.live_speaker_enabled_var = tk.BooleanVar(value=False)
        self.live_stop_after_minutes_var = tk.StringVar(value="")
        self.live_device_var = tk.StringVar()
        self.live_input_level_var = tk.StringVar(value="No signal")
        self._live_control = LiveRecordingController(
            scheduler=self.root,
            on_auto_stop=self._on_live_auto_stop,
        )

        self.import_file_var = tk.StringVar()
        self.import_title_var = tk.StringVar()
        self.import_kind_var = tk.StringVar(value="generic")
        self.import_language_var = tk.StringVar(value="沿用默认设置")
        self.import_speaker_enabled_var = tk.BooleanVar(value=False)

        self.ffmpeg_var = tk.StringVar()
        self.whisper_binary_var = tk.StringVar()
        self.whisper_model_var = tk.StringVar()
        self.whisper_host_var = tk.StringVar(value="127.0.0.1")
        self.whisper_port_var = tk.StringVar(value="8178")
        self.whisper_threads_var = tk.StringVar(value="4")
        self.whisper_language_var = tk.StringVar(value="自动识别 / 中英混合 / 多语言（auto）")
        self.whisper_translate_var = tk.BooleanVar(value=False)
        self.save_session_wav_var = tk.BooleanVar(value=True)
        self.refine_enabled_var = tk.BooleanVar(value=True)
        self.refine_auto_after_live_var = tk.BooleanVar(value=True)
        self.obsidian_enabled_var = tk.BooleanVar(value=True)
        self.obsidian_base_url_var = tk.StringVar(value="https://127.0.0.1:27124")
        self.obsidian_transcript_dir_var = tk.StringVar(value="Sessions/Transcripts")
        self.obsidian_structured_dir_var = tk.StringVar(value="Sessions/Summaries")
        self.obsidian_verify_ssl_var = tk.BooleanVar(value=False)
        self.obsidian_api_key_var = tk.StringVar()
        self.llm_enabled_var = tk.BooleanVar(value=False)
        self.llm_base_url_var = tk.StringVar(value="https://api.openai.com/v1")
        self.llm_model_var = tk.StringVar(value="gpt-4.1-mini")
        self.llm_stream_var = tk.BooleanVar(value=False)
        self.llm_wire_api_var = tk.StringVar(value="chat_completions")
        self.llm_requires_openai_auth_var = tk.BooleanVar(value=False)
        self.llm_api_key_var = tk.StringVar()
        self.remote_enabled_var = tk.BooleanVar(value=False)
        self.remote_base_url_var = tk.StringVar(value="http://127.0.0.1:8765")
        self.remote_api_token_var = tk.StringVar()
        self.remote_live_chunk_ms_var = tk.StringVar(value="240")
        self.serve_host_var = tk.StringVar(value="127.0.0.1")
        self.serve_port_var = tk.StringVar(value="8765")
        self.serve_api_token_var = tk.StringVar()
        self.funasr_enabled_var = tk.BooleanVar(value=False)
        self.funasr_base_url_var = tk.StringVar(value="ws://127.0.0.1:10095")
        self.funasr_mode_var = tk.StringVar(value="2pass")
        self.funasr_use_itn_var = tk.BooleanVar(value=True)
        self.speaker_enabled_var = tk.BooleanVar(value=False)
        self.speaker_backend_var = tk.StringVar(value="sherpa_onnx")
        self.speaker_segmentation_model_var = tk.StringVar()
        self.speaker_embedding_model_var = tk.StringVar()
        self.speaker_cluster_threshold_var = tk.StringVar(value="0.5")
        self.speaker_pyannote_model_var = tk.StringVar(
            value="pyannote/speaker-diarization-community-1"
        )
        self.gui_palette, self.gui_metrics = _apply_visual_theme(self.root)

        self.root.title("live-note")
        self.root.geometry("1060x720")
        self.root.minsize(940, 640)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._apply_branding()

        self._build_ui()
        self.save_session_wav_var.trace_add("write", self._on_refine_prerequisite_changed)
        self.refine_enabled_var.trace_add("write", self._on_refine_prerequisite_changed)
        self._load_settings(self.service.load_settings_draft())
        self._load_task_queue_state()
        self._refresh_devices()
        self._refresh_history()
        self._refresh_remote_tasks()
        self._refresh_doctor_checks()
        self.root.after(self._next_poll_interval_ms(), self._poll_events)
        self.root.after(self._next_remote_task_poll_interval_ms(), self._poll_remote_tasks)
        if not self.service.config_exists():
            self.root.after(250, self._show_first_run_wizard)

    def run(self) -> None:
        self.root.mainloop()

    def _task_state_model(self) -> GuiTaskState:
        state = getattr(self, "_task_state", None)
        if state is None:
            state = GuiTaskState()
            self._task_state = state
        return state

    def _live_controller(self) -> LiveRecordingController:
        controller = getattr(self, "_live_control", None)
        if controller is None:
            scheduler = getattr(self, "root", None) or _NoopScheduler()
            controller = LiveRecordingController(
                scheduler=scheduler,
                on_auto_stop=self._on_live_auto_stop,
            )
            self._live_control = controller
        return controller

    def _theme_palette(self) -> GuiPalette:
        return getattr(self, "gui_palette", _default_gui_palette())

    def _theme_metrics(self) -> GuiMetrics:
        return getattr(self, "gui_metrics", _default_gui_metrics())

    @property
    def busy(self) -> bool:
        return self._task_state_model().busy

    @busy.setter
    def busy(self, value: bool) -> None:
        self._task_state_model().busy = value

    @property
    def current_task_id(self) -> str | None:
        return self._task_state_model().current_task_id

    @current_task_id.setter
    def current_task_id(self, value: str | None) -> None:
        self._task_state_model().current_task_id = value

    @property
    def current_task_label(self) -> str | None:
        return self._task_state_model().current_task_label

    @current_task_label.setter
    def current_task_label(self, value: str | None) -> None:
        self._task_state_model().current_task_label = value

    @property
    def current_task_session_id(self) -> str | None:
        return self._task_state_model().current_task_session_id

    @current_task_session_id.setter
    def current_task_session_id(self, value: str | None) -> None:
        self._task_state_model().current_task_session_id = value

    @property
    def current_live_task_id(self) -> str | None:
        return self._task_state_model().current_live_task_id

    @current_live_task_id.setter
    def current_live_task_id(self, value: str | None) -> None:
        self._task_state_model().current_live_task_id = value

    @property
    def queue_current_task_id(self) -> str | None:
        return self._task_state_model().queue_current_task_id

    @queue_current_task_id.setter
    def queue_current_task_id(self, value: str | None) -> None:
        self._task_state_model().queue_current_task_id = value

    @property
    def queue_current_task_label(self) -> str | None:
        return self._task_state_model().queue_current_task_label

    @queue_current_task_label.setter
    def queue_current_task_label(self, value: str | None) -> None:
        self._task_state_model().queue_current_task_label = value

    @property
    def background_tasks(self) -> dict[str, str]:
        return self._task_state_model().background_tasks

    @background_tasks.setter
    def background_tasks(self, value: dict[str, str]) -> None:
        self._task_state_model().background_tasks = value

    @property
    def background_task_sessions(self) -> dict[str, str | None]:
        return self._task_state_model().background_task_sessions

    @background_task_sessions.setter
    def background_task_sessions(self, value: dict[str, str | None]) -> None:
        self._task_state_model().background_task_sessions = value

    @property
    def current_live_runner(self):
        return self._live_controller().runner

    @current_live_runner.setter
    def current_live_runner(self, value) -> None:
        self._live_controller().bind_runner(value)

    def _apply_branding(self) -> None:
        self.window_logo_image: tk.PhotoImage | None = None
        self.header_logo_image: tk.PhotoImage | None = None
        logo_path = brand_logo_png_path()
        if not logo_path.exists():
            return
        try:
            self.window_logo_image = tk.PhotoImage(file=str(logo_path))
            self.root.iconphoto(True, self.window_logo_image)
            self.header_logo_image = self.window_logo_image.subsample(8, 8)
        except tk.TclError:
            self.window_logo_image = None
            self.header_logo_image = None

    def _build_ui(self) -> None:
        metrics = self._theme_metrics()
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        header = ttk.Frame(self.root, padding=metrics.header_padding, style="Header.TFrame")
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)

        brand_frame = ttk.Frame(header, style="Header.TFrame")
        brand_frame.grid(row=0, column=0, rowspan=2, sticky="w")
        brand_frame.columnconfigure(1, weight=1)
        if self.header_logo_image is not None:
            ttk.Label(brand_frame, image=self.header_logo_image).grid(
                row=0, column=0, rowspan=2, sticky="w", padx=(0, 12)
            )
        ttk.Label(
            brand_frame,
            text="live-note",
            style="BrandTitle.TLabel",
        ).grid(row=0, column=1, sticky="w")
        ttk.Label(
            brand_frame,
            text="本地优先的课程 / 会议 / 音频内容记录器",
            style="Header.TLabel",
        ).grid(row=1, column=1, sticky="w", pady=(2, 0))
        ttk.Label(
            brand_frame,
            textvariable=self.execution_target_var,
            style="Header.TLabel",
        ).grid(row=2, column=1, sticky="w", pady=(2, 0))
        ttk.Label(
            header,
            textvariable=self.status_var,
            anchor="e",
            style="Status.TLabel",
        ).grid(row=0, column=1, rowspan=3, sticky="e")

        notebook = ttk.Notebook(self.root, style="App.TNotebook")
        self.main_notebook = notebook
        notebook.grid(
            row=1,
            column=0,
            sticky="nsew",
            padx=metrics.page_padding,
            pady=(0, metrics.page_padding),
        )

        new_session_tab = ttk.Frame(notebook, padding=metrics.page_padding)
        history_tab = ttk.Frame(notebook, padding=metrics.page_padding)
        self.history_tab = history_tab
        settings_tab = ttk.Frame(notebook, padding=metrics.page_padding)
        notebook.add(new_session_tab, text="新建会话")
        notebook.add(history_tab, text="历史会话")
        notebook.add(settings_tab, text="设置与诊断")

        self._build_new_session_tab(new_session_tab)
        self._build_history_tab(history_tab)
        self._build_settings_tab(settings_tab)

    def _build_new_session_tab(self, parent: ttk.Frame) -> None:
        metrics = self._theme_metrics()
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)

        session_tabs = ttk.Notebook(parent, style="App.TNotebook")
        session_tabs.grid(row=0, column=0, sticky="ew")

        live_tab = ttk.Frame(session_tabs, padding=metrics.section_padding)
        import_tab = ttk.Frame(session_tabs, padding=metrics.section_padding)
        session_tabs.add(live_tab, text="实时录音")
        session_tabs.add(import_tab, text="导入文件")

        self._build_live_tab(live_tab)
        self._build_import_tab(import_tab)

        activity = ttk.LabelFrame(
            parent,
            text="运行状态",
            padding=metrics.section_padding,
            style="Section.TLabelframe",
        )
        activity.grid(row=1, column=0, sticky="nsew", pady=(metrics.section_gap, 0))
        activity.columnconfigure(0, weight=1)
        activity.rowconfigure(1, weight=1)

        self.progress = ttk.Progressbar(
            activity,
            mode="determinate",
            style="App.Horizontal.TProgressbar",
        )
        self.progress.grid(row=0, column=0, sticky="ew")

        self.log_text = tk.Text(
            activity,
            height=metrics.log_height,
            wrap="word",
            state="disabled",
            relief="flat",
            borderwidth=1,
            highlightthickness=1,
            background=self._theme_palette().field_bg,
            foreground=self._theme_palette().text_primary,
            highlightbackground=self._theme_palette().border,
            highlightcolor=self._theme_palette().border,
            padx=10,
            pady=10,
        )
        self.log_text.grid(row=1, column=0, sticky="nsew", pady=(metrics.section_gap, 0))

    def _build_live_tab(self, parent: ttk.Frame) -> None:
        metrics = self._theme_metrics()
        parent.columnconfigure(1, weight=1)

        _entry_row(parent, 0, "会话标题", self.live_title_var, "例如：产品周会")

        ttk.Label(parent, text="输入设备", style="SectionTitle.TLabel").grid(
            row=1,
            column=0,
            sticky="w",
            pady=(10, 0),
        )
        device_frame = ttk.Frame(parent)
        device_frame.grid(row=1, column=1, sticky="ew", pady=(10, 0))
        device_frame.columnconfigure(0, weight=1)
        self.live_device_combo = ttk.Combobox(
            device_frame,
            textvariable=self.live_device_var,
            state="readonly",
        )
        self.live_device_combo.grid(row=0, column=0, sticky="ew")
        ttk.Button(
            device_frame,
            text="刷新设备",
            command=self._refresh_devices,
        ).grid(row=0, column=1, padx=(8, 0))

        advanced = ttk.LabelFrame(
            parent,
            text="高级选项",
            padding=metrics.section_padding,
            style="Section.TLabelframe",
        )
        advanced.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(metrics.section_gap, 0))
        advanced.columnconfigure(1, weight=1)

        _combobox_row(advanced, 0, "内容类型", self.live_kind_var, KIND_CHOICES)
        _language_row(
            advanced,
            1,
            "语言覆盖",
            self.live_language_var,
            include_blank=True,
        )
        _entry_row(
            advanced,
            2,
            "自动停止（分钟）",
            self.live_stop_after_minutes_var,
            "留空或 0 表示关闭，可输入小数，例如 1.5。",
        )
        self.live_auto_refine_checkbutton = ttk.Checkbutton(
            advanced,
            text="本次停止后自动离线精修",
            variable=self.live_auto_refine_var,
        )
        self.live_auto_refine_checkbutton.grid(
            row=3,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(metrics.field_row_gap, 0),
        )
        ttk.Checkbutton(
            advanced,
            text="本次启用说话人区分",
            variable=self.live_speaker_enabled_var,
        ).grid(
            row=4,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(metrics.field_row_gap, 0),
        )
        ttk.Label(
            advanced,
            text=(
                "关闭 Obsidian 同步时仅保留本地 Markdown；关闭 LLM 整理时会生成待整理模板。"
                "自动离线精修需要同时启用“保存整场 WAV”和“离线精修”。"
            ),
            style="Hint.TLabel",
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(metrics.field_row_gap, 0))
        self._update_live_auto_refine_state()

        actions = ttk.Frame(parent)
        actions.grid(row=3, column=0, columnspan=2, sticky="w", pady=(metrics.section_gap + 2, 0))
        self.start_live_button = ttk.Button(
            actions,
            text="开始并生成",
            command=self._start_live_session,
            style="Primary.TButton",
        )
        self.start_live_button.grid(row=0, column=0)
        self.stop_live_button = ttk.Button(
            actions,
            text="停止录音",
            command=self._stop_live_session,
            state="disabled",
        )
        self.stop_live_button.grid(row=0, column=1, padx=(8, 0))
        self.pause_live_button = ttk.Button(
            actions,
            text="暂停录音",
            command=self._toggle_live_pause,
            state="disabled",
        )
        self.pause_live_button.grid(row=0, column=2, padx=(8, 0))

        input_meter = ttk.Frame(parent)
        input_meter.grid(
            row=4,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(metrics.inline_gap, 0),
        )
        input_meter.columnconfigure(1, weight=1)
        ttk.Label(input_meter, text="Input", style="Subtle.TLabel").grid(
            row=0,
            column=0,
            sticky="w",
        )
        self.live_input_meter = ttk.Progressbar(
            input_meter,
            mode="determinate",
            maximum=100,
            length=150,
            style="InputMeter.NoSignal.Horizontal.TProgressbar",
        )
        self.live_input_meter.grid(row=0, column=1, sticky="w", padx=(10, 8))
        self.live_input_meter_status = ttk.Label(
            input_meter,
            textvariable=self.live_input_level_var,
            style="InputMeter.NoSignal.TLabel",
            anchor="w",
            width=10,
        )
        self.live_input_meter_status.grid(row=0, column=2, sticky="w")
        self._reset_live_input_meter()

    def _build_import_tab(self, parent: ttk.Frame) -> None:
        metrics = self._theme_metrics()
        parent.columnconfigure(1, weight=1)

        ttk.Label(parent, text="媒体文件", style="SectionTitle.TLabel").grid(
            row=0,
            column=0,
            sticky="w",
        )
        file_frame = ttk.Frame(parent)
        file_frame.grid(row=0, column=1, sticky="ew")
        file_frame.columnconfigure(0, weight=1)
        ttk.Entry(file_frame, textvariable=self.import_file_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(
            file_frame,
            text="选择文件",
            command=self._choose_import_file,
        ).grid(row=0, column=1, padx=(8, 0))

        _entry_row(parent, 1, "会话标题", self.import_title_var, "留空时使用文件名")

        advanced = ttk.LabelFrame(
            parent,
            text="高级选项",
            padding=metrics.section_padding,
            style="Section.TLabelframe",
        )
        advanced.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(metrics.section_gap, 0))
        advanced.columnconfigure(1, weight=1)

        _combobox_row(advanced, 0, "内容类型", self.import_kind_var, KIND_CHOICES)
        _language_row(
            advanced,
            1,
            "语言覆盖",
            self.import_language_var,
            include_blank=True,
        )
        ttk.Checkbutton(
            advanced,
            text="本次启用说话人区分",
            variable=self.import_speaker_enabled_var,
        ).grid(
            row=2,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(metrics.field_row_gap, 0),
        )
        ttk.Label(
            advanced,
            text=(
                "支持音频和视频本地文件，例如 "
                "mp3 / m4a / wav / mp4 / mov / mkv；本地模式也可单独使用。"
            ),
            style="Hint.TLabel",
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(metrics.field_row_gap, 0))

        ttk.Button(
            parent,
            text="导入并生成",
            command=self._start_import,
            style="Primary.TButton",
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(metrics.section_gap + 2, 0))

    def _build_history_tab(self, parent: ttk.Frame) -> None:
        metrics = self._theme_metrics()
        content = _build_vertical_scroller(parent)
        content.columnconfigure(0, weight=1)

        task_frame = ttk.LabelFrame(
            content,
            text="任务队列",
            padding=metrics.section_padding,
            style="Section.TLabelframe",
        )
        task_frame.grid(row=0, column=0, sticky="ew")
        task_frame.columnconfigure(0, weight=1)

        ttk.Label(
            task_frame,
            textvariable=self.task_progress_var,
            justify="left",
        ).grid(row=0, column=0, sticky="w")
        self.history_progress = ttk.Progressbar(
            task_frame,
            mode="determinate",
            style="App.Horizontal.TProgressbar",
        )
        self.history_progress.grid(row=1, column=0, sticky="ew", pady=(metrics.field_row_gap, 0))

        self.queue_tree = ttk.Treeview(
            task_frame,
            columns=("status", "task", "target", "created"),
            show="headings",
            height=3,
            selectmode="browse",
            style="App.Treeview",
        )
        self.queue_tree.grid(row=2, column=0, sticky="ew", pady=(metrics.field_row_gap, 0))
        for key, label, width in [
            ("status", "状态", 90),
            ("task", "任务", 170),
            ("target", "目标", 280),
            ("created", "加入时间", 180),
        ]:
            self.queue_tree.heading(key, text=label)
            self.queue_tree.column(key, width=width, anchor="w")
        self.queue_tree.bind("<<TreeviewSelect>>", self._on_queue_select)

        queue_actions = ttk.Frame(task_frame)
        queue_actions.grid(row=3, column=0, sticky="w", pady=(metrics.field_row_gap, 0))
        self.cancel_queue_button = ttk.Button(
            queue_actions,
            text="取消所选",
            command=self._cancel_selected_queue_task,
            state="disabled",
        )
        self.cancel_queue_button.grid(row=0, column=0)

        remote_frame = ttk.LabelFrame(
            content,
            text="远端任务",
            padding=metrics.section_padding,
            style="Section.TLabelframe",
        )
        remote_frame.grid(row=1, column=0, sticky="ew", pady=(metrics.section_gap, 0))
        remote_frame.columnconfigure(0, weight=1)

        ttk.Label(
            remote_frame,
            textvariable=self.remote_task_status_var,
            justify="left",
            style="Subtle.TLabel",
        ).grid(row=0, column=0, sticky="w")
        self.remote_task_progress = ttk.Progressbar(
            remote_frame,
            mode="determinate",
            style="App.Horizontal.TProgressbar",
        )
        self.remote_task_progress.grid(
            row=1,
            column=0,
            sticky="ew",
            pady=(metrics.field_row_gap, 0),
        )
        self.remote_task_tree = ttk.Treeview(
            remote_frame,
            columns=("status", "task", "session", "progress", "updated"),
            show="headings",
            height=3,
            selectmode="browse",
            style="App.Treeview",
        )
        self.remote_task_tree.grid(
            row=2,
            column=0,
            sticky="ew",
            pady=(metrics.field_row_gap, 0),
        )
        for key, label, width in [
            ("status", "状态", 90),
            ("task", "任务", 180),
            ("session", "记录", 220),
            ("progress", "进度", 180),
            ("updated", "更新时间", 180),
        ]:
            self.remote_task_tree.heading(key, text=label)
            self.remote_task_tree.column(key, width=width, anchor="w")
        self.remote_task_tree.bind("<<TreeviewSelect>>", self._on_remote_task_select)

        remote_actions = ttk.Frame(remote_frame)
        remote_actions.grid(row=3, column=0, sticky="w", pady=(metrics.field_row_gap, 0))
        self.open_remote_task_button = ttk.Button(
            remote_actions,
            text="查看记录",
            command=self._open_remote_task_session,
            state="disabled",
        )
        self.open_remote_task_button.grid(row=0, column=0)
        self.cancel_remote_task_button = ttk.Button(
            remote_actions,
            text="取消",
            command=self._cancel_selected_remote_task,
            state="disabled",
        )
        self.cancel_remote_task_button.grid(row=0, column=1, padx=(8, 0))
        ttk.Button(
            remote_actions,
            text="刷新远端任务",
            command=self._refresh_remote_tasks,
        ).grid(row=0, column=2, padx=(8, 0))

        table = ttk.Frame(content)
        table.grid(row=2, column=0, sticky="nsew", pady=(metrics.section_gap, 0))
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)

        self.history_tree = ttk.Treeview(
            table,
            columns=("time", "title", "kind", "mode", "status", "segments", "errors"),
            show="headings",
            height=14,
            selectmode="extended",
            style="App.Treeview",
        )
        self.history_tree.grid(row=0, column=0, sticky="nsew")
        for key, label, width in [
            ("time", "开始时间", 180),
            ("title", "标题", 220),
            ("kind", "类型", 90),
            ("mode", "输入", 80),
            ("status", "状态", 120),
            ("segments", "片段", 80),
            ("errors", "失败", 80),
        ]:
            self.history_tree.heading(key, text=label)
            self.history_tree.column(key, width=width, anchor="w")
        self.history_tree.bind("<<TreeviewSelect>>", self._on_history_select)

        actions = ttk.Frame(content)
        actions.grid(row=3, column=0, sticky="ew", pady=(metrics.section_gap, 0))
        self.history_actions_frame = actions
        self.history_action_buttons: list[ttk.Button] = []

        self.history_action_buttons.append(
            ttk.Button(actions, text="刷新列表", command=self._refresh_history)
        )
        self.history_action_buttons.append(
            ttk.Button(actions, text="打开原文", command=self._open_transcript)
        )
        self.history_action_buttons.append(
            ttk.Button(actions, text="打开整理", command=self._open_structured)
        )
        self.history_action_buttons.append(
            ttk.Button(actions, text="打开目录", command=self._open_session_dir)
        )
        self.history_action_buttons.append(
            ttk.Button(actions, text="合并所选会话", command=self._merge_selected_sessions)
        )
        self.merge_sessions_button = self.history_action_buttons[-1]
        self.history_action_buttons.append(
            ttk.Button(actions, text="重转写并重写", command=self._retry_retranscribe)
        )
        self.retry_refine_button = ttk.Button(
            actions,
            text="离线精修并重写",
            command=self._retry_refine,
            state="disabled",
        )
        self.history_action_buttons.append(self.retry_refine_button)
        self.history_action_buttons.append(
            ttk.Button(actions, text="重新生成整理", command=self._retry_republish)
        )
        self.history_action_buttons.append(
            ttk.Button(actions, text="重新同步 Obsidian", command=self._retry_resync)
        )
        actions.bind("<Configure>", self._on_history_actions_resize)
        self.root.after_idle(self._relayout_history_actions)

        ttk.Label(
            content,
            textvariable=self.history_detail_var,
            wraplength=980,
            justify="left",
            style="Subtle.TLabel",
        ).grid(row=4, column=0, sticky="w", pady=(metrics.section_gap, 0))

    def _build_settings_tab(self, parent: ttk.Frame) -> None:
        metrics = self._theme_metrics()
        content = _build_vertical_scroller(parent)
        content.columnconfigure(0, weight=1)

        toolbar = ttk.Frame(content, style="Toolbar.TFrame")
        toolbar.grid(row=0, column=0, sticky="w")
        ttk.Button(
            toolbar,
            text="自动检测",
            command=self._autodetect_settings,
        ).grid(row=0, column=0)
        ttk.Button(
            toolbar,
            text="保存设置",
            command=self._save_settings,
            style="Primary.TButton",
        ).grid(row=0, column=1, padx=(8, 0))
        ttk.Button(toolbar, text="重新诊断", command=self._refresh_doctor_checks).grid(
            row=0, column=2, padx=(8, 0)
        )
        ttk.Button(
            toolbar, text="打开 config", command=lambda: self._open_path(self.service.config_path)
        ).grid(
            row=0,
            column=3,
            padx=(8, 0),
        )
        ttk.Button(
            toolbar, text="打开 .env", command=lambda: self._open_path(self.service.env_path)
        ).grid(
            row=0,
            column=4,
            padx=(8, 0),
        )

        whisper_frame = ttk.LabelFrame(
            content,
            text="Whisper / FFmpeg",
            padding=metrics.section_padding,
            style="Section.TLabelframe",
        )
        whisper_frame.grid(row=1, column=0, sticky="ew", pady=(metrics.section_gap, 0))
        whisper_frame.columnconfigure(1, weight=1)
        _entry_row_with_button(
            whisper_frame,
            0,
            "ffmpeg",
            self.ffmpeg_var,
            "可执行文件路径",
            lambda: self._browse_binary(self.ffmpeg_var),
        )
        _entry_row_with_button(
            whisper_frame,
            1,
            "whisper-server",
            self.whisper_binary_var,
            "可执行文件路径",
            lambda: self._browse_binary(self.whisper_binary_var),
        )
        _entry_row_with_button(
            whisper_frame,
            2,
            "模型文件",
            self.whisper_model_var,
            "选择 ggml 模型文件",
            lambda: self._browse_file(self.whisper_model_var, "选择 Whisper 模型"),
        )
        _entry_row(whisper_frame, 3, "Host", self.whisper_host_var)
        _entry_row(whisper_frame, 4, "Port", self.whisper_port_var)
        _entry_row(whisper_frame, 5, "Threads", self.whisper_threads_var)
        _language_row(
            whisper_frame,
            6,
            "默认语言",
            self.whisper_language_var,
            include_blank=False,
        )
        ttk.Checkbutton(
            whisper_frame,
            text="启用翻译模式",
            variable=self.whisper_translate_var,
        ).grid(row=7, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Checkbutton(
            whisper_frame,
            text="保存整场 live WAV（session.live.wav）",
            variable=self.save_session_wav_var,
        ).grid(row=8, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Checkbutton(
            whisper_frame,
            text="启用离线精修",
            variable=self.refine_enabled_var,
        ).grid(row=9, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Checkbutton(
            whisper_frame,
            text="live 结束后自动离线精修",
            variable=self.refine_auto_after_live_var,
        ).grid(row=10, column=0, columnspan=2, sticky="w", pady=(10, 0))

        obsidian_frame = ttk.LabelFrame(
            content,
            text="Obsidian",
            padding=metrics.section_padding,
            style="Section.TLabelframe",
        )
        obsidian_frame.grid(row=2, column=0, sticky="ew", pady=(metrics.section_gap, 0))
        obsidian_frame.columnconfigure(1, weight=1)
        ttk.Checkbutton(
            obsidian_frame,
            text="启用同步到 Obsidian",
            variable=self.obsidian_enabled_var,
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        _entry_row(obsidian_frame, 1, "Base URL", self.obsidian_base_url_var)
        _entry_row(obsidian_frame, 2, "原文目录", self.obsidian_transcript_dir_var)
        _entry_row(obsidian_frame, 3, "整理目录", self.obsidian_structured_dir_var)
        _entry_row(obsidian_frame, 4, "API Key", self.obsidian_api_key_var)
        ttk.Checkbutton(
            obsidian_frame,
            text="验证 HTTPS 证书",
            variable=self.obsidian_verify_ssl_var,
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Label(
            obsidian_frame,
            text="关闭后仍会把 transcript.md 和 structured.md 写入本地会话目录。",
            style="Hint.TLabel",
        ).grid(row=6, column=0, columnspan=3, sticky="w", pady=(metrics.field_row_gap, 0))

        llm_frame = ttk.LabelFrame(
            content,
            text="LLM",
            padding=metrics.section_padding,
            style="Section.TLabelframe",
        )
        llm_frame.grid(row=3, column=0, sticky="ew", pady=(metrics.section_gap, 0))
        llm_frame.columnconfigure(1, weight=1)
        ttk.Checkbutton(
            llm_frame,
            text="启用自动整理",
            variable=self.llm_enabled_var,
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        _entry_row(llm_frame, 1, "Base URL", self.llm_base_url_var)
        _entry_row(llm_frame, 2, "模型名", self.llm_model_var)
        _combobox_row(llm_frame, 3, "协议", self.llm_wire_api_var, LLM_WIRE_API_CHOICES)
        ttk.Checkbutton(
            llm_frame,
            text="使用 Stream 模式",
            variable=self.llm_stream_var,
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Checkbutton(
            llm_frame,
            text="优先使用 OPENAI_API_KEY 鉴权",
            variable=self.llm_requires_openai_auth_var,
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(10, 0))
        _entry_row(llm_frame, 6, "API Key", self.llm_api_key_var)
        ttk.Label(
            llm_frame,
            text="`responses` 协议会请求 /responses；开启 Stream 时会聚合 SSE 流式事件。",
            style="Hint.TLabel",
        ).grid(row=7, column=0, columnspan=3, sticky="w", pady=(metrics.field_row_gap, 0))

        remote_frame = ttk.LabelFrame(
            content,
            text="远端转写",
            padding=metrics.section_padding,
            style="Section.TLabelframe",
        )
        remote_frame.grid(row=4, column=0, sticky="ew", pady=(metrics.section_gap, 0))
        remote_frame.columnconfigure(1, weight=1)
        ttk.Checkbutton(
            remote_frame,
            text="启用远端转写服务",
            variable=self.remote_enabled_var,
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        _entry_row(remote_frame, 1, "Base URL", self.remote_base_url_var)
        _entry_row(remote_frame, 2, "API Token", self.remote_api_token_var)
        _entry_row(
            remote_frame,
            3,
            "推送间隔(ms)",
            self.remote_live_chunk_ms_var,
            "默认 240；越小延迟越低，但请求更频繁。",
        )
        ttk.Checkbutton(
            remote_frame,
            text="保存本地远端模板：FunASR 实时稿",
            variable=self.funasr_enabled_var,
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(10, 0))
        _entry_row(remote_frame, 5, "FunASR 地址", self.funasr_base_url_var)
        _combobox_row(remote_frame, 6, "FunASR 模式", self.funasr_mode_var, ["2pass", "online"])
        ttk.Checkbutton(
            remote_frame,
            text="FunASR 启用 ITN",
            variable=self.funasr_use_itn_var,
        ).grid(row=7, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Label(
            remote_frame,
            text=(
                "这里只保存桌面端的远端配置模板。"
                "实际生效后端以诊断结果里的 backend=... 为准；"
                "切换远端实时后端仍需修改 Mac mini 上的 config.remote.toml 并重启服务。"
            ),
            style="Hint.TLabel",
        ).grid(row=8, column=0, columnspan=3, sticky="w", pady=(metrics.field_row_gap, 0))

        speaker_frame = ttk.LabelFrame(
            content,
            text="说话人区分",
            padding=metrics.section_padding,
            style="Section.TLabelframe",
        )
        speaker_frame.grid(row=5, column=0, sticky="ew", pady=(metrics.section_gap, 0))
        speaker_frame.columnconfigure(1, weight=1)
        ttk.Checkbutton(
            speaker_frame,
            text="启用说话人区分后处理",
            variable=self.speaker_enabled_var,
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        _combobox_row(
            speaker_frame,
            1,
            "后端",
            self.speaker_backend_var,
            SPEAKER_BACKEND_CHOICES,
        )
        _entry_row_with_button(
            speaker_frame,
            2,
            "分割模型",
            self.speaker_segmentation_model_var,
            "sherpa_onnx 使用；pyannote 后端可留空",
            lambda: self._browse_file(
                self.speaker_segmentation_model_var,
                "选择说话人分割模型",
            ),
        )
        _entry_row_with_button(
            speaker_frame,
            3,
            "嵌入模型",
            self.speaker_embedding_model_var,
            "sherpa_onnx 使用；pyannote 后端可留空",
            lambda: self._browse_file(
                self.speaker_embedding_model_var,
                "选择说话人嵌入模型",
            ),
        )
        _entry_row(
            speaker_frame,
            4,
            "聚类阈值",
            self.speaker_cluster_threshold_var,
            (
                "默认 0.5；已知说话人数时优先在 "
                "config.toml / config.remote.toml 里填 expected_speakers。"
            ),
        )
        _entry_row(
            speaker_frame,
            5,
            "pyannote 模型",
            self.speaker_pyannote_model_var,
            "pyannote 后端使用；如需访问令牌，请在 .env 里配置 PYANNOTE_AUTH_TOKEN。",
        )
        ttk.Label(
            speaker_frame,
            text=(
                "这里配置的是说话人区分的后端、模型与默认开关。"
                "新建页可按本次任务临时开启或关闭；"
                "远端模式下，这里主要用于本地模板与诊断展示，"
                "真正生效的远端后端仍需同步修改 Mac mini 上的 config.remote.toml。"
            ),
            style="Hint.TLabel",
        ).grid(row=6, column=0, columnspan=3, sticky="w", pady=(metrics.field_row_gap, 0))

        doctor_frame = ttk.LabelFrame(
            content,
            text="诊断结果",
            padding=metrics.section_padding,
            style="Section.TLabelframe",
        )
        doctor_frame.grid(row=6, column=0, sticky="ew", pady=(metrics.section_gap, 16))
        doctor_frame.columnconfigure(0, weight=1)
        self.doctor_tree = ttk.Treeview(
            doctor_frame,
            columns=("name", "status", "detail"),
            show="headings",
            height=8,
            style="App.Treeview",
        )
        self.doctor_tree.grid(row=0, column=0, sticky="ew")
        for key, label, width in [
            ("name", "项目", 180),
            ("status", "状态", 90),
            ("detail", "说明", 700),
        ]:
            self.doctor_tree.heading(key, text=label)
            self.doctor_tree.column(key, width=width, anchor="w")

    def _load_settings(self, draft: SettingsDraft) -> None:
        self.ffmpeg_var.set(draft.ffmpeg_binary)
        self.whisper_binary_var.set(draft.whisper_binary)
        self.whisper_model_var.set(draft.whisper_model)
        self.whisper_host_var.set(draft.whisper_host)
        self.whisper_port_var.set(str(draft.whisper_port))
        self.whisper_threads_var.set(str(draft.whisper_threads))
        self.live_language_var.set("沿用默认设置")
        self.import_language_var.set("沿用默认设置")
        self.whisper_language_var.set(
            _language_code_to_display(draft.whisper_language, allow_blank=False)
        )
        self.whisper_translate_var.set(draft.whisper_translate)
        self.save_session_wav_var.set(draft.save_session_wav)
        self.refine_enabled_var.set(draft.refine_enabled)
        self.refine_auto_after_live_var.set(draft.refine_auto_after_live)
        self.live_auto_refine_var.set(draft.refine_auto_after_live)
        self.obsidian_enabled_var.set(draft.obsidian_enabled)
        self.obsidian_base_url_var.set(draft.obsidian_base_url)
        self.obsidian_transcript_dir_var.set(draft.obsidian_transcript_dir)
        self.obsidian_structured_dir_var.set(draft.obsidian_structured_dir)
        self.obsidian_verify_ssl_var.set(draft.obsidian_verify_ssl)
        self.obsidian_api_key_var.set(draft.obsidian_api_key)
        self.llm_enabled_var.set(draft.llm_enabled)
        self.llm_base_url_var.set(draft.llm_base_url)
        self.llm_model_var.set(draft.llm_model)
        self.llm_stream_var.set(draft.llm_stream)
        self.llm_wire_api_var.set(draft.llm_wire_api)
        self.llm_requires_openai_auth_var.set(draft.llm_requires_openai_auth)
        self.llm_api_key_var.set(draft.llm_api_key)
        self.remote_enabled_var.set(draft.remote_enabled)
        self.remote_base_url_var.set(draft.remote_base_url)
        self.remote_api_token_var.set(draft.remote_api_token)
        self.remote_live_chunk_ms_var.set(str(draft.remote_live_chunk_ms))
        self.serve_host_var.set(draft.serve_host)
        self.serve_port_var.set(str(draft.serve_port))
        self.serve_api_token_var.set(draft.serve_api_token)
        self.funasr_enabled_var.set(draft.funasr_enabled)
        self.funasr_base_url_var.set(draft.funasr_base_url)
        self.funasr_mode_var.set(draft.funasr_mode)
        self.funasr_use_itn_var.set(draft.funasr_use_itn)
        self.speaker_enabled_var.set(draft.speaker_enabled)
        self.speaker_backend_var.set(draft.speaker_backend)
        self.speaker_segmentation_model_var.set(draft.speaker_segmentation_model)
        self.speaker_embedding_model_var.set(draft.speaker_embedding_model)
        self.speaker_cluster_threshold_var.set(str(draft.speaker_cluster_threshold))
        self.speaker_pyannote_model_var.set(draft.speaker_pyannote_model)
        self._update_live_auto_refine_state()
        self._update_execution_target_hint()

    def _current_settings(self) -> SettingsDraft:
        return SettingsDraft(
            ffmpeg_binary=self.ffmpeg_var.get().strip(),
            whisper_binary=self.whisper_binary_var.get().strip(),
            whisper_model=self.whisper_model_var.get().strip(),
            whisper_host=self.whisper_host_var.get().strip() or "127.0.0.1",
            whisper_port=int(self.whisper_port_var.get().strip() or "8178"),
            whisper_threads=int(self.whisper_threads_var.get().strip() or "4"),
            whisper_language=_normalize_language_value(
                self.whisper_language_var.get(),
                blank_fallback="auto",
            ),
            whisper_translate=self.whisper_translate_var.get(),
            save_session_wav=self.save_session_wav_var.get(),
            refine_enabled=self.refine_enabled_var.get(),
            refine_auto_after_live=self.refine_auto_after_live_var.get(),
            obsidian_enabled=self.obsidian_enabled_var.get(),
            obsidian_base_url=self.obsidian_base_url_var.get().strip(),
            obsidian_transcript_dir=self.obsidian_transcript_dir_var.get().strip(),
            obsidian_structured_dir=self.obsidian_structured_dir_var.get().strip(),
            obsidian_verify_ssl=self.obsidian_verify_ssl_var.get(),
            obsidian_api_key=self.obsidian_api_key_var.get().strip(),
            llm_enabled=self.llm_enabled_var.get(),
            llm_base_url=self.llm_base_url_var.get().strip(),
            llm_model=self.llm_model_var.get().strip(),
            llm_stream=self.llm_stream_var.get(),
            llm_wire_api=self.llm_wire_api_var.get().strip() or "chat_completions",
            llm_requires_openai_auth=self.llm_requires_openai_auth_var.get(),
            remote_enabled=self.remote_enabled_var.get(),
            remote_base_url=self.remote_base_url_var.get().strip(),
            remote_api_token=self.remote_api_token_var.get().strip(),
            remote_live_chunk_ms=int(self.remote_live_chunk_ms_var.get().strip() or "240"),
            serve_host=self.serve_host_var.get().strip() or "127.0.0.1",
            serve_port=int(self.serve_port_var.get().strip() or "8765"),
            serve_api_token=self.serve_api_token_var.get().strip(),
            funasr_enabled=self.funasr_enabled_var.get(),
            funasr_base_url=self.funasr_base_url_var.get().strip(),
            funasr_mode=self.funasr_mode_var.get().strip() or "2pass",
            funasr_use_itn=self.funasr_use_itn_var.get(),
            speaker_enabled=self.speaker_enabled_var.get(),
            speaker_backend=self.speaker_backend_var.get().strip() or "sherpa_onnx",
            speaker_segmentation_model=self.speaker_segmentation_model_var.get().strip(),
            speaker_embedding_model=self.speaker_embedding_model_var.get().strip(),
            speaker_cluster_threshold=float(
                self.speaker_cluster_threshold_var.get().strip() or "0.5"
            ),
            speaker_pyannote_model=self.speaker_pyannote_model_var.get().strip()
            or "pyannote/speaker-diarization-community-1",
            llm_api_key=self.llm_api_key_var.get().strip(),
        )

    def _refresh_devices(self) -> None:
        try:
            self.live_devices = self.service.list_input_devices()
        except Exception as exc:
            self.live_devices = []
            messagebox.showerror("读取设备失败", str(exc))
            return
        names = [device.name for device in self.live_devices]
        self.live_device_combo["values"] = names
        if names:
            self.live_device_combo.current(0)
            self.live_device_var.set(names[0])

    def _choose_import_file(self) -> None:
        path = filedialog.askopenfilename(
            title="选择媒体文件",
            filetypes=[
                ("媒体文件", "*.mp3 *.m4a *.wav *.aac *.flac *.mp4 *.mov *.mkv *.webm"),
                ("所有文件", "*.*"),
            ],
        )
        if path:
            self.import_file_var.set(path)
            if not self.import_title_var.get().strip():
                self.import_title_var.set(Path(path).stem)

    def _browse_file(self, target_var: tk.StringVar, title: str) -> None:
        path = filedialog.askopenfilename(title=title)
        if path:
            target_var.set(path)

    def _browse_binary(self, target_var: tk.StringVar) -> None:
        self._browse_file(target_var, "选择可执行文件")

    def _progress_callback(self, source: str, task_id: str) -> Callable[[ProgressEvent], None]:
        def callback(event: ProgressEvent) -> None:
            self.event_queue.put(
                replace(
                    event,
                    source=source,
                    task_id=event.task_id or task_id,
                )
            )

        return callback

    def _load_task_queue_state(self) -> None:
        loaded = self._queue_runtime().load()
        self._sync_queue_compat_state()
        self._refresh_queue_tree()
        for warning in loaded.warnings:
            self._append_log(warning)
        for record in loaded.interrupted_records:
            self._append_log(f"上次未完成的任务已标记为中断：{record.label}")
        self._maybe_start_next_queue_task()

    def _ensure_queue_ready(self) -> bool:
        if not self.service.config_exists():
            messagebox.showinfo("需要先配置", "请先完成首次启动向导或在设置页保存配置。")
            self._show_first_run_wizard()
            return False
        return True

    def _enqueue_queue_task(self, *, label: str, action: str, payload: dict[str, object]) -> None:
        record = self._queue_runtime().enqueue(
            label=label,
            action=action,
            payload=payload,
            created_at=iso_now(),
        )
        self._sync_queue_compat_state()
        if record is None:
            self._append_log(f"{label} 已在队列中，跳过重复入队。")
            self._update_idle_status()
            return
        self._append_log(f"已加入队列：{label}")
        self._refresh_queue_tree()
        self._maybe_start_next_queue_task()

    def _maybe_start_next_queue_task(self) -> None:
        service = getattr(self, "service", None)
        next_record = next_queued_record_to_start(
            self._queue_runtime(),
            queue_worker=self.queue_worker,
            busy=self.busy,
            background_tasks=self.background_tasks,
            config_exists=service is None or service.config_exists(),
        )
        if next_record is None:
            self._update_idle_status()
            return
        self._start_queue_task(next_record)

    def _start_queue_task(self, record: QueuedTaskRecord) -> None:
        started_record = self._queue_runtime().mark_running(record.task_id, started_at=iso_now())
        self._sync_queue_compat_state()
        self._task_state_model().mark_queue_running(
            task_id=started_record.task_id,
            label=started_record.label,
        )
        cancel_event = threading.Event()
        self.queue_cancel_task_id = started_record.task_id
        self.queue_cancel_action = started_record.action
        self.queue_cancel_event = cancel_event
        self._refresh_queue_tree()
        self._set_queue_progress_state(f"{started_record.label}：准备中")
        if not self.busy:
            self.status_var.set(f"{started_record.label}：准备中")

        def worker() -> None:
            run_queue_task_worker(
                self.service,
                started_record,
                on_progress=self._progress_callback("queue", started_record.task_id),
                cancel_event=cancel_event,
                remove_record=self._remove_queue_record,
                event_queue=self.event_queue,
            )

        self.queue_worker = threading.Thread(target=worker, daemon=False)
        self.queue_worker.start()

    def _remove_queue_record(self, task_id: str) -> None:
        if not self._queue_runtime().remove(task_id):
            return
        self._sync_queue_compat_state()

    def _refresh_queue_tree(self) -> None:
        tree = getattr(self, "queue_tree", None)
        if tree is None:
            return
        for item in tree.get_children():
            tree.delete(item)
        for record in self._queue_records_snapshot():
            tree.insert(
                "",
                "end",
                iid=record.task_id,
                values=(
                    "运行中" if record.status == "running" else "排队中",
                    record.label,
                    _queue_target_text(record),
                    record.created_at.replace("T", " ").split("+")[0],
                ),
            )
        self._on_queue_select(None)

    def _on_queue_select(self, _event: object | None) -> None:
        button = getattr(self, "cancel_queue_button", None)
        tree = getattr(self, "queue_tree", None)
        if button is None or tree is None:
            return
        selection = tree.selection()
        cancellable = any(
            (record := self._queue_record(task_id)) is not None
            and (
                record.status == "queued"
                or (
                    record.status == "running"
                    and record.action == "import"
                    and task_id == self.queue_current_task_id
                )
            )
            for task_id in selection
        )
        button.configure(state="normal" if cancellable else "disabled")

    def _cancel_selected_queue_task(self) -> None:
        tree = getattr(self, "queue_tree", None)
        if tree is None:
            return
        selection = set(tree.selection())
        if not selection:
            return
        requested_cancel = False
        if self.queue_current_task_id in selection:
            requested_cancel = self._request_running_queue_import_cancel(self.queue_current_task_id)
        cancelled = self._queue_runtime().cancel(selection)
        self._sync_queue_compat_state()
        if requested_cancel:
            self._append_log("已请求取消当前导入任务。")
            self._refresh_queue_tree()
            self._update_idle_status()
        if cancelled:
            self._append_log("已取消所选排队任务。")
            self._refresh_queue_tree()
            self._update_idle_status()

    def _request_running_queue_import_cancel(self, task_id: str) -> bool:
        if task_id != getattr(self, "queue_cancel_task_id", None):
            return False
        if getattr(self, "queue_cancel_action", None) != "import":
            return False
        cancel_event = getattr(self, "queue_cancel_event", None)
        if cancel_event is None or cancel_event.is_set():
            return False
        cancel_event.set()
        return True

    def _refresh_remote_tasks(self) -> None:
        snapshot = self.service.list_remote_task_summaries()
        tree = getattr(self, "remote_task_tree", None)
        if tree is None:
            return
        for item in tree.get_children():
            tree.delete(item)
        self.remote_task_rows = {}
        for index, record in enumerate(snapshot.tasks):
            iid = record.remote_task_id or f"remote-pending-{index}"
            self.remote_task_rows[iid] = record
            progress_text = (
                f"{record.current}/{record.total}"
                if record.current is not None and record.total is not None
                else record.stage
            )
            tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    _remote_task_status_text(record),
                    record.label,
                    record.session_id or "尚未关联记录",
                    progress_text,
                    record.updated_at.replace("T", " ").split("+")[0],
                ),
            )
        primary = _primary_remote_task(snapshot.tasks)
        self._set_remote_task_progress_state(primary)
        if snapshot.availability_message:
            self.remote_task_status_var.set(snapshot.availability_message)
        else:
            active = [
                record
                for record in snapshot.tasks
                if getattr(record, "status", "") in {"queued", "running", "cancelling"}
                and getattr(record, "attachment_state", "") != "lost"
            ]
            lost_count = sum(
                1
                for record in snapshot.tasks
                if str(getattr(record, "attachment_state", "") or "").strip() == "lost"
            )
            if active and primary is not None:
                self.remote_task_status_var.set(f"远端活动任务 {len(active)} 项。{primary.message}")
            elif snapshot.tasks:
                suffix = f"历史 {len(snapshot.tasks)} 项"
                if lost_count:
                    suffix = f"{suffix}，其中已丢失 {lost_count} 项"
                self.remote_task_status_var.set(f"远端没有活动任务（{suffix}）。")
            else:
                self.remote_task_status_var.set("当前没有远端任务。")
        self._on_remote_task_select(None)

    def _selected_remote_task(self) -> object | None:
        tree = getattr(self, "remote_task_tree", None)
        if tree is None:
            return None
        selection = tree.selection()
        if not selection:
            return None
        return self.remote_task_rows.get(selection[0])

    def _on_remote_task_select(self, _event: object | None) -> None:
        record = self._selected_remote_task()
        open_button = getattr(self, "open_remote_task_button", None)
        cancel_button = getattr(self, "cancel_remote_task_button", None)
        if open_button is not None:
            can_open = bool(record is not None and getattr(record, "session_id", None))
            open_button.configure(state="normal" if can_open else "disabled")
        if cancel_button is not None:
            can_retry_sync = _remote_task_requires_sync(record)
            can_cancel = bool(
                record is not None
                and getattr(record, "remote_task_id", None)
                and getattr(record, "can_cancel", False)
                and getattr(record, "attachment_state", "") != "lost"
            )
            if can_retry_sync:
                cancel_button.configure(
                    text="重试同步",
                    command=self._retry_selected_remote_task_sync,
                    state="normal",
                )
            else:
                cancel_button.configure(
                    text="取消",
                    command=self._cancel_selected_remote_task,
                    state="normal" if can_cancel else "disabled",
                )

    def _open_remote_task_session(self) -> None:
        record = self._selected_remote_task()
        if record is None:
            return
        session_id = getattr(record, "session_id", None)
        if not session_id:
            return
        self._refresh_history()
        if session_id in self.history_rows:
            self.history_tree.selection_set(session_id)
            self.history_tree.focus(session_id)
            self.history_tree.see(session_id)
            self._on_history_select(None)

    def _cancel_selected_remote_task(self) -> None:
        record = self._selected_remote_task()
        if record is None:
            return
        remote_task_id = getattr(record, "remote_task_id", None)
        if not remote_task_id:
            return
        self.service.cancel_remote_task(remote_task_id)
        self._append_log(f"已请求取消远端任务：{remote_task_id}")
        self._refresh_remote_tasks()

    def _retry_selected_remote_task_sync(self) -> None:
        record = self._selected_remote_task()
        if record is None:
            return
        remote_task_id = getattr(record, "remote_task_id", None)
        if not remote_task_id:
            return
        self.service.sync_remote_task(remote_task_id)
        self._append_log(f"已重试同步远端任务：{remote_task_id}")
        self._refresh_remote_tasks()

    def _next_remote_task_poll_interval_ms(self) -> int:
        records = list(getattr(self, "remote_task_rows", {}).values())
        has_active = any(
            getattr(record, "status", "") in {"queued", "running", "cancelling"}
            and getattr(record, "attachment_state", "") != "lost"
            for record in records
        )
        if not has_active:
            return REMOTE_TASK_POLL_IDLE_MS
        notebook = getattr(self, "main_notebook", None)
        history_tab = getattr(self, "history_tab", None)
        if (
            notebook is not None
            and history_tab is not None
            and notebook.select() == str(history_tab)
        ):
            return REMOTE_TASK_POLL_ACTIVE_MS
        return REMOTE_TASK_POLL_BACKGROUND_MS

    def _poll_remote_tasks(self) -> None:
        try:
            self._refresh_remote_tasks()
        finally:
            self.root.after(self._next_remote_task_poll_interval_ms(), self._poll_remote_tasks)

    def _start_live_task(
        self,
        task_id: str,
        label: str,
        action: Callable[[], int],
    ) -> None:
        if self.busy:
            messagebox.showinfo("任务进行中", "请先停止当前录音。")
            return
        self._task_state_model().start_live(task_id=task_id, label=label)
        self.start_live_button.configure(state="disabled")
        self.status_var.set(f"{label}：准备中")
        self._set_live_progress_state(f"{label}：准备中")

        def worker() -> None:
            try:
                result = action()
            except Exception as exc:
                self.event_queue.put(("task_error", "live", task_id, label, str(exc)))
            else:
                self.event_queue.put(("task_done", "live", task_id, label, result))

        self.current_worker = threading.Thread(target=worker, daemon=False)
        self.current_worker.start()

    def _start_live_session(self) -> None:
        if not self._ensure_ready_for_run():
            return
        try:
            auto_stop_seconds = self._parse_live_auto_stop_seconds()
        except ValueError as exc:
            messagebox.showwarning("自动停止无效", str(exc))
            return
        title = self.live_title_var.get().strip()
        if not title:
            messagebox.showwarning("信息不完整", "请填写会话标题。")
            return
        if not self.live_devices:
            messagebox.showwarning("没有输入设备", "请先刷新并选择一个输入设备。")
            return
        index = self.live_device_combo.current()
        if index < 0:
            messagebox.showwarning("没有输入设备", "请先选择一个输入设备。")
            return
        device = self.live_devices[index]
        task_id = self._next_task_id()
        self._reset_live_input_meter()
        try:
            runner = self.service.create_live_coordinator(
                title=title,
                source=str(device.index),
                kind=self.live_kind_var.get(),
                language=_optional_language_override(self.live_language_var.get()),
                on_progress=self._progress_callback("live", task_id),
                auto_refine_after_live=(
                    self.live_auto_refine_var.get()
                    if self.refine_enabled_var.get() and self.save_session_wav_var.get()
                    else False
                ),
                speaker_enabled=self.live_speaker_enabled_var.get(),
            )
        except Exception as exc:
            messagebox.showerror("启动失败", str(exc))
            return
        self.current_live_runner = runner
        self._append_log(self.execution_target_var.get())
        self._start_live_task(task_id, "实时录音", runner.run)
        self._arm_live_auto_stop(auto_stop_seconds)
        self.stop_live_button.configure(state="normal")
        self.pause_live_button.configure(state="normal", text="暂停录音")

    def _stop_live_session(self) -> None:
        self._request_live_stop("已请求停止录音，等待当前片段收尾。")

    def _toggle_live_pause(self) -> None:
        if self.current_live_runner is None:
            return
        if self.current_live_runner.is_paused:
            self._resume_live_auto_stop()
            self._resume_live_recording()
            self.pause_live_button.configure(text="暂停录音")
            self._append_log("已请求继续录音。")
        else:
            self._pause_live_auto_stop()
            self._pause_live_recording()
            self.pause_live_button.configure(text="继续录音")
            self._append_log("已请求暂停录音。")

    def _parse_live_auto_stop_seconds(self) -> int | None:
        return parse_auto_stop_seconds(self.live_stop_after_minutes_var.get())

    def _request_live_stop(self, message: str = "已请求停止录音，等待当前片段收尾。") -> None:
        if self.current_live_runner is None:
            return
        self._cancel_live_auto_stop()
        self._live_controller().request_stop()
        self.stop_live_button.configure(state="disabled")
        self.pause_live_button.configure(state="disabled")
        self._append_log(message)

    def _arm_live_auto_stop(self, seconds: int | None) -> None:
        self._live_controller().arm_auto_stop(seconds)

    def _cancel_live_auto_stop(self) -> None:
        self._live_controller().cancel_auto_stop()

    def _pause_live_auto_stop(self) -> None:
        self._live_controller().pause_auto_stop()

    def _resume_live_auto_stop(self) -> None:
        self._live_controller().resume_auto_stop()

    def _pause_live_recording(self) -> None:
        self._live_controller().request_pause()

    def _resume_live_recording(self) -> None:
        self._live_controller().request_resume()

    def _on_live_auto_stop(self) -> None:
        self._request_live_stop("已到自动停止时间，等待当前片段收尾。")

    def _start_import(self) -> None:
        if not self._ensure_queue_ready():
            return
        media_path = Path(self.import_file_var.get().strip()).expanduser()
        if not media_path.exists():
            messagebox.showwarning("文件不存在", "请选择一个有效的本地音频或视频文件。")
            return
        request = build_import_task_request(
            file_path=media_path,
            title=self.import_title_var.get().strip() or None,
            kind=self.import_kind_var.get(),
            language=_optional_language_override(self.import_language_var.get()),
            speaker_enabled=self.import_speaker_enabled_var.get(),
        )
        self._enqueue_queue_task(
            label=request.label,
            action=request.action,
            payload=request.payload,
        )

    def _retry_retranscribe(self) -> None:
        summary = self._selected_summary()
        if summary is None or not self._ensure_queue_ready():
            return
        request = build_session_task_request(
            label="重转写并重写",
            operation="retranscribe",
            session_id=summary.session_id,
        )
        self._enqueue_queue_task(
            label=request.label,
            action=request.action,
            payload=request.payload,
        )

    def _retry_refine(self) -> None:
        summary = self._selected_summary()
        if summary is None:
            return
        if not _summary_supports_refine(summary):
            messagebox.showinfo(
                "无法离线精修",
                "所选会话没有可用的整场录音（session.live.wav），无法执行离线精修并重写。",
            )
            return
        if not self._ensure_queue_ready():
            return
        request = build_session_task_request(
            label="离线精修并重写",
            operation="refine",
            session_id=summary.session_id,
        )
        self._enqueue_queue_task(
            label=request.label,
            action=request.action,
            payload=request.payload,
        )

    def _merge_selected_sessions(self) -> None:
        summaries = self._selected_summaries(min_count=2)
        if summaries is None or not self._ensure_queue_ready():
            return
        if any(getattr(summary, "execution_target", "local") == "remote" for summary in summaries):
            messagebox.showinfo(
                "暂不支持",
                "远端会话暂不支持在桌面端直接合并，请先分别完成整理或后续在服务端处理。",
            )
            return
        if not messagebox.askyesno(
            "合并会话",
            (
                f"将按开始时间顺序合并 {len(summaries)} 条会话，并生成一条新的合并会话。"
                "原始会话会保留。是否继续？"
            ),
        ):
            return
        request = build_merge_task_request(
            session_ids=[summary.session_id for summary in summaries],
        )
        self._enqueue_queue_task(
            label=request.label,
            action=request.action,
            payload=request.payload,
        )

    def _retry_republish(self) -> None:
        summary = self._selected_summary()
        if summary is None or not self._ensure_queue_ready():
            return
        request = build_session_task_request(
            label="重新生成整理",
            operation="republish",
            session_id=summary.session_id,
        )
        self._enqueue_queue_task(
            label=request.label,
            action=request.action,
            payload=request.payload,
        )

    def _retry_resync(self) -> None:
        summary = self._selected_summary()
        if summary is None or not self._ensure_queue_ready():
            return
        request = build_session_task_request(
            label="重新同步 Obsidian",
            operation="resync",
            session_id=summary.session_id,
        )
        self._enqueue_queue_task(
            label=request.label,
            action=request.action,
            payload=request.payload,
        )

    def _run_background(
        self,
        label: str,
        action: Callable[[], int],
        *,
        detachable_live: bool = False,
    ) -> None:
        if self.busy:
            messagebox.showinfo("任务进行中", "请等待当前任务完成，或先停止录音。")
            return
        task_id = self._next_task_id()
        self._task_state_model().start_foreground(
            task_id=task_id,
            label=label,
            detachable_live=detachable_live,
        )
        self.start_live_button.configure(state="disabled")
        self.status_var.set(f"{label}：准备中")
        self.progress.configure(mode="indeterminate", value=0)
        self.progress.start(self._progress_animation_interval_ms())
        self._set_task_progress_state(f"{label}：准备中")

        def worker() -> None:
            try:
                result = action()
            except Exception as exc:
                self.event_queue.put(("task_error", task_id, label, str(exc)))
            else:
                self.event_queue.put(("task_done", task_id, label, result))

        self.current_worker = threading.Thread(target=worker, daemon=False)
        self.current_worker.start()

    def _refresh_history(self) -> None:
        selected = self.history_tree.selection()
        selected_id = selected[0] if selected else None
        self.history_rows = {}
        for item in self.history_tree.get_children():
            self.history_tree.delete(item)
        for summary in self.service.list_session_summaries():
            self.history_rows[summary.session_id] = summary
            self.history_tree.insert(
                "",
                "end",
                iid=summary.session_id,
                values=(
                    summary.started_at.replace("T", " ").split("+")[0],
                    summary.title,
                    summary.kind,
                    summary.input_mode,
                    summary.status,
                    summary.segment_count,
                    summary.failed_count,
                ),
            )
        if selected_id and selected_id in self.history_rows:
            self.history_tree.selection_set(selected_id)
            self.history_tree.focus(selected_id)
        summaries = self._selected_summaries(prompt=False)
        self._update_history_action_states(summaries)
        detail = build_history_detail(summaries)
        self.history_detail_var.set(detail or "选择一条历史会话查看详情。")

    def _on_history_select(self, _event: object) -> None:
        summaries = self._selected_summaries(prompt=False)
        self._update_history_action_states(summaries)
        detail = build_history_detail(summaries)
        self.history_detail_var.set(detail or "选择一条历史会话查看详情。")

    def _selected_summaries(
        self,
        *,
        prompt: bool = True,
        min_count: int = 1,
    ) -> list[SessionSummary] | None:
        selection = self.history_tree.selection()
        if len(selection) < min_count:
            if prompt:
                if min_count > 1:
                    message = f"请先从历史列表中选择至少 {min_count} 条会话。"
                else:
                    message = "请先从历史列表中选择一条会话。"
                messagebox.showinfo("请选择会话", message)
            return None
        summaries = [self.history_rows[item] for item in selection if item in self.history_rows]
        if len(summaries) < min_count:
            if prompt:
                if min_count > 1:
                    message = f"请先从历史列表中选择至少 {min_count} 条会话。"
                else:
                    message = "请先从历史列表中选择一条会话。"
                messagebox.showinfo("请选择会话", message)
            return None
        return summaries

    def _selected_summary(self, prompt: bool = True) -> SessionSummary | None:
        summaries = self._selected_summaries(prompt=prompt)
        if not summaries:
            return None
        return summaries[0]

    def _update_history_action_states(self, summaries: list[SessionSummary] | None) -> None:
        refine_button = getattr(self, "retry_refine_button", None)
        merge_button = getattr(self, "merge_sessions_button", None)
        if refine_button is None:
            return
        if summaries and len(summaries) == 1 and _summary_supports_refine(summaries[0]):
            refine_button.configure(state="normal")
        else:
            refine_button.configure(state="disabled")
        if merge_button is not None:
            enabled = can_merge_summaries(summaries)
            merge_button.configure(state="normal" if enabled else "disabled")

    def _on_history_actions_resize(self, event: tk.Event[tk.Misc]) -> None:
        self._relayout_history_actions(event.width)

    def _relayout_history_actions(self, available_width: int | None = None) -> None:
        frame = getattr(self, "history_actions_frame", None)
        buttons = getattr(self, "history_action_buttons", None)
        if frame is None or not buttons:
            return
        resolved_width = available_width or frame.winfo_width()
        item_widths = [button.winfo_reqwidth() for button in buttons]
        if resolved_width <= 1:
            resolved_width = sum(item_widths) + max(0, len(item_widths) - 1) * 8
        positions = _wrap_action_rows(resolved_width, item_widths, gap=8)
        max_columns = max((column for _, column in positions), default=-1) + 1
        for index in range(max(len(buttons), max_columns)):
            frame.grid_columnconfigure(index, weight=0)
        for button in buttons:
            button.grid_forget()
        for button, (row, column) in zip(buttons, positions, strict=False):
            button.grid(
                row=row,
                column=column,
                sticky="w",
                padx=(0 if column == 0 else 8, 0),
                pady=(0 if row == 0 else 8, 0),
            )
        frame.update_idletasks()

    def _open_transcript(self) -> None:
        summary = self._selected_summary()
        if summary is None:
            return
        self._open_path(summary.transcript_file)

    def _open_structured(self) -> None:
        summary = self._selected_summary()
        if summary is None:
            return
        self._open_path(summary.structured_file)

    def _open_session_dir(self) -> None:
        summary = self._selected_summary()
        if summary is None:
            return
        self._open_path(summary.session_dir)

    def _open_path(self, path: Path) -> None:
        if not path.exists():
            messagebox.showwarning("文件不存在", f"{path} 还不存在。")
            return
        self.service.open_path(path)

    def _autodetect_settings(self) -> None:
        self._load_settings(self.service.detect_settings_draft())
        self._append_log("已根据本机环境重新检测默认路径。")

    def _save_settings(self) -> None:
        try:
            self.service.save_settings(self._current_settings())
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))
            return
        self._append_log("设置已保存到 config.toml 和 .env。")
        self._refresh_doctor_checks()
        self._refresh_history()
        self._refresh_devices()

    def _refresh_doctor_checks(self) -> None:
        checks = self.service.doctor_checks()
        for item in self.doctor_tree.get_children():
            self.doctor_tree.delete(item)
        for index, check in enumerate(checks):
            self.doctor_tree.insert(
                "",
                "end",
                iid=f"check-{index}",
                values=(check.name, check.status, check.detail),
            )
        self._update_execution_target_hint(checks)

    def _update_execution_target_hint(self, checks: list[object] | None = None) -> None:
        remote_enabled = bool(self.remote_enabled_var.get())
        remote_base_url = str(self.remote_base_url_var.get()).strip()
        remote_health_status: str | None = None
        for check in checks or []:
            if getattr(check, "name", None) == "remote_health":
                remote_health_status = str(getattr(check, "status", "")).strip() or None
                break
        self.execution_target_var.set(
            _build_execution_target_hint(
                remote_enabled,
                remote_base_url,
                remote_health_status,
            )
        )

    def _show_first_run_wizard(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("首次启动向导")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.geometry("760x420")
        dialog.configure(bg=self._theme_palette().app_bg)
        dialog.columnconfigure(0, weight=1)

        frame = ttk.Frame(dialog, padding=16)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(1, weight=1)
        ttk.Label(
            frame,
            text=(
                "先完成一次本机检测和最小配置，之后就可以用 GUI 直接录音或导入文件。"
                "早期也可以只启用本地转写。"
            ),
            wraplength=700,
            style="Subtle.TLabel",
        ).grid(row=0, column=0, columnspan=2, sticky="w")

        _entry_row_with_button(
            frame,
            1,
            "ffmpeg",
            self.ffmpeg_var,
            "可执行文件路径",
            lambda: self._browse_binary(self.ffmpeg_var),
        )
        _entry_row_with_button(
            frame,
            2,
            "whisper-server",
            self.whisper_binary_var,
            "可执行文件路径",
            lambda: self._browse_binary(self.whisper_binary_var),
        )
        _entry_row_with_button(
            frame,
            3,
            "模型文件",
            self.whisper_model_var,
            "选择 ggml 模型文件",
            lambda: self._browse_file(self.whisper_model_var, "选择 Whisper 模型"),
        )
        ttk.Checkbutton(
            frame,
            text="保存整场 live WAV（session.live.wav）",
            variable=self.save_session_wav_var,
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(14, 0))
        ttk.Checkbutton(
            frame,
            text="启用离线精修",
            variable=self.refine_enabled_var,
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Checkbutton(
            frame,
            text="live 结束后自动离线精修",
            variable=self.refine_auto_after_live_var,
        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Checkbutton(
            frame,
            text="启用同步到 Obsidian",
            variable=self.obsidian_enabled_var,
        ).grid(row=7, column=0, columnspan=2, sticky="w", pady=(14, 0))
        _entry_row(frame, 8, "Obsidian URL", self.obsidian_base_url_var)
        _entry_row(frame, 9, "Obsidian API Key", self.obsidian_api_key_var)
        ttk.Checkbutton(
            frame,
            text="启用自动整理",
            variable=self.llm_enabled_var,
        ).grid(row=10, column=0, columnspan=2, sticky="w", pady=(14, 0))
        _entry_row(frame, 11, "LLM Base URL", self.llm_base_url_var)
        _entry_row(frame, 12, "LLM 模型名", self.llm_model_var)
        _combobox_row(frame, 13, "LLM 协议", self.llm_wire_api_var, LLM_WIRE_API_CHOICES)
        ttk.Checkbutton(
            frame,
            text="LLM 使用 Stream 模式",
            variable=self.llm_stream_var,
        ).grid(row=14, column=0, columnspan=2, sticky="w", pady=(14, 0))
        ttk.Checkbutton(
            frame,
            text="优先使用 OPENAI_API_KEY 鉴权",
            variable=self.llm_requires_openai_auth_var,
        ).grid(row=15, column=0, columnspan=2, sticky="w", pady=(14, 0))
        _entry_row(frame, 16, "LLM API Key", self.llm_api_key_var)

        actions = ttk.Frame(frame)
        actions.grid(row=17, column=0, columnspan=2, sticky="w", pady=(18, 0))
        ttk.Button(
            actions,
            text="重新检测",
            command=self._autodetect_settings,
        ).grid(row=0, column=0)

        def save_and_close() -> None:
            try:
                self.service.save_settings(self._current_settings())
            except Exception as exc:
                messagebox.showerror("保存失败", str(exc), parent=dialog)
                return
            dialog.destroy()
            self._append_log("首次启动配置已保存。")
            self._refresh_doctor_checks()
            self._refresh_devices()

        ttk.Button(
            actions,
            text="保存并开始",
            command=save_and_close,
            style="Primary.TButton",
        ).grid(row=0, column=1, padx=(8, 0))
        ttk.Button(actions, text="稍后配置", command=dialog.destroy).grid(
            row=0, column=2, padx=(8, 0)
        )

    def _on_refine_prerequisite_changed(self, *_args: object) -> None:
        self._update_live_auto_refine_state()

    def _update_live_auto_refine_state(self) -> None:
        checkbutton = getattr(self, "live_auto_refine_checkbutton", None)
        if checkbutton is None:
            return
        enabled = bool(self.refine_enabled_var.get()) and bool(self.save_session_wav_var.get())
        checkbutton.configure(state="normal" if enabled else "disabled")
        if not enabled:
            self.live_auto_refine_var.set(False)

    def _ensure_ready_for_run(self) -> bool:
        if self.busy:
            messagebox.showinfo("任务进行中", "请等待当前任务完成，或先停止录音。")
            return False
        if not self.service.config_exists():
            messagebox.showinfo("需要先配置", "请先完成首次启动向导或在设置页保存配置。")
            self._show_first_run_wizard()
            return False
        return True

    def _progress_animation_interval_ms(self) -> int:
        return PROGRESS_ANIMATION_INTERVAL_MS

    def _next_poll_interval_ms(self, *, processed_events: bool = False) -> int:
        if processed_events:
            return POLL_ACTIVE_INTERVAL_MS
        if getattr(self, "busy", False):
            return POLL_ACTIVE_INTERVAL_MS
        if getattr(self, "queue_worker", None) is not None:
            return POLL_ACTIVE_INTERVAL_MS
        if getattr(self, "background_tasks", {}):
            return POLL_ACTIVE_INTERVAL_MS
        return POLL_IDLE_INTERVAL_MS

    def _poll_events(self) -> None:
        processed_events = False
        while True:
            try:
                item = self.event_queue.get_nowait()
            except queue.Empty:
                break
            processed_events = True

            if isinstance(item, ProgressEvent):
                self._handle_progress(item)
                continue

            if len(item) == 5:
                event_type, source, task_id, label, payload = item
            else:
                event_type, task_id, label, payload = item
                source = "live"
            if source == "queue":
                self._handle_queue_terminal_event(event_type, task_id, label, payload)
            elif event_type == "task_error":
                if task_id == self.current_task_id:
                    self._finish_task()
                    self._append_log(f"{label}失败：{payload}")
                    messagebox.showerror(f"{label}失败", str(payload))
                else:
                    self._finish_background_task(task_id)
                    self._append_log(f"{label}后台失败：{payload}")
                    self._refresh_history()
                    self._refresh_doctor_checks()
            elif event_type == "task_done":
                if task_id == self.current_task_id:
                    self._finish_task()
                    self._append_log(f"{label}完成。")
                    self._refresh_history()
                    self._refresh_doctor_checks()
                else:
                    self._finish_background_task(task_id)
                    self._append_log(f"{label}后台完成。")
                    self._refresh_history()
                    self._refresh_doctor_checks()
        self.root.after(
            self._next_poll_interval_ms(processed_events=processed_events),
            self._poll_events,
        )

    def _handle_progress(self, event: ProgressEvent) -> None:
        if event.source == "queue":
            self._handle_queue_progress(event)
            return
        if event.source == "live":
            self._handle_live_progress(event)
            return

        is_foreground = self._is_foreground_event(event)
        if is_foreground and event.stage == "capture_finished" and self.current_live_task_id:
            self._append_log(event.message)
            self._detach_live_task(event.session_id)
            self._refresh_history()
            return

        if not is_foreground:
            prefix = f"[后台 {event.session_id}] " if event.session_id else "[后台] "
            self._append_log(f"{prefix}{event.message}")
            return

        self.status_var.set(event.message)
        self._append_log(event.message)
        self._set_task_progress_state(
            event.message,
            current=event.current,
            total=event.total,
            active=event.stage not in {"done", "error"},
        )

    def _handle_live_progress(self, event: ProgressEvent) -> None:
        if event.stage == "input_level":
            if event.task_id != self.current_live_task_id:
                return
            self._set_live_input_meter(level=event.current or 0, state=event.message or "No signal")
            return
        if event.stage == "capture_finished" and event.task_id == self.current_live_task_id:
            self._append_log(event.message)
            self._detach_live_task(event.session_id)
            self._refresh_history()
            return
        if event.task_id in self.background_tasks:
            prefix = f"[后台 {event.session_id}] " if event.session_id else "[后台] "
            self._append_log(f"{prefix}{event.message}")
            return
        self.status_var.set(event.message)
        self._append_log(event.message)
        self._set_live_progress_state(
            event.message,
            current=event.current,
            total=event.total,
            active=event.stage not in {"done", "error"},
        )

    def _handle_queue_progress(self, event: ProgressEvent) -> None:
        if self.queue_current_task_id is not None and event.task_id != self.queue_current_task_id:
            return
        if not self.busy:
            self.status_var.set(event.message)
        self._append_log(f"[队列] {event.message}")
        self._set_queue_progress_state(
            event.message,
            current=event.current,
            total=event.total,
            active=event.stage not in {"done", "error"},
        )

    def _handle_queue_terminal_event(
        self,
        event_type: str,
        task_id: str,
        label: str,
        payload: object,
    ) -> None:
        self._finish_queue_task(task_id)
        if event_type == "task_error":
            self._append_log(f"{label}失败：{payload}")
            messagebox.showerror(f"{label}失败", str(payload))
        elif event_type == "task_cancelled":
            self._append_log(f"{label}已取消。")
        else:
            self._append_log(f"{label}完成。")
        self._refresh_history()
        self._refresh_doctor_checks()
        self._maybe_start_next_queue_task()

    def _finish_task(self) -> None:
        self._live_controller().clear()
        self._task_state_model().finish_foreground()
        self.current_worker = None
        self.start_live_button.configure(state="normal")
        self._reset_live_controls()
        self._reset_live_input_meter()
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        self._update_idle_status()
        self._maybe_start_next_queue_task()

    def _finish_queue_task(self, task_id: str) -> None:
        if task_id == self.queue_current_task_id:
            self.queue_worker = None
            self._task_state_model().finish_queue(task_id)
        if task_id == getattr(self, "queue_cancel_task_id", None):
            self.queue_cancel_task_id = None
            self.queue_cancel_action = None
            self.queue_cancel_event = None
        self._refresh_queue_tree()
        queued = self._queued_count()
        if queued:
            self._set_queue_progress_state(f"等待执行 {queued} 项。", active=False)
        elif not self.busy:
            self._set_queue_progress_state("当前没有任务。", active=False)
        self._update_idle_status()

    def _finish_background_task(self, task_id: str) -> None:
        self._task_state_model().finish_background(task_id)
        self._update_idle_status()
        self._maybe_start_next_queue_task()

    def _detach_live_task(self, session_id: str | None) -> None:
        if self._task_state_model().detach_live(session_id=session_id) is None:
            return
        self._live_controller().clear()
        self.current_worker = None
        self.start_live_button.configure(state="normal")
        self._reset_live_controls()
        self._reset_live_input_meter()
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        self._update_idle_status()

    def _is_foreground_event(self, event: ProgressEvent) -> bool:
        return self._task_state_model().is_foreground_event(event)

    def _find_background_task_by_session(self, session_id: str) -> str | None:
        return self._task_state_model().find_background_task_by_session(session_id)

    def _next_task_id(self) -> str:
        runtime = getattr(self, "queue_runtime", None)
        if runtime is not None:
            task_id = runtime.next_task_id()
            self._sync_queue_compat_state()
            return task_id
        task_sequence = getattr(self, "task_sequence", 0) + 1
        self.task_sequence = task_sequence
        return f"task-{task_sequence:04d}"

    def _sync_task_sequence(self, records: list[QueuedTaskRecord]) -> None:
        runtime = getattr(self, "queue_runtime", None)
        if runtime is not None:
            self.queue_records = runtime.records
            return
        max_seen = getattr(self, "task_sequence", 0)
        for record in records:
            prefix, _, suffix = record.task_id.partition("-")
            if prefix != "task" or not suffix.isdigit():
                continue
            max_seen = max(max_seen, int(suffix))
        self.task_sequence = max_seen

    def _reset_live_controls(self) -> None:
        self.stop_live_button.configure(state="disabled")
        self.pause_live_button.configure(state="disabled", text="暂停录音")

    def _update_idle_status(self) -> None:
        if self.busy:
            return
        if getattr(self, "queue_worker", None) is not None:
            return
        background_tasks = getattr(self, "background_tasks", {})
        pending = len(background_tasks)
        if pending:
            self.status_var.set(f"准备就绪（后台处理中 {pending} 项）")
            queued = self._queued_count()
            if queued:
                self._set_queue_progress_state(f"等待执行 {queued} 项。", active=False)
            else:
                self._set_queue_progress_state(f"后台处理中 {pending} 项。", active=False)
            return
        queued = self._queued_count()
        if queued:
            self.status_var.set(f"准备就绪（队列中 {queued} 项）")
            self._set_queue_progress_state(f"等待执行 {queued} 项。", active=False)
            return
        self.status_var.set("准备就绪")
        self._set_queue_progress_state("当前没有任务。", active=False)

    def _set_live_progress_state(
        self,
        message: str,
        *,
        current: int | None = None,
        total: int | None = None,
        active: bool = True,
    ) -> None:
        if total:
            self.progress.stop()
            self.progress.configure(mode="determinate")
            self.progress["value"] = round((current or 0) / total * 100)
            return
        if active:
            self.progress.configure(mode="indeterminate")
            self.progress.start(self._progress_animation_interval_ms())
            return
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)

    def _set_live_input_meter(self, *, level: int, state: str) -> None:
        style_name = _input_meter_style_name(state)
        if hasattr(self, "live_input_level_var"):
            self.live_input_level_var.set(state)
        meter = getattr(self, "live_input_meter", None)
        if meter is not None:
            meter.stop()
            meter.configure(mode="determinate")
            meter["value"] = max(0, min(100, level))
            meter.configure(style=f"InputMeter.{style_name}.Horizontal.TProgressbar")
        status = getattr(self, "live_input_meter_status", None)
        if status is not None:
            status.configure(style=f"InputMeter.{style_name}.TLabel")

    def _reset_live_input_meter(self) -> None:
        self._set_live_input_meter(level=0, state="No signal")

    def _set_queue_progress_state(
        self,
        message: str,
        *,
        current: int | None = None,
        total: int | None = None,
        active: bool = True,
    ) -> None:
        if hasattr(self, "task_progress_var"):
            self.task_progress_var.set(message)
        self._apply_progress_widget(
            getattr(self, "history_progress", None),
            current=current,
            total=total,
            active=active,
        )
        self._apply_progress_widget(
            getattr(self, "progress", None),
            current=current,
            total=total,
            active=active,
        )

    def _set_task_progress_state(
        self,
        message: str,
        *,
        current: int | None = None,
        total: int | None = None,
        active: bool = True,
    ) -> None:
        self._set_queue_progress_state(
            message,
            current=current,
            total=total,
            active=active,
        )

    def _set_remote_task_progress_state(self, record: object | None) -> None:
        widget = getattr(self, "remote_task_progress", None)
        if widget is None:
            return
        if record is None:
            self._apply_progress_widget(widget, current=None, total=None, active=False)
            return
        if str(getattr(record, "attachment_state", "") or "").strip() == "lost":
            self._apply_progress_widget(widget, current=None, total=None, active=False)
            return
        status = str(getattr(record, "status", "") or "").strip()
        current = getattr(record, "current", None)
        total = getattr(record, "total", None)
        self._apply_progress_widget(
            widget,
            current=current,
            total=total,
            active=status in {"queued", "running", "cancelling"},
        )

    def _apply_progress_widget(
        self,
        widget: ttk.Progressbar | object | None,
        *,
        current: int | None,
        total: int | None,
        active: bool,
    ) -> None:
        if widget is None:
            return
        if total:
            widget.stop()
            widget.configure(mode="determinate")
            widget["value"] = round((current or 0) / total * 100)
            return
        if active:
            widget.configure(mode="indeterminate")
            widget.start(self._progress_animation_interval_ms())
            return
        widget.stop()
        widget.configure(mode="determinate", value=0)

    def _append_log(self, line: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{line}\n")
        self.log_text.see("end")
        if int(self.log_text.index("end-1c").split(".")[0]) > 300:
            self.log_text.delete("1.0", "50.0")
        self.log_text.configure(state="disabled")

    def _on_close(self) -> None:
        if self.busy and self.current_live_runner is not None:
            if self._live_controller().is_stopping:
                if not messagebox.askyesno(
                    "退出",
                    "当前录音正在停止中。关闭窗口后会继续等待后台收尾。是否继续关闭窗口？",
                ):
                    return
                self.root.destroy()
                return
            if messagebox.askyesno("退出", "当前仍在录音。要先发送停止请求再退出吗？"):
                self._request_live_stop()
                return
        if self.busy:
            if not messagebox.askyesno("退出", "当前任务仍在进行，确定直接关闭窗口吗？"):
                return
        elif self.queue_worker is not None:
            queued = self._queued_count()
            if not messagebox.askyesno(
                "退出",
                (
                    "当前仍有离线任务在执行。关闭窗口后，本轮运行中的任务会继续到自然结束；"
                    f"剩余 {queued} 项待执行任务会保留到下次启动。是否继续关闭窗口？"
                ),
            ):
                return
        elif self.background_tasks:
            if not messagebox.askyesno(
                "退出",
                (
                    f"仍有 {len(self.background_tasks)} 个后台任务在继续处理。"
                    "关闭窗口后进程会继续运行直到这些任务完成。是否继续关闭窗口？"
                ),
            ):
                return
        elif self._queued_count():
            if not messagebox.askyesno(
                "退出",
                "仍有待执行的排队任务。关闭窗口后它们会在下次启动时恢复。是否继续关闭窗口？",
            ):
                return
        self.root.destroy()

    def _queue_runtime(self) -> TaskQueueRuntime:
        runtime = getattr(self, "queue_runtime", None)
        if runtime is None:
            store = getattr(self, "queue_store", None)
            if store is None:
                service = getattr(self, "service", None)
                if service is not None:
                    store = TaskQueueStore(service.task_queue_path())
                else:
                    store = _InMemoryQueueStore(list(getattr(self, "queue_records", [])))
                self.queue_store = store
            runtime = TaskQueueRuntime(
                store,
                initial_records=list(getattr(self, "queue_records", [])),
            )
            self.queue_runtime = runtime
        return runtime

    def _sync_queue_compat_state(self) -> None:
        runtime = getattr(self, "queue_runtime", None)
        if runtime is None:
            return
        self.queue_records = runtime.records

    def _queue_records_snapshot(self) -> list[QueuedTaskRecord]:
        runtime = getattr(self, "queue_runtime", None)
        if runtime is not None:
            return runtime.records
        return list(getattr(self, "queue_records", []))

    def _queued_count(self) -> int:
        runtime = getattr(self, "queue_runtime", None)
        if runtime is not None:
            return runtime.queued_count()
        return sum(1 for record in getattr(self, "queue_records", []) if record.status == "queued")

    def _queue_record(self, task_id: str) -> QueuedTaskRecord | None:
        runtime = getattr(self, "queue_runtime", None)
        if runtime is not None:
            return runtime.get(task_id)
        return next(
            (record for record in getattr(self, "queue_records", []) if record.task_id == task_id),
            None,
        )


def _input_meter_style_name(state: str) -> str:
    return {
        "No signal": "NoSignal",
        "Low": "Low",
        "OK": "OK",
        "High": "High",
        "Clipping": "Clipping",
    }.get(state, "NoSignal")


def _entry_row(
    parent: ttk.Frame,
    row: int,
    label: str,
    variable: tk.StringVar,
    hint: str | None = None,
) -> None:
    metrics = _default_gui_metrics()
    ttk.Label(parent, text=label, style="SectionTitle.TLabel").grid(
        row=row,
        column=0,
        sticky="w",
        pady=(metrics.field_row_gap, 0),
    )
    ttk.Entry(parent, textvariable=variable).grid(
        row=row,
        column=1,
        sticky="ew",
        pady=(metrics.field_row_gap, 0),
    )
    if hint:
        ttk.Label(parent, text=hint, style="Hint.TLabel").grid(
            row=row,
            column=2,
            sticky="w",
            padx=(metrics.inline_gap, 0),
            pady=(metrics.field_row_gap, 0),
        )


class _InMemoryQueueStore:
    def __init__(self, initial_records: list[QueuedTaskRecord]) -> None:
        self._records = list(initial_records)

    def load(self) -> QueueLoadResult:
        return QueueLoadResult(active=list(self._records), interrupted=[], warnings=[])

    def save(self, records: list[QueuedTaskRecord]) -> None:
        self._records = list(records)


def _build_vertical_scroller(parent: ttk.Frame) -> ttk.Frame:
    palette = _default_gui_palette()
    parent.columnconfigure(0, weight=1)
    parent.rowconfigure(0, weight=1)

    canvas = tk.Canvas(
        parent,
        highlightthickness=0,
        borderwidth=0,
        background=palette.surface_bg,
    )
    canvas.grid(row=0, column=0, sticky="nsew")

    scrollbar = ttk.Scrollbar(
        parent,
        orient="vertical",
        command=canvas.yview,
        style="App.Vertical.TScrollbar",
    )
    scrollbar.grid(row=0, column=1, sticky="ns")
    canvas.configure(yscrollcommand=scrollbar.set)

    content = ttk.Frame(canvas)
    content_window = canvas.create_window((0, 0), window=content, anchor="nw")

    def _sync_scroll_region(_event: object) -> None:
        bbox = canvas.bbox("all")
        if bbox is not None:
            canvas.configure(scrollregion=bbox)

    def _sync_content_width(event: tk.Event[tk.Misc]) -> None:
        canvas.itemconfigure(content_window, width=event.width)

    content.bind("<Configure>", _sync_scroll_region)
    canvas.bind("<Configure>", _sync_content_width)
    _bind_mousewheel_scrolling(parent.winfo_toplevel(), canvas)
    return content


def _summary_supports_refine(summary: SessionSummary) -> bool:
    return supports_refine(summary)


def _entry_row_with_button(
    parent: ttk.Frame,
    row: int,
    label: str,
    variable: tk.StringVar,
    hint: str,
    command: Callable[[], None],
) -> None:
    metrics = _default_gui_metrics()
    ttk.Label(parent, text=label, style="SectionTitle.TLabel").grid(
        row=row,
        column=0,
        sticky="w",
        pady=(metrics.field_row_gap, 0),
    )
    wrapper = ttk.Frame(parent)
    wrapper.grid(row=row, column=1, sticky="ew", pady=(metrics.field_row_gap, 0))
    wrapper.columnconfigure(0, weight=1)
    ttk.Entry(wrapper, textvariable=variable).grid(row=0, column=0, sticky="ew")
    ttk.Button(wrapper, text="浏览", command=command).grid(
        row=0,
        column=1,
        padx=(metrics.inline_gap, 0),
    )
    ttk.Label(parent, text=hint, style="Hint.TLabel").grid(
        row=row,
        column=2,
        sticky="w",
        padx=(metrics.inline_gap, 0),
        pady=(metrics.field_row_gap, 0),
    )


def _combobox_row(
    parent: ttk.Frame,
    row: int,
    label: str,
    variable: tk.StringVar,
    values: list[str],
) -> None:
    metrics = _default_gui_metrics()
    ttk.Label(parent, text=label, style="SectionTitle.TLabel").grid(
        row=row,
        column=0,
        sticky="w",
        pady=(metrics.field_row_gap, 0),
    )
    ttk.Combobox(parent, textvariable=variable, state="readonly", values=values).grid(
        row=row,
        column=1,
        sticky="ew",
        pady=(metrics.field_row_gap, 0),
    )


def _language_row(
    parent: ttk.Frame,
    row: int,
    label: str,
    variable: tk.StringVar,
    include_blank: bool,
) -> None:
    metrics = _default_gui_metrics()
    values = SESSION_LANGUAGE_CHOICES if include_blank else DEFAULT_LANGUAGE_CHOICES
    hint = "中英混合或多语言建议选 auto；也可直接输入其他 Whisper 语言代码"
    ttk.Label(parent, text=label, style="SectionTitle.TLabel").grid(
        row=row,
        column=0,
        sticky="w",
        pady=(metrics.field_row_gap, 0),
    )
    ttk.Combobox(parent, textvariable=variable, values=values).grid(
        row=row,
        column=1,
        sticky="ew",
        pady=(metrics.field_row_gap, 0),
    )
    ttk.Label(parent, text=hint, style="Hint.TLabel").grid(
        row=row,
        column=2,
        sticky="w",
        padx=(metrics.inline_gap, 0),
        pady=(metrics.field_row_gap, 0),
    )
