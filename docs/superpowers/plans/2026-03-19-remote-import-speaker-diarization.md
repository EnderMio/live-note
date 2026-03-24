# Remote Import Speaker Diarization Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让远端导入任务在开启 speaker runtime 时自动执行说话人区分，并把 `speaker_label`/`speaker_status` 回写到本地镜像与 Obsidian，同时保证本地导入行为不变。

**Architecture:** 在共享 `FileImportCoordinator` 上增加受调用方控制的扩展点，用于“保留 `session.live.wav`”和“发布前后处理”；只有远端导入包装层会传入这些扩展点。本地导入继续走原链路，远端导入则在转写完成后进入 `speaker -> publishing` 阶段，并在失败时回退为 `speaker_status = failed` 而不是整任务失败。

**Tech Stack:** Python 3.12, unittest, existing `FileImportCoordinator`, `RemoteImportTaskManager`, `apply_speaker_labels`, Obsidian Local REST API

---

## File Structure

- Modify: `src/live_note/app/coordinator.py`
  - 为共享导入流程增加最小扩展点：保留 `session.live.wav`、发布前后处理 hook、导入阶段额外进度
- Modify: `src/live_note/remote/import_jobs.py`
  - 远端导入入口；只在这里挂接 speaker 后处理，保持本地导入不受影响
- Modify: `README.md`
  - 说明远端导入在 speaker runtime 打开时支持说话人区分
- Modify: `tests/test_coordinator.py`
  - 锁定共享导入 coordinator 的扩展点行为
- Create: `tests/test_remote_import_jobs.py`
  - 锁定远端导入任务管理器的 speaker 阶段、失败回退和阶段顺序
- Modify: `tests/test_remote_import.py`
  - 保持客户端远端导入协调器测试聚焦于桌面端上传/轮询逻辑

## Chunk 1: Shared Import Extension Points

### Task 1: Lock the shared import coordinator behavior with failing tests

**Files:**
- Modify: `tests/test_coordinator.py`

- [ ] **Step 1: Write a failing test for preserving `session.live.wav` when requested**

```python
def test_import_coordinator_preserves_session_audio_when_requested(self) -> None:
    coordinator = FileImportCoordinator(
        config=replace(config, importer=replace(config.importer, keep_normalized_audio=False)),
        file_path=str(media_path),
        title="股票课",
        kind="lecture",
        preserve_session_audio=True,
    )
    coordinator.run()
    self.assertTrue(Path(session_dir, "session.live.wav").exists())
```

- [ ] **Step 2: Run the targeted test and verify it fails**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_coordinator.CoordinatorFailureTests.test_import_coordinator_preserves_session_audio_when_requested -v`

Expected: `FAIL` because `FileImportCoordinator` does not yet accept `preserve_session_audio` and does not create `session.live.wav`.

- [ ] **Step 3: Write a failing test for the pre-publish hook**

```python
def test_import_coordinator_runs_before_publish_hook(self) -> None:
    call_order = []

    def hook(*, workspace, metadata, logger, on_progress):
        entries = workspace.transcript_entries()
        call_order.append(
            ("hook", workspace.session_live_wav.exists(), metadata.status, len(entries))
        )
        return metadata

    with patch("live_note.app.coordinator.publish_final_outputs") as publish_mock:
        publish_mock.side_effect = (
            lambda *args, **kwargs: call_order.append(("publish", True, None))
        )
        coordinator = FileImportCoordinator(
            config=config,
            file_path=str(media_path),
            title="股票课",
            kind="lecture",
            preserve_session_audio=True,
            before_publish=hook,
        )
        coordinator.run()

    self.assertEqual("hook", call_order[0][0])
    self.assertEqual("publish", call_order[1][0])
    self.assertTrue(call_order[0][1])
    self.assertGreater(call_order[0][3], 0)
```

- [ ] **Step 4: Run the targeted test and verify it fails**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_coordinator.CoordinatorFailureTests.test_import_coordinator_runs_before_publish_hook -v`

