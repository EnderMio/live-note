from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from live_note.config import AppConfig, load_config
from live_note.runtime.domain.task_state import TaskRecord, TaskStatus
from live_note.runtime.live_control import (
    LIVE_TASK_PAUSE_REQUESTED,
    LIVE_TASK_RESUME_REQUESTED,
    LIVE_TASK_STARTED,
    LIVE_TASK_STOP_REQUESTED,
    append_live_control_command,
    get_live_control_state,
)
from live_note.runtime.recovery_actions import LOCAL_RECOVERABLE_ACTIONS
from live_note.runtime.remote_projection_sync import sync_remote_task_projections
from live_note.runtime.task_execution import RuntimeQueueExecutor
from live_note.runtime.store import LogRepo
from live_note.runtime.supervisors import RecoveryReport, RuntimeHost
from live_note.runtime.task_runners import TaskRunnerFactory
from live_note.runtime.types import ProgressEvent
from live_note.task_errors import TaskCancelledError
from live_note.utils import iso_now

LOGGER = logging.getLogger(__name__)

_THREAD_JOIN_TIMEOUT_SECONDS = 2.0
_LOCK_STALE_SECONDS = 10.0
_REMOTE_TASK_SYNC_INTERVAL_SECONDS = 1.5

_LIVE_COMMAND_METHODS = {
    LIVE_TASK_STOP_REQUESTED: "request_stop",
    LIVE_TASK_PAUSE_REQUESTED: "request_pause",
    LIVE_TASK_RESUME_REQUESTED: "request_resume",
}


