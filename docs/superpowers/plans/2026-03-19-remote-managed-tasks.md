# Remote Managed Tasks Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让远端导入、远端精修/重转写、远端 live 停止后的后台收尾都由服务端托管，并让客户端重连后恢复任务进度与结果同步。

**Architecture:** 服务端新增统一远端任务注册表，导入/精修/重转写/收尾都注册为同一种任务实体；客户端新增 `remote_tasks.json` 附着表，用于重连时重新附着、轮询、同步 artifacts。GUI 在“记录库”顶部新增远端任务区，只展示当前活动和最近终态，不做第二套历史页。

**Tech Stack:** Python 3.12, unittest, FastAPI, existing `RemoteSessionService` / `RemoteClient` / Tk GUI

---

## Chunk 1: Service-Side Managed Task Registry

### Task 1: 用失败测试锁定统一任务 API 与 registry 行为

**Files:**
- Modify: `tests/test_remote_api.py`
- Create: `tests/test_remote_tasks.py`
- Modify: `tests/test_remote_client.py`

- [ ] **Step 1: 为 `GET /api/v1/tasks` 与 `GET /api/v1/tasks/{task_id}` 写失败测试**
- [ ] **Step 2: 为 `POST /api/v1/tasks/{task_id}/actions/cancel` 写失败测试**
- [ ] **Step 3: 为 import `request_id` 幂等创建写失败测试**
- [ ] **Step 4: 为同一 `session_id` 的 `refine/retranscribe/postprocess` 互斥复用写失败测试**
- [ ] **Step 5: 运行 `PYTHONPATH=src .venv/bin/python -m unittest tests.test_remote_api tests.test_remote_client tests.test_remote_tasks -v`，确认先红**

### Task 2: 实现统一 registry，并接管 import/refine/retranscribe/postprocess

**Files:**
- Create: `src/live_note/remote/tasks.py`
- Modify: `src/live_note/remote/service.py`
- Modify: `src/live_note/remote/api.py`
- Modify: `src/live_note/remote/client.py`
- Modify: `src/live_note/remote/protocol.py`

- [ ] **Step 1: 增加统一任务实体、活动/最近终态索引、`server_id` 与 `result_version`**
- [ ] **Step 2: 保留 `/api/v1/imports` 创建入口，但内部改为统一任务注册**
- [ ] **Step 3: 将 `request_refine()` 改为“创建任务并立即返回”**
- [ ] **Step 4: 新增远端 `retranscribe` 创建入口**
- [ ] **Step 5: live 停止后把后台收尾改为 `postprocess task`，并把 `task_id` 发回客户端**
- [ ] **Step 6: 运行同一组远端测试，确认转绿**

## Chunk 2: Client Attachment Store And Reconnect Sync

### Task 3: 用失败测试锁定 `remote_tasks.json` 的附着/恢复/同步行为

**Files:**
- Create: `src/live_note/app/remote_tasks.py`
- Create: `tests/test_remote_tasks_store.py`
- Modify: `tests/test_remote_import.py`
- Modify: `tests/test_services.py`

- [ ] **Step 1: 为附着表加载、保存、损坏降级写失败测试**
- [ ] **Step 2: 为 `server_id` 变化后的 `lost` 判定写失败测试**
- [ ] **Step 3: 为 `completed` 且 `result_version` 递增时自动拉取 artifacts 写失败测试**
- [ ] **Step 4: 为导入任务按 `request_id` 重绑写失败测试**
- [ ] **Step 5: 运行 `PYTHONPATH=src .venv/bin/python -m unittest tests.test_remote_import tests.test_services tests.test_remote_tasks_store -v`，确认先红**

### Task 4: 实现客户端远端任务附着与轮询

**Files:**
- Modify: `src/live_note/app/services.py`
- Modify: `src/live_note/app/remote_import.py`
- Modify: `src/live_note/app/remote_coordinator.py`
- Modify: `src/live_note/app/remote_sync.py`
- Modify: `src/live_note/app/remote_tasks.py`

- [ ] **Step 1: `AppService` 增加 `remote_tasks_path()` 与远端任务恢复/同步接口**
- [ ] **Step 2: 远端 import 创建时写入附着记录，完成后更新同步状态**
- [ ] **Step 3: 远端 live 停止收到 `postprocess task_id` 后写入附着记录**
- [ ] **Step 4: 启动/轮询时合并 `GET /api/v1/tasks`、自动同步 artifacts、更新 `attachment_state`**
- [ ] **Step 5: 远端暂不可达时保留上次已知状态，不误标失败**
- [ ] **Step 6: 运行客户端远端同步测试并确认转绿**

## Chunk 3: GUI Remote Task Section

### Task 5: 在记录库顶部接入远端任务区与重连轮询

**Files:**
- Modify: `src/live_note/app/gui.py`
- Modify: `tests/test_gui.py`

- [ ] **Step 1: 为记录库顶部远端任务区渲染与状态刷新写失败测试**
- [ ] **Step 2: 为启动时恢复远端任务、不可达提示、终态“重试同步”写失败测试**
- [ ] **Step 3: 在历史页顶部增加“远端任务”列表与进度条、查看记录/取消操作**
- [ ] **Step 4: 复用现有轮询节奏，在有远端活动任务时加密轮询**
- [ ] **Step 5: 运行 `PYTHONPATH=src .venv/bin/python -m unittest tests.test_gui -v` 并确认转绿**

## Chunk 4: Documentation And Verification

### Task 6: 更新文档并做最终验证

**Files:**
- Modify: `README.md`

- [ ] **Step 1: 记录远端托管任务、客户端关闭后继续执行、重连恢复、限制条件（服务端重启不恢复）**
- [ ] **Step 2: 运行 `ruff check src tests`**
- [ ] **Step 3: 运行 `PYTHONPATH=src .venv/bin/python -m compileall src tests`**
- [ ] **Step 4: 运行聚焦验证**

```bash
PYTHONPATH=src .venv/bin/python -m unittest \
  tests.test_remote_api \
  tests.test_remote_client \
  tests.test_remote_tasks \
  tests.test_remote_tasks_store \
  tests.test_remote_import \
  tests.test_services \
  tests.test_gui -v
```
