# runtime_v2 重构计划

日期：2026-04-09

本文档固化 `runtime_v2` 的最小正确重构方案。目标不是做一套通用分布式调度系统，而是在保留会话工件格式与基础数据模型的前提下，用一个更粗糙但正确的控制平面替换当前 orchestration / remote / GUI 的多真相结构。

说明：

- 文档里的 `runtime_v2` 指这次破坏性替换后的新运行时架构，不要求最终目录名真的长期叫 `runtime_v2/`。
- 当前代码承载路径是 `src/live_note/runtime/`；它就是新的唯一 runtime，而不是和旧系统并存的长期兼容层。

## 重构目标

- 保留两样东西：会话工件格式和基础数据模型。
- 可保留的核心边界是 `src/live_note/domain.py:7` 起的基础数据模型，以及 `.live-note/sessions/<id>/` 的工件目录约定。
- orchestration 层不再继续修补，也不保留长期兼容层；切到新 runtime 的入口应直接替换旧状态源。
- 需要整体替换的核心是：
  - `src/live_note/app/services.py:71`
  - `src/live_note/remote/service.py:628`
  - `src/live_note/remote/tasks.py:88`
  - `src/live_note/app/remote_coordinator.py:88`
  - `src/live_note/app/remote_task_service.py:67`
  - `src/live_note/app/remote_tasks.py:150`
  - `src/live_note/app/gui.py:1585`

## 必须保留的边界

- 会话目录继续使用 `.live-note/sessions/<session_id>/`。
- 以下工件必须继续可读、可恢复、可导出：
  - `session.toml`
  - `live.ingest.pcm`
  - `session.live.wav`
  - `segments/`
  - `segments.jsonl`
  - `transcript.md`
  - `structured.md`
  - `logs.txt`
- 新 runtime 可以新增控制平面文件，但不能破坏现有工件目录的兼容性。

## 核心设计

### 权威状态源

- 使用 SQLite 作为唯一权威状态源。
- 本机执行时：本机 `control.db` 是唯一权威状态源。
- 远端执行时：远端 `control.db` 是唯一权威状态源。
- remote 模式下，本地客户端只缓存远端读模型和同步时间，不写 remote session/task 的业务真相。
- 不允许出现“两份控制库双写后再合并”的模型。

### 控制平面最小结构

- `sessions`：当前 session 真值。
- `tasks`：当前 task 真值。
- `commands`：append-only command log。
- `events`：append-only event log。
- `session_projections`：本地会话统计读模型。
- `remote_session_projections`：远端 session 只读缓存。
- `remote_task_projections`：远端 task 只读缓存。

说明：

- 先不做 distributed lease 表。
- GUI、CLI、HTTP 查询优先读取 `sessions/tasks` 与必要的 projection 表。
- projection 必须由写路径更新；
  - 不再依赖 daemon 周期性全量扫 workspace 来修正读模型。
- 如果需要从工件重建 projection，那只能是显式 repair 工具；
  - 不是常驻模块
  - 不是主链路依赖
  - 不能替代写路径维护

### 运行时模型

- 一个 `RuntimeHost` 负责一个 `control.db`。
- 单个 `RuntimeHost` 管理：
  - `SessionSupervisor`
  - `TaskSupervisor`
- recovery 先作为 `RuntimeHost` 启动时的一段恢复流程，而不是独立复杂子系统。

### 工件模型

- SQLite 负责业务状态真相。
- 文件系统工件负责：
  - 可恢复输入
  - 最终输出
  - 向旧链路暴露兼容工件
- `session.toml`、`segments.jsonl`、`transcript.md`、`structured.md` 都是从控制平面导出的兼容工件，不再是业务真相。
- 但 workflow 可以读取这些工件内容来继续处理 transcript / audio；
  - 禁止的是用工件去判定 session/task 生命周期真相
  - 不是禁止读取工件本身的数据内容

### 传输模型

- HTTP API 负责：
  - 提交 command
  - 查询读模型
  - 拉取 artifacts