class RuntimeDaemon:
    def __init__(
        self,
        *,
        config_path,
        env_path=None,
        poll_interval_seconds: float = 0.2,
    ) -> None:
        self.config_path = config_path.resolve()
        self.env_path = (env_path or self.config_path.parent / ".env").resolve()
        self.poll_interval_seconds = max(0.05, float(poll_interval_seconds))
        self.runtime = RuntimeHost.for_root(
            self.config_path.parent,
            cancelled_exceptions=(TaskCancelledError,),
            recoverable_actions=set(LOCAL_RECOVERABLE_ACTIONS),
        )
        self.logs = LogRepo(self.runtime.db)
        self._runner_factory = TaskRunnerFactory(
            load_config=self.load_config,
        )
        self._queue_executor = RuntimeQueueExecutor(
            self.runtime,
            dispatch_task=self._dispatch_queue_task,
            poll_interval_seconds=self.poll_interval_seconds,
            thread_name="runtime-daemon-queue",
        )
        self._lock_path = self.runtime.db.path.with_name("runtime-daemon.lock")
        self._lock_owner_pid: int | None = None
        self._stop_event = threading.Event()
        self._live_lock = threading.Lock()
        self._live_task_id: str | None = None
        self._live_runner: object | None = None
        self._live_done_event: threading.Event | None = None
        self._live_thread: threading.Thread | None = None
        self._live_commands_thread: threading.Thread | None = None
        self._next_remote_task_sync_at = 0.0

    def load_config(self) -> AppConfig:
        return load_config(self.config_path, self.env_path)

    def start(self) -> RecoveryReport:
        self._acquire_single_instance_lock()
        report = self.runtime.start()
        self._sync_remote_task_projections_if_due(force=True)
        return report

    def run_forever(self) -> None:
        try:
            self.start()
            while not self._stop_event.is_set():
                try:
                    self.run_once()
                except Exception:
                    LOGGER.exception("runtime daemon loop failed")
                    time.sleep(self.poll_interval_seconds)
        finally:
            self._release_single_instance_lock()

    def stop(self) -> None:
        self._stop_event.set()
        with self._live_lock:
            live_runner = self._live_runner
            live_done_event = self._live_done_event
            live_thread = self._live_thread
            live_commands_thread = self._live_commands_thread
        if live_runner is not None:
            stop = getattr(live_runner, "request_stop", None)
            if callable(stop):
                stop()
        if live_done_event is not None:
            live_done_event.set()
        if live_commands_thread is not None:
            live_commands_thread.join(timeout=_THREAD_JOIN_TIMEOUT_SECONDS)
        if live_thread is not None:
            live_thread.join(timeout=_THREAD_JOIN_TIMEOUT_SECONDS)
        self._queue_executor.shutdown()
        self._release_single_instance_lock()

    def run_once(self) -> None:
        self._start_live_if_needed()
        self._queue_executor.run_once()
        self._sync_remote_task_projections_if_due()
        time.sleep(self.poll_interval_seconds)

    def _start_live_if_needed(self) -> None:
        with self._live_lock:
            live_thread = self._live_thread
            if live_thread is not None and live_thread.is_alive():
                return
        live_record = self._active_live_record()
        if live_record is None:
            return
        if live_record.status == TaskStatus.QUEUED.value:
            live_record = self.runtime.task_supervisor.start_task(live_record.task_id)
        if live_record.status != TaskStatus.RUNNING.value:
            return
        self._start_live_worker(live_record)

    def _active_live_record(self) -> TaskRecord | None:
        tasks = [
            item
            for item in self.runtime.tasks.list_by_status(
                TaskStatus.QUEUED.value,
                TaskStatus.RUNNING.value,
            )
            if item.action == "live"
        ]
        if not tasks:
            return None
        return min(
            tasks,
            key=lambda item: (
                0 if item.status == TaskStatus.RUNNING.value else 1,
                item.created_at,
                item.task_id,
            ),
        )

    def _start_live_worker(self, record: TaskRecord) -> None:
        done_event = threading.Event()
        append_live_control_command(
            self.logs,
            task_id=record.task_id,
            kind=LIVE_TASK_STARTED,
            created_at=iso_now(),
        )

        def command_loop() -> None:
            last_sequence = 0
            while not done_event.is_set():
                commands = self.logs.list_commands(task_id=record.task_id)
                for command in commands:
                    sequence = int(command.sequence or 0)
                    if sequence <= last_sequence:
                        continue
                    last_sequence = sequence
                    self._apply_live_command(record.task_id, command.kind)
                done_event.wait(0.05)

        def live_worker() -> None:
            command_thread = threading.Thread(
                target=command_loop,
                daemon=True,
                name=f"runtime-live-commands-{record.task_id}",
            )
            with self._live_lock:
                self._live_task_id = record.task_id
                self._live_done_event = done_event
                self._live_commands_thread = command_thread
            command_thread.start()
            try:
                self.runtime.task_supervisor.run_started_task(
                    record.task_id,
                    runner=lambda current, **_kwargs: self._runner_factory.run_task_record(
                        current,
                        on_progress=self._progress_callback(current.task_id),
                        on_live_runner=self._bind_live_runner,
                    ),
                )
            finally:
                done_event.set()
                command_thread.join(timeout=0.5)
                with self._live_lock:
                    self._live_task_id = None
                    self._live_runner = None
                    self._live_done_event = None
                    self._live_thread = None
                    self._live_commands_thread = None

        thread = threading.Thread(
            target=live_worker,
            daemon=True,
            name=f"runtime-live-{record.task_id}",
        )
        with self._live_lock:
            self._live_thread = thread
        thread.start()

    def _dispatch_queue_task(
        self,
        record: TaskRecord,
        cancel_event: threading.Event | None,
    ) -> int:
        return self._runner_factory.run_task_record(
            record,
            on_progress=self._progress_callback(record.task_id),
            cancel_event=cancel_event,
        )

    def _sync_remote_task_projections_if_due(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now < self._next_remote_task_sync_at:
            return
        self._next_remote_task_sync_at = now + _REMOTE_TASK_SYNC_INTERVAL_SECONDS
        try:
            config = self.load_config()
        except Exception:
            return
        if not config.remote.enabled:
            return
        try:
            sync_remote_task_projections(config)
        except Exception:
            LOGGER.exception("runtime daemon remote projection sync failed")

    def _progress_callback(self, task_id: str) -> Callable[[ProgressEvent], None]:
        def callback(event: ProgressEvent) -> None:
            enriched = replace(
                event,
                task_id=event.task_id or task_id,
                source="live" if self._is_live_task(task_id) else "queue",
            )
            if enriched.stage == "input_level":
                return
            try:
                self.runtime.task_supervisor.record_progress(
                    task_id,
                    enriched,
                    result_changed=_progress_changes_result(enriched.stage),
                )
            except FileNotFoundError:
                return

        return callback

    def _is_live_task(self, task_id: str) -> bool:
        task = self.runtime.tasks.get(task_id)
        return task is not None and task.action == "live"

    def _bind_live_runner(self, runner: object | None) -> None:
        with self._live_lock:
            self._live_runner = runner

    def _acquire_single_instance_lock(self) -> None:
        pid = os.getpid()
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                fd = os.open(
                    self._lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o644,
                )
            except FileExistsError:
                owner_pid = _read_lock_pid(self._lock_path)
                if owner_pid is not None and _pid_is_alive(owner_pid):
                    raise RuntimeError(
                        f"runtime daemon already running with pid {owner_pid}"
                    )
                _unlink_stale_lock(self._lock_path)
                time.sleep(0.05)
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(f"{pid}\n")
            self._lock_owner_pid = pid
            return

    def _release_single_instance_lock(self) -> None:
        if self._lock_owner_pid is None:
            return
        owner_pid = _read_lock_pid(self._lock_path)
        if owner_pid == self._lock_owner_pid:
            _unlink_stale_lock(self._lock_path)
        self._lock_owner_pid = None

    def _apply_live_command(self, task_id: str, kind: str) -> None:
        method_name = _LIVE_COMMAND_METHODS.get(kind)
        if method_name is None:
            return
        with self._live_lock:
            if task_id != self._live_task_id:
                return
            runner = self._live_runner
        if runner is None:
            return
        if kind == LIVE_TASK_PAUSE_REQUESTED and get_live_control_state(self.logs, task_id).stop_requested:
            return
        method = getattr(runner, method_name, None)
        if callable(method):
            method()

def _progress_changes_result(stage: str) -> bool:
    return stage in {
        "segment_transcribed",
        "capture_finished",
        "publishing",
        "summarizing",
        "done",
    }


def _read_lock_pid(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw.isdigit():
        return None
    return int(raw)


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _unlink_stale_lock(path: Path) -> None:
    try:
        stat = path.stat()
    except OSError:
        return
    if time.time() - stat.st_mtime < _LOCK_STALE_SECONDS:
        try:
            owner_pid = _read_lock_pid(path)
        except OSError:
            owner_pid = None
        if owner_pid is not None and _pid_is_alive(owner_pid):
            return
    try:
        path.unlink()
    except OSError:
        return