Expected: `FAIL` because the hook API does not exist yet.

- [ ] **Step 5: Write a regression test for default local behavior**

```python
def test_import_coordinator_does_not_create_session_audio_without_flag(self) -> None:
    coordinator = FileImportCoordinator(
        config=config,
        file_path=str(media_path),
        title="股票课",
        kind="lecture",
    )
    coordinator.run()
    self.assertFalse(Path(session_dir, "session.live.wav").exists())
```

- [ ] **Step 6: Run the regression test and verify it already passes**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_coordinator.CoordinatorFailureTests.test_import_coordinator_does_not_create_session_audio_without_flag -v`

Expected: `PASS` against current code; this locks the non-goal that local import stays unchanged.

- [ ] **Step 7: Implement the minimal shared extension points in `src/live_note/app/coordinator.py`**

```python
BeforePublishHook = Callable[..., SessionMetadata]

class FileImportCoordinator:
    def __init__(..., preserve_session_audio: bool = False, before_publish: BeforePublishHook | None = None):
        self.preserve_session_audio = preserve_session_audio
        self.before_publish = before_publish

    def run(self) -> int:
        ...
        convert_audio_to_wav(...)
        if self.preserve_session_audio:
            shutil.copy2(normalized_path, workspace.session_live_wav)
        ...
        if self.before_publish is not None:
            metadata = self.before_publish(
                workspace=workspace,
                metadata=workspace.read_session(),
                logger=logger,
                on_progress=self.on_progress,
            )
        publish_final_outputs(...)
```

- [ ] **Step 8: Keep the hook boundary remote-safe**

Implementation notes:
- The default values must keep local import behavior unchanged.
- The shared coordinator must not import remote-only modules directly.
- The hook must run after transcript entries are written, before `publish_final_outputs()`.
- The `session.live.wav` copy must not depend on `importer.keep_normalized_audio`.
- Add small helper functions inside `coordinator.py` for “copy session audio” and “run before_publish hook” so the import path does not tangle further.

- [ ] **Step 9: Run the targeted coordinator tests and verify they pass**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_coordinator -v`

Expected: the new tests pass; existing import-coordinator tests remain green.

- [ ] **Step 10: Commit the shared coordinator change**

```bash
git add tests/test_coordinator.py src/live_note/app/coordinator.py
git commit -m "feat: add remote import postprocess hooks"
```

## Chunk 2: Remote Import Speaker Postprocess

### Task 2: Lock remote-only speaker behavior with failing tests

**Files:**
- Create: `tests/test_remote_import_jobs.py`
- Modify: `src/live_note/remote/import_jobs.py`
- Modify: `tests/test_remote_import.py`

- [ ] **Step 1: Write a failing manager-level test for ordered stage progression**

```python
def test_remote_import_manager_reports_speaker_stage_between_transcribing_and_done(self) -> None:
    observed = []

    class _FakeCoordinator:
        def __init__(self, *, on_progress, **kwargs):
            self.on_progress = on_progress

        def run(self) -> int:
            self.on_progress(ProgressEvent(stage="transcribing", message="正在转写片段 1/1", session_id="remote-import-1"))
            self.on_progress(ProgressEvent(stage="speaker", message="正在进行说话人区分。", session_id="remote-import-1"))
            self.on_progress(ProgressEvent(stage="publishing", message="正在生成最终原文。", session_id="remote-import-1"))
            self.on_progress(ProgressEvent(stage="done", message="导入会话已完成。", session_id="remote-import-1"))
            return 0

    with patch("live_note.remote.import_jobs.FileImportCoordinator", _FakeCoordinator):
        manager = RemoteImportTaskManager(config_with_speaker)
        payload = manager.create_task(
            filename="meeting.mp3",
            title="多人讨论",
            kind="meeting",
            language="zh",
            file_bytes=b"fake-audio",
        )
        while True:
            state = manager.task_payload(str(payload["task_id"]))
            observed.append(str(state["stage"]))
            if state["status"] in {"completed", "failed", "cancelled"}:
                break

    self.assertEqual(["transcribing", "speaker", "publishing", "done"], _dedupe_in_order(observed))
```