- websocket 只负责 attached live 的：
  - ingest uplink
  - command uplink
  - 实时读模型增量下发
- websocket 消息不是业务真相，只是实时视图。

### 远端任务边界

- remote `import` 也必须直接在远端创建 task。
  - 本地客户端只负责上传文件、拿回远端 task payload、写入 `remote_task_projections`
  - 不允许再用本地 queue/runtime worker 代远端排队
- remote 会话的产物变更类动作：
  - `postprocess`
  - `finalize`
  - `refine`
  - `retranscribe`
  - `republish`
  必须直接在远端创建 task，并以远端 `control.db` 为唯一真值。
- 本地客户端对这些动作只做两件事：
  - 发远端 command
  - 读取 / 同步 `remote_task_projections`
- `resync_notes` 明确不是远端任务。
  - 它只负责把已经同步到本地的 artifacts 再写入本地 Obsidian
  - 因此属于本地 workflow / 本地 task，而不是远端产物真值的一部分

### GUI 模型

- GUI 不再维护业务真相。
- GUI 不再持有业务线程生命周期。
- GUI 只做三件事：
  - 发 command
  - 读读模型
  - 处理当前窗口的 attached/detached 订阅状态
- `ATTACHED / DETACHED / CLOSED` 只作为 GUI / transport 的临时状态，不进入权威控制平面。

## 最小状态机

### Session

- `runtime_status` 的最小权威状态：
  - `STARTING -> INGESTING -> PAUSED -> STOP_REQUESTED -> HANDOFF_COMMITTED -> COMPLETED`
- 非 live 会话主路径：
  - `STARTING -> COMPLETED`
- 失败出口：
  - `FAILED`
  - `ABANDONED`

说明：

- `HANDOFF_COMMITTED` 必须是 session 持久状态。
- 它表示 durable 所有权边界已经切换：
  - live ingest 不再拥有该 session
  - postprocess task 已经 durable 入库
  - 后续完成/失败由 task runtime 负责
- `INGEST_SEALED`、`DRAINING` 保留为 event / task stage / supervisor 内部阶段，不再做成 session 持久状态。
- `display_status` 继续保留兼容映射：
  - `HANDOFF_COMMITTED -> handoff_committed`
  - `COMPLETED -> finalized / transcript_only / structured_failed / merged`
- GUI、CLI、`session.toml` 主要消费 `display_status`；
  - 需要精确恢复语义时读取 `runtime_status`

### Task

- `QUEUED -> RUNNING -> SUCCEEDED`
- 失败出口：
  - `FAILED`
  - `CANCELLED`
  - `INTERRUPTED`

说明：

- 先不引入持久化 `CLAIMED`。
- 单机单 `RuntimeHost` 下，`QUEUED -> RUNNING` 的原子切换已经足够。

### stop 相关原则

- 废弃 `stop_received`。
- 改为两个 durable 事件：
  - `stop_accepted`
  - `handoff_committed`
- `stop_accepted` 只表示停止请求已被接受。
- `handoff_committed` 才表示：
  - session `runtime_status` 已推进到 `HANDOFF_COMMITTED`
  - postprocess task 已 durable 入库
  - 会话可以安全从 live ingest 切到后台处理

## 任务协调规则

### 单 host 假设

- 一个 `control.db` 同时只允许一个活跃的 `RuntimeHost`。
- 本阶段不做多进程/多机器竞争同一控制库的分布式调度。
- 如果未来需要多 host 抢占，再单独设计。

### 必须保留的正确性约束

- 同一 `request_id` 必须幂等返回既有 task。
- 同一 session 的 mutation task 不能并发执行。
- `merge` 不能与其源 session 的其它 mutation 并发执行。

### 最小实现方式

- `tasks` 表保留这些关键字段：
  - `request_id`
  - `dedupe_key`
  - `resource_keys`
  - `status`
  - `started_at`
  - `updated_at`
  - `attempt`
- `resource_keys` 可以先做成简单字符串列表序列化，不必提前抽象成复杂资源系统。

### recovery 规则

