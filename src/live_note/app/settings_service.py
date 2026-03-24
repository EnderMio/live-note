from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

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
    funasr_enabled: bool = False
    funasr_base_url: str = "ws://127.0.0.1:10095"
    funasr_mode: str = "2pass"
    funasr_use_itn: bool = True
    speaker_enabled: bool = False
    speaker_backend: str = "sherpa_onnx"
    speaker_segmentation_model: str = ""
    speaker_embedding_model: str = ""
    speaker_cluster_threshold: float = 0.5
    speaker_pyannote_model: str = "pyannote/speaker-diarization-community-1"
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
            funasr_enabled=config.funasr.enabled,
            funasr_base_url=config.funasr.base_url,
            funasr_mode=config.funasr.mode,
            funasr_use_itn=config.funasr.use_itn,
            speaker_enabled=config.speaker.enabled,
            speaker_backend=config.speaker.backend,
            speaker_segmentation_model=str(config.speaker.segmentation_model or ""),
            speaker_embedding_model=str(config.speaker.embedding_model or ""),
            speaker_cluster_threshold=config.speaker.cluster_threshold,
            speaker_pyannote_model=config.speaker.pyannote_model,
            obsidian_api_key=config.obsidian.api_key or "",
            llm_api_key=config.llm.api_key or "",
        )


class SettingsService:
    def __init__(self, config_path: Path, env_path: Path):
        self.config_path = config_path.resolve()
        self.env_path = env_path.resolve()

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
            funasr_enabled=False,
            funasr_base_url="ws://127.0.0.1:10095",
            funasr_mode="2pass",
            funasr_use_itn=True,
            speaker_enabled=False,
            speaker_backend="sherpa_onnx",
            speaker_segmentation_model="",
            speaker_embedding_model="",
            speaker_cluster_threshold=0.5,
            speaker_pyannote_model="pyannote/speaker-diarization-community-1",
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
        if draft.funasr_enabled and not draft.funasr_base_url.strip():
            errors.append("启用 FunASR 实时稿时，FunASR WebSocket 地址不能为空。")
        if draft.speaker_enabled:
            if draft.speaker_backend == "pyannote":
                if not draft.speaker_pyannote_model.strip():
                    errors.append("启用 pyannote 说话人区分时，模型名不能为空。")
            else:
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
                enabled=draft.funasr_enabled,
                base_url=draft.funasr_base_url.rstrip("/"),
                mode=draft.funasr_mode,
                use_itn=draft.funasr_use_itn,
            ),
            speaker=SpeakerConfig(
                enabled=draft.speaker_enabled,
                backend=draft.speaker_backend or existing.speaker.backend,
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
                expected_speakers=existing.speaker.expected_speakers,
                cluster_threshold=float(draft.speaker_cluster_threshold),
                min_duration_on=existing.speaker.min_duration_on,
                min_duration_off=existing.speaker.min_duration_off,
                pyannote_model=(
                    draft.speaker_pyannote_model.strip() or existing.speaker.pyannote_model
                ),
                pyannote_auth_token=existing.speaker.pyannote_auth_token,
            ),
            root_dir=self.config_path.parent,
        )
        save_config(config, self.config_path, self.env_path)
        return config


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
