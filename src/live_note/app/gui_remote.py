from __future__ import annotations


def remote_task_status_text(record: object) -> str:
    attachment_state = str(getattr(record, "attachment_state", "") or "").strip()
    if attachment_state == "lost":
        return "已丢失"
    status = str(getattr(record, "status", "") or "").strip()
    if status == "completed":
        result_version = int(getattr(record, "result_version", 0) or 0)
        last_synced = int(getattr(record, "last_synced_result_version", 0) or 0)
        if result_version > last_synced:
            return "同步失败" if getattr(record, "last_error", None) else "待同步"
        return "已完成"
    return {
        "queued": "排队中",
        "running": "运行中",
        "cancelling": "取消中",
        "failed": "失败",
        "cancelled": "已取消",
    }.get(status, status or "未知")


def primary_remote_task(records: list[object]) -> object | None:
    for record in records:
        if (
            str(getattr(record, "status", "") or "").strip() in {"running", "cancelling"}
            and str(getattr(record, "attachment_state", "") or "").strip() != "lost"
        ):
            return record
    for record in records:
        if (
            str(getattr(record, "status", "") or "").strip() in {"queued", "running", "cancelling"}
            and str(getattr(record, "attachment_state", "") or "").strip() != "lost"
        ):
            return record
    for record in records:
        if str(getattr(record, "attachment_state", "") or "").strip() != "lost":
            return record
    return records[0] if records else None


def remote_task_requires_sync(record: object | None) -> bool:
    if record is None:
        return False
    if str(getattr(record, "status", "") or "").strip() != "completed":
        return False
    if str(getattr(record, "attachment_state", "") or "").strip() == "lost":
        return False
    if not getattr(record, "remote_task_id", None) or not getattr(record, "session_id", None):
        return False
    result_version = int(getattr(record, "result_version", 0) or 0)
    last_synced = int(getattr(record, "last_synced_result_version", 0) or 0)
    return result_version > last_synced or bool(getattr(record, "last_error", None))
