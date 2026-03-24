# Remote Task Persistence Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist remote task registry state so service restarts preserve `server_id`, terminal task history, and queued task recovery for the highest-value remote actions.

**Architecture:** Extend `RemoteTaskRegistry` with an on-disk state file under `.live-note/`, persist authoritative task records plus queue order, and rebuild derived indexes on startup. Recover queued tasks by reconstructing runners from stored task specs; convert interrupted `running` or `cancelling` tasks into explicit `failed` terminal states.

**Tech Stack:** Python 3.12+, dataclasses, JSON persistence, `pytest`, existing `live_note.remote` service/task architecture

---

## Chunk 1: Persistence Model + Registry Recovery

### Task 1: Add failing registry recovery tests

**Files:**
- Modify: `tests/test_remote_tasks.py`
- Reference: `docs/superpowers/specs/2026-03-23-remote-task-persistence-design.md`

- [ ] **Step 1: Write failing test for persisted `server_id` and queued task recovery**

Add tests that:
- create a registry rooted in a temp `.live-note` directory
- enqueue a queued `import` task with a reproducible runner
- instantiate a fresh registry against the same root
- assert `server_id` is reused and the queued task is still recoverable/executable
- assert the same `request_id` resolves to the existing task after restart
- assert pending FIFO order and recent terminal ordering survive registry recreation

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src pytest tests/test_remote_tasks.py -q`
Expected: FAIL because `RemoteTaskRegistry` does not persist state or recover queued tasks yet.

- [ ] **Step 3: Add failing test for interrupted running task recovery**

Add a test that marks a task as `running`, recreates the registry, and asserts the task becomes terminal `failed` with a restart-specific message.

- [ ] **Step 4: Run test to verify it fails for the expected reason**

Run: `PYTHONPATH=src pytest tests/test_remote_tasks.py -q`
Expected: FAIL because restarted registries currently lose the task entirely instead of preserving a failed record.

- [ ] **Step 5: Extend failing coverage for idempotency and ordering**

Add explicit red tests for:
- recreating a task with the same `request_id` after restart returns the existing task
- `pending_task_ids` order is preserved across restart
- `recent_terminal_ids` order is preserved across restart

- [ ] **Step 6: Run the expanded red suite and verify the failures are persistence-related**

Run: `PYTHONPATH=src pytest tests/test_remote_tasks.py -q`
Expected: FAIL only because persistence/recovery behavior is still missing.

### Task 2: Implement persisted registry state

**Files:**
- Modify: `src/live_note/remote/tasks.py`
- Test: `tests/test_remote_tasks.py`

- [ ] **Step 1: Add persisted task/task-spec model and state-file helpers**

Implement JSON-backed load/save helpers in `src/live_note/remote/tasks.py` for:
- `version`
- `server_id`
- `tasks`
- `pending_task_ids`
- `recent_terminal_ids`

Persist `created_at`, `updated_at`, and per-task `task_spec` in the authoritative task record.

- [ ] **Step 2: Restore registry state before dispatcher startup**

Update `RemoteTaskRegistry.__init__()` so it:
- loads the state file before starting the dispatcher thread
- rebuilds derived indexes (`request_ids`, `session_mutations`)
- reconstructs pending queue order
- downgrades restored `running` / `cancelling` tasks to `failed` with an explicit restart message

- [ ] **Step 3: Persist every state mutation atomically**

Write minimal save hooks for create, progress, completion, cancellation, result version bump, and terminal record updates using temp-file + replace semantics.

- [ ] **Step 4: Run focused tests to verify registry recovery passes**

Run: `PYTHONPATH=src pytest tests/test_remote_tasks.py -q`
Expected: PASS.

## Chunk 2: Service Runner Rebuild + High-Value Recovery Paths

### Task 3: Store recoverable task specs and rebuild runners

**Files:**
- Modify: `src/live_note/remote/tasks.py`
- Modify: `src/live_note/remote/service.py`
- Test: `tests/test_remote_tasks.py`

- [ ] **Step 1: Write failing tests for recovered action runners**

Add tests proving a restarted registry can rebuild queued runners for:
- `import`
- `refine`
- `retranscribe`

If `postprocess` is still awkward in the current code shape, add a failing test that it is preserved as history but restored as `failed` with a clear recovery message.

- [ ] **Step 2: Run tests to verify the runner-rebuild cases fail**

Run: `PYTHONPATH=src pytest tests/test_remote_tasks.py -q`
Expected: FAIL because persisted tasks do not yet carry reconstructible runner data.

- [ ] **Step 3: Implement task-spec capture and runner reconstruction**

Update remote service / registry integration so task creation persists minimal `task_spec` values:
- `import`: uploaded path, title, kind, language, speaker toggle
- `refine`: session id
- `retranscribe`: session id
- `postprocess`: session id + speaker toggle, or explicitly restore as failed if not yet safe to rebuild

Use a small builder hook from `RemoteSessionService` so `RemoteTaskRegistry` can reconstruct queued runners on startup without duplicating service logic.

- [ ] **Step 4: Run focused tests to verify recovered runners pass**

Run: `PYTHONPATH=src pytest tests/test_remote_tasks.py -q`
Expected: PASS.

### Task 4: Verify outward behavior and regression safety

**Files:**
- Modify (only if needed): `tests/test_remote_api.py`
- Modify (only if needed): `tests/test_services.py`
- Test: `tests/test_remote_tasks.py`

- [ ] **Step 1: Update existing restart-semantics tests to the new contract**

Adjust the already-existing restart-related expectations in `tests/test_services.py` so they verify the new contract explicitly:
- stable `server_id` across restart instead of unconditional `lost`
- preserved recent terminal tasks after restart
- attached tasks only become `lost` when state is actually unrecoverable, not merely because the service process restarted

- [ ] **Step 2: Run targeted suite to verify failures are behavior-related**

Run: `PYTHONPATH=src pytest tests/test_remote_tasks.py tests/test_remote_api.py tests/test_services.py -q`
Expected: only the new persistence expectations fail.

- [ ] **Step 3: Implement minimal compatibility fixes**

Adjust tests or small service-facing behavior only where persistence intentionally changes the externally visible contract. Do not refactor unrelated remote flows.

- [ ] **Step 4: Run targeted verification**

Run: `PYTHONPATH=src pytest tests/test_remote_tasks.py tests/test_remote_api.py tests/test_services.py -q`
Expected: PASS.

## Chunk 3: Final Verification

### Task 5: Full validation

**Files:**
- Verify: `src/live_note/remote/tasks.py`
- Verify: `src/live_note/remote/service.py`
- Verify: `tests/test_remote_tasks.py`

- [ ] **Step 1: Run full test suite against current workspace source**

Run: `PYTHONPATH=src pytest -q`
Expected: PASS.

- [ ] **Step 2: Run static sanity checks required by repo guidance**

Run: `python -m compileall src tests`
Expected: PASS with no syntax errors.

- [ ] **Step 3: Review final diff for scope discipline**

Confirm the diff is limited to remote task persistence, recovery tests, and any small compatibility updates required by the new contract.

## Commit Strategy

- Commit 1: registry persistence red/green cycle in `tests/test_remote_tasks.py` and `src/live_note/remote/tasks.py`
- Commit 2: runner rebuild red/green cycle in `src/live_note/remote/service.py` plus any matching registry tests
- Commit 3: service/API regression alignment and final verification

Note: create commits only if the user explicitly asks for them. This section exists to keep implementation chunks atomic.
