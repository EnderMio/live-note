from __future__ import annotations

from urllib.parse import urlparse


def build_execution_target_hint(
    remote_enabled: bool,
    remote_base_url: str,
    remote_health_status: str | None,
) -> str:
    if not remote_enabled:
        return "当前转写：本机"
    host = display_remote_host(remote_base_url)
    if remote_health_status == "OK":
        return f"当前转写：远端服务（{host}，已连接）"
    if remote_health_status == "FAIL":
        return f"当前转写：远端服务（{host}，未连通）"
    return f"当前转写：远端服务（{host}，待检测）"


def display_remote_host(base_url: str) -> str:
    candidate = base_url.strip()
    if not candidate:
        return "未配置"
    parsed = urlparse(candidate)
    if parsed.hostname:
        return parsed.hostname
    return candidate.rstrip("/")
