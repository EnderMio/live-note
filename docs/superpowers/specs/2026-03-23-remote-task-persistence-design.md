# Remote Task Persistence Design

## Goal

在不大改远端执行模型的前提下，让 `RemoteTaskRegistry` 从纯进程内状态升级为“可恢复状态”：远端服务重启后，至少能保留任务历史、复用同一个 `server_id`，并恢复未开始执行的任务；对重启前正在运行的任务则明确落成可解释的终态，而不是直接丢失。

## Why Now

当前产品把“本地 journal 可恢复”和“远端任务可重连附着”作为重要卖点，但 README 也明确写了 v1 不支持“服务端重启后任务恢复”。这导致远端任务一旦遇到服务重启，就会和仓库的恢复叙事发生冲突。

当前实现里，`RemoteTaskRegistry` 的任务表、排队顺序、幂等索引和 `server_id` 都只存在内存中。服务进程一旦重启：

- 客户端此前附着的任务会因为 `server_id` 变化而被判成 `lost`
- 已排队但尚未开始的任务无法继续
- 已完成但仍需重试同步的任务历史也无法保留

## Recommended Scope

本次只做“状态可恢复 + 未开始任务可恢复”，不尝试恢复重启前已经执行中的任务。

### In Scope

- 把远端任务注册表状态持久化到 `.live-note/remote_task_registry.json`
- 重启后复用同一个 `server_id`
- 恢复 `queued` 任务并继续 FIFO 调度
- 保留 `completed / failed / cancelled` 历史任务
- 将重启前的 `running / cancelling` 任务标记为明确终态和错误原因
- 保持现有 `request_id` 幂等和按 `session_id` 去重语义

### Out of Scope

- 恢复执行到一半的导入 / 转写 / speaker / refine 任务
- 跨机器迁移远端任务状态
- 为状态文件引入数据库或外部依赖

## Approach Options

### Option A: 只持久化展示态

只保存任务列表和最近终态，启动后不恢复排队任务。

优点：改动最小。
缺点：对“服务重启后继续工作”的帮助有限。

### Option B: 持久化展示态 + 恢复未开始任务

保存任务状态和最小可重建参数；启动后将 `queued` 任务重新入队，将 `running / cancelling` 任务转为失败。

优点：风险低、价值高、实现边界清晰。
缺点：不能无缝接续执行中的重任务。

### Option C: 尝试恢复执行中的任务

在状态文件里保存更多中间阶段，并为导入 / refine / retranscribe / postprocess 设计断点续跑。

优点：理论上最完整。
缺点：容易产生重复副作用，超出当前时间预算。

### Decision

采用 Option B。

## State File

新增状态文件：`.live-note/remote_task_registry.json`

建议结构：

```json
{
  "version": 1,
  "server_id": "server-abc123",
  "tasks": {
    "task-1": {
      "task_id": "task-1",
      "server_id": "server-abc123",
      "action": "import",
      "label": "文件导入",
      "status": "queued",
      "stage": "queued",
      "message": "已加入远端队列。",
      "created_at": "2026-03-23T10:00:00Z",
      "updated_at": "2026-03-23T10:00:00Z",
      "session_id": null,
      "request_id": "req-1",
      "current": null,
      "total": null,
      "result_version": 0,
      "error": null,
      "can_cancel": true,
      "task_spec": {
        "uploaded_path": "/path/to/.live-note/uploads/demo.mp3",
        "title": "股票课",
        "kind": "lecture",
        "language": "zh",
        "speaker_enabled": true
      }
    }
  },
  "pending_task_ids": ["task-1"],
  "recent_terminal_ids": []
}
```

说明：`request_ids` 和 `session_mutations` 不单独持久化，而是在加载状态时根据 `tasks` 重建，避免重复索引和源数据漂移。

## Task Spec for Runner Rebuild

当前 `_runners` 保存的是运行时 closure，无法直接落盘。因此需要把“如何重建 runner”的最小信息也纳入持久状态。

