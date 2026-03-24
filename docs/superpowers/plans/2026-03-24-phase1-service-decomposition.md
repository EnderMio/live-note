# Phase 1 Service Decomposition Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Shrink the change radius of the app orchestration layer by extracting the first narrow service boundaries from `AppService` without changing user-visible behavior.

**Architecture:** Keep `AppService` as a compatibility façade while moving cohesive responsibilities into smaller focused modules. Start with settings/config workflows because they are already heavily covered by tests and provide the narrowest extraction seam inside `src/live_note/app/services.py`.

**Tech Stack:** Python 3.12+, dataclasses, existing `live_note.app` services/config stack, `pytest`

---

## Chunk 1: Extract Settings Service

### Task 1: Introduce a dedicated settings service behind `AppService`

**Files:**
- Create: `src/live_note/app/settings_service.py`
- Modify: `src/live_note/app/services.py`
- Modify: `tests/test_services.py`

- [ ] **Step 1: Write failing tests for the new settings boundary**

Add focused tests that:
- instantiate a dedicated settings service with `config.toml` and `.env` paths
- verify `load_settings_draft()` falls back to detection when config loading fails
- verify `AppService.save_settings()` delegates through the extracted settings service instead of owning settings assembly directly

- [ ] **Step 2: Run the focused tests to verify they fail for the expected reason**

Run: `PYTHONPATH=src pytest tests/test_services.py -q -k "settings_service or save_settings_delegates"`
Expected: FAIL because `SettingsService` does not exist yet and `AppService` still owns settings logic.

- [ ] **Step 3: Implement `SettingsService` with no behavior change**

Move these responsibilities into `src/live_note/app/settings_service.py`:
- settings draft loading and auto-detection
- validation of `SettingsDraft`
- config reconstruction and `save_config()` writeback

Keep `AppService` methods as thin pass-through wrappers to preserve call sites.

- [ ] **Step 4: Run focused tests to verify the new boundary passes**

Run: `PYTHONPATH=src pytest tests/test_services.py -q -k "settings_service or save_settings_delegates"`
Expected: PASS.

### Task 2: Preserve existing settings behavior through façade compatibility

**Files:**
- Modify: `src/live_note/app/services.py`
- Test: `tests/test_services.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Run broader settings/service tests against the façade**

Run: `PYTHONPATH=src pytest tests/test_services.py tests/test_cli.py -q`
Expected: PASS except for regressions introduced by the extraction.

- [ ] **Step 2: Fix only compatibility regressions caused by extraction**

Ensure existing callers still import `AppService` and `SettingsDraft` from `src/live_note/app/services.py` with no user-visible behavior changes.

- [ ] **Step 3: Re-run the broader service/CLI verification**

Run: `PYTHONPATH=src pytest tests/test_services.py tests/test_cli.py -q`
Expected: PASS.

## Chunk 2: Prepare the next narrow seams

### Task 3: Stage follow-up extraction points without behavior changes

**Files:**
- Modify: `src/live_note/app/services.py`
- Reference: `src/live_note/app/coordinator.py`
- Reference: `src/live_note/app/gui.py`

- [ ] **Step 1: Leave explicit internal seams for doctor/session/remote-task extraction**

After the settings extraction, keep `AppService` organized so the next responsibilities to extract are obvious and do not re-entangle settings code.

- [ ] **Step 2: Run full verification for the first decomposition slice**

Run: `PYTHONPATH=src pytest -q`
Expected: PASS.

- [ ] **Step 3: Run syntax sanity checks required by repo guidance**

Run: `python -m compileall src tests`
Expected: PASS.

## Commit Strategy

- Commit 1: `SettingsService` red/green extraction in `src/live_note/app/settings_service.py`, `src/live_note/app/services.py`, and `tests/test_services.py`
- Commit 2: façade compatibility cleanup and verification

Note: create commits only if the user explicitly asks for them. This section exists to keep implementation chunks atomic.
