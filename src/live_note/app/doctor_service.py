from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from live_note.config import ObsidianConfig, RemoteConfig

from .settings_service import SettingsService


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    name: str
    status: str
    detail: str


class DoctorService:
    def __init__(
        self,
        config_path: Path,
        env_path: Path,
        *,
        settings_service: SettingsService | None = None,
        obsidian_client_factory: Callable[[ObsidianConfig], Any],
        remote_client_factory: Callable[[RemoteConfig], Any],
        module_available: Callable[[str], bool],
        binary_exists: Callable[[str], bool],
    ):
        self.config_path = config_path.resolve()
        self.env_path = env_path.resolve()
        self._settings_service = settings_service or SettingsService(
            self.config_path, self.env_path
        )
        self._obsidian_client_factory = obsidian_client_factory
        self._remote_client_factory = remote_client_factory
        self._module_available = module_available
        self._binary_exists = binary_exists

    def doctor_checks(self) -> list[DoctorCheck]:
        draft = self._settings_service.load_settings_draft()
        checks: list[DoctorCheck] = []
        loaded_config = None
        try:
            loaded_config = self._settings_service.load_config()
        except Exception as exc:
            checks.append(DoctorCheck("config", "FAIL", str(exc)))
        else:
            checks.append(DoctorCheck("config", "OK", f"已加载 {self.config_path}"))

        checks.append(
            DoctorCheck(
                "sounddevice",
                "OK" if self._module_available("sounddevice") else "FAIL",
                "Python 包 sounddevice",
            )
        )
        checks.append(
            DoctorCheck(
                "webrtcvad",
                "OK" if self._module_available("webrtcvad") else "FAIL",
                "Python 包 webrtcvad / webrtcvad-wheels",
            )
        )
        checks.append(
            DoctorCheck(
                "whisper-server",
                "OK" if self._binary_exists(draft.whisper_binary) else "FAIL",
                f"可执行文件 {draft.whisper_binary or '未配置'}",
            )
        )
        checks.append(
            DoctorCheck(
                "ffmpeg",
                "OK" if self._binary_exists(draft.ffmpeg_binary) else "FAIL",
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
                obsidian = self._obsidian_client_factory(
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
                remote_client = self._remote_client_factory(
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
                if payload.get("realtime_backend"):
                    detail += f" | backend={payload['realtime_backend']}"
                if "speaker_enabled" in payload:
                    detail += (
                        f" | speaker={'on' if bool(payload.get('speaker_enabled')) else 'off'}"
                    )
                checks.append(DoctorCheck("remote_health", "OK", detail))
        else:
            checks.append(DoctorCheck("remote", "SKIP", "已关闭远端模式，默认使用本机转写"))

        if draft.speaker_enabled:
            speaker_backend = (
                loaded_config.speaker.backend
                if loaded_config is not None
                else draft.speaker_backend
            )
            if speaker_backend == "pyannote":
                checks.append(
                    DoctorCheck(
                        "speaker_pyannote_model",
                        "OK" if draft.speaker_pyannote_model.strip() else "FAIL",
                        f"pyannote 模型 {draft.speaker_pyannote_model or '未配置'}",
                    )
                )
                checks.append(
                    DoctorCheck(
                        "speaker_pyannote_token",
                        "OK"
                        if loaded_config is not None and loaded_config.speaker.pyannote_auth_token
                        else "WARN",
                        "环境变量 PYANNOTE_AUTH_TOKEN；私有或受限模型通常需要它",
                    )
                )
                checks.append(
                    DoctorCheck(
                        "speaker_pyannote_audio",
                        "OK" if self._module_available("pyannote.audio") else "FAIL",
                        "Python 包 pyannote.audio",
                    )
                )
            else:
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
                        "OK" if self._module_available("numpy") else "FAIL",
                        "Python 包 numpy",
                    )
                )
                checks.append(
                    DoctorCheck(
                        "speaker_sherpa_onnx",
                        "OK" if self._module_available("sherpa_onnx") else "FAIL",
                        "Python 包 sherpa_onnx",
                    )
                )
        else:
            checks.append(DoctorCheck("speaker", "SKIP", "已关闭说话人区分"))

        return checks
