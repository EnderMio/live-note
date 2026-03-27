from __future__ import annotations

LANGUAGE_LABEL_TO_CODE = {
    "沿用默认设置": "",
    "自动识别 / 中英混合 / 多语言（auto）": "auto",
    "中文（zh）": "zh",
    "英文（en）": "en",
    "日文（ja）": "ja",
    "韩文（ko）": "ko",
}
LANGUAGE_CODE_TO_LABEL = {code: label for label, code in LANGUAGE_LABEL_TO_CODE.items() if code}


def normalize_language_value(value: str, blank_fallback: str = "") -> str:
    normalized = LANGUAGE_LABEL_TO_CODE.get(value.strip(), value.strip()).lower()
    if not normalized:
        return blank_fallback
    return normalized


def optional_language_override(value: str) -> str | None:
    normalized = normalize_language_value(value)
    return normalized or None


def language_code_to_display(code: str, allow_blank: bool) -> str:
    normalized = code.strip().lower()
    if not normalized:
        return "沿用默认设置" if allow_blank else "自动识别 / 中英混合 / 多语言（auto）"
    return LANGUAGE_CODE_TO_LABEL.get(normalized, normalized)
