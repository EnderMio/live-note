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

from .coordinator import can_reconstruct_session_live_audio
from .events import ProgressEvent
from .journal import SessionWorkspace
from .services import AppService, SessionSummary, SettingsDraft
from .task_queue import QueuedTaskRecord, TaskQueueStore, build_task_record

KIND_CHOICES = ["generic", "meeting", "lecture"]
LLM_WIRE_API_CHOICES = ["chat_completions", "responses"]
SESSION_LANGUAGE_CHOICES = [
    "沿用默认设置",
    "自动识别 / 中英混合 / 多语言（auto）",
    "中文（zh）",
    "英文（en）",
    "日文（ja）",
    "韩文（ko）",
]
DEFAULT_LANGUAGE_CHOICES = SESSION_LANGUAGE_CHOICES[1:]
LANGUAGE_LABEL_TO_CODE = {
    "沿用默认设置": "",
    "自动识别 / 中英混合 / 多语言（auto）": "auto",
    "中文（zh）": "zh",
    "英文（en）": "en",
    "日文（ja）": "ja",
    "韩文（ko）": "ko",
}
LANGUAGE_CODE_TO_LABEL = {code: label for label, code in LANGUAGE_LABEL_TO_CODE.items() if code}


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
        self.queue_lock = threading.Lock()
        self.task_sequence = 0
        self.current_worker: threading.Thread | None = None
        self.current_task_id: str | None = None
        self.current_task_label: str | None = None
        self.current_task_session_id: str | None = None
        self.current_live_task_id: str | None = None
        self.current_live_runner = None
        self.background_tasks: dict[str, str] = {}
        self.background_task_sessions: dict[str, str | None] = {}
        self.busy = False
        self.queue_store = TaskQueueStore(self.service.task_queue_path())
        self.queue_records: list[QueuedTaskRecord] = []
        self.queue_worker: threading.Thread | None = None
        self.queue_current_task_id: str | None = None
        self.queue_current_task_label: str | None = None
        self.live_devices: list[InputDevice] = []
        self.history_rows: dict[str, SessionSummary] = {}

        self.status_var = tk.StringVar(value="准备就绪")
        self.history_detail_var = tk.StringVar(value="选择一条历史会话查看详情。")
        self.task_progress_var = tk.StringVar(value="当前没有任务。")

        self.live_title_var = tk.StringVar()
        self.live_kind_var = tk.StringVar(value="generic")
        self.live_language_var = tk.StringVar(value="沿用默认设置")
        self.live_device_var = tk.StringVar()

        self.import_file_var = tk.StringVar()
        self.import_title_var = tk.StringVar()
        self.import_kind_var = tk.StringVar(value="generic")
        self.import_language_var = tk.StringVar(value="沿用默认设置")

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

        self.root.title("live-note")
        self.root.geometry("1100x760")
        self.root.minsize(980, 680)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._apply_branding()

        self._build_ui()
        self._load_settings(self.service.load_settings_draft())
        self._load_task_queue_state()
        self._refresh_devices()
        self._refresh_history()
        self._refresh_doctor_checks()
        self.root.after(150, self._poll_events)
        if not self.service.config_exists():
            self.root.after(250, self._show_first_run_wizard)

    def run(self) -> None:
        self.root.mainloop()

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
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        header = ttk.Frame(self.root, padding=(18, 14))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)

        brand_frame = ttk.Frame(header)
        brand_frame.grid(row=0, column=0, rowspan=2, sticky="w")
        brand_frame.columnconfigure(1, weight=1)
        if self.header_logo_image is not None:
            ttk.Label(brand_frame, image=self.header_logo_image).grid(
                row=0, column=0, rowspan=2, sticky="w", padx=(0, 12)
            )
        ttk.Label(
            brand_frame,
            text="live-note",
            font=("SF Pro Text", 22, "bold"),
        ).grid(row=0, column=1, sticky="w")
        ttk.Label(
            brand_frame,
            text="本地优先的课程 / 会议 / 音频内容记录器",
        ).grid(row=1, column=1, sticky="w", pady=(4, 0))
        ttk.Label(
            header,
            textvariable=self.status_var,
            anchor="e",
        ).grid(row=0, column=1, rowspan=2, sticky="e")

        notebook = ttk.Notebook(self.root)
        notebook.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 16))

        new_session_tab = ttk.Frame(notebook, padding=16)
        history_tab = ttk.Frame(notebook, padding=16)
        settings_tab = ttk.Frame(notebook, padding=16)
        notebook.add(new_session_tab, text="新建会话")
        notebook.add(history_tab, text="历史会话")
        notebook.add(settings_tab, text="设置与诊断")

        self._build_new_session_tab(new_session_tab)
        self._build_history_tab(history_tab)
        self._build_settings_tab(settings_tab)

    def _build_new_session_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)

        session_tabs = ttk.Notebook(parent)
        session_tabs.grid(row=0, column=0, sticky="ew")

        live_tab = ttk.Frame(session_tabs, padding=12)
        import_tab = ttk.Frame(session_tabs, padding=12)
        session_tabs.add(live_tab, text="实时录音")
        session_tabs.add(import_tab, text="导入文件")

        self._build_live_tab(live_tab)
        self._build_import_tab(import_tab)

        activity = ttk.LabelFrame(parent, text="运行状态", padding=12)
        activity.grid(row=1, column=0, sticky="nsew", pady=(16, 0))
        activity.columnconfigure(0, weight=1)
        activity.rowconfigure(1, weight=1)

        self.progress = ttk.Progressbar(activity, mode="determinate")
        self.progress.grid(row=0, column=0, sticky="ew")

        self.log_text = tk.Text(activity, height=12, wrap="word", state="disabled")
        self.log_text.grid(row=1, column=0, sticky="nsew", pady=(12, 0))

    def _build_live_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(1, weight=1)

        _entry_row(parent, 0, "会话标题", self.live_title_var, "例如：产品周会")

        ttk.Label(parent, text="输入设备").grid(row=1, column=0, sticky="w", pady=(10, 0))
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

        advanced = ttk.LabelFrame(parent, text="高级选项", padding=12)
        advanced.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(16, 0))
        advanced.columnconfigure(1, weight=1)

        _combobox_row(advanced, 0, "内容类型", self.live_kind_var, KIND_CHOICES)
        _language_row(
            advanced,
            1,
            "语言覆盖",
            self.live_language_var,
            include_blank=True,
        )
        ttk.Label(
            advanced,
            text="关闭 Obsidian 同步时仅保留本地 Markdown；关闭 LLM 整理时会生成待整理模板。",
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))

        actions = ttk.Frame(parent)
        actions.grid(row=3, column=0, columnspan=2, sticky="w", pady=(18, 0))
        self.start_live_button = ttk.Button(
            actions,
            text="开始并生成",
            command=self._start_live_session,
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

    def _build_import_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(1, weight=1)

        ttk.Label(parent, text="媒体文件").grid(row=0, column=0, sticky="w")
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

        advanced = ttk.LabelFrame(parent, text="高级选项", padding=12)
        advanced.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(16, 0))
        advanced.columnconfigure(1, weight=1)

        _combobox_row(advanced, 0, "内容类型", self.import_kind_var, KIND_CHOICES)
        _language_row(
            advanced,
            1,
            "语言覆盖",
            self.import_language_var,
            include_blank=True,
        )
        ttk.Label(
            advanced,
            text=(
                "支持音频和视频本地文件，例如 "
                "mp3 / m4a / wav / mp4 / mov / mkv；本地模式也可单独使用。"
            ),
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))

        ttk.Button(
            parent,
            text="导入并生成",
            command=self._start_import,
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(18, 0))

    def _build_history_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)

        task_frame = ttk.LabelFrame(parent, text="任务队列", padding=12)
        task_frame.grid(row=0, column=0, sticky="ew")
        task_frame.columnconfigure(0, weight=1)

        ttk.Label(
            task_frame,
            textvariable=self.task_progress_var,
            justify="left",
        ).grid(row=0, column=0, sticky="w")
        self.history_progress = ttk.Progressbar(task_frame, mode="determinate")
        self.history_progress.grid(row=1, column=0, sticky="ew", pady=(10, 0))

        self.queue_tree = ttk.Treeview(
            task_frame,
            columns=("status", "task", "target", "created"),
            show="headings",
            height=4,
            selectmode="browse",
        )
        self.queue_tree.grid(row=2, column=0, sticky="ew", pady=(10, 0))
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
        queue_actions.grid(row=3, column=0, sticky="w", pady=(10, 0))
        self.cancel_queue_button = ttk.Button(
            queue_actions,
            text="取消所选",
            command=self._cancel_selected_queue_task,
            state="disabled",
        )
        self.cancel_queue_button.grid(row=0, column=0)

        table = ttk.Frame(parent)
        table.grid(row=1, column=0, sticky="nsew", pady=(14, 0))
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)

        self.history_tree = ttk.Treeview(
            table,
            columns=("time", "title", "kind", "mode", "status", "segments", "errors"),
            show="headings",
            height=14,
            selectmode="extended",
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

        actions = ttk.Frame(parent)
        actions.grid(row=2, column=0, sticky="ew", pady=(16, 0))
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
            parent,
            textvariable=self.history_detail_var,
            wraplength=980,
            justify="left",
        ).grid(row=3, column=0, sticky="w", pady=(14, 0))

    def _build_settings_tab(self, parent: ttk.Frame) -> None:
        content = _build_vertical_scroller(parent)
        content.columnconfigure(0, weight=1)

        toolbar = ttk.Frame(content)
        toolbar.grid(row=0, column=0, sticky="w")
        ttk.Button(
            toolbar,
            text="自动检测",
            command=self._autodetect_settings,
        ).grid(row=0, column=0)
        ttk.Button(toolbar, text="保存设置", command=self._save_settings).grid(
            row=0, column=1, padx=(8, 0)
        )
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

        whisper_frame = ttk.LabelFrame(content, text="Whisper / FFmpeg", padding=12)
        whisper_frame.grid(row=1, column=0, sticky="ew", pady=(16, 0))
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

        obsidian_frame = ttk.LabelFrame(content, text="Obsidian", padding=12)
        obsidian_frame.grid(row=2, column=0, sticky="ew", pady=(16, 0))
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
        ).grid(row=6, column=0, columnspan=3, sticky="w", pady=(10, 0))

        llm_frame = ttk.LabelFrame(content, text="LLM", padding=12)
        llm_frame.grid(row=3, column=0, sticky="ew", pady=(16, 0))
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
        ).grid(row=7, column=0, columnspan=3, sticky="w", pady=(10, 0))

        doctor_frame = ttk.LabelFrame(content, text="诊断结果", padding=12)
        doctor_frame.grid(row=4, column=0, sticky="ew", pady=(16, 16))
        doctor_frame.columnconfigure(0, weight=1)
        self.doctor_tree = ttk.Treeview(
            doctor_frame,
            columns=("name", "status", "detail"),
            show="headings",
            height=8,
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
            self.event_queue.put(replace(event, source=source, task_id=task_id))

        return callback

    def _load_task_queue_state(self) -> None:
        loaded = self.queue_store.load()
        with self.queue_lock:
            self.queue_records = list(loaded.active_records)
        self._sync_task_sequence(self.queue_records)
        if loaded.interrupted_records:
            self.queue_store.save(self.queue_records)
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
        record = build_task_record(
            task_id=self._next_task_id(),
            action=action,
            label=label,
            payload=payload,
            created_at=iso_now(),
        )
        with self.queue_lock:
            duplicate = any(item.fingerprint == record.fingerprint for item in self.queue_records)
            if not duplicate:
                self.queue_records.append(record)
                self.queue_store.save(self.queue_records)
        if duplicate:
            self._append_log(f"{label} 已在队列中，跳过重复入队。")
            self._update_idle_status()
            return
        self._append_log(f"已加入队列：{label}")
        self._refresh_queue_tree()
        self._maybe_start_next_queue_task()

    def _maybe_start_next_queue_task(self) -> None:
        service = getattr(self, "service", None)
        if (
            self.queue_worker is not None
            or self.busy
            or self.background_tasks
            or (service is not None and not service.config_exists())
        ):
            self._update_idle_status()
            return
        with self.queue_lock:
            next_record = next(
                (record for record in self.queue_records if record.status == "queued"),
                None,
            )
        if next_record is None:
            self._update_idle_status()
            return
        self._start_queue_task(next_record)

    def _start_queue_task(self, record: QueuedTaskRecord) -> None:
        started_record = replace(record, status="running", started_at=iso_now())
        with self.queue_lock:
            self.queue_records = [
                started_record if item.task_id == record.task_id else item
                for item in self.queue_records
            ]
            self.queue_store.save(self.queue_records)
        self.queue_current_task_id = started_record.task_id
        self.queue_current_task_label = started_record.label
        self._refresh_queue_tree()
        self._set_queue_progress_state(f"{started_record.label}：准备中")
        if not self.busy:
            self.status_var.set(f"{started_record.label}：准备中")

        def worker() -> None:
            try:
                result = self._run_queue_action(started_record)
            except Exception as exc:
                self._remove_queue_record(started_record.task_id)
                self.event_queue.put(
                    ("task_error", "queue", started_record.task_id, started_record.label, str(exc))
                )
            else:
                self._remove_queue_record(started_record.task_id)
                self.event_queue.put(
                    ("task_done", "queue", started_record.task_id, started_record.label, result)
                )

        self.queue_worker = threading.Thread(target=worker, daemon=False)
        self.queue_worker.start()

    def _run_queue_action(self, record: QueuedTaskRecord) -> int:
        callback = self._progress_callback("queue", record.task_id)
        payload = record.payload
        if record.action == "import":
            language = payload.get("language")
            runner = self.service.create_import_coordinator(
                file_path=str(payload["file_path"]),
                title=payload.get("title") or None,
                kind=str(payload.get("kind") or "generic"),
                language=language if isinstance(language, str) else None,
                on_progress=callback,
            )
            return runner.run()
        if record.action == "merge":
            return self.service.merge(
                [str(item) for item in payload.get("session_ids", [])],
                title=payload.get("title") if isinstance(payload.get("title"), str) else None,
                on_progress=callback,
            )
        if record.action == "session_action":
            operation = payload.get("action") or payload.get("operation")
            session_id = str(payload["session_id"])
            if operation == "retranscribe":
                return self.service.retranscribe(session_id, on_progress=callback)
            if operation == "refine":
                return self.service.refine(session_id, on_progress=callback)
            if operation == "republish":
                return self.service.republish(session_id, on_progress=callback)
            if operation == "resync":
                return self.service.resync_notes(session_id, on_progress=callback)
        raise RuntimeError(f"不支持的队列任务：{record.action}")

    def _remove_queue_record(self, task_id: str) -> None:
        with self.queue_lock:
            remaining = [record for record in self.queue_records if record.task_id != task_id]
            if len(remaining) == len(self.queue_records):
                return
            self.queue_records = remaining
            self.queue_store.save(self.queue_records)

    def _refresh_queue_tree(self) -> None:
        tree = getattr(self, "queue_tree", None)
        if tree is None:
            return
        for item in tree.get_children():
            tree.delete(item)
        with self.queue_lock:
            records = list(self.queue_records)
        for record in records:
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
        with self.queue_lock:
            cancellable = any(
                record.task_id in selection and record.status == "queued"
                for record in self.queue_records
            )
        button.configure(state="normal" if cancellable else "disabled")

    def _cancel_selected_queue_task(self) -> None:
        tree = getattr(self, "queue_tree", None)
        if tree is None:
            return
        selection = set(tree.selection())
        if not selection:
            return
        with self.queue_lock:
            remaining = [
                record
                for record in self.queue_records
                if record.task_id not in selection or record.status != "queued"
            ]
            changed = len(remaining) != len(self.queue_records)
            if changed:
                self.queue_records = remaining
                self.queue_store.save(self.queue_records)
        if changed:
            self._append_log("已取消所选排队任务。")
            self._refresh_queue_tree()
            self._update_idle_status()

    def _start_live_task(
        self,
        task_id: str,
        label: str,
        action: Callable[[], int],
    ) -> None:
        if self.busy:
            messagebox.showinfo("任务进行中", "请先停止当前录音。")
            return
        self.busy = True
        self.current_task_id = task_id
        self.current_task_label = label
        self.current_task_session_id = None
        self.current_live_task_id = task_id
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
        try:
            runner = self.service.create_live_coordinator(
                title=title,
                source=str(device.index),
                kind=self.live_kind_var.get(),
                language=_optional_language_override(self.live_language_var.get()),
                on_progress=self._progress_callback("live", task_id),
            )
        except Exception as exc:
            messagebox.showerror("启动失败", str(exc))
            return
        self.current_live_runner = runner
        self._start_live_task(task_id, "实时录音", runner.run)
        self.stop_live_button.configure(state="normal")
        self.pause_live_button.configure(state="normal", text="暂停录音")

    def _stop_live_session(self) -> None:
        if self.current_live_runner is None:
            return
        self.current_live_runner.request_stop()
        self.stop_live_button.configure(state="disabled")
        self.pause_live_button.configure(state="disabled")
        self._append_log("已请求停止录音，等待当前片段收尾。")

    def _toggle_live_pause(self) -> None:
        if self.current_live_runner is None:
            return
        if self.current_live_runner.is_paused:
            self.current_live_runner.request_resume()
            self.pause_live_button.configure(text="暂停录音")
            self._append_log("已请求继续录音。")
        else:
            self.current_live_runner.request_pause()
            self.pause_live_button.configure(text="继续录音")
            self._append_log("已请求暂停录音。")

    def _start_import(self) -> None:
        if not self._ensure_queue_ready():
            return
        media_path = Path(self.import_file_var.get().strip()).expanduser()
        if not media_path.exists():
            messagebox.showwarning("文件不存在", "请选择一个有效的本地音频或视频文件。")
            return
        self._enqueue_queue_task(
            label="文件导入",
            action="import",
            payload={
                "file_path": str(media_path),
                "title": self.import_title_var.get().strip() or None,
                "kind": self.import_kind_var.get(),
                "language": _optional_language_override(self.import_language_var.get()),
            },
        )

    def _retry_retranscribe(self) -> None:
        summary = self._selected_summary()
        if summary is None or not self._ensure_queue_ready():
            return
        self._enqueue_queue_task(
            label="重转写并重写",
            action="session_action",
            payload={"action": "retranscribe", "session_id": summary.session_id},
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
        self._enqueue_queue_task(
            label="离线精修并重写",
            action="session_action",
            payload={"action": "refine", "session_id": summary.session_id},
        )

    def _merge_selected_sessions(self) -> None:
        summaries = self._selected_summaries(min_count=2)
        if summaries is None or not self._ensure_queue_ready():
            return
        if not messagebox.askyesno(
            "合并会话",
            (
                f"将按开始时间顺序合并 {len(summaries)} 条会话，并生成一条新的合并会话。"
                "原始会话会保留。是否继续？"
            ),
        ):
            return
        self._enqueue_queue_task(
            label="合并会话",
            action="merge",
            payload={"session_ids": [summary.session_id for summary in summaries]},
        )

    def _retry_republish(self) -> None:
        summary = self._selected_summary()
        if summary is None or not self._ensure_queue_ready():
            return
        self._enqueue_queue_task(
            label="重新生成整理",
            action="session_action",
            payload={"action": "republish", "session_id": summary.session_id},
        )

    def _retry_resync(self) -> None:
        summary = self._selected_summary()
        if summary is None or not self._ensure_queue_ready():
            return
        self._enqueue_queue_task(
            label="重新同步 Obsidian",
            action="session_action",
            payload={"action": "resync", "session_id": summary.session_id},
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
        self.busy = True
        task_id = self._next_task_id()
        self.current_task_id = task_id
        self.current_task_label = label
        self.current_task_session_id = None
        self.current_live_task_id = task_id if detachable_live else None
        self.start_live_button.configure(state="disabled")
        self.status_var.set(f"{label}：准备中")
        self.progress.configure(mode="indeterminate", value=0)
        self.progress.start(12)
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
        if not summaries:
            self.history_detail_var.set("选择一条历史会话查看详情。")

    def _on_history_select(self, _event: object) -> None:
        summaries = self._selected_summaries(prompt=False)
        self._update_history_action_states(summaries)
        if not summaries:
            self.history_detail_var.set("选择一条历史会话查看详情。")
            return
        if len(summaries) > 1:
            titles = " / ".join(summary.title for summary in summaries[:3])
            if len(summaries) > 3:
                titles = f"{titles} / ..."
            self.history_detail_var.set(
                f"已选择 {len(summaries)} 条会话：{titles}。可执行“合并所选会话”，原始会话会保留。"
            )
            return
        summary = summaries[0]
        detail = (
            f"Session ID: {summary.session_id} | 已转写 {summary.transcribed_count}/"
            f"{summary.segment_count} | 来源: {summary.transcript_source} | "
            f"精修: {summary.refine_status} | 最近错误: {summary.latest_error or '无'}"
        )
        self.history_detail_var.set(detail)

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
        if refine_button is None:
            return
        if summaries and len(summaries) == 1 and _summary_supports_refine(summaries[0]):
            refine_button.configure(state="normal")
            return
        refine_button.configure(state="disabled")

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

    def _show_first_run_wizard(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("首次启动向导")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.geometry("760x420")
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

        ttk.Button(actions, text="保存并开始", command=save_and_close).grid(
            row=0, column=1, padx=(8, 0)
        )
        ttk.Button(actions, text="稍后配置", command=dialog.destroy).grid(
            row=0, column=2, padx=(8, 0)
        )

    def _ensure_ready_for_run(self) -> bool:
        if self.busy:
            messagebox.showinfo("任务进行中", "请等待当前任务完成，或先停止录音。")
            return False
        if not self.service.config_exists():
            messagebox.showinfo("需要先配置", "请先完成首次启动向导或在设置页保存配置。")
            self._show_first_run_wizard()
            return False
        return True

    def _poll_events(self) -> None:
        while True:
            try:
                item = self.event_queue.get_nowait()
            except queue.Empty:
                break

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
        self.root.after(150, self._poll_events)

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
        else:
            self._append_log(f"{label}完成。")
        self._refresh_history()
        self._refresh_doctor_checks()
        self._maybe_start_next_queue_task()

    def _finish_task(self) -> None:
        self.busy = False
        self.current_worker = None
        self.current_task_id = None
        self.current_task_label = None
        self.current_task_session_id = None
        self.current_live_task_id = None
        self.current_live_runner = None
        self.start_live_button.configure(state="normal")
        self._reset_live_controls()
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        self._update_idle_status()
        self._maybe_start_next_queue_task()

    def _finish_queue_task(self, task_id: str) -> None:
        if task_id == self.queue_current_task_id:
            self.queue_worker = None
            self.queue_current_task_id = None
            self.queue_current_task_label = None
        self._refresh_queue_tree()
        queued = sum(1 for record in self.queue_records if record.status == "queued")
        if queued:
            self._set_queue_progress_state(f"等待执行 {queued} 项。", active=False)
        elif not self.busy:
            self._set_queue_progress_state("当前没有任务。", active=False)
        self._update_idle_status()

    def _finish_background_task(self, task_id: str) -> None:
        self.background_tasks.pop(task_id, None)
        self.background_task_sessions.pop(task_id, None)
        self._update_idle_status()
        self._maybe_start_next_queue_task()

    def _detach_live_task(self, session_id: str | None) -> None:
        task_id = self.current_task_id
        if task_id is None:
            return
        resolved_session_id = session_id or self.current_task_session_id
        self.background_tasks[task_id] = self.current_task_label or "实时录音"
        self.background_task_sessions[task_id] = resolved_session_id
        self.busy = False
        self.current_worker = None
        self.current_task_id = None
        self.current_task_label = None
        self.current_task_session_id = None
        self.current_live_task_id = None
        self.current_live_runner = None
        self.start_live_button.configure(state="normal")
        self._reset_live_controls()
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        self._update_idle_status()

    def _is_foreground_event(self, event: ProgressEvent) -> bool:
        if self.current_task_id is None:
            return False
        if event.session_id:
            if self.current_task_session_id is None:
                background_task_id = self._find_background_task_by_session(event.session_id)
                if background_task_id is None:
                    self.current_task_session_id = event.session_id
            if self.current_task_session_id == event.session_id:
                return True
            background_task_id = self._find_background_task_by_session(event.session_id)
            if (
                background_task_id is not None
                and self.background_task_sessions[background_task_id] is None
            ):
                self.background_task_sessions[background_task_id] = event.session_id
            return False
        return True

    def _find_background_task_by_session(self, session_id: str) -> str | None:
        for task_id, task_session_id in self.background_task_sessions.items():
            if task_session_id == session_id:
                return task_id
        for task_id, task_session_id in self.background_task_sessions.items():
            if task_session_id is None:
                return task_id
        return None

    def _next_task_id(self) -> str:
        self.task_sequence += 1
        return f"task-{self.task_sequence:04d}"

    def _sync_task_sequence(self, records: list[QueuedTaskRecord]) -> None:
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
        queue_records = getattr(self, "queue_records", [])
        pending = len(background_tasks)
        if pending:
            self.status_var.set(f"准备就绪（后台处理中 {pending} 项）")
            queued = sum(1 for record in queue_records if record.status == "queued")
            if queued:
                self._set_queue_progress_state(f"等待执行 {queued} 项。", active=False)
            else:
                self._set_queue_progress_state(f"后台处理中 {pending} 项。", active=False)
            return
        queued = sum(1 for record in queue_records if record.status == "queued")
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
            self.progress.start(12)
            return
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)

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
        history_progress = getattr(self, "history_progress", None)
        if history_progress is None:
            return
        if total:
            history_progress.stop()
            history_progress.configure(mode="determinate")
            history_progress["value"] = round((current or 0) / total * 100)
            return
        if active:
            history_progress.configure(mode="indeterminate")
            history_progress.start(12)
            return
        history_progress.stop()
        history_progress.configure(mode="determinate", value=0)

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

    def _append_log(self, line: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{line}\n")
        self.log_text.see("end")
        if int(self.log_text.index("end-1c").split(".")[0]) > 300:
            self.log_text.delete("1.0", "50.0")
        self.log_text.configure(state="disabled")

    def _on_close(self) -> None:
        if self.busy and self.current_live_runner is not None:
            if messagebox.askyesno("退出", "当前仍在录音。要先发送停止请求再退出吗？"):
                self.current_live_runner.request_stop()
                return
        if self.busy:
            if not messagebox.askyesno("退出", "当前任务仍在进行，确定直接关闭窗口吗？"):
                return
        elif self.queue_worker is not None:
            queued = sum(1 for record in self.queue_records if record.status == "queued")
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
        elif any(record.status == "queued" for record in self.queue_records):
            if not messagebox.askyesno(
                "退出",
                "仍有待执行的排队任务。关闭窗口后它们会在下次启动时恢复。是否继续关闭窗口？",
            ):
                return
        self.root.destroy()


def _entry_row(
    parent: ttk.Frame,
    row: int,
    label: str,
    variable: tk.StringVar,
    hint: str | None = None,
) -> None:
    ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=(10, 0))
    ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=(10, 0))
    if hint:
        ttk.Label(parent, text=hint).grid(row=row, column=2, sticky="w", padx=(8, 0), pady=(10, 0))


def _build_vertical_scroller(parent: ttk.Frame) -> ttk.Frame:
    parent.columnconfigure(0, weight=1)
    parent.rowconfigure(0, weight=1)

    canvas = tk.Canvas(parent, highlightthickness=0, borderwidth=0)
    canvas.grid(row=0, column=0, sticky="nsew")

    scrollbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
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


def _bind_mousewheel_scrolling(root: tk.Misc, canvas: tk.Canvas) -> None:
    def _is_descendant_of_canvas(widget: tk.Misc | None) -> bool:
        current = widget
        while current is not None:
            if current is canvas:
                return True
            current = getattr(current, "master", None)
        return False

    def _scroll(event: tk.Event[tk.Misc]) -> None:
        try:
            widget_under_pointer = canvas.winfo_containing(event.x_root, event.y_root)
        except tk.TclError:
            return
        if not _is_descendant_of_canvas(widget_under_pointer):
            return
        if getattr(event, "num", None) == 4:
            delta = -1
        elif getattr(event, "num", None) == 5:
            delta = 1
        elif getattr(event, "delta", 0):
            delta = -1 if event.delta > 0 else 1
        else:
            return
        canvas.yview_scroll(delta, "units")

    root.bind_all("<MouseWheel>", _scroll, add="+")
    root.bind_all("<Button-4>", _scroll, add="+")
    root.bind_all("<Button-5>", _scroll, add="+")


def _wrap_action_rows(
    available_width: int,
    item_widths: list[int],
    *,
    gap: int,
) -> list[tuple[int, int]]:
    if not item_widths:
        return []
    width_limit = max(available_width, max(item_widths))
    used_width = 0
    row = 0
    column = 0
    layout: list[tuple[int, int]] = []
    for item_width in item_widths:
        proposed_width = item_width if column == 0 else used_width + gap + item_width
        if column > 0 and proposed_width > width_limit:
            row += 1
            column = 0
            used_width = item_width
        else:
            used_width = proposed_width
        layout.append((row, column))
        column += 1
    return layout


def _summary_supports_refine(summary: SessionSummary) -> bool:
    if summary.input_mode != "live":
        return False
    if (summary.session_dir / "session.live.wav").exists():
        return True
    try:
        workspace = SessionWorkspace.load(summary.session_dir)
    except Exception:
        return False
    return can_reconstruct_session_live_audio(workspace)


def _queue_target_text(record: QueuedTaskRecord) -> str:
    payload = record.payload
    if record.action == "import":
        return Path(str(payload.get("file_path", ""))).name or "本地文件"
    if record.action == "merge":
        session_ids = payload.get("session_ids", [])
        if isinstance(session_ids, list):
            return f"{len(session_ids)} 条会话"
        return "多条会话"
    if record.action == "session_action":
        return str(payload.get("session_id") or "会话")
    return record.label


def _entry_row_with_button(
    parent: ttk.Frame,
    row: int,
    label: str,
    variable: tk.StringVar,
    hint: str,
    command: Callable[[], None],
) -> None:
    ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=(10, 0))
    wrapper = ttk.Frame(parent)
    wrapper.grid(row=row, column=1, sticky="ew", pady=(10, 0))
    wrapper.columnconfigure(0, weight=1)
    ttk.Entry(wrapper, textvariable=variable).grid(row=0, column=0, sticky="ew")
    ttk.Button(wrapper, text="浏览", command=command).grid(row=0, column=1, padx=(8, 0))
    ttk.Label(parent, text=hint).grid(row=row, column=2, sticky="w", padx=(8, 0), pady=(10, 0))


def _combobox_row(
    parent: ttk.Frame,
    row: int,
    label: str,
    variable: tk.StringVar,
    values: list[str],
) -> None:
    ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=(10, 0))
    ttk.Combobox(parent, textvariable=variable, state="readonly", values=values).grid(
        row=row,
        column=1,
        sticky="ew",
        pady=(10, 0),
    )


