from __future__ import annotations

import shutil
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path


class AudioImportError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ImportedChunk:
    segment_id: str
    started_ms: int
    ended_ms: int
    wav_path: Path


def convert_audio_to_wav(
    input_path: Path,
    output_path: Path,
    sample_rate: int,
    ffmpeg_binary: str = "ffmpeg",
) -> Path:
    binary = shutil.which(ffmpeg_binary) if not Path(ffmpeg_binary).exists() else ffmpeg_binary
    if not binary:
        raise AudioImportError(f"找不到 ffmpeg 可执行文件: {ffmpeg_binary}")
    if not input_path.exists():
        raise FileNotFoundError(f"音频文件不存在: {input_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(binary),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(input_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-c:a",
        "pcm_s16le",
        str(output_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise AudioImportError(f"ffmpeg 转换失败: {detail or '未知错误'}")
    return output_path


def split_wav_file(input_path: Path, output_dir: Path, chunk_seconds: int) -> list[ImportedChunk]:
    if chunk_seconds <= 0:
        raise ValueError("chunk_seconds 必须大于 0")

    chunks: list[ImportedChunk] = []
    with wave.open(str(input_path), "rb") as handle:
        channels = handle.getnchannels()
        sample_width = handle.getsampwidth()
        sample_rate = handle.getframerate()
        if channels != 1 or sample_width != 2:
            raise AudioImportError("归一化后的 WAV 必须是 16-bit 单声道。")

        frames_per_chunk = max(1, sample_rate * chunk_seconds)
        started_frame = 0
        counter = 0
        while True:
            raw = handle.readframes(frames_per_chunk)
            if not raw:
                break
            counter += 1
            frame_count = len(raw) // (channels * sample_width)
            segment_id = f"seg-{counter:05d}"
            wav_path = output_dir / f"{segment_id}.wav"
            _write_wav(wav_path, sample_rate, raw)
            chunks.append(
                ImportedChunk(
                    segment_id=segment_id,
                    started_ms=round(started_frame * 1000 / sample_rate),
                    ended_ms=round((started_frame + frame_count) * 1000 / sample_rate),
                    wav_path=wav_path,
                )
            )
            started_frame += frame_count
    return chunks


def _write_wav(path: Path, sample_rate: int, frames: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(frames)
