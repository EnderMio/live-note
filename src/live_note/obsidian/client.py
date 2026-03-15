from __future__ import annotations

import ssl
import time
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from live_note.config import ObsidianConfig


class ObsidianError(RuntimeError):
    pass


@dataclass(slots=True)
class ObsidianClient:
    config: ObsidianConfig

    def is_enabled(self) -> bool:
        return self.config.enabled

    def ping(self) -> None:
        if not self.is_enabled():
            return
        self._request("GET", "/vault/")

    def put_note(self, path: str, content: str) -> None:
        if not self.is_enabled():
            return
        note_path = path if path.endswith(".md") else f"{path}.md"
        encoded_path = quote(note_path, safe="/")
        self._request(
            "PUT",
            f"/vault/{encoded_path}",
            body=content.encode("utf-8"),
            headers={"Content-Type": "text/markdown; charset=utf-8"},
        )

    def _request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        request_headers = dict(headers or {})
        if self.config.api_key:
            request_headers["Authorization"] = f"Bearer {self.config.api_key}"
        request = Request(
            url=f"{self.config.base_url}{path}",
            data=body,
            headers=request_headers,
            method=method,
        )
        context = None if self.config.verify_ssl else ssl._create_unverified_context()

        attempts = max(1, self.config.retry_attempts)
        for attempt in range(1, attempts + 1):
            try:
                with urlopen(
                    request,
                    timeout=self.config.timeout_seconds,
                    context=context,
                ) as response:
                    return response.read()
            except HTTPError as exc:
                should_retry = exc.code >= 500 and attempt < attempts
                if should_retry:
                    time.sleep(self.config.retry_backoff_seconds * attempt)
                    continue
                detail = exc.read().decode("utf-8", errors="ignore")
                raise ObsidianError(
                    f"Obsidian 请求失败: {exc.code} {exc.reason} {detail}".strip()
                ) from exc
            except URLError as exc:
                if attempt < attempts:
                    time.sleep(self.config.retry_backoff_seconds * attempt)
                    continue
                raise ObsidianError(f"无法连接 Obsidian Local REST API: {exc}") from exc
        raise ObsidianError("Obsidian 请求重试后仍失败")