- `RuntimeHost` 启动时扫描 `RUNNING` task。
- 可恢复的 task 重新入队。
- 不可恢复的 task 标记为 `INTERRUPTED`。
- 不依赖“内存里还记得谁接过任务”。
- 本阶段不做 heartbeat / lease timeout / 分布式 reclaim。

## 兼容工件与展示态

### `session.toml`

- `session.toml` 继续保留，作为兼容工件。
- 其字段继续服务于：
  - 历史列表
  - 工件恢复
  - 旧脚本
  - 现有测试夹具

### `segments.jsonl`

- `segments.jsonl` 继续保留，作为兼容 transcript artifact。
- 新运行时代码不得再把它当唯一业务真相。

### Markdown 工件

- `transcript.md` 是 transcript / session 读模型导出的最终兼容工件。
- `structured.md` 是 workflow 产物，经同一导出链路落盘为兼容工件。

### 时间字段

- 远端业务更新时间保留为 `remote_updated_at`。
- 本地只额外记录：
  - `last_seen_at`
  - `artifacts_synced_at`
- 本地同步动作不得覆盖远端业务更新时间。

### 展示态映射

- 内部状态继续映射回现有展示态。
- `runtime_status` 是恢复与执行边界的真值。
- `display_status` 是兼容展示值。
- GUI、CLI、Markdown frontmatter 默认只读 `display_status`；
  - 需要区分 `STOP_REQUESTED` 与 `HANDOFF_COMMITTED` 时，再显式读取 `runtime_status`

## 模块划分

### `runtime/domain/`

- `session_state.py`
- `task_state.py`
- `commands.py`
- `events.py`

### `runtime/store/`

- `control_db.py`
- `session_repo.py`
- `task_repo.py`
- `log_repo.py`

### `runtime/supervisors/`

- `runtime_host.py`
- `session_supervisor.py`
- `task_supervisor.py`

### `runtime/ingest/`

- `audio_spool.py`
- `spool_reader.py`
- `audio_assembler.py`

### `runtime/workflows/`

- `live_postprocess.py`
- `import_postprocess.py`
- `finalize.py`
- `refine.py`
- `retranscribe.py`
- `merge.py`
- `republish.py`
- `resync_notes.py`

### `runtime/read_model/`

- `session_queries.py`
- `task_queries.py`
- `history_queries.py`

### `runtime/export/`

- `artifact_export.py`

### `runtime/transport/`

- `remote_http_api.py`
- `remote_ws_server.py`
- `remote_client_protocol.py`

## 最重要的设计变化

### websocket 不再阻塞业务处理链

- 现有 `src/live_note/remote/service.py:749` 这类“收包后还要等待业务处理”的路径要消失。
- ingest 收包、command 落库、后台处理彼此解耦。

### `live.ingest.pcm` 升级为正式 journal

- 它不再只是缓冲细节。
- 它是 live 输入源的 durable journal。
- stale live、stop 后恢复、服务重启恢复都围绕它设计。

### postprocess handoff 变成 durable commit

- 不再允许“task payload 已存在，但后台其实没接手”的半状态。
- `handoff_committed` 必须在 durable 入库后才能对外可见。
- remote live runner 只负责 ingest / stop / spool。
- remote postprocess 不再挂在 live session runner 上，统一走 task runtime 调用的 workflow。

### GUI 不再拥有业务线程

- 移除本地 `background_tasks` 作为权威来源。
- GUI 关闭不再影响后台任务。

### 远端时间只显示远端真值

- 不再把本地同步时间伪装成远端 `updated_at`。

## 为什么这能一次性解决整组 bug

- 过载 abort / ping timeout：因为 ingest 与 processing 分离。
- stop 假确认 / stuck live：因为 handoff 只有在 durable commit 后可见。
- 手工从 `live.ingest.pcm` 救回：因为 recovery 变成正式能力。
- 更新时间乱跳：因为本地同步时间不再冒充远端更新时间。
- 重复 stop 日志：因为只有一条 session 事件流。
- GUI 关闭假死：因为 GUI 不再持有业务线程生命周期。
- remote/local split-brain：因为权威控制库位置被固定。