新增概念：`task_spec`，直接作为每个持久化任务记录的一部分，按 action 保存最小参数。

### `import`

- `uploaded_path`
- `title`
- `kind`
- `language`
- `speaker_enabled`

### `refine`

- `session_id`

### `retranscribe`

- `session_id`

### `postprocess`

- `session_id`
- `speaker_enabled`

`RemoteTaskRegistry` 启动恢复时，根据 `action + task_spec` 调用 `RemoteSessionService` 提供的 runner builder 重新装配 `_runners`。

## Recovery Rules

服务启动并加载状态文件后：

1. 若文件不存在：生成全新空注册表和新的 `server_id`
2. 若文件存在：复用其中的 `server_id`
3. 在 dispatcher 启动前完成全部恢复
4. 遍历所有任务：
   - `queued`：保留为 `queued`，重新加入 `pending_task_ids`
   - `running` / `cancelling`：落成 `failed`，`stage = "failed"`，`error` 和 `message` 写明“远端服务重启导致任务中断，请重试”
   - `completed` / `failed` / `cancelled`：原样保留
5. 重新构建 `request_ids`、`session_mutations`、`_cancel_events`、`_runners`

这样做的结果是：

- 客户端看到的 `server_id` 不会因为一次服务重启而变化
- 已排队任务会继续运行
- 已运行任务不会静默丢失，而是变成可解释终态

## Persistence Semantics

- 每次任务创建、状态更新、取消、结果版本递增、recent 列表变化后都立即原子写回状态文件
- 采用“先写临时文件再 replace”的方式，避免半写入文件
- 状态文件损坏时，服务可以回退到空注册表，但要记录清晰 warning
- `GET /api/v1/tasks` 仍只返回现有语义下的 `recent` 终态任务，不因为持久化而放大返回集合

## API Impact

HTTP / WebSocket 协议不变。

主要行为变化：

- `GET /api/v1/health` 返回的 `server_id` 在同一份状态文件存在期间保持稳定
- `GET /api/v1/tasks` 能在服务重启后继续看到历史任务和恢复后的排队任务
- 原先会被客户端判成 `lost` 的场景，改为看到明确的 `failed` 或继续中的 `queued/running`

## Error Handling

- 状态文件加载失败：记录 warning，初始化为空注册表
- 某个持久化任务缺少 `task_spec` 或参数不合法：任务直接标记 `failed`，错误文案说明“无法从持久状态恢复 runner”
- `uploaded_path` 已不存在：对应 `import` 任务在恢复阶段直接标记 `failed`
- `session_id` 指向的会话目录已不存在：对应 `refine / retranscribe / postprocess` 任务在恢复阶段直接标记 `failed`

## Testing Plan

先从 `tests/test_remote_tasks.py` 做 TDD，至少覆盖：

1. 创建 queued 任务后重建注册表，任务仍可继续执行
2. 创建 running 任务后模拟重启，任务被安全标记为 `failed`
3. 重建后 `server_id` 复用而不是重新生成
4. 重建后相同 `request_id` 仍然返回既有任务
5. recent terminal 和 pending FIFO 顺序都能保留
6. `tests/test_services.py` 中依赖“`server_id` 变化即判 lost”的用例同步调整为新语义

必要时再补一条 `tests/test_remote_api.py`，验证服务重启前后 `GET /api/v1/tasks` / `health` 的对外可见行为。

## Implementation Notes

为了控制改动面，这次优先把持久化逻辑放在 `src/live_note/remote/tasks.py` 内部，而不是先抽新模块。等功能落稳后，再考虑是否把状态文件读写拆到独立 store。

如果实现时发现 `postprocess` 的 runner 重建边界比预期更绕，本次可以进一步收口：先仅恢复 `import / refine / retranscribe` 的 queued 任务，对恢复出来的 `postprocess` 直接标记 `failed`，但保持其历史可见。
