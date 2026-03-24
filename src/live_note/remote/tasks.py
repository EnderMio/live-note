from __future__ import annotations

import json
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from live_note.app.events import ProgressEvent
from live_note.app.task_errors import TaskCancelledError
from live_note.config import AppConfig
from live_note.utils import iso_now

ACTIVE_STATUSES = {"queued", "running", "cancelling"}
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
SESSION_MUTATION_ACTIONS = {"postprocess", "refine", "retranscribe"}
REGISTRY_STATE_VERSION = 1
RECOVERY_INTERRUPTED_MESSAGE = "远端服务重启导致任务中断，请重试。"
RECOVERY_RUNNER_MESSAGE = "无法从持久状态恢复 runner，任务已失败。"
RECOVERY_PENDING_MESSAGE = "持久化队列数据缺失，任务已失败。"


@dataclass(slots=True)
class RemoteTask:
    task_id: str
    server_id: str
    action: str
    label: str
    status: str
    stage: str
    message: str
    created_at: str
    updated_at: str
    session_id: str | None = None
    request_id: str | None = None
    current: int | None = None
    total: int | None = None
    result_version: int = 0
    error: str | None = None
    can_cancel: bool = False
    task_spec: dict[str, Any] | None = None


RunnerFactory = Callable[[str, threading.Event | None], Callable[[], object | None]]
RecoverRunnerFactory = Callable[
    [str, dict[str, Any] | None, threading.Event | None],
    Callable[[], object | None],
]