## 切换策略

- 不在旧架构上继续补。
- 直接用新的 `src/live_note/runtime/` 替换 orchestration 主路径，不保留双写、双真相或 JSON 队列兼容层。
- 每切完一条入口，就直接改为读取新的 runtime 真相。
- 旧模块只在尚未替换到的入口上短暂停留；一旦切流完成立即删除。

## 最终删除

以下删除只能发生在切流完成之后：

- `src/live_note/app/remote_coordinator.py`
- `src/live_note/app/remote_task_service.py`
- `src/live_note/app/remote_tasks.py`
- `src/live_note/app/task_queue.py`
- `src/live_note/app/task_queue_runtime.py`
- `src/live_note/app/task_dispatch_service.py`
- `src/live_note/remote/tasks.py`
- `src/live_note/remote/service.py` 的现有 live/task 结构

## 实施阶段

### Phase 0：只冻结三件事

- 冻结权威模型：
  - local execution -> local `control.db`
  - remote execution -> remote `control.db`
- 冻结 `stop_accepted -> handoff_committed` 的 durable 语义。
- 冻结兼容工件边界：
  - `session.toml`
  - `segments.jsonl`
  - `transcript.md`
  - `structured.md`

说明：

- 其它实现细节允许边做边收敛，不在文档阶段过度锁死。

### Phase 1：最小控制平面

- 建 `sessions/tasks/commands/events` 四张核心表。
- 建最小 query 层，先满足：
  - 会话列表
  - 任务列表
  - 单 session 详情
- 打通兼容展示态映射。

### Phase 2：TaskSupervisor 与启动恢复

- 实现 task 入队、运行、取消、失败、成功。
- 实现 `request_id` 幂等。
- 实现 session mutation 互斥。
- 启动时恢复 `RUNNING` task：
  - 可恢复则重新入队
  - 不可恢复则标记 `INTERRUPTED`
- 先把这些任务纳入统一调度：
  - import
  - finalize
  - refine
  - retranscribe
  - merge
  - republish
  - resync notes

### Phase 3：SessionSupervisor 与 live recovery

- 落地 live session 最小状态机。
- 落地 `live.ingest.pcm` 正式恢复模型。
- 落地 `stop_accepted -> handoff_committed` 的 durable 切换。
- 打通 live 与 import 两条主路径。

### Phase 4：transport 与入口替换

- 重写 remote HTTP API，使其面向 command、读模型、artifacts。
- 重写 websocket，使其只承载 attached live 的 ingest / command / 实时读模型增量。
- 旧 GUI、CLI、remote client 入口直接改读新 runtime，不保留中间兼容层。
- GUI 移除本地业务线程和本地业务真相。

### Phase 5：workflow、工件导出、切流

- 实现：
  - `live_postprocess`
  - `import_postprocess`
  - `finalize`
  - `refine`
  - `retranscribe`
  - `merge`
  - `republish`
  - `resync_notes`
- 打通兼容工件导出：
  - `session.toml`
  - `segments.jsonl`
  - `transcript.md`
  - `structured.md`
- 重写旧测试语义：
  - 删除 `stop_received`
  - 删除 GUI `background_tasks` 权威语义
  - 删除本地 attachment rebind 真相语义
- 完成能力对齐后切流并删除旧 orchestration。

## 验收闸门

- remote 模式下仍然只有一个权威控制库。
- 新代码只能从 SQLite 读 session/task 生命周期真相，不能回退到工件读真相。
- workflow / exporter / replay fixture 可以读取工件内容；
  - 但它们不能自行发明或覆盖 session/task 权威状态。
- 切流前必须完成这些能力：
  - live
  - import
  - finalize
  - refine
  - retranscribe
  - merge
  - republish
  - resync notes
- 切流前至少验证：
  - live ingest spool recovery
  - stop 后 handoff recovery
  - GUI 关闭不影响后台任务
  - 远端更新时间语义不再被本地同步污染
- 兼容工件仍能被现有查询、导出和测试夹具消费。
