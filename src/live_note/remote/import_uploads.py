from __future__ import annotations

import threading
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from live_note.utils import slugify_filename


class RemoteImportUploads:
    def __init__(self, root_dir: Path) -> None:
        self.root_dir = root_dir
        self._request_upload_locks: dict[str, threading.Lock] = {}
        self._request_upload_locks_lock = threading.Lock()

    def create_uploaded_file(
        self,
        *,
        filename: str,
        request_id: str | None,
        file_bytes: bytes,
    ) -> Path:
        normalized_name = slugify_filename(Path(filename).name.strip())
        if not normalized_name:
            raise ValueError("上传文件名不能为空。")
        if not file_bytes:
            raise ValueError("上传文件为空。")
        normalized_request_id = str(request_id).strip() if request_id is not None else ""
        request_lock = (
            self._request_upload_lock(normalized_request_id) if normalized_request_id else None
        )
        if request_lock is not None:
            with request_lock:
                return self._create_uploaded_file_locked(
                    normalized_name=normalized_name,
                    normalized_request_id=normalized_request_id,
                    file_bytes=file_bytes,
                )
        return self._create_uploaded_file_locked(
            normalized_name=normalized_name,
            normalized_request_id=normalized_request_id,
            file_bytes=file_bytes,
        )

    def _create_uploaded_file_locked(
        self,
        *,
        normalized_name: str,
        normalized_request_id: str,
        file_bytes: bytes,
    ) -> Path:
        upload_name = "upload.bin" if normalized_request_id else normalized_name
        uploaded_path = self._uploads_dir(request_id=normalized_request_id or None) / upload_name
        uploaded_path.parent.mkdir(parents=True, exist_ok=True)
        uploaded_path.write_bytes(file_bytes)
        return uploaded_path

    def _uploads_dir(self, *, request_id: str | None = None) -> Path:
        root = self.root_dir / ".live-note" / "remote-imports"
        normalized_request_id = str(request_id).strip() if request_id is not None else ""
        if normalized_request_id:
            return root / uuid5(NAMESPACE_URL, normalized_request_id).hex
        return root / uuid4().hex

    def _request_upload_lock(self, request_id: str) -> threading.Lock:
        with self._request_upload_locks_lock:
            existing = self._request_upload_locks.get(request_id)
            if existing is not None:
                return existing
            created = threading.Lock()
            self._request_upload_locks[request_id] = created
            return created
