from __future__ import annotations

import queue
import threading
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import Tk, filedialog, messagebox, ttk

from live_note.audio.capture import InputDevice

from .events import ProgressEvent
from .services import AppService, SessionSummary, SettingsDraft

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
        self.event_queue: queue.Queue[ProgressEvent | tuple[str, str, object]] = queue.Queue()
        self.current_worker: threading.Thread | None = None
        self.current_live_runner = None
        self.busy = False
        self.live_devices: list[InputDevice] = []
        self.history_rows: dict[str, SessionSummary] = {}

        self.status_var = tk.StringVar(value="准备就绪")
        self.history_detail_var = tk.StringVar(value="选择一条历史会话查看详情。")

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

        self._build_ui()
        self._load_settings(self.service.load_settings_draft())
        self._refresh_devices()
        self._refresh_history()
        self._refresh_doctor_checks()
        self.root.after(150, self._poll_events)
        if not self.service.config_exists():
            self.root.after(250, self._show_first_run_wizard)

    def run(self) -> None:
        self.root.mainloop()

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        header = ttk.Frame(self.root, padding=(18, 14))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)
        ttk.Label(
            header,
            text="live-note",
            font=("SF Pro Text", 22, "bold"),
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            header,
            text="本地优先的课程 / 会议 / 音频内容记录器",
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))
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
        parent.rowconfigure(0, weight=1)

        table = ttk.Frame(parent)
        table.grid(row=0, column=0, sticky="nsew")
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
        actions.grid(row=1, column=0, sticky="ew", pady=(16, 0))
        ttk.Button(actions, text="刷新列表", command=self._refresh_history).grid(row=0, column=0)
        ttk.Button(actions, text="打开原文", command=self._open_transcript).grid(
            row=0, column=1, padx=(8, 0)
        )
        ttk.Button(actions, text="打开整理", command=self._open_structured).grid(
            row=0, column=2, padx=(8, 0)
        )
        ttk.Button(actions, text="打开目录", command=self._open_session_dir).grid(
            row=0, column=3, padx=(8, 0)
        )
        ttk.Button(actions, text="合并所选会话", command=self._merge_selected_sessions).grid(
            row=0, column=4, padx=(16, 0)
        )
        ttk.Button(actions, text="重转写并重写", command=self._retry_retranscribe).grid(
            row=0, column=5, padx=(8, 0)
        )
        ttk.Button(actions, text="离线精修并重写", command=self._retry_refine).grid(
            row=0, column=6, padx=(8, 0)
        )
        ttk.Button(actions, text="重新生成整理", command=self._retry_republish).grid(
            row=0, column=7, padx=(8, 0)
        )
        ttk.Button(actions, text="重新同步 Obsidian", command=self._retry_resync).grid(
            row=0, column=8, padx=(8, 0)
        )

        ttk.Label(
            parent,
            textvariable=self.history_detail_var,
            wraplength=980,
            justify="left",
        ).grid(row=2, column=0, sticky="w", pady=(14, 0))

    def _build_settings_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)

        toolbar = ttk.Frame(parent)
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

        whisper_frame = ttk.LabelFrame(parent, text="Whisper / FFmpeg", padding=12)
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

        obsidian_frame = ttk.LabelFrame(parent, text="Obsidian", padding=12)
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

        llm_frame = ttk.LabelFrame(parent, text="LLM", padding=12)
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

        doctor_frame = ttk.LabelFrame(parent, text="诊断结果", padding=12)
        doctor_frame.grid(row=4, column=0, sticky="nsew", pady=(16, 0))
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
        try:
            runner = self.service.create_live_coordinator(
                title=title,
                source=str(device.index),
                kind=self.live_kind_var.get(),
                language=_optional_language_override(self.live_language_var.get()),
                on_progress=self.event_queue.put,
            )
        except Exception as exc:
            messagebox.showerror("启动失败", str(exc))
            return
        self.current_live_runner = runner
        self._run_background("实时录音", runner.run)
        self.stop_live_button.configure(state="normal")

    def _stop_live_session(self) -> None:
        if self.current_live_runner is None:
            return
        self.current_live_runner.request_stop()
        self._append_log("已请求停止录音，等待当前片段收尾。")

    def _start_import(self) -> None:
        if not self._ensure_ready_for_run():
            return
        media_path = Path(self.import_file_var.get().strip()).expanduser()
        if not media_path.exists():
            messagebox.showwarning("文件不存在", "请选择一个有效的本地音频或视频文件。")
            return
        try:
            runner = self.service.create_import_coordinator(
                file_path=str(media_path),
                title=self.import_title_var.get().strip() or None,
                kind=self.import_kind_var.get(),
                language=_optional_language_override(self.import_language_var.get()),
                on_progress=self.event_queue.put,
            )
        except Exception as exc:
            messagebox.showerror("导入失败", str(exc))
            return
        self._run_background("文件导入", runner.run)

    def _retry_retranscribe(self) -> None:
        summary = self._selected_summary()
        if summary is None or not self._ensure_ready_for_run():
            return
        self._run_background(
            "重转写并重写",
            lambda: self.service.retranscribe(summary.session_id, on_progress=self.event_queue.put),
        )

    def _retry_refine(self) -> None:
        summary = self._selected_summary()
        if summary is None or not self._ensure_ready_for_run():
            return
        self._run_background(
            "离线精修并重写",
            lambda: self.service.refine(summary.session_id, on_progress=self.event_queue.put),
        )

    def _merge_selected_sessions(self) -> None:
        summaries = self._selected_summaries(min_count=2)
        if summaries is None or not self._ensure_ready_for_run():
            return
        if not messagebox.askyesno(
            "合并会话",
            (
                f"将按开始时间顺序合并 {len(summaries)} 条会话，并生成一条新的合并会话。"
                "原始会话会保留。是否继续？"
            ),
        ):
            return
        self._run_background(
            "合并会话",
            lambda: self.service.merge(
                [summary.session_id for summary in summaries],
                on_progress=self.event_queue.put,
            ),
        )

    def _retry_republish(self) -> None:
        summary = self._selected_summary()
        if summary is None or not self._ensure_ready_for_run():
            return
        self._run_background(
            "重新生成整理",
            lambda: self.service.republish(summary.session_id, on_progress=self.event_queue.put),
        )

    def _retry_resync(self) -> None:
        summary = self._selected_summary()
        if summary is None or not self._ensure_ready_for_run():
            return
        self._run_background(
            "重新同步 Obsidian",
            lambda: self.service.resync_notes(summary.session_id, on_progress=self.event_queue.put),
        )

    def _run_background(self, label: str, action: Callable[[], int]) -> None:
        if self.busy:
            messagebox.showinfo("任务进行中", "请等待当前任务完成，或先停止录音。")
            return
        self.busy = True
        self.start_live_button.configure(state="disabled")
        self.status_var.set(f"{label}：准备中")
        self.progress.configure(mode="indeterminate", value=0)
        self.progress.start(12)

        def worker() -> None:
            try:
                result = action()
            except Exception as exc:
                self.event_queue.put(("task_error", label, str(exc)))
            else:
                self.event_queue.put(("task_done", label, result))

        self.current_worker = threading.Thread(target=worker, daemon=True)
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
        elif not self._selected_summary(prompt=False):
            self.history_detail_var.set("选择一条历史会话查看详情。")

    def _on_history_select(self, _event: object) -> None:
        summaries = self._selected_summaries(prompt=False)
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

            event_type, label, payload = item
            if event_type == "task_error":
                self._finish_task()
                self._append_log(f"{label}失败：{payload}")
                messagebox.showerror(f"{label}失败", str(payload))
            elif event_type == "task_done":
                self._finish_task()
                self._append_log(f"{label}完成。")
                self._refresh_history()
                self._refresh_doctor_checks()
        self.root.after(150, self._poll_events)

    def _handle_progress(self, event: ProgressEvent) -> None:
        self.status_var.set(event.message)
        self._append_log(event.message)
        if event.total:
            self.progress.stop()
            self.progress.configure(mode="determinate")
            self.progress["value"] = round(event.current or 0) / event.total * 100
        elif event.stage in {"done", "error"}:
            self.progress.stop()
            self.progress.configure(mode="determinate", value=0)
        else:
            self.progress.configure(mode="indeterminate")
            self.progress.start(12)

    def _finish_task(self) -> None:
        self.busy = False
        self.current_worker = None
        self.current_live_runner = None
        self.start_live_button.configure(state="normal")
        self.stop_live_button.configure(state="disabled")
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        self.status_var.set("准备就绪")

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
