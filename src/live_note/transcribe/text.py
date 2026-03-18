from __future__ import annotations

import math
import re
from array import array
from collections.abc import Sequence

from live_note.domain import TranscriptEntry
from live_note.utils import compact_text

try:
    from opencc import OpenCC
except ImportError:  # pragma: no cover - optional dependency
    OpenCC = None

_SIMPLIFIER = OpenCC("t2s") if OpenCC else None
_CONTEXT_ENTRY_LIMIT = 3
_CONTEXT_CHAR_LIMIT = 120
_CN_PROMPT_GUARD = (
    "只转写实际听到的语音。"
    "如果只有静音、背景噪声、音乐、掌声或环境声，请返回空字符串。"
    "不要补全，不要编造，不要输出“谢谢观看”“感谢观看”“欢迎订阅我的频道”“谢谢大家”等片尾话术。"
    "如果结果只剩标点、破折号、重复短语或明显胡言乱语，也请返回空字符串。"
    "中文请使用简体中文。"
)
_EN_PROMPT_GUARD = (
    "Transcribe only audible speech. "
    "If the audio contains only silence, noise, music, applause, or ambient sound, "
    "return an empty string. "
    "If the result would be only punctuation, dashes, "
    "or a repeated short phrase, return an empty string. "
    "Do not invent outro phrases such as 'thanks for watching' or 'please subscribe'."
)
_AUTO_PROMPT_GUARD = (
    "Transcribe only audible speech. "
    "If the audio contains only silence, noise, music, applause, or ambient sound, "
    "return an empty string. "
    "Preserve the original spoken languages and scripts. "
    "Use Simplified Chinese for Chinese text. "
    "If speakers switch languages, keep the mixed output instead of translating or rewriting. "
    "If the result would be only punctuation, dashes, "
    "or a repeated short phrase, return an empty string. "
    "Do not invent outro phrases such as 'thanks for watching' or 'please subscribe'."
)
_SUSPICIOUS_OUTRO_PHRASES = (
    "谢谢观看",
    "感谢观看",
    "谢谢收看",
    "感谢收看",
    "欢迎订阅我的频道",
    "欢迎订阅",
    "请订阅我的频道",
    "点赞关注",
    "记得点赞订阅",
    "欢迎点赞订阅",
    "谢谢大家",
    "感谢大家",
)
_PUNCTUATION_PATTERN = re.compile(r"[，。！？!?,.、：:；;“”\"'`·\s]+")
_REPEATED_SPAN_PATTERN = re.compile(r"(.{1,8})\1{2,}")


def build_transcription_prompt(language: str, entries: Sequence[TranscriptEntry]) -> str:
    guard = _build_guard_prompt(language)
    context = _build_context(entries)
    if not context:
        return guard
    if _is_chinese_language(language):
        return f"{guard}\n最近上下文：{context}"
    return f"{guard}\nRecent context: {context}"


def normalize_transcript_text(
    text: str,
    language: str,
    *,
    pcm16: bytes | None = None,
    sample_rate: int | None = None,
) -> str:
    normalized = " ".join(part.strip() for part in text.splitlines() if part.strip())
    if not normalized:
        return ""

    if _should_simplify(language, normalized) and _SIMPLIFIER:
        normalized = _SIMPLIFIER.convert(normalized)
    normalized = normalized.strip()
    if pcm16 and sample_rate and _should_drop_silence_hallucination(normalized, pcm16, sample_rate):
        return ""
    return normalized


def _build_context(entries: Sequence[TranscriptEntry]) -> str:
    snippets = [
        entry.text.strip() for entry in entries[-_CONTEXT_ENTRY_LIMIT:] if entry.text.strip()
    ]
    if not snippets:
        return ""
    context = " ".join(snippets)
    if len(context) <= _CONTEXT_CHAR_LIMIT:
        return context
    return context[-_CONTEXT_CHAR_LIMIT:]


def _normalize_language(language: str) -> str:
    return language.strip().lower().replace("_", "-") or "auto"


def _is_auto_language(language: str) -> bool:
    return _normalize_language(language) == "auto"


def _is_chinese_language(language: str) -> bool:
    normalized = _normalize_language(language)
    return normalized == "zh" or normalized.startswith("zh-")


def _should_simplify(language: str, text: str) -> bool:
    if _is_chinese_language(language):
        return True
    if not _is_auto_language(language):
        return False
    return not _contains_japanese_kana(text)


def _build_guard_prompt(language: str) -> str:
    if _is_chinese_language(language):
        return _CN_PROMPT_GUARD
    if _is_auto_language(language):
        return _AUTO_PROMPT_GUARD
    return _EN_PROMPT_GUARD


def _contains_japanese_kana(text: str) -> bool:
    return any("\u3040" <= char <= "\u30ff" for char in text)


def _should_drop_silence_hallucination(text: str, pcm16: bytes, sample_rate: int) -> bool:
    if _looks_like_punctuation_noise(text):
        return True

    cleaned = _normalize_phrase(text)
    if not cleaned:
        return True

    rms_ratio = _estimate_rms_ratio(pcm16)
    noise_floor = _noise_floor_threshold(sample_rate)

    if len(cleaned) <= 20 and any(phrase in cleaned for phrase in _SUSPICIOUS_OUTRO_PHRASES):
        return rms_ratio < noise_floor
    if _looks_like_repetitive_hallucination(cleaned):
        return rms_ratio < noise_floor * 1.5
    return False


def _normalize_phrase(text: str) -> str:
    return _PUNCTUATION_PATTERN.sub("", compact_text(text)).lower()


def _looks_like_punctuation_noise(text: str) -> bool:
    condensed = compact_text(text)
    return bool(condensed) and not _normalize_phrase(condensed)


def _looks_like_repetitive_hallucination(cleaned: str) -> bool:
    if len(cleaned) < 6:
        return False
    if _REPEATED_SPAN_PATTERN.search(cleaned):
        return True
    return any(char * 4 in cleaned for char in cleaned if char.strip())


def _estimate_rms_ratio(pcm16: bytes) -> float:
    if not pcm16:
        return 0.0
    samples = array("h")
    samples.frombytes(pcm16)
    if not samples:
        return 0.0
    mean_square = sum(sample * sample for sample in samples) / len(samples)
    return math.sqrt(mean_square) / 32768.0


def _noise_floor_threshold(sample_rate: int) -> float:
    if sample_rate >= 16000:
        return 0.012
    return 0.015
