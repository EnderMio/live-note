from __future__ import annotations

import importlib
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from live_note.audio.capture import InputDevice, list_input_devices
from live_note.config import (
    AppConfig,
    AudioConfig,
    ImportConfig,
    LlmConfig,
    ObsidianConfig,
    RefineConfig,
    WhisperConfig,
    load_config,
    load_env_file,
    save_config,
)
from live_note.obsidian.client import ObsidianClient

from .coordinator import (
    FileImportCoordinator,
    SessionCoordinator,
    finalize_session,
    merge_sessions,
    refine_session,
    republish_session,
    retranscribe_session,
    sync_session_notes,
)
from .events import ProgressCallback
from .journal import SessionWorkspace
from .journal import list_sessions as iter_session_roots


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    name: str
    status: str
    detail: str


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
    session_dir: Path
    transcript_file: Path
    structured_file: Path


@dataclass(frozen=True, slots=True)
class SettingsDraft:
    ffmpeg_binary: str = "ffmpeg"
    whisper_binary: str = ""
    whisper_model: str = ""
    whisper_host: str = "127.0.0.1"
    whisper_port: int = 8178
    whisper_threads: int = 4
    whisper_language: str = "auto"
    whisper_translate: bool = False
    save_session_wav: bool = True
    refine_enabled: bool = True
    refine_auto_after_live: bool = True
    obsidian_enabled: bool = True
    obsidian_base_url: str = "https://127.0.0.1:27124"
    obsidian_transcript_dir: str = "Sessions/Transcripts"
    obsidian_structured_dir: str = "Sessions/Summaries"
    obsidian_verify_ssl: bool = False
    llm_enabled: bool = True
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4.1-mini"
    llm_stream: bool = False
    llm_wire_api: str = "chat_completions"
    llm_requires_openai_auth: bool = False
    obsidian_api_key: str = ""
    llm_api_key: str = ""

    @classmethod
    def from_config(cls, config: AppConfig) -> SettingsDraft:
        return cls(
            ffmpeg_binary=config.importer.ffmpeg_binary,
            whisper_binary=config.whisper.binary,
            whisper_model=str(config.whisper.model),
            whisper_host=config.whisper.host,
            whisper_port=config.whisper.port,
            whisper_threads=config.whisper.threads,
            whisper_language=config.whisper.language,
            whisper_translate=config.whisper.translate,
            save_session_wav=config.audio.save_session_wav,
            refine_enabled=config.refine.enabled,
            refine_auto_after_live=config.refine.auto_after_live,
            obsidian_enabled=config.obsidian.enabled,
            obsidian_base_url=config.obsidian.base_url,
            obsidian_transcript_dir=config.obsidian.transcript_dir,
            obsidian_structured_dir=config.obsidian.structured_dir,
            obsidian_verify_ssl=config.obsidian.verify_ssl,
            llm_enabled=config.llm.enabled,
            llm_base_url=config.llm.base_url,
            llm_model=config.llm.model,
            llm_stream=config.llm.stream,
            llm_wire_api=config.llm.wire_api,
            llm_requires_openai_auth=config.llm.requires_openai_auth,
            obsidian_api_key=config.obsidian.api_key or "",
            llm_api_key=config.llm.api_key or "",
        )


