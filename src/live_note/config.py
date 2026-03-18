from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AudioConfig:
    sample_rate: int = 16000
    frame_duration_ms: int = 30
    vad_aggressiveness: int = 2
    silence_ms: int = 800
    min_segment_ms: int = 2000
    max_segment_ms: int = 8000
    queue_size: int = 256
    save_session_wav: bool = True


@dataclass(frozen=True, slots=True)
class ImportConfig:
    ffmpeg_binary: str = "ffmpeg"
    chunk_seconds: int = 45
    keep_normalized_audio: bool = False


@dataclass(frozen=True, slots=True)
class RefineConfig:
    enabled: bool = True
    auto_after_live: bool = True


@dataclass(frozen=True, slots=True)
class WhisperConfig:
    binary: str
    model: Path
    host: str = "127.0.0.1"
    port: int = 8178
    threads: int = 4
    language: str = "auto"
    translate: bool = False
    request_timeout_seconds: int = 120
    startup_timeout_seconds: int = 45


@dataclass(frozen=True, slots=True)
class ObsidianConfig:
    base_url: str
    transcript_dir: str
    structured_dir: str
    enabled: bool = True
    verify_ssl: bool = False
    timeout_seconds: int = 10
    retry_attempts: int = 3
    retry_backoff_seconds: float = 0.5
    api_key: str | None = None


@dataclass(frozen=True, slots=True)
class LlmConfig:
    base_url: str
    model: str
    enabled: bool = True
    stream: bool = False
    wire_api: str = "chat_completions"
    requires_openai_auth: bool = False
    timeout_seconds: int = 45
    api_key: str | None = None


@dataclass(frozen=True, slots=True)
class RemoteConfig:
    enabled: bool = False
    base_url: str = "http://127.0.0.1:8765"
    api_token: str | None = None
    timeout_seconds: int = 20
    live_chunk_ms: int = 240


@dataclass(frozen=True, slots=True)
class ServeConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    api_token: str | None = None


@dataclass(frozen=True, slots=True)
class FunAsrConfig:
    base_url: str = "ws://127.0.0.1:10095"
    mode: str = "2pass"
    use_itn: bool = True


@dataclass(frozen=True, slots=True)
class SpeakerConfig:
    enabled: bool = False
    segmentation_model: Path | None = None
    embedding_model: Path | None = None
    cluster_threshold: float = 0.5
    min_duration_on: float = 0.3
    min_duration_off: float = 0.5


@dataclass(frozen=True, slots=True)
class AppConfig:
    audio: AudioConfig
    importer: ImportConfig
    refine: RefineConfig
    whisper: WhisperConfig
    obsidian: ObsidianConfig
    llm: LlmConfig
    root_dir: Path
    remote: RemoteConfig = field(default_factory=RemoteConfig)
    serve: ServeConfig = field(default_factory=ServeConfig)
    funasr: FunAsrConfig = field(default_factory=FunAsrConfig)
    speaker: SpeakerConfig = field(default_factory=SpeakerConfig)