- [ ] **Step 2: Run the targeted test and verify it fails**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_remote_import_jobs.RemoteImportTaskManagerTests.test_remote_import_manager_reports_speaker_stage_between_transcribing_and_done -v`

Expected: `FAIL` because there is no dedicated manager-level test scaffold yet.

- [ ] **Step 3: Write a failing test for successful speaker postprocess**

```python
def test_remote_import_before_publish_runs_apply_speaker_labels_when_enabled(self) -> None:
    workspace = SessionWorkspace.create(Path(metadata.session_dir), metadata)
    with patch("live_note.remote.import_jobs.apply_speaker_labels") as speaker_mock:
        speaker_mock.return_value = workspace.update_session(speaker_status="done")
        updated = _remote_import_before_publish(
            config=config_with_speaker,
            workspace=workspace,
            metadata=metadata,
            logger=workspace.session_logger(),
            on_progress=None,
        )
    self.assertEqual("done", updated.speaker_status)
    speaker_mock.assert_called_once()
```

- [ ] **Step 4: Write a failing test for the fallback path**

```python
def test_remote_import_before_publish_marks_failed_when_speaker_diarization_raises(self) -> None:
    workspace = SessionWorkspace.create(Path(metadata.session_dir), metadata)
    with patch("live_note.remote.import_jobs.apply_speaker_labels", side_effect=RuntimeError("boom")):
        updated = _remote_import_before_publish(
            config=config_with_speaker,
            workspace=workspace,
            metadata=metadata,
            logger=workspace.session_logger(),
            on_progress=None,
        )
    self.assertEqual("failed", updated.speaker_status)
```

- [ ] **Step 5: Write a failing test for the disabled path**

```python
def test_remote_import_before_publish_skips_speaker_when_disabled(self) -> None:
    workspace = SessionWorkspace.create(Path(metadata.session_dir), metadata)
    with patch("live_note.remote.import_jobs.apply_speaker_labels") as speaker_mock:
        updated = _remote_import_before_publish(
            config=config_without_speaker,
            workspace=workspace,
            metadata=metadata,
            logger=workspace.session_logger(),
            on_progress=None,
        )
    self.assertEqual("disabled", updated.speaker_status)
    speaker_mock.assert_not_called()
```

- [ ] **Step 6: Write a failing artifact-contract test**

```python
def test_remote_import_manager_artifacts_keep_speaker_labels_after_postprocess(self) -> None:
    with patch("live_note.remote.import_jobs.FileImportCoordinator", _FakeSuccessfulSpeakerCoordinator):
        manager = RemoteImportTaskManager(config_with_speaker)
        payload = manager.create_task(
            filename="meeting.mp3",
            title="多人讨论",
            kind="meeting",
            language="zh",
            file_bytes=b"fake-audio",
        )
        final_state = _wait_for_terminal_state(manager, str(payload["task_id"]))
    workspace = SessionWorkspace.load(
        config_with_speaker.root_dir / ".live-note" / "sessions" / str(final_state["session_id"])
    )
    metadata = workspace.read_session()
    entries = workspace.transcript_entries()
    self.assertEqual("done", metadata.speaker_status)
    self.assertEqual("Speaker 1", entries[0].speaker_label)
