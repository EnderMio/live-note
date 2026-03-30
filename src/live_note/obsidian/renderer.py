from __future__ import annotations

from live_note.domain import ReviewItem, SessionMetadata, TranscriptEntry
from live_note.utils import compact_text, format_ms, yaml_quote


def build_transcript_note(
    metadata: SessionMetadata,
    entries: list[TranscriptEntry],
    status: str,
    review_items: list[ReviewItem] | None = None,
    session_audio_path: str | None = None,
) -> str:
    lines = [
        "---",
        f"title: {yaml_quote(metadata.note_stem)}",
        f"session_title: {yaml_quote(metadata.title)}",
        f"session_id: {yaml_quote(metadata.session_id)}",
        f"started_at: {yaml_quote(metadata.started_at)}",
        f"kind: {yaml_quote(metadata.kind)}",
        f"input_mode: {yaml_quote(metadata.input_mode)}",
        f"source_label: {yaml_quote(metadata.source_label)}",
        f"source_ref: {yaml_quote(metadata.source_ref)}",
        f"status: {yaml_quote(status)}",
        f"transcript_source: {yaml_quote(metadata.transcript_source)}",
        f"refine_status: {yaml_quote(metadata.refine_status)}",
        f"execution_target: {yaml_quote(metadata.execution_target)}",
        f"speaker_status: {yaml_quote(metadata.speaker_status)}",
        "tags:",
        "  - live-note/transcript",
        f"  - live-note/{metadata.kind}",
        f"  - live-note/{metadata.input_mode}",
        "---",
        "",
        f"# {metadata.title}",
        "",
        "## 转写记录",
    ]
    if entries:
        lines.extend(f"- [{format_ms(entry.started_ms)}] {_entry_text(entry)}" for entry in entries)
    else:
        lines.append("- 等待音频输入或文件转写…")
    lines.extend(
        [
            "",
            "## 会话信息",
            f"- Session ID: `{metadata.session_id}`",
            f"- 类型: `{metadata.kind}`",
            f"- 输入模式: `{metadata.input_mode}`",
            f"- 输入源: `{metadata.source_label}`",
            f"- 语言: `{metadata.language}`",
            f"- 转写来源: `{metadata.transcript_source}`",
            f"- 精修状态: `{metadata.refine_status}`",
            f"- 运行位置: `{metadata.execution_target}`",
            f"- 说话人区分: `{metadata.speaker_status}`",
        ]
    )
    if metadata.remote_session_id:
        lines.append(f"- 远端会话: `{metadata.remote_session_id}`")
    if session_audio_path:
        lines.append(f"- 整场录音: `{session_audio_path}`")
    if review_items is not None:
        lines.extend(["", "## 待复核段落"])
        if review_items:
            for item in review_items:
                labels = " / ".join(item.reason_labels)
                excerpt = _clip_review_excerpt(item.excerpt)
                line = (
                    f"- [{format_ms(item.started_ms)} - {format_ms(item.ended_ms)}] "
                    f"{labels}：{excerpt}"
                )
                if session_audio_path:
                    line = (
                        f"{line}；回听：`{session_audio_path} @ "
                        f"{format_ms(item.started_ms)}-{format_ms(item.ended_ms)}`"
                    )
                lines.append(line)
        else:
            lines.append("- 未发现明显异常段落。")
    return "\n".join(lines) + "\n"


def build_transcript_failure_note(metadata: SessionMetadata, reason: str) -> str:
    lines = [
        "---",
        f"title: {yaml_quote(metadata.note_stem)}",
        f"session_title: {yaml_quote(metadata.title)}",
        f"session_id: {yaml_quote(metadata.session_id)}",
        f"started_at: {yaml_quote(metadata.started_at)}",
        f"kind: {yaml_quote(metadata.kind)}",
        f"input_mode: {yaml_quote(metadata.input_mode)}",
        f"source_label: {yaml_quote(metadata.source_label)}",
        f"source_ref: {yaml_quote(metadata.source_ref)}",
        'status: "failed"',
        f"transcript_source: {yaml_quote(metadata.transcript_source)}",
        f"refine_status: {yaml_quote(metadata.refine_status)}",
        f"execution_target: {yaml_quote(metadata.execution_target)}",
        f"speaker_status: {yaml_quote(metadata.speaker_status)}",
        "tags:",
        "  - live-note/transcript",
        "  - live-note/failed",
        "---",
        "",
        f"# {metadata.title}",
        "",
        "## 转写记录",
        "",
        "原文暂未成功生成。",
        "",
        "## 待跟进",
        "",
        f"- {compact_text(reason)}",
        "",
        "## 会话信息",
        f"- Session ID: `{metadata.session_id}`",
        f"- 类型: `{metadata.kind}`",
        f"- 输入模式: `{metadata.input_mode}`",
        f"- 输入源: `{metadata.source_label}`",
        f"- 语言: `{metadata.language}`",
        f"- 转写来源: `{metadata.transcript_source}`",
        f"- 精修状态: `{metadata.refine_status}`",
        f"- 运行位置: `{metadata.execution_target}`",
        f"- 说话人区分: `{metadata.speaker_status}`",
    ]
    if metadata.remote_session_id:
        lines.append(f"- 远端会话: `{metadata.remote_session_id}`")
    return "\n".join(lines) + "\n"