def with_refine_auto_after_live(config: AppConfig, auto_after_live: bool | None) -> AppConfig:
    if auto_after_live is None or config.refine.auto_after_live == auto_after_live:
        return config
    return replace(
        config,
        refine=replace(config.refine, auto_after_live=auto_after_live),
    )


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def load_config(config_path: Path | None = None, env_path: Path | None = None) -> AppConfig:
    resolved_config = (config_path or Path("config.toml")).resolve()
    if not resolved_config.exists():
        raise FileNotFoundError(
            f"配置文件不存在: {resolved_config}. 先复制 config.example.toml 为 config.toml。"
        )

    root_dir = resolved_config.parent
    env_values = load_env_file((env_path or root_dir / ".env").resolve())
    merged_env = dict(os.environ)
    merged_env.update(env_values)

    with resolved_config.open("rb") as handle:
        data = tomllib.load(handle)

    audio_data = data.get("audio", {})
    import_data = data.get("import", {})
    refine_data = data.get("refine", {})
    whisper_data = data.get("whisper", {})
    obsidian_data = data.get("obsidian", {})
    llm_data = data.get("llm", {})
    remote_data = data.get("remote", {})
    serve_data = data.get("serve", {})
    funasr_data = data.get("funasr", {})
    speaker_data = data.get("speaker", {})

    model_path = Path(str(whisper_data["model"])).expanduser()
    if not model_path.is_absolute():
        model_path = (root_dir / model_path).resolve()

    segmentation_model = _resolve_optional_path(root_dir, speaker_data.get("segmentation_model"))
    embedding_model = _resolve_optional_path(root_dir, speaker_data.get("embedding_model"))

    llm_requires_openai_auth = bool(llm_data.get("requires_openai_auth", False))
    llm_api_key = merged_env.get("LLM_API_KEY")
    if llm_requires_openai_auth:
        llm_api_key = merged_env.get("OPENAI_API_KEY") or llm_api_key

    return AppConfig(
        audio=AudioConfig(**audio_data),
        importer=ImportConfig(
            ffmpeg_binary=str(Path(str(import_data.get("ffmpeg_binary", "ffmpeg"))).expanduser()),
            chunk_seconds=int(import_data.get("chunk_seconds", 45)),
            keep_normalized_audio=bool(import_data.get("keep_normalized_audio", False)),
        ),
        refine=RefineConfig(
            enabled=bool(refine_data.get("enabled", True)),
            auto_after_live=bool(refine_data.get("auto_after_live", True)),
        ),
        whisper=WhisperConfig(
            binary=str(Path(str(whisper_data["binary"])).expanduser()),
            model=model_path,
            host=whisper_data.get("host", "127.0.0.1"),
            port=int(whisper_data.get("port", 8178)),
            threads=int(whisper_data.get("threads", 4)),
            language=whisper_data.get("language", "auto"),
            translate=bool(whisper_data.get("translate", False)),
            request_timeout_seconds=int(whisper_data.get("request_timeout_seconds", 120)),
            startup_timeout_seconds=int(whisper_data.get("startup_timeout_seconds", 45)),
        ),
        obsidian=ObsidianConfig(
            enabled=bool(obsidian_data.get("enabled", True)),
            base_url=str(obsidian_data.get("base_url", "https://127.0.0.1:27124")).rstrip("/"),
            transcript_dir=str(obsidian_data.get("transcript_dir", "Sessions/Transcripts")).strip(
                "/"
            ),
            structured_dir=str(obsidian_data.get("structured_dir", "Sessions/Summaries")).strip(
                "/"
            ),
            verify_ssl=bool(obsidian_data.get("verify_ssl", False)),
            timeout_seconds=int(obsidian_data.get("timeout_seconds", 10)),
            retry_attempts=int(obsidian_data.get("retry_attempts", 3)),
            retry_backoff_seconds=float(obsidian_data.get("retry_backoff_seconds", 0.5)),
            api_key=merged_env.get("OBSIDIAN_API_KEY"),
        ),
        llm=LlmConfig(
            enabled=bool(llm_data.get("enabled", True)),
            base_url=str(llm_data.get("base_url", "https://api.openai.com/v1")).rstrip("/"),
            model=str(llm_data.get("model", "gpt-4.1-mini")),
            stream=bool(llm_data.get("stream", False)),
            wire_api=_normalize_wire_api(str(llm_data.get("wire_api", "chat_completions"))),
            requires_openai_auth=llm_requires_openai_auth,
            timeout_seconds=int(llm_data.get("timeout_seconds", 45)),
            api_key=llm_api_key,
        ),
        remote=RemoteConfig(
            enabled=bool(remote_data.get("enabled", False)),
            base_url=str(remote_data.get("base_url", "http://127.0.0.1:8765")).rstrip("/"),
            api_token=(
                str(remote_data["api_token"]).strip() if remote_data.get("api_token") else None
            ),
            timeout_seconds=int(remote_data.get("timeout_seconds", 20)),
            live_chunk_ms=int(remote_data.get("live_chunk_ms", 240)),
        ),
        serve=ServeConfig(
            host=str(serve_data.get("host", "127.0.0.1")),
            port=int(serve_data.get("port", 8765)),
            api_token=str(serve_data["api_token"]).strip() if serve_data.get("api_token") else None,
        ),
        funasr=FunAsrConfig(
            base_url=str(funasr_data.get("base_url", "ws://127.0.0.1:10095")).rstrip("/"),
            mode=str(funasr_data.get("mode", "2pass")),
            use_itn=bool(funasr_data.get("use_itn", True)),
        ),
        speaker=SpeakerConfig(
            enabled=bool(speaker_data.get("enabled", False)),
            segmentation_model=segmentation_model,
            embedding_model=embedding_model,
            cluster_threshold=float(speaker_data.get("cluster_threshold", 0.5)),
            min_duration_on=float(speaker_data.get("min_duration_on", 0.3)),
            min_duration_off=float(speaker_data.get("min_duration_off", 0.5)),
        ),
        root_dir=root_dir,
    )


