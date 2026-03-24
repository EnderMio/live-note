# Remote Import Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让本机 GUI/CLI 的 `import` 在 `remote.enabled = true` 时自动把音频上传到远端服务处理，并在完成后回写本地 artifacts。

**Architecture:** 追加一个最小远端导入协议：桌面端上传文件到远端 `POST /api/v1/imports`，远端后台线程运行现有 `FileImportCoordinator`，桌面端轮询导入任务状态并在完成后拉取 artifacts。现有本地导入链路和远端实时链路都保持不变。

**Tech Stack:** Python 3.12, FastAPI, urllib, 现有 coordinator / journal / remote_sync

---

## Chunk 1: 测试先行

### Task 1: 锁定 CLI / Service / Client / API 的远端导入入口

**Files:**
- Modify: `tests/test_cli.py`
- Modify: `tests/test_services.py`
- Modify: `tests/test_remote_client.py`
- Modify: `tests/test_remote_api.py`
- Create: `tests/test_remote_import.py`

- [ ] 写失败测试，覆盖 `import` 的远端分发、远端上传/轮询、API 新路由，以及远端导入协调器完成后回写 artifacts。
- [ ] 分别运行目标测试，确认以“缺少远端导入实现”失败。

## Chunk 2: 最小远端导入协议

### Task 2: 实现远端导入任务管理

**Files:**
- Create: `src/live_note/remote/import_jobs.py`
- Modify: `src/live_note/remote/api.py`
- Modify: `src/live_note/remote/client.py`
- Modify: `src/live_note/remote/service.py`

- [ ] 新增远端导入任务管理器，保存上传文件、后台运行 `FileImportCoordinator`、记录任务进度。
- [ ] 在 API 暴露 `POST /api/v1/imports` 与 `GET /api/v1/imports/{task_id}`。
- [ ] 在客户端增加上传文件与轮询任务状态的方法。
- [ ] 运行对应测试，确认通过。

## Chunk 3: 本地协调器接入

### Task 3: 让 GUI/CLI/队列自动切到远端导入

**Files:**
- Create: `src/live_note/app/remote_import.py`
- Modify: `src/live_note/app/cli.py`
- Modify: `src/live_note/app/services.py`

- [ ] 新增 `RemoteImportCoordinator`，负责上传、轮询、拉取 artifacts 并调用 `apply_remote_artifacts()`。
- [ ] 让 CLI 与 `AppService.create_import_coordinator()` 在远端模式下选用它。
- [ ] 运行目标测试，确认远端模式导入链路成立。

## Chunk 4: 文档与验收

### Task 4: 更新 README 并做回归验证

**Files:**
- Modify: `README.md`

- [ ] 补 README 的远端导入说明、约束与示例命令。
- [ ] 运行 `PYTHONPATH=src .venv/bin/python -m unittest tests.test_cli tests.test_services tests.test_remote_client tests.test_remote_api tests.test_remote_import -v`。
- [ ] 如有必要再跑更广的相关测试集，确认没有破坏现有远端实时链路。
