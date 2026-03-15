from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from live_note.config import LlmConfig
from live_note.domain import SessionMetadata, TranscriptEntry
from live_note.utils import compact_text


class LlmError(RuntimeError):
    pass


SYSTEM_PROMPT = """你是音频内容整理助手。
请只输出 Markdown，使用以下二级标题且不要改名：
## 摘要
## 关键点
## 时间线
## 待跟进
每一节都要有内容。内容必须严格基于原始转写，不要虚构事实。"""
USER_AGENT = "live-note/0.1"


def _kind_guidance(kind: str) -> str:
    if kind == "meeting":
        return "重点提炼会议结论、待办事项、责任分工和阻塞点。"
    if kind == "lecture":
        return "重点提炼知识点、论证脉络、例子和需要复习的问题。"
    return "重点提炼主题、重要观点、关键事实和后续行动。"


@dataclass(slots=True)
class OpenAiCompatibleClient:
    config: LlmConfig

    def is_enabled(self) -> bool:
        return self.config.enabled

    def is_configured(self) -> bool:
        return bool(
            self.config.enabled
            and self.config.base_url
            and self.config.model
            and self.config.api_key
        )

    def generate_structured_note(
        self, metadata: SessionMetadata, entries: list[TranscriptEntry]
    ) -> str:
        if not self.is_configured():
            raise LlmError("LLM 配置不完整，无法生成结构化笔记。")

        payload = _build_request_payload(self.config, metadata, entries)
        request = Request(
            url=_request_url(self.config),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream" if self.config.stream else "application/json",
                "Authorization": f"Bearer {self.config.api_key}",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                if self.config.stream:
                    return _read_streaming_completion(response, self.config.wire_api)
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise LlmError(f"LLM 请求失败: {exc.code} {detail}".strip()) from exc
        except URLError as exc:
            raise LlmError(f"无法连接 LLM 服务: {exc}") from exc

        if not body.strip():
            raise LlmError("LLM 返回为空。")
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise LlmError("LLM 返回格式不符合预期。") from exc
        return _read_non_stream_completion(data, self.config.wire_api)


def _build_request_payload(
    config: LlmConfig,
    metadata: SessionMetadata,
    entries: list[TranscriptEntry],
) -> dict[str, object]:
    transcript = "\n".join(
        f"[{entry.started_ms // 1000:04d}s] {compact_text(entry.text)}" for entry in entries
    )
    user_prompt = (
        f"标题: {metadata.title}\n"
        f"内容类型: {metadata.kind}\n"
        f"输入模式: {metadata.input_mode}\n"
        f"语言: {metadata.language}\n\n"
        f"整理要求: {_kind_guidance(metadata.kind)}\n\n"
        f"原始转写:\n{transcript}"
    )
    if config.wire_api == "responses":
        payload: dict[str, object] = {
            "model": config.model,
            "instructions": SYSTEM_PROMPT,
            "input": user_prompt,
            "temperature": 0.2,
        }
    else:
        payload = {
            "model": config.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
        }
    if config.stream:
        payload["stream"] = True
    return payload


def _request_url(config: LlmConfig) -> str:
    if config.wire_api == "responses":
        return f"{config.base_url}/responses"
    return f"{config.base_url}/chat/completions"


def _read_non_stream_completion(data: dict[str, object], wire_api: str) -> str:
    if wire_api == "responses":
        content = _extract_responses_output(data)
    else:
        content = _extract_choice_content(data)
    if content is None:
        raise LlmError("LLM 返回格式不符合预期。")
    return content.strip()


def _read_streaming_completion(response: object, wire_api: str) -> str:
    parts: list[str] = []
    for payload in _iter_sse_payloads(response):
        if payload == "[DONE]":
            break
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise LlmError("LLM 流式返回格式不符合预期。") from exc
        if isinstance(data, dict) and data.get("error"):
            raise LlmError(f"LLM 流式请求失败: {data['error']}")
        delta = _extract_stream_delta(data, wire_api)
        if delta:
            parts.append(delta)
    content = "".join(parts).strip()
    if not content:
        raise LlmError("LLM 流式返回为空。")
    return content


def _iter_sse_payloads(response: object) -> list[str]:
    payloads: list[str] = []
    event_lines: list[str] = []
    for raw_line in response:
        line = raw_line.decode("utf-8", errors="ignore").rstrip("\r\n")
        if not line:
            if event_lines:
                payloads.append("\n".join(event_lines))
                event_lines.clear()
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            event_lines.append(line[5:].lstrip())
    if event_lines:
        payloads.append("\n".join(event_lines))
    return payloads


def _extract_choice_content(data: dict[str, object]) -> str | None:
    try:
        choice = data["choices"][0]
    except (KeyError, IndexError, TypeError):
        return None
    if not isinstance(choice, dict):
        return None
    message = choice.get("message")
    if isinstance(message, dict):
        content = _normalize_content_value(message.get("content"))
        if content is not None:
            return content
    delta = choice.get("delta")
    if isinstance(delta, dict):
        return _normalize_content_value(delta.get("content"))
    return _normalize_content_value(choice.get("text"))


def _extract_stream_delta_with_wire_api(
    data: dict[str, object],
    event_type: object,
    wire_api: str,
) -> str | None:
    if wire_api == "responses":
        return _extract_responses_stream_delta(data, event_type)
    return _extract_choice_content(data)


def _extract_stream_delta(data: object, wire_api: str) -> str | None:
    if not isinstance(data, dict):
        return None
    return _extract_stream_delta_with_wire_api(data, data.get("type"), wire_api)


def _extract_responses_output(data: dict[str, object]) -> str | None:
    output_text = data.get("output_text")
    normalized = _normalize_content_value(output_text)
    if normalized is not None:
        return normalized

    output = data.get("output")
    if not isinstance(output, list):
        return None

    parts: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for chunk in content:
            if not isinstance(chunk, dict):
                continue
            if chunk.get("type") in {"output_text", "text"}:
                text = _normalize_content_value(chunk.get("text"))
                if text:
                    parts.append(text)
    if parts:
        return "".join(parts)
    return None


def _extract_responses_stream_delta(data: dict[str, object], event_type: object) -> str | None:
    if event_type == "response.output_text.delta":
        return _normalize_content_value(data.get("delta"))
    return None


def _normalize_content_value(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
        if parts:
            return "".join(parts)
    return None