class RemoteTaskRegistry:
    def __init__(
        self,
        config: AppConfig,
        *,
        recent_limit: int = 50,
        recover_runner: RecoverRunnerFactory | None = None,
    ) -> None:
        self.config = config
        self.server_id = f"server-{uuid4().hex[:12]}"
        self._tasks: dict[str, RemoteTask] = {}
        self._request_ids: dict[str, str] = {}
        self._session_mutations: dict[str, str] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._pending_task_ids: deque[str] = deque()
        self._runners: dict[str, Callable[[], object | None]] = {}
        self._recent_terminal_ids: deque[str] = deque(maxlen=max(1, recent_limit))
        self._state_path = self.config.root_dir / ".live-note" / "remote_task_registry.json"
        self._state_load_error: str | None = None
        self._recover_runner = recover_runner
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._active_runner_count = 0
        self._stopping = False
        with self._lock:
            self._restore_state_locked()
            self._save_state_locked(force_create=True)
        self._dispatcher = threading.Thread(
            target=self._dispatch_loop,
            daemon=True,
            name="remote-task-dispatcher",
        )
        self._dispatcher.start()

    def create_task(
        self,
        *,
        action: str,
        label: str,
        build_runner: RunnerFactory,
        session_id: str | None = None,
        request_id: str | None = None,
        can_cancel: bool = False,
        task_spec: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_request_id = _normalize_optional_string(request_id)
        normalized_session_id = _normalize_optional_string(session_id)
        with self._lock:
            if normalized_request_id:
                existing_id = self._request_ids.get(normalized_request_id)
                if existing_id is not None and existing_id in self._tasks:
                    return _task_to_public_dict(self._tasks[existing_id])
            if action in SESSION_MUTATION_ACTIONS and normalized_session_id:
                existing_id = self._session_mutations.get(normalized_session_id)
                existing = self._tasks.get(existing_id or "")
                if existing is not None and existing.status in ACTIVE_STATUSES:
                    return _task_to_public_dict(existing)

            task_id = f"task-{uuid4().hex[:12]}"
            cancel_event = threading.Event() if can_cancel else None
            now = iso_now()
            task = RemoteTask(
                task_id=task_id,
                server_id=self.server_id,
                action=action,
                label=label,
                status="queued",
                stage="queued",
                message="已加入远端队列。",
                created_at=now,
                updated_at=now,
                session_id=normalized_session_id,
                request_id=normalized_request_id,
                can_cancel=can_cancel,
                task_spec=_normalize_task_spec(task_spec),
            )
            self._tasks[task_id] = task
            if normalized_request_id:
                self._request_ids[normalized_request_id] = task_id
            if action in SESSION_MUTATION_ACTIONS and normalized_session_id:
                self._session_mutations[normalized_session_id] = task_id
            if cancel_event is not None:
                self._cancel_events[task_id] = cancel_event
            self._runners[task_id] = build_runner(task_id, cancel_event)
            self._pending_task_ids.append(task_id)
            self._save_state_locked()
            self._condition.notify()
        return self.task_payload(task_id)

    def existing_task_for_request_id(self, request_id: str | None) -> dict[str, Any] | None:
        normalized_request_id = _normalize_optional_string(request_id)
        if normalized_request_id is None:
            return None
        with self._lock:
            existing_id = self._request_ids.get(normalized_request_id)
            if existing_id is None:
                return None
            task = self._tasks.get(existing_id)
            if task is None:
                return None
            return _task_to_public_dict(task)

    def list_tasks(self) -> dict[str, object]:
        with self._lock:
            active = [
                _task_to_public_dict(task)
                for task in sorted(
                    (item for item in self._tasks.values() if item.status in ACTIVE_STATUSES),
                    key=lambda item: item.updated_at,
                    reverse=True,
                )
            ]
            recent: list[dict[str, Any]] = []
            for task_id in self._recent_terminal_ids:
                task = self._tasks.get(task_id)
                if task is None or task.status not in TERMINAL_STATUSES:
                    continue
                recent.append(_task_to_public_dict(task))
            return {
                "server_id": self.server_id,
                "active": active,
                "recent": recent,
            }

    def task_payload(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise FileNotFoundError(f"未找到远端任务: {task_id}")
            return _task_to_public_dict(task)

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise FileNotFoundError(f"未找到远端任务: {task_id}")
            if task.status in TERMINAL_STATUSES:
                return _task_to_public_dict(task)
            if not task.can_cancel:
                return _task_to_public_dict(task)
            if task.status == "queued":
                self._remove_pending_task_locked(task_id)
                updated = replace(
                    task,
                    status="cancelled",
                    stage="cancelled",
                    message="远端任务已取消。",
                    updated_at=iso_now(),
                )
                self._tasks[task_id] = updated
                self._cleanup_task_artifacts_locked(updated)
                self._record_terminal_locked(task_id)
                self._cancel_events.pop(task_id, None)
                self._runners.pop(task_id, None)
                if task.session_id and self._session_mutations.get(task.session_id) == task_id:
                    self._session_mutations.pop(task.session_id, None)
                self._save_state_locked()
                return _task_to_public_dict(updated)
            cancel_event = self._cancel_events.get(task_id)
            if cancel_event is not None:
                cancel_event.set()
            updated = replace(
                task,
                status="cancelling",
                stage="cancelling",
                message="已请求取消远端任务。",
                updated_at=iso_now(),
            )
            self._tasks[task_id] = updated
            self._save_state_locked()
            return _task_to_public_dict(updated)

    def _dispatch_loop(self) -> None:
        while True:
            with self._condition:
                task_id = self._wait_for_next_task_locked()
                if task_id is None:
                    return
                runner = self._runners.get(task_id)
                task = self._tasks.get(task_id)
                if runner is None or task is None:
                    continue
                if task.status in TERMINAL_STATUSES:
                    self._runners.pop(task_id, None)
                    continue
                self._tasks[task_id] = replace(
                    task,
                    status="running",
                    stage="starting",
                    message="远端任务已开始。",
                    updated_at=iso_now(),
                )
                self._save_state_locked()
                self._active_runner_count += 1
            try:
                self._run_task(task_id, runner)
            finally:
                with self._condition:
                    self._active_runner_count = max(0, self._active_runner_count - 1)
                    self._runners.pop(task_id, None)
                    self._condition.notify_all()

    def _wait_for_next_task_locked(self) -> str | None:
        while True:
            while (
                not self._pending_task_ids or self._active_runner_count >= 1
            ) and not self._stopping:
                self._condition.wait()
            if self._stopping:
                return None
            task_id = self._pending_task_ids.popleft()
            task = self._tasks.get(task_id)
            if task is None or task.status in TERMINAL_STATUSES:
                self._runners.pop(task_id, None)
                continue
            return task_id

    def shutdown(self) -> None:
        with self._condition:
            self._stopping = True
            for cancel_event in self._cancel_events.values():
                cancel_event.set()
            self._condition.notify_all()
        self._dispatcher.join(timeout=1.0)

    def _remove_pending_task_locked(self, task_id: str) -> None:
        try:
            self._pending_task_ids.remove(task_id)
        except ValueError:
            pass

    def record_progress(
        self,
        task_id: str,
        event: ProgressEvent,
        *,
        result_changed: bool = False,
    ) -> None:
        status = "running"
        if event.stage == "done":
            status = "completed"
        elif event.stage == "error":
            status = "failed"
        self._update_task(
            task_id,
            status=status,
            stage=event.stage,
            message=event.message,
            session_id=event.session_id,
            current=event.current,
            total=event.total,
            error=event.error,
            result_changed=result_changed,
        )

    def bump_result_version(self, task_id: str) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            self._tasks[task_id] = replace(
                task,
                result_version=task.result_version + 1,
                updated_at=iso_now(),
            )
            self._save_state_locked()

    def mark_running(
        self,
        task_id: str,
        *,
        stage: str = "starting",
        message: str = "远端任务已开始。",
    ) -> None:
        self._update_task(
            task_id,
            status="running",
            stage=stage,
            message=message,
        )

    def mark_completed(
        self,
        task_id: str,
        *,
        stage: str = "done",
        message: str = "远端任务已完成。",
        result_changed: bool = False,
    ) -> None:
        self._update_task(
            task_id,
            status="completed",
            stage=stage,
            message=message,
            result_changed=result_changed,
        )

    def mark_failed(
        self,
        task_id: str,
        error: Exception | str,
        *,
        stage: str = "error",
        message: str | None = None,
    ) -> None:
        text = str(error)
        self._update_task(
            task_id,
            status="failed",
            stage=stage,
            message=message or f"远端任务失败：{text}",
            error=text,
        )

    def mark_cancelled(
        self,
        task_id: str,
        *,
        message: str = "远端任务已取消。",
    ) -> None:
        self._update_task(
            task_id,
            status="cancelled",
            stage="cancelled",
            message=message,
            error=None,
        )

    def _run_task(self, task_id: str, runner: Callable[[], object | None]) -> None:
        try:
            runner()
            snapshot = self.task_payload(task_id)
            if str(snapshot.get("status")) not in TERMINAL_STATUSES:
                self.mark_completed(task_id)
        except TaskCancelledError:
            self.mark_cancelled(task_id)
        except Exception as exc:
            self.mark_failed(task_id, exc)

    def _update_task(
        self,
        task_id: str,
        *,
        result_changed: bool = False,
        **changes: Any,
    ) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            next_result_version = task.result_version + (1 if result_changed else 0)
            updated = replace(
                task,
                updated_at=iso_now(),
                result_version=next_result_version,
                **changes,
            )
            self._tasks[task_id] = updated
            if updated.status in TERMINAL_STATUSES:
                self._record_terminal_locked(task_id)
                self._cancel_events.pop(task_id, None)
                self._runners.pop(task_id, None)
                if (
                    updated.session_id
                    and self._session_mutations.get(updated.session_id) == task_id
                ):
                    self._session_mutations.pop(updated.session_id, None)
            self._save_state_locked()

    def _record_terminal_locked(self, task_id: str) -> None:
        try:
            self._recent_terminal_ids.remove(task_id)
        except ValueError:
            pass
        self._recent_terminal_ids.appendleft(task_id)

    def _restore_state_locked(self) -> None:
        state = self._load_state_file()
        if not state:
            if self._state_load_error is not None:
                self._create_recovery_error_task_locked(
                    f"state-load-{uuid4().hex[:8]}",
                    self._state_load_error,
                )
            return
        persisted_server_id = _normalize_optional_string(str(state.get("server_id") or ""))
        if persisted_server_id:
            self.server_id = persisted_server_id

        self._tasks = self._restore_tasks_from_state(state)
        self._pending_task_ids = self._restore_pending_ids_from_state(state)
        self._recent_terminal_ids = self._restore_recent_ids_from_state(state)
        self._request_ids = {}
        self._session_mutations = {}
        self._cancel_events = {}
        self._runners = {}

        for task_id, task in self._tasks.items():
            if task.status in TERMINAL_STATUSES:
                self._cleanup_task_artifacts_locked(task)
            if task.request_id:
                self._request_ids[task.request_id] = task_id
            if (
                task.action in SESSION_MUTATION_ACTIONS
                and task.session_id
                and task.status in ACTIVE_STATUSES
            ):
                self._session_mutations[task.session_id] = task_id
            if task.can_cancel and task.status in ACTIVE_STATUSES:
                self._cancel_events[task_id] = threading.Event()

        for task_id in list(self._pending_task_ids):
            task = self._tasks.get(task_id)
            if task is None or task.status != "queued":
                self._remove_pending_task_locked(task_id)
                continue
            cancel_event = self._cancel_events.get(task_id)
            if self._recover_runner is None:
                self._mark_recovered_task_failed_locked(
                    task_id,
                    RECOVERY_RUNNER_MESSAGE,
                    RECOVERY_RUNNER_MESSAGE,
                )
                continue
            try:
                runner = self._recover_runner(task_id, task.task_spec, cancel_event)
            except Exception as exc:
                self._mark_recovered_task_failed_locked(
                    task_id,
                    f"{RECOVERY_RUNNER_MESSAGE} {exc}",
                    str(exc),
                )
                continue
            self._runners[task_id] = runner

    def _restore_tasks_from_state(self, state: dict[str, Any]) -> dict[str, RemoteTask]:
        restored: dict[str, RemoteTask] = {}
        raw_tasks = state.get("tasks")
        if not isinstance(raw_tasks, dict):
            return restored
        now = iso_now()
        for key, raw in raw_tasks.items():
            if not isinstance(raw, dict):
                error_task_id = f"persisted-task-{uuid4().hex[:8]}"
                restored[error_task_id] = self._build_recovery_error_task(
                    error_task_id,
                    "持久化任务记录无效，已标记失败。",
                )
                self._record_terminal_locked(error_task_id)
                continue
            candidate_id = _normalize_optional_string(str(raw.get("task_id") or key or ""))
            task_id = candidate_id or f"task-recovered-{uuid4().hex[:12]}"
            status = str(raw.get("status") or "failed")
            stage = str(raw.get("stage") or status)
            message = str(raw.get("message") or "")
            error = raw.get("error")
            error_text = None if error is None else str(error)
            result_version_raw = raw.get("result_version", 0)
            try:
                result_version = max(0, int(result_version_raw))
            except (TypeError, ValueError):
                result_version = 0
            task = RemoteTask(
                task_id=task_id,
                server_id=self.server_id,
                action=str(raw.get("action") or "unknown"),
                label=str(raw.get("label") or "恢复任务"),
                status=status,
                stage=stage,
                message=message,
                created_at=str(raw.get("created_at") or now),
                updated_at=str(raw.get("updated_at") or now),
                session_id=_normalize_optional_string(_coerce_optional_text(raw.get("session_id"))),
                request_id=_normalize_optional_string(_coerce_optional_text(raw.get("request_id"))),
                current=_coerce_optional_int(raw.get("current")),
                total=_coerce_optional_int(raw.get("total")),
                result_version=result_version,
                error=error_text,
                can_cancel=bool(raw.get("can_cancel", False)),
                task_spec=_normalize_task_spec(raw.get("task_spec")),
            )
            restored[task_id] = task

        for task_id, task in list(restored.items()):
            if task.status in {"running", "cancelling"}:
                restored[task_id] = replace(
                    task,
                    status="failed",
                    stage="failed",
                    message=RECOVERY_INTERRUPTED_MESSAGE,
                    error=RECOVERY_INTERRUPTED_MESSAGE,
                    updated_at=iso_now(),
                )
                self._record_terminal_locked(task_id)
            elif task.status in TERMINAL_STATUSES:
                continue
            elif task.status != "queued":
                restored[task_id] = replace(
                    task,
                    status="failed",
                    stage="failed",
                    message="持久化任务状态无效，已标记失败。",
                    error="invalid persisted status",
                    updated_at=iso_now(),
                )
                self._record_terminal_locked(task_id)

        return restored

    def _restore_pending_ids_from_state(self, state: dict[str, Any]) -> deque[str]:
        pending = deque[str]()
        raw_pending = state.get("pending_task_ids")
        if not isinstance(raw_pending, list):
            raw_pending = []
        referenced: set[str] = set()
        for raw_id in raw_pending:
            task_id = _normalize_optional_string(_coerce_optional_text(raw_id))
            if not task_id or task_id in referenced:
                continue
            referenced.add(task_id)
            task = self._tasks.get(task_id)
            if task is None:
                self._create_recovery_error_task_locked(
                    f"task-missing-{uuid4().hex[:8]}",
                    f"持久化队列引用缺失任务：{task_id}",
                )
                continue
            if task.status != "queued":
                continue
            pending.append(task_id)

        queued_not_listed = [
            task_id
            for task_id, task in self._tasks.items()
            if task.status == "queued" and task_id not in referenced
        ]
        for task_id in queued_not_listed:
            self._mark_recovered_task_failed_locked(
                task_id, RECOVERY_PENDING_MESSAGE, RECOVERY_PENDING_MESSAGE
            )
        return pending

    def _restore_recent_ids_from_state(self, state: dict[str, Any]) -> deque[str]:
        recent = deque[str](self._recent_terminal_ids, maxlen=self._recent_terminal_ids.maxlen)
        raw_recent = state.get("recent_terminal_ids")
        if not isinstance(raw_recent, list):
            raw_recent = []
        for raw_id in raw_recent:
            task_id = _normalize_optional_string(_coerce_optional_text(raw_id))
            if not task_id:
                continue
            task = self._tasks.get(task_id)
            if task is None or task.status not in TERMINAL_STATUSES:
                continue
            if task_id in recent:
                continue
            if recent.maxlen is not None and len(recent) >= recent.maxlen:
                break
            recent.append(task_id)
        return recent

    def _mark_recovered_task_failed_locked(self, task_id: str, message: str, error: str) -> None:
        task = self._tasks.get(task_id)
        if task is None:
            return
        self._remove_pending_task_locked(task_id)
        self._runners.pop(task_id, None)
        self._cancel_events.pop(task_id, None)
        updated = replace(
            task,
            status="failed",
            stage="failed",
            message=message,
            error=error,
            updated_at=iso_now(),
        )
        self._tasks[task_id] = updated
        self._cleanup_task_artifacts_locked(updated)
        self._record_terminal_locked(task_id)
        if updated.session_id and self._session_mutations.get(updated.session_id) == task_id:
            self._session_mutations.pop(updated.session_id, None)

    def _create_recovery_error_task_locked(self, task_id: str, message: str) -> None:
        recovered = self._build_recovery_error_task(task_id, message)
        self._tasks[task_id] = recovered
        self._cleanup_task_artifacts_locked(recovered)
        self._record_terminal_locked(task_id)

    def _build_recovery_error_task(self, task_id: str, message: str) -> RemoteTask:
        now = iso_now()
        return RemoteTask(
            task_id=task_id,
            server_id=self.server_id,
            action="recovery",
            label="恢复失败",
            status="failed",
            stage="failed",
            message=message,
            created_at=now,
            updated_at=now,
            error=message,
        )

    def _load_state_file(self) -> dict[str, Any] | None:
        self._state_load_error = None
        if not self._state_path.exists():
            return None
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self._state_load_error = f"持久化任务状态损坏，已标记恢复失败：{exc}"
            return None
        if not isinstance(payload, dict):
            self._state_load_error = "持久化任务状态格式无效，已标记恢复失败。"
            return None
        return payload

    def _save_state_locked(self, *, force_create: bool = False) -> None:
        if not force_create and not self._state_path.exists():
            return
        state = {
            "version": REGISTRY_STATE_VERSION,
            "server_id": self.server_id,
            "tasks": {task_id: asdict(task) for task_id, task in self._tasks.items()},
            "pending_task_ids": list(self._pending_task_ids),
            "recent_terminal_ids": list(self._recent_terminal_ids),
        }
        parent = self._state_path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
            temp_path = parent / f".{self._state_path.name}.{uuid4().hex}.tmp"
            temp_path.write_text(
                json.dumps(state, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            temp_path.replace(self._state_path)
        except OSError:
            try:
                if "temp_path" in locals() and temp_path.exists():
                    temp_path.unlink()
            except OSError:
                pass

    def _cleanup_task_artifacts_locked(self, task: RemoteTask) -> None:
        if task.action != "import" or not isinstance(task.task_spec, dict):
            return
        uploaded_path = self._validated_uploaded_path(task.task_spec.get("uploaded_path"))
        if uploaded_path is None:
            return
        try:
            uploaded_path.unlink(missing_ok=True)
        except OSError:
            return
        uploads_root = (self.config.root_dir / ".live-note" / "remote-imports").resolve(
            strict=False
        )
        parent = uploaded_path.parent
        while parent != uploads_root and parent.is_relative_to(uploads_root):
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

    def _validated_uploaded_path(self, uploaded_path: object) -> Path | None:
        uploaded_path_text = _normalize_optional_string(_coerce_optional_text(uploaded_path))
        if uploaded_path_text is None:
            return None
        try:
            candidate = Path(uploaded_path_text).expanduser().resolve(strict=False)
        except OSError:
            return None
        uploads_root = (self.config.root_dir / ".live-note" / "remote-imports").resolve(
            strict=False
        )
        try:
            candidate.relative_to(uploads_root)
        except ValueError:
            return None
        if candidate == uploads_root:
            return None
        return candidate


def _normalize_optional_string(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_task_spec(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    normalized: dict[str, Any] = {}
    for key, item in value.items():
        normalized[str(key)] = item
    return normalized


def _coerce_optional_text(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _coerce_optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _task_to_public_dict(task: RemoteTask) -> dict[str, Any]:
    payload = asdict(task)
    payload.pop("task_spec", None)
    return payload
