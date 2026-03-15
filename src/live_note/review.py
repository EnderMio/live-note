from __future__ import annotations

import re
from collections.abc import Iterable

from live_note.domain import ReviewItem, TranscriptEntry
from live_note.utils import compact_text

_REPEATED_SPAN_PATTERN = re.compile(r"(.{1,3})\1{2,}")
_BRACKET_NOISE_PATTERN = re.compile(r"[()（）【】\[\]{}<>《》]")
_ALNUM_PATTERN = re.compile(r"[A-Za-z0-9]")
_CHINESE_PATTERN = re.compile(r"[\u4e00-\u9fff]")


def detect_review_items(entries: list[TranscriptEntry], language: str) -> list[ReviewItem]:
    review_items: list[ReviewItem] = []
    grouped_entries: list[TranscriptEntry] = []
    grouped_reasons: set[str] = set()

    def flush() -> None:
        nonlocal grouped_entries, grouped_reasons
        if not grouped_entries:
            return
        excerpt = compact_text(" ".join(entry.text for entry in grouped_entries))
        review_items.append(
            ReviewItem(
                started_ms=grouped_entries[0].started_ms,
                ended_ms=grouped_entries[-1].ended_ms,
                reason_labels=tuple(sorted(grouped_reasons)),
                excerpt=excerpt,
            )
        )
        grouped_entries = []
        grouped_reasons = set()

    for entry in entries:
        reasons = set(_detect_entry_reasons(entry, language))
        if not reasons:
            flush()
            continue
        if grouped_entries and entry.started_ms - grouped_entries[-1].ended_ms > 3000:
            flush()
        grouped_entries.append(entry)
        grouped_reasons.update(reasons)

    flush()
    return review_items


def _detect_entry_reasons(entry: TranscriptEntry, language: str) -> Iterable[str]:
    text = compact_text(entry.text)
    if not text:
        return []

    duration_ms = max(0, entry.ended_ms - entry.started_ms)
    merged = re.sub(r"\s+", "", text)
    reasons: list[str] = []

    if duration_ms >= 6000 and len(merged) <= 4:
        reasons.append("时长偏长但文本过短")
    if duration_ms <= 2000 and len(merged) >= 40:
        reasons.append("时长偏短但文本过长")
    if _has_repeated_span(merged):
        reasons.append("存在明显重复")
    if _has_noise_heavy_text(text):
        reasons.append("噪声符号偏多")
    if language.startswith("zh") and _has_suspicious_mixed_scripts(text):
        reasons.append("中英数字混杂异常")
    return reasons


def _has_repeated_span(text: str) -> bool:
    if _REPEATED_SPAN_PATTERN.search(text):
        return True
    return any(char * 4 in text for char in text if char.strip())


def _has_noise_heavy_text(text: str) -> bool:
    bracket_count = len(_BRACKET_NOISE_PATTERN.findall(text))
    punctuation_count = sum(1 for char in text if not char.isalnum() and not char.isspace())
    return bracket_count >= 2 or punctuation_count >= max(6, len(text) // 2)


def _has_suspicious_mixed_scripts(text: str) -> bool:
    condensed = re.sub(r"\s+", "", text)
    if len(condensed) < 10:
        return False
    chinese_count = len(_CHINESE_PATTERN.findall(condensed))
    alnum_count = len(_ALNUM_PATTERN.findall(condensed))
    if chinese_count < 2 or alnum_count < 4:
        return False
    ratio = alnum_count / max(len(condensed), 1)
    if 0.2 <= ratio <= 0.8:
        return True
    transitions = 0
    previous = ""
    for char in condensed:
        if _CHINESE_PATTERN.fullmatch(char):
            current = "zh"
        elif _ALNUM_PATTERN.fullmatch(char):
            current = "alnum"
        else:
            current = "other"
        if previous and current != previous:
            transitions += 1
        previous = current
    return transitions >= 5 and alnum_count * 3 >= len(condensed)