```

- [ ] **Step 7: Add a client-side progress regression test in `tests/test_remote_import.py`**

```python
def test_run_emits_remote_speaker_and_publishing_progress(self) -> None:
    progress_events = []
    client.import_states = [
        {"task_id": "import-1", "status": "queued", "stage": "queued", "message": "已接收上传。"},
        {"task_id": "import-1", "session_id": "remote-import-1", "status": "running", "stage": "speaker", "message": "正在进行说话人区分。"},
        {"task_id": "import-1", "session_id": "remote-import-1", "status": "running", "stage": "publishing", "message": "正在生成最终原文。"},
        {"task_id": "import-1", "session_id": "remote-import-1", "status": "completed", "stage": "done", "message": "远端导入已完成。"},
    ]
    coordinator = RemoteImportCoordinator(
        config=config,
        file_path=str(media_path),
        title="股票课",
        kind="lecture",
        language="zh",
        on_progress=lambda event: progress_events.append((event.stage, event.message)),
        client=client,
        poll_interval_seconds=0.0,
    )
    with patch("live_note.app.remote_import.apply_remote_artifacts", return_value=expected_metadata):
        coordinator.run()
    self.assertIn(("speaker", "正在进行说话人区分。"), progress_events)
    self.assertIn(("publishing", "正在生成最终原文。"), progress_events)
```

- [ ] **Step 8: Run the targeted test group and verify they fail**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_remote_import_jobs tests.test_remote_import -v`

Expected: the new tests fail for missing `_remote_import_before_publish`, missing manager-level stage ordering, and missing client-side stage coverage.

- [ ] **Step 9: Implement the remote-only postprocess in `src/live_note/remote/import_jobs.py`**

```python
def _run_import(...):
    runner = FileImportCoordinator(
        ...,
        preserve_session_audio=self.config.speaker.enabled,
        before_publish=lambda **kwargs: _remote_import_before_publish(config=self.config, **kwargs),
    )

def _remote_import_before_publish(*, config, workspace, metadata, logger, on_progress):
    if not config.speaker.enabled:
        return workspace.update_session(speaker_status="disabled")
    _emit_progress(on_progress, "speaker", "正在进行说话人区分。", session_id=metadata.session_id)
    try:
        return apply_speaker_labels(config, workspace, metadata, on_progress=on_progress)
    except Exception as exc:
        logger.error("远端导入说话人区分失败: %s", exc)
        _emit_progress(on_progress, "speaker", f"说话人区分失败：{exc}", session_id=metadata.session_id)
        return workspace.update_session(speaker_status="failed")
```

- [ ] **Step 10: Keep the remote-only boundary explicit**

Implementation notes:
- Do not call `apply_speaker_labels()` from local import code paths.
- The remote manager should be the only caller passing `preserve_session_audio=True` for this feature.
- Preserve current cancellation behavior and final `completed` status when speaker postprocess alone fails.
- Use a small helper in `import_jobs.py` instead of inlining the full fallback logic into `_run_import`.

- [ ] **Step 11: Run the targeted remote tests and verify they pass**

Run: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_remote_import_jobs tests.test_remote_import tests.test_services -v`

Expected: manager-level stage ordering, speaker success/failure/disabled behavior, artifact contract, and client-side remote import stage tests all pass.

- [ ] **Step 12: Commit the remote speaker change**

```bash
git add tests/test_remote_import_jobs.py tests/test_remote_import.py src/live_note/remote/import_jobs.py src/live_note/app/coordinator.py
git commit -m "feat: add speaker diarization for remote imports"
```

### Task 3: Update docs and run final verification

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Document the new behavior in README**

Add a short section or bullets covering:
- remote import supports speaker diarization when remote speaker runtime is enabled
- the feature is remote-only
- failures degrade to `speaker_status = failed` without failing the whole import

- [ ] **Step 2: Run lint and compile checks**

Run:

```bash
ruff check src tests
PYTHONPATH=src .venv/bin/python -m compileall src tests
```

Expected: both commands succeed with no new errors.

- [ ] **Step 3: Run the focused verification suite**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m unittest \
  tests.test_coordinator \
  tests.test_remote_import_jobs \
  tests.test_remote_import \
  tests.test_services \
  tests.test_remote_coordinator -v
```

Expected: all tests pass; no regression in remote live synchronization or import behavior.

- [ ] **Step 4: Commit the docs and verification-safe polish**

```bash
git add README.md tests/test_coordinator.py tests/test_remote_import.py tests/test_remote_import_jobs.py src/live_note/app/coordinator.py src/live_note/remote/import_jobs.py
git commit -m "docs: describe remote import speaker diarization"
```