class AppService:
    def __init__(self, config_path: Path | None = None):
        self.config_path = (config_path or Path("config.toml")).resolve()
        self.env_path = (self.config_path.parent / ".env").resolve()

    def task_queue_path(self) -> Path:
        return (self.config_path.parent / ".live-note" / "task_queue.json").resolve()

    def config_exists(self) -> bool:
        return self.config_path.exists()

    def load_config(self) -> AppConfig:
        return load_config(self.config_path, self.env_path)

    def load_settings_draft(self) -> SettingsDraft:
        try:
            return SettingsDraft.from_config(self.load_config())
        except Exception:
            return self.detect_settings_draft()

    def detect_settings_draft(self) -> SettingsDraft:
        env_values = dict(os.environ)
        env_values.update(load_env_file(self.env_path))
        return SettingsDraft(
            ffmpeg_binary=_detect_ffmpeg_binary(),
            whisper_binary=_detect_whisper_binary(),
            whisper_model=_detect_whisper_model(),
            save_session_wav=True,
            refine_enabled=True,
            refine_auto_after_live=True,
            obsidian_enabled=bool(env_values.get("OBSIDIAN_API_KEY")),
            llm_enabled=bool(env_values.get("LLM_API_KEY") or env_values.get("OPENAI_API_KEY")),
            obsidian_api_key=env_values.get("OBSIDIAN_API_KEY", ""),
            llm_api_key=env_values.get("LLM_API_KEY") or env_values.get("OPENAI_API_KEY", ""),
        )

    def validate_settings(self, draft: SettingsDraft) -> list[str]:
        errors: list[str] = []
        if not draft.ffmpeg_binary.strip():
            errors.append("ffmpeg 路径不能为空。")
        if not draft.whisper_binary.strip():
            errors.append("whisper-server 路径不能为空。")
        if not draft.whisper_model.strip():
            errors.append("Whisper 模型路径不能为空。")
        if draft.refine_auto_after_live and not draft.refine_enabled:
            errors.append("开启自动离线精修前，必须先启用离线精修。")
        if draft.refine_auto_after_live and not draft.save_session_wav:
            errors.append("开启自动离线精修前，必须同时保存整场 WAV。")
        if draft.obsidian_enabled:
            if not draft.obsidian_base_url.strip():
                errors.append("启用 Obsidian 同步时，Obsidian 地址不能为空。")
            if not draft.obsidian_transcript_dir.strip():
                errors.append("启用 Obsidian 同步时，原文输出目录不能为空。")
            if not draft.obsidian_structured_dir.strip():
                errors.append("启用 Obsidian 同步时，整理输出目录不能为空。")
        if draft.llm_enabled:
            if not draft.llm_base_url.strip():
                errors.append("启用 LLM 整理时，LLM Base URL 不能为空。")
            if not draft.llm_model.strip():
                errors.append("启用 LLM 整理时，LLM 模型名不能为空。")
            if draft.llm_wire_api not in {"chat_completions", "responses"}:
                errors.append("LLM 协议仅支持 chat_completions 或 responses。")
        return errors

    def save_settings(self, draft: SettingsDraft) -> AppConfig:
        errors = self.validate_settings(draft)
        if errors:
            raise ValueError("\n".join(errors))

        try:
            existing = self.load_config()
        except Exception:
            existing = _default_config(self.config_path.parent)

        config = AppConfig(
            audio=AudioConfig(
                sample_rate=existing.audio.sample_rate,
                frame_duration_ms=existing.audio.frame_duration_ms,
                vad_aggressiveness=existing.audio.vad_aggressiveness,
                silence_ms=existing.audio.silence_ms,
                min_segment_ms=existing.audio.min_segment_ms,
                max_segment_ms=existing.audio.max_segment_ms,
                queue_size=existing.audio.queue_size,
                save_session_wav=draft.save_session_wav,
            ),
            importer=ImportConfig(
                ffmpeg_binary=draft.ffmpeg_binary,
                chunk_seconds=existing.importer.chunk_seconds,
                keep_normalized_audio=existing.importer.keep_normalized_audio,
            ),
            refine=RefineConfig(
                enabled=draft.refine_enabled,
                auto_after_live=draft.refine_auto_after_live,
            ),
            whisper=WhisperConfig(
                binary=draft.whisper_binary,
                model=Path(draft.whisper_model).expanduser(),
                host=draft.whisper_host,
                port=int(draft.whisper_port),
                threads=int(draft.whisper_threads),
                language=draft.whisper_language,
                translate=draft.whisper_translate,
                request_timeout_seconds=existing.whisper.request_timeout_seconds,
                startup_timeout_seconds=existing.whisper.startup_timeout_seconds,
            ),
            obsidian=ObsidianConfig(
                enabled=draft.obsidian_enabled,
                base_url=draft.obsidian_base_url.rstrip("/"),
                transcript_dir=draft.obsidian_transcript_dir.strip("/"),
                structured_dir=draft.obsidian_structured_dir.strip("/"),
                verify_ssl=draft.obsidian_verify_ssl,
                timeout_seconds=existing.obsidian.timeout_seconds,
                retry_attempts=existing.obsidian.retry_attempts,
                retry_backoff_seconds=existing.obsidian.retry_backoff_seconds,
                api_key=draft.obsidian_api_key.strip() or None,
            ),
            llm=LlmConfig(
                enabled=draft.llm_enabled,
                base_url=draft.llm_base_url.rstrip("/"),
                model=draft.llm_model,
                stream=draft.llm_stream,
                wire_api=draft.llm_wire_api,
                requires_openai_auth=draft.llm_requires_openai_auth,
                timeout_seconds=existing.llm.timeout_seconds,
                api_key=draft.llm_api_key.strip() or None,
            ),
            root_dir=self.config_path.parent,
        )
        save_config(config, self.config_path, self.env_path)
        return config

    def doctor_checks(self) -> list[DoctorCheck]:
        draft = self.load_settings_draft()
        checks: list[DoctorCheck] = []
        try:
            self.load_config()
        except Exception as exc:
            checks.append(DoctorCheck("config", "FAIL", str(exc)))
        else:
            checks.append(DoctorCheck("config", "OK", f"已加载 {self.config_path}"))

        checks.append(
            DoctorCheck(
                "sounddevice",
                "OK" if _module_available("sounddevice") else "FAIL",
                "Python 包 sounddevice",
            )
        )
        checks.append(
            DoctorCheck(
                "webrtcvad",
                "OK" if _module_available("webrtcvad") else "FAIL",
                "Python 包 webrtcvad / webrtcvad-wheels",
            )
        )
        checks.append(
            DoctorCheck(
                "whisper-server",
                "OK" if _binary_exists(draft.whisper_binary) else "FAIL",
                f"可执行文件 {draft.whisper_binary or '未配置'}",
            )
        )
        checks.append(
            DoctorCheck(
                "ffmpeg",
                "OK" if _binary_exists(draft.ffmpeg_binary) else "FAIL",
                f"可执行文件 {draft.ffmpeg_binary or '未配置'}",
            )
        )
        model_path = Path(draft.whisper_model).expanduser() if draft.whisper_model else None
        checks.append(
            DoctorCheck(
                "model",
                "OK" if model_path and model_path.exists() else "FAIL",
                f"模型文件 {draft.whisper_model or '未配置'}",
            )
        )
        if draft.obsidian_enabled:
            checks.append(
                DoctorCheck(
                    "obsidian_api_key",
                    "OK" if draft.obsidian_api_key else "WARN",
                    "环境变量 OBSIDIAN_API_KEY；未设置时将只保留本地文件",
                )
            )
            try:
                obsidian = ObsidianClient(
                    ObsidianConfig(
                        base_url=draft.obsidian_base_url.rstrip("/"),
                        transcript_dir=draft.obsidian_transcript_dir.strip("/"),
                        structured_dir=draft.obsidian_structured_dir.strip("/"),
                        enabled=True,
                        verify_ssl=draft.obsidian_verify_ssl,
                        api_key=draft.obsidian_api_key or None,
                    )
                )
                obsidian.ping()
            except Exception as exc:
                detail = str(exc) if draft.obsidian_base_url else "未配置 Obsidian 地址"
                checks.append(DoctorCheck("obsidian_ping", "FAIL", detail))
            else:
                checks.append(
                    DoctorCheck(
                        "obsidian_ping", "OK", f"连通 {draft.obsidian_base_url.rstrip('/')}"
                    ),
                )
        else:
            checks.append(DoctorCheck("obsidian", "SKIP", "已关闭 Obsidian 同步，仅保留本地输出"))

        if draft.llm_enabled:
            checks.append(
                DoctorCheck(
                    "llm_endpoint",
                    "OK",
                    (
                        f"{draft.llm_base_url.rstrip('/')} | "
                        f"{draft.llm_wire_api} | "
                        f"{'stream' if draft.llm_stream else 'non-stream'} | "
                        f"{'openai-auth' if draft.llm_requires_openai_auth else 'custom-auth'}"
                    ),
                )
            )
            checks.append(
                DoctorCheck(
                    "llm_api_key",
                    "OK" if draft.llm_api_key else "WARN",
                    (
                        "环境变量 OPENAI_API_KEY（优先）/ LLM_API_KEY"
                        if draft.llm_requires_openai_auth
                        else "环境变量 LLM_API_KEY"
                    )
                    + "；未设置时将生成待整理模板",
                )
            )
        else:
            checks.append(DoctorCheck("llm", "SKIP", "已关闭 LLM 整理，仅输出原文和待整理模板"))

        return checks

    def list_input_devices(self) -> list[InputDevice]:
        return list_input_devices()

    def list_session_summaries(self) -> list[SessionSummary]:
        try:
            config = self.load_config()
        except Exception:
            return []

        items: list[SessionSummary] = []
        for root in iter_session_roots(config.root_dir):
            try:
                workspace = SessionWorkspace.load(root)
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
                    session_dir=workspace.root,
                    transcript_file=workspace.transcript_md,
                    structured_file=workspace.structured_md,
                )
            )
        return sorted(items, key=lambda item: item.started_at, reverse=True)

    def create_live_coordinator(
        self,
        title: str,
        source: str,
        kind: str,
        language: str | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> SessionCoordinator:
        return SessionCoordinator(
            config=self.load_config(),
            title=title,
            source=source,
            kind=kind,
            language=language,
            on_progress=on_progress,
        )

    def create_import_coordinator(
        self,
        file_path: str,
        title: str | None,
        kind: str,
        language: str | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> FileImportCoordinator:
        return FileImportCoordinator(
            config=self.load_config(),
            file_path=file_path,
            title=title,
            kind=kind,
            language=language,
            on_progress=on_progress,
        )

    def finalize(
        self,
        session_id: str,
        on_progress: ProgressCallback | None = None,
    ) -> int:
        return finalize_session(self.load_config(), session_id, on_progress=on_progress)

    def retranscribe(
        self,
        session_id: str,
        on_progress: ProgressCallback | None = None,
    ) -> int:
        return retranscribe_session(self.load_config(), session_id, on_progress=on_progress)

    def refine(
        self,
        session_id: str,
        on_progress: ProgressCallback | None = None,
    ) -> int:
        return refine_session(self.load_config(), session_id, on_progress=on_progress)

    def merge(
        self,
        session_ids: list[str],
        title: str | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> int:
        return merge_sessions(
            self.load_config(),
            session_ids,
            title=title,
            on_progress=on_progress,
        )

    def republish(
        self,
        session_id: str,
        on_progress: ProgressCallback | None = None,
    ) -> int:
        return republish_session(self.load_config(), session_id, on_progress=on_progress)

    def resync_notes(
        self,
        session_id: str,
        on_progress: ProgressCallback | None = None,
    ) -> int:
        return sync_session_notes(self.load_config(), session_id, on_progress=on_progress)

    def open_path(self, path: Path) -> None:
        subprocess.run(["open", str(path)], check=False)


def _binary_exists(value: str) -> bool:
    if not value:
        return False
    return bool(shutil.which(value) or Path(value).expanduser().exists())


def _module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _detect_ffmpeg_binary() -> str:
    which = shutil.which("ffmpeg")
    if which:
        return which
    homebrew = Path("/opt/homebrew/bin/ffmpeg")
    if homebrew.exists():
        return str(homebrew)
    return "ffmpeg"


def _detect_whisper_binary() -> str:
    which = shutil.which("whisper-server")
    if which:
        return which
    local = Path("~/whisper.cpp/build/bin/whisper-server").expanduser()
    if local.exists():
        return str(local)
    return ""


def _detect_whisper_model() -> str:
    models_dir = Path("~/whisper.cpp/models").expanduser()
    candidates: list[Path] = []
    if models_dir.exists():
        candidates.extend(sorted(models_dir.glob("ggml-*.bin"), key=_whisper_model_sort_key))
    else:
        candidates.extend(
            sorted(
                [
                    Path("~/whisper.cpp/models/ggml-large-v3-turbo.bin").expanduser(),
                    Path("~/whisper.cpp/models/ggml-large-v3.bin").expanduser(),
                    Path("~/whisper.cpp/models/ggml-medium.bin").expanduser(),
                    Path("~/whisper.cpp/models/ggml-small.bin").expanduser(),
                    Path("~/whisper.cpp/models/ggml-base.bin").expanduser(),
                ],
                key=_whisper_model_sort_key,
            )
        )
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return ""


def _whisper_model_sort_key(path: Path) -> tuple[int, str]:
    name = path.name.lower()
    priorities = [
        "large-v3-turbo",
        "large-v3",
        "large-v2",
        "large",
        "medium",
        "small",
        "base",
        "tiny",
    ]
    for index, marker in enumerate(priorities):
        if marker in name:
            penalty = 100 if name.endswith(".en.bin") else 0
            return (index + penalty, name)
    return (999, name)


def _default_config(root_dir: Path) -> AppConfig:
    return AppConfig(
        audio=AudioConfig(),
        importer=ImportConfig(ffmpeg_binary=_detect_ffmpeg_binary()),
        refine=RefineConfig(),
        whisper=WhisperConfig(
            binary=_detect_whisper_binary(),
            model=Path(_detect_whisper_model()).expanduser(),
        ),
        obsidian=ObsidianConfig(
            base_url="https://127.0.0.1:27124",
            transcript_dir="Sessions/Transcripts",
            structured_dir="Sessions/Summaries",
            enabled=True,
            verify_ssl=False,
            api_key=None,
        ),
        llm=LlmConfig(
            base_url="https://api.openai.com/v1",
            model="gpt-4.1-mini",
            enabled=True,
            stream=False,
            wire_api="chat_completions",
            requires_openai_auth=False,
            api_key=None,
        ),
        root_dir=root_dir.resolve(),
    )


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
        session_dir=root,
        transcript_file=root / "transcript.md",
        structured_file=root / "structured.md",
    )
