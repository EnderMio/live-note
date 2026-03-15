from __future__ import annotations

import shutil
import socket
import subprocess
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from live_note.config import WhisperConfig


class WhisperError(RuntimeError):
    pass


def _normalize_language(language: str) -> str:
    normalized = language.strip().lower()
    return normalized or "auto"


def with_language_override(config: WhisperConfig, language: str | None) -> WhisperConfig:
    resolved = _normalize_language(language or config.language)
    if resolved == config.language:
        return config
    return replace(config, language=resolved)


def _encode_multipart(
    fields: dict[str, str], file_field: str, file_path: Path
) -> tuple[bytes, str]:
    boundary = f"live-note-{uuid4().hex}"
    chunks: list[bytes] = []
    for key, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode(),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )
    chunks.extend(
        [
            f"--{boundary}\r\n".encode(),
            (
                f'Content-Disposition: form-data; name="{file_field}"; '
                f'filename="{file_path.name}"\r\n'
            ).encode(),
            b"Content-Type: audio/wav\r\n\r\n",
            file_path.read_bytes(),
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    return b"".join(chunks), boundary


@dataclass(slots=True)
class WhisperInferenceClient:
    config: WhisperConfig

    def transcribe(self, wav_path: Path, prompt: str | None = None) -> str:
        fields = {
            "response_format": "text",
            "no_timestamps": "true",
            "language": _normalize_language(self.config.language),
        }
        if self.config.translate:
            fields["translate"] = "true"
        if prompt:
            fields["prompt"] = prompt
        payload, boundary = _encode_multipart(
            fields=fields,
            file_field="file",
            file_path=wav_path,
        )
        request = Request(
            url=f"http://{self.config.host}:{self.config.port}/inference",
            data=payload,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.config.request_timeout_seconds) as response:
                return response.read().decode("utf-8", errors="ignore").strip()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise WhisperError(f"whisper-server 返回错误: {exc.code} {detail}".strip()) from exc
        except URLError as exc:
            raise WhisperError(f"无法连接 whisper-server: {exc}") from exc


@dataclass(slots=True)
class WhisperServerProcess(AbstractContextManager["WhisperServerProcess"]):
    config: WhisperConfig
    log_path: Path
    process: subprocess.Popen[str] | None = None
    _log_handle: Any = None

    def start(self) -> None:
        binary = (
            shutil.which(self.config.binary)
            if not Path(self.config.binary).exists()
            else self.config.binary
        )
        if not binary:
            raise FileNotFoundError(f"找不到 whisper-server 可执行文件: {self.config.binary}")
        if not self.config.model.exists():
            raise FileNotFoundError(f"找不到模型文件: {self.config.model}")

        command = [
            str(binary),
            "-m",
            str(self.config.model),
            "--host",
            self.config.host,
            "--port",
            str(self.config.port),
            "-t",
            str(self.config.threads),
        ]
        if self.config.translate:
            command.append("-tr")
        command.extend(["-l", _normalize_language(self.config.language)])

        self._log_handle = self.log_path.open("a", encoding="utf-8")
        try:
            self.process = subprocess.Popen(
                command,
                stdout=self._log_handle,
                stderr=subprocess.STDOUT,
                text=True,
            )
            self._wait_until_ready()
        except BaseException:
            self.stop()
            raise

    def _wait_until_ready(self) -> None:
        deadline = time.time() + self.config.startup_timeout_seconds
        while time.time() < deadline:
            if self.process and self.process.poll() is not None:
                raise WhisperError("whisper-server 启动后立即退出，请检查 logs.txt。")
            with socket.socket() as sock:
                sock.settimeout(0.5)
                if sock.connect_ex((self.config.host, self.config.port)) == 0:
                    return
            time.sleep(0.5)
        raise WhisperError("whisper-server 启动超时。")

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.process = None
        if self._log_handle:
            self._log_handle.close()
            self._log_handle = None

    def __enter__(self) -> WhisperServerProcess:
        self.start()
        return self

    def __exit__(self, exc_type, exc, exc_tb) -> None:
        self.stop()
        return None