def render_config(config: AppConfig) -> str:
    sections = [
        (
            "audio",
            {
                "sample_rate": config.audio.sample_rate,
                "frame_duration_ms": config.audio.frame_duration_ms,
                "vad_aggressiveness": config.audio.vad_aggressiveness,
                "silence_ms": config.audio.silence_ms,
                "min_segment_ms": config.audio.min_segment_ms,
                "max_segment_ms": config.audio.max_segment_ms,
                "queue_size": config.audio.queue_size,
                "save_session_wav": config.audio.save_session_wav,
            },
        ),
        (
            "import",
            {
                "ffmpeg_binary": config.importer.ffmpeg_binary,
                "chunk_seconds": config.importer.chunk_seconds,
                "keep_normalized_audio": config.importer.keep_normalized_audio,
            },
        ),
        (
            "refine",
            {
                "enabled": config.refine.enabled,
                "auto_after_live": config.refine.auto_after_live,
            },
        ),
        (
            "whisper",
            {
                "binary": config.whisper.binary,
                "model": str(config.whisper.model),
                "host": config.whisper.host,
                "port": config.whisper.port,
                "threads": config.whisper.threads,
                "language": config.whisper.language,
                "translate": config.whisper.translate,
                "request_timeout_seconds": config.whisper.request_timeout_seconds,
                "startup_timeout_seconds": config.whisper.startup_timeout_seconds,
            },
        ),
        (
            "obsidian",
            {
                "enabled": config.obsidian.enabled,
                "base_url": config.obsidian.base_url,
                "transcript_dir": config.obsidian.transcript_dir,
                "structured_dir": config.obsidian.structured_dir,
                "verify_ssl": config.obsidian.verify_ssl,
                "timeout_seconds": config.obsidian.timeout_seconds,
                "retry_attempts": config.obsidian.retry_attempts,
                "retry_backoff_seconds": config.obsidian.retry_backoff_seconds,
            },
        ),
        (
            "llm",
            {
                "enabled": config.llm.enabled,
                "base_url": config.llm.base_url,
                "model": config.llm.model,
                "stream": config.llm.stream,
                "wire_api": config.llm.wire_api,
                "requires_openai_auth": config.llm.requires_openai_auth,
                "timeout_seconds": config.llm.timeout_seconds,
            },
        ),
        (
            "remote",
            {
                "enabled": config.remote.enabled,
                "base_url": config.remote.base_url,
                "api_token": config.remote.api_token or "",
                "timeout_seconds": config.remote.timeout_seconds,
                "live_chunk_ms": config.remote.live_chunk_ms,
            },
        ),
        (
            "serve",
            {
                "host": config.serve.host,
                "port": config.serve.port,
                "api_token": config.serve.api_token or "",
            },
        ),
        (
            "funasr",
            {
                "base_url": config.funasr.base_url,
                "mode": config.funasr.mode,
                "use_itn": config.funasr.use_itn,
            },
        ),
        (
            "speaker",
            {
                "enabled": config.speaker.enabled,
                "segmentation_model": str(config.speaker.segmentation_model or ""),
                "embedding_model": str(config.speaker.embedding_model or ""),
                "cluster_threshold": config.speaker.cluster_threshold,
                "min_duration_on": config.speaker.min_duration_on,
                "min_duration_off": config.speaker.min_duration_off,
            },
        ),
    ]
    lines: list[str] = []
    for section_name, values in sections:
        lines.append(f"[{section_name}]")
        for key, value in values.items():
            lines.append(f"{key} = {_render_toml_value(value)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def save_config(
    config: AppConfig,
    config_path: Path | None = None,
    env_path: Path | None = None,
) -> None:
    resolved_config = (config_path or config.root_dir / "config.toml").resolve()
    resolved_env = (env_path or resolved_config.parent / ".env").resolve()
    resolved_config.parent.mkdir(parents=True, exist_ok=True)
    resolved_env.parent.mkdir(parents=True, exist_ok=True)
    resolved_config.write_text(render_config(config), encoding="utf-8")
    env_values = load_env_file(resolved_env)
    env_values["OBSIDIAN_API_KEY"] = config.obsidian.api_key or ""
    env_values["LLM_API_KEY"] = config.llm.api_key or ""
    if config.llm.requires_openai_auth:
        env_values["OPENAI_API_KEY"] = config.llm.api_key or ""
    resolved_env.write_text(
        "\n".join(f"{key}={value}" for key, value in env_values.items()) + "\n",
        encoding="utf-8",
    )


def _render_toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _normalize_wire_api(value: str) -> str:
    normalized = value.strip().lower().replace("/", "_").replace("-", "_")
    if normalized in {"chat", "chat_completions", "chatcompletion", "chatcompletions"}:
        return "chat_completions"
    if normalized == "responses":
        return "responses"
    return value.strip() or "chat_completions"


def _resolve_optional_path(root_dir: Path, value: object) -> Path | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path
    return (root_dir / path).resolve()