def _language_row(
    parent: ttk.Frame,
    row: int,
    label: str,
    variable: tk.StringVar,
    include_blank: bool,
) -> None:
    values = SESSION_LANGUAGE_CHOICES if include_blank else DEFAULT_LANGUAGE_CHOICES
    hint = "中英混合或多语言建议选 auto；也可直接输入其他 Whisper 语言代码"
    ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=(10, 0))
    ttk.Combobox(parent, textvariable=variable, values=values).grid(
        row=row,
        column=1,
        sticky="ew",
        pady=(10, 0),
    )
    ttk.Label(parent, text=hint).grid(row=row, column=2, sticky="w", padx=(8, 0), pady=(10, 0))


def _normalize_language_value(value: str, blank_fallback: str = "") -> str:
    normalized = LANGUAGE_LABEL_TO_CODE.get(value.strip(), value.strip()).lower()
    if not normalized:
        return blank_fallback
    return normalized


def _optional_language_override(value: str) -> str | None:
    normalized = _normalize_language_value(value)
    return normalized or None


def _language_code_to_display(code: str, allow_blank: bool) -> str:
    normalized = code.strip().lower()
    if not normalized:
        return "沿用默认设置" if allow_blank else "自动识别 / 中英混合 / 多语言（auto）"
    return LANGUAGE_CODE_TO_LABEL.get(normalized, normalized)
