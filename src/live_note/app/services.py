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
    FunAsrConfig,
    ImportConfig,
    LlmConfig,
    ObsidianConfig,
    RefineConfig,
    RemoteConfig,
    ServeConfig,
    SpeakerConfig,
    WhisperConfig,
    load_config,
    load_env_file,
    save_config,
)
from live_note.obsidian.client import ObsidianClient

from ..remote.client import RemoteClient
from ..remote.protocol import entry_from_dict, metadata_from_dict
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
from .remote_coordinator import RemoteLiveCoordinator
from .remote_sync import apply_remote_artifacts
from .task_queue import QueuedTaskRecord


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
    execution_target: str
    speaker_status: str
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
    remote_enabled: bool = False
    remote_base_url: str = "http://127.0.0.1:8765"
    remote_api_token: str = ""
    remote_live_chunk_ms: int = 240
    serve_host: str = "127.0.0.1"
    serve_port: int = 8765
    serve_api_token: str = ""
    funasr_base_url: str = "ws://127.0.0.1:10095"
    funasr_mode: str = "2pass"
    funasr_use_itn: bool = True
    speaker_enabled: bool = False
    speaker_segmentation_model: str = ""
    speaker_embedding_model: str = ""
    speaker_cluster_threshold: float = 0.5
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
            remote_enabled=config.remote.enabled,
            remote_base_url=config.remote.base_url,
            remote_api_token=config.remote.api_token or "",
            remote_live_chunk_ms=config.remote.live_chunk_ms,
            serve_host=config.serve.host,
            serve_port=config.serve.port,
            serve_api_token=config.serve.api_token or "",
            funasr_base_url=config.funasr.base_url,
            funasr_mode=config.funasr.mode,
            funasr_use_itn=config.funasr.use_itn,
            speaker_enabled=config.speaker.enabled,
            speaker_segmentation_model=str(config.speaker.segmentation_model or ""),
            speaker_embedding_model=str(config.speaker.embedding_model or ""),
            speaker_cluster_threshold=config.speaker.cluster_threshold,
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
            remote_enabled=False,
            remote_base_url="http://127.0.0.1:8765",
            remote_api_token="",
            remote_live_chunk_ms=240,
            serve_host="127.0.0.1",
            serve_port=8765,
            serve_api_token="",
            funasr_base_url="ws://127.0.0.1:10095",
            funasr_mode="2pass",
            funasr_use_itn=True,
            speaker_enabled=False,
            speaker_segmentation_model="",
            speaker_embedding_model="",
            speaker_cluster_threshold=0.5,
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
        if draft.remote_enabled and not draft.remote_base_url.strip():
            errors.append("启用远端模式时，远端 Base URL 不能为空。")
        if draft.speaker_enabled:
            if not draft.speaker_segmentation_model.strip():
                errors.append("启用说话人区分时，分割模型路径不能为空。")
            if not draft.speaker_embedding_model.strip():
                errors.append("启用说话人区分时，嵌入模型路径不能为空。")
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
            remote=RemoteConfig(
                enabled=draft.remote_enabled,
                base_url=draft.remote_base_url.rstrip("/"),
                api_token=draft.remote_api_token.strip() or None,
                timeout_seconds=existing.remote.timeout_seconds,
                live_chunk_ms=int(draft.remote_live_chunk_ms),
            ),
            serve=ServeConfig(
                host=draft.serve_host.strip() or "127.0.0.1",
                port=int(draft.serve_port),
                api_token=draft.serve_api_token.strip() or None,
            ),
            funasr=FunAsrConfig(
                base_url=draft.funasr_base_url.rstrip("/"),
                mode=draft.funasr_mode,
                use_itn=draft.funasr_use_itn,
            ),
            speaker=SpeakerConfig(
                enabled=draft.speaker_enabled,
                segmentation_model=(
                    Path(draft.speaker_segmentation_model).expanduser()
                    if draft.speaker_segmentation_model.strip()
                    else None
                ),
                embedding_model=(
                    Path(draft.speaker_embedding_model).expanduser()
                    if draft.speaker_embedding_model.strip()
                    else None
                ),
                cluster_threshold=float(draft.speaker_cluster_threshold),
                min_duration_on=existing.speaker.min_duration_on,
                min_duration_off=existing.speaker.min_duration_off,
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

        if draft.remote_enabled:
            checks.append(
                DoctorCheck(
                    "remote_api_token",
                    "OK" if draft.remote_api_token else "WARN",
                    "远端 API Token；未设置时仅适合无认证的局域网环境",
                )
            )
            try:
                remote_client = RemoteClient(
                    RemoteConfig(
                        enabled=True,
                        base_url=draft.remote_base_url.rstrip("/"),
                        api_token=draft.remote_api_token or None,
                    )
                )
                payload = remote_client.health()
            except Exception as exc:
                detail = str(exc) if draft.remote_base_url else "未配置远端 Base URL"
                checks.append(DoctorCheck("remote_health", "FAIL", detail))
            else:
                detail = (
                    f"连通 {draft.remote_base_url.rstrip('/')} | "
                    f"{payload.get('service', 'unknown-service')}"
                )
                if "speaker_enabled" in payload:
                    detail += (
                        f" | speaker={'on' if bool(payload.get('speaker_enabled')) else 'off'}"
                    )
                checks.append(DoctorCheck("remote_health", "OK", detail))
        else:
            checks.append(DoctorCheck("remote", "SKIP", "已关闭远端模式，默认使用本机转写"))

        if draft.speaker_enabled:
            segmentation_model = (
                Path(draft.speaker_segmentation_model).expanduser()
                if draft.speaker_segmentation_model
                else None
            )
            embedding_model = (
                Path(draft.speaker_embedding_model).expanduser()
                if draft.speaker_embedding_model
                else None
            )
            checks.append(
                DoctorCheck(
                    "speaker_segmentation_model",
                    "OK" if segmentation_model and segmentation_model.exists() else "FAIL",
                    f"分割模型 {draft.speaker_segmentation_model or '未配置'}",
                )
            )
            checks.append(
                DoctorCheck(
                    "speaker_embedding_model",
                    "OK" if embedding_model and embedding_model.exists() else "FAIL",
                    f"嵌入模型 {draft.speaker_embedding_model or '未配置'}",
                )
            )
            checks.append(
                DoctorCheck(
                    "speaker_numpy",
                    "OK" if _module_available("numpy") else "FAIL",
                    "Python 包 numpy",
                )
            )
            checks.append(
                DoctorCheck(
                    "speaker_sherpa_onnx",
                    "OK" if _module_available("sherpa_onnx") else "FAIL",
                    "Python 包 sherpa_onnx",
                )
            )
        else:
            checks.append(DoctorCheck("speaker", "SKIP", "已关闭说话人区分"))

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
                    execution_target=metadata.execution_target,
                    speaker_status=metadata.speaker_status,
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
        auto_refine_after_live: bool | None = None,
    ) -> SessionCoordinator:
        config = self.load_config()
        coordinator_cls = (
            RemoteLiveCoordinator
            if getattr(config.remote, "enabled", False)
            else SessionCoordinator
        )
        kwargs = dict(
            config=config,
            title=title,
            source=source,
            kind=kind,
            language=language,
            on_progress=on_progress,
        )
        if auto_refine_after_live is not None:
            kwargs["auto_refine_after_live"] = auto_refine_after_live
        return coordinator_cls(
            **kwargs,
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
        config = self.load_config()
        workspace = SessionWorkspace.load(config.root_dir / ".live-note" / "sessions" / session_id)
        metadata = workspace.read_session()
        if metadata.execution_target == "remote":
            remote_session_id = metadata.remote_session_id or metadata.session_id
            client = RemoteClient(config.remote)
            client.refine(remote_session_id)
            artifacts = client.get_artifacts(remote_session_id)
            apply_remote_artifacts(
                config,
                metadata_from_dict(dict(artifacts["metadata"])),
                [entry_from_dict(dict(item)) for item in artifacts.get("entries", [])],
                on_progress=on_progress,
            )
            return 0
        return refine_session(config, session_id, on_progress=on_progress)

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

    def run_queue_task(
        self,
        record: QueuedTaskRecord,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> int:
        payload = record.payload
        if record.action == "import":
            language = payload.get("language")
            runner = self.create_import_coordinator(
                file_path=str(payload["file_path"]),
                title=payload.get("title") or None,
                kind=str(payload.get("kind") or "generic"),
                language=language if isinstance(language, str) else None,
                on_progress=on_progress,
            )
            return runner.run()
        if record.action == "merge":
            return self.merge(
                [str(item) for item in payload.get("session_ids", [])],
                title=payload.get("title") if isinstance(payload.get("title"), str) else None,
                on_progress=on_progress,
            )
        if record.action == "session_action":
            operation = payload.get("action") or payload.get("operation")
            session_id = str(payload["session_id"])
            if operation == "retranscribe":
                return self.retranscribe(session_id, on_progress=on_progress)
            if operation == "refine":
                return self.refine(session_id, on_progress=on_progress)
            if operation == "republish":
                return self.republish(session_id, on_progress=on_progress)
            if operation == "resync":
                return self.resync_notes(session_id, on_progress=on_progress)
        raise RuntimeError(f"不支持的队列任务：{record.action}")

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
        remote=RemoteConfig(),
        serve=ServeConfig(),
        funasr=FunAsrConfig(),
        speaker=SpeakerConfig(),
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
        execution_target="unknown",
        speaker_status="unknown",
        session_dir=root,
        transcript_file=root / "transcript.md",
        structured_file=root / "structured.md",
    )