def build_structured_note(
    metadata: SessionMetadata,
    llm_markdown: str,
    transcript_note_path: str,
    status: str,
) -> str:
    link_target = transcript_note_path.removesuffix(".md")
    body = llm_markdown.strip() or "## 摘要\n\n暂无内容。\n"
    body = f"{_build_generation_section(metadata)}\n\n{body}".strip()
    if "## 原文链接" not in body:
        body = f"{body.rstrip()}\n\n## 原文链接\n\n- [[{link_target}|查看原文]]\n"
    return "\n".join(
        [
            "---",
            f"title: {yaml_quote(metadata.note_stem + ' 整理')}",
            f"session_title: {yaml_quote(metadata.title)}",
            f"session_id: {yaml_quote(metadata.session_id)}",
            f"started_at: {yaml_quote(metadata.started_at)}",
            f"kind: {yaml_quote(metadata.kind)}",
            f"input_mode: {yaml_quote(metadata.input_mode)}",
            f"status: {yaml_quote(status)}",
            f"transcript_source: {yaml_quote(metadata.transcript_source)}",
            f"refine_status: {yaml_quote(metadata.refine_status)}",
            f"execution_target: {yaml_quote(metadata.execution_target)}",
            f"speaker_status: {yaml_quote(metadata.speaker_status)}",
            "tags:",
            "  - live-note/structured",
            f"  - live-note/{metadata.kind}",
            f"  - live-note/{metadata.input_mode}",
            "---",
            "",
            f"# {metadata.title} 整理笔记",
            "",
            body.rstrip(),
            "",
        ]
    )


def build_structured_failure_note(
    metadata: SessionMetadata,
    transcript_note_path: str,
    reason: str,
) -> str:
    link_target = transcript_note_path.removesuffix(".md")
    return "\n".join(
        [
            "---",
            f"title: {yaml_quote(metadata.note_stem + ' 整理')}",
            f"session_title: {yaml_quote(metadata.title)}",
            f"session_id: {yaml_quote(metadata.session_id)}",
            f"started_at: {yaml_quote(metadata.started_at)}",
            f"kind: {yaml_quote(metadata.kind)}",
            f"input_mode: {yaml_quote(metadata.input_mode)}",
            'status: "failed"',
            f"transcript_source: {yaml_quote(metadata.transcript_source)}",
            f"refine_status: {yaml_quote(metadata.refine_status)}",
            f"execution_target: {yaml_quote(metadata.execution_target)}",
            f"speaker_status: {yaml_quote(metadata.speaker_status)}",
            "tags:",
            "  - live-note/structured",
            "  - live-note/failed",
            "---",
            "",
            f"# {metadata.title} 整理笔记",
            "",
            _build_generation_section(metadata),
            "",
            "## 摘要",
            "",
            "结构化整理尚未成功完成。",
            "",
            "## 待跟进",
            "",
            f"- {compact_text(reason)}",
            "",
            "## 原文链接",
            "",
            f"- [[{link_target}|查看原文]]",
            "",
        ]
    )


def build_structured_pending_note(
    metadata: SessionMetadata,
    transcript_note_path: str,
    reason: str,
) -> str:
    link_target = transcript_note_path.removesuffix(".md")
    return "\n".join(
        [
            "---",
            f"title: {yaml_quote(metadata.note_stem + ' 整理')}",
            f"session_title: {yaml_quote(metadata.title)}",
            f"session_id: {yaml_quote(metadata.session_id)}",
            f"started_at: {yaml_quote(metadata.started_at)}",
            f"kind: {yaml_quote(metadata.kind)}",
            f"input_mode: {yaml_quote(metadata.input_mode)}",
            'status: "pending"',
            f"transcript_source: {yaml_quote(metadata.transcript_source)}",
            f"refine_status: {yaml_quote(metadata.refine_status)}",
            f"execution_target: {yaml_quote(metadata.execution_target)}",
            f"speaker_status: {yaml_quote(metadata.speaker_status)}",
            "tags:",
            "  - live-note/structured",
            "  - live-note/pending",
            "---",
            "",
            f"# {metadata.title} 整理笔记",
            "",
            _build_generation_section(metadata),
            "",
            "## 摘要",
            "",
            f"- {compact_text(reason)}",
            "",
            "## 关键点",
            "",
            "- 待补充",
            "",
            "## 时间线",
            "",
            "- 参考原文记录补充关键节点",
            "",
            "## 待跟进",
            "",
            "- 如需自动整理，请在设置中启用并配置 LLM",
            "",
            "## 原文链接",
            "",
            f"- [[{link_target}|查看原文]]",
            "",
        ]
    )


def _build_generation_section(metadata: SessionMetadata) -> str:
    lines = [
        "## 生成说明",
        "",
        f"- 转写来源: `{metadata.transcript_source}`",
        f"- 精修状态: `{metadata.refine_status}`",
        f"- 运行位置: `{metadata.execution_target}`",
        f"- 说话人区分: `{metadata.speaker_status}`",
    ]
    if metadata.transcript_source == "live":
        if metadata.refine_status == "failed":
            lines.append("- 当前整理基于实时草稿；最近一次离线精修失败。")
        elif metadata.refine_status == "pending":
            lines.append("- 当前整理基于实时草稿；尚未执行离线精修。")
        elif metadata.refine_status == "disabled":
            lines.append("- 当前整理基于实时草稿；离线精修已关闭。")
    return "\n".join(lines)


def _clip_review_excerpt(value: str) -> str:
    excerpt = compact_text(value)
    if len(excerpt) <= 80:
        return excerpt
    return excerpt[:77].rstrip() + "..."


def _entry_text(entry: TranscriptEntry) -> str:
    text = compact_text(entry.text)
    if entry.speaker_label:
        return f"{entry.speaker_label}: {text}"
    return text
