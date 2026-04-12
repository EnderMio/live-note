# runtime 架构说明

日期：2026-04-12

本文档记录当前重构后的 runtime 架构。它描述控制平面、工件平面、运行时平面、投影平面、远端传输平面与同步平面的职责边界，并给出当前代码中的主要落点。

## 架构总览

这次重构保留两类稳定边界：

- 基础数据模型：`src/live_note/domain.py`
- 会话工件目录：`.live-note/sessions/<session_id>/`

系统围绕四个核心平面组织：

- 控制平面：SQLite `control.db`
- 工件平面：session workspace 下的 durable 文件
- 运行时平面：`RuntimeHost`、`SessionSupervisor`、`TaskSupervisor`
- 投影平面：GUI、CLI、remote task/session 列表读取的读模型

远端模式在这四个平面之外增加两层：

- 传输平面：HTTP API 与 live websocket
- 同步平面：远端 artifacts 拉取、本地投影更新、本地 Obsidian 写入

## 控制平面

控制平面的唯一权威状态源是 SQLite。当前数据库路径是：

- `.live-note/runtime/control.db`

当前 schema 定义位于：

- `src/live_note/runtime/store/control_db.py`

核心表：

- `sessions`：session 当前真值
- `tasks`：task 当前真值
- `commands`：append-only command log
- `events`：append-only event log
- `session_projections`：本地统计读模型
- `remote_session_projections`：远端 session 本地只读缓存
- `remote_task_projections`：远端 task 本地只读缓存

职责分配：

- `sessions/tasks` 提供当前生命周期真值
- `commands/events` 提供 durable 历史流
- projection 表提供 GUI、CLI、同步器的查询视图

## Session 状态机

session 持久状态机定义位于：

- `src/live_note/runtime/domain/session_state.py`

当前最小状态机：

- `starting -> ingesting -> paused -> stop_requested -> handoff_committed -> completed`
- 失败出口：`failed`
- 放弃出口：`abandoned`

说明：

- `runtime_status` 保存执行与恢复边界
- `display_status` 保存展示态与兼容态
- `handoff_committed` 表示后台 postprocess task 已 durable 入库，并且 live ownership 已经转交完成

命令与事件的关系：

- `session_begin_ingest -> ingest_started`
- `session_pause_ingest -> ingest_paused`
- `session_resume_ingest -> ingest_resumed`
- `session_accept_stop -> stop_accepted`
- `session_commit_handoff -> handoff_committed`
- `session_complete -> session_completed`
- `session_fail -> session_failed`
- `session_abandon -> session_abandoned`

## Task 状态机

task 持久状态机定义位于：

- `src/live_note/runtime/domain/task_state.py`

当前最小状态机：

- `queued -> running -> succeeded`
- 失败出口：`failed`
- 取消出口：`cancelled`
- 中断出口：`interrupted`

当前任务模型包含这些关键字段：

- `request_id`
- `dedupe_key`
- `resource_keys`
- `status`
- `stage`
- `attempt`
- `cancel_requested`
- `result_version`

资源互斥由 `resource_keys` 参与调度，冲突检查发生在 `TaskSupervisor.start_task()`。

## 运行时平面

运行时宿主位于：

- `src/live_note/runtime/supervisors/runtime_host.py`

一个 `RuntimeHost` 负责一个 `control.db`，当前包含：

- `SessionSupervisor`
- `TaskSupervisor`
- 启动时 recovery

当前职责：

- `SessionSupervisor` 负责 session reducer、命令应用、metadata 变更、event 追加
- `TaskSupervisor` 负责 task 提交、去重、启动、进度、取消、终态收口
- `RuntimeHost.commit_session_task_handoff()` 负责 session 与后台 task 的原子交接

`commit_session_task_handoff()` 的事务语义：

1. 创建后台 task
2. 写入 handoff 事件数据
3. 推进 session 到 `handoff_committed`
4. 提交事务

这个边界承载 live 到 postprocess 的 durable ownership 切换。

## 工件平面

工件目录继续使用：

- `.live-note/sessions/<session_id>/`

核心工件：

- `session.toml`
- `live.ingest.pcm`
- `session.live.wav`
- `segments/`
- `segments.jsonl`
- `transcript.md`
- `structured.md`
- `logs.txt`

工件职责：

- `live.ingest.pcm` 保存 live 输入 journal
- `session.live.wav` 保存整场音频
- `segments/` 与 `segments.jsonl` 保存 transcript 输入与 transcript artifact
- `transcript.md` 与 `structured.md` 保存最终可消费输出
- `session.toml` 保存兼容元数据快照

工件写入路径主要在：

- `src/live_note/session_workspace.py`
- `src/live_note/runtime/session_outputs.py`
- `src/live_note/runtime/workflow_support.py`

## ingest journal 与恢复

ingest journal 的定义位于：

- `src/live_note/runtime/ingest/audio_spool.py`
- `src/live_note/runtime/ingest/spool_reader.py`

`live.ingest.pcm` 当前保存每个音频 frame 的：

- `started_ms`
- `ended_ms`
- `pcm16`

恢复逻辑位于：

- `src/live_note/runtime/supervisors/recovery_supervisor.py`
- `src/live_note/runtime/workflow_support.py`

当前恢复流程：

1. `RuntimeHost.start()` 触发 recovery
2. 扫描 `RUNNING` task，决定 requeue 或 `interrupted`
3. 扫描 live session
4. 读取 `live.ingest.pcm`
5. 提交 `postprocess` task
6. 推进 session 到 `handoff_committed`

live workflow 内也支持从 spool 重建 transcript 与产物。

## workflow 平面

任务入口位于：

- `src/live_note/runtime/task_runners.py`

session 相关 workflow 位于：

- `src/live_note/runtime/session_workflows.py`

当前主要 workflow：

- `postprocess_session`
- `finalize_session`
- `retranscribe_session`
- `refine_session`
- `republish_session`
- `merge_sessions`
- `sync_session_notes`

当前执行链：

1. `TaskSupervisor` 选出可运行 task
2. `TaskRunnerFactory` 按 action 构建 runner 或直接调用 workflow
3. workflow 读取 workspace、更新 session metadata、生成 artifacts、发布最终输出
4. `publish_final_outputs()` 推进 session 完成态

最终输出与 session 完成态的收口位于：

- `src/live_note/runtime/session_outputs.py`

## 投影平面

读模型入口位于：

- `src/live_note/runtime/read_model.py`
- `src/live_note/runtime/projections/session_summaries.py`

当前投影职责：

- 为 GUI 提供历史会话列表
- 为 GUI 提供活动任务列表
- 为 CLI 提供会话查询入口
- 为 remote 同步器提供远端任务/会话缓存

本地 session summary 组合来源：

- `sessions`
- `session_projections`
- `remote_session_projections`

当前 GUI 通过 `AppService` 读取 projection，并通过 command 提交操作。

## 本地执行拓扑

本地后台执行器位于：

- `src/live_note/runtime/runtime_daemon.py`
- `src/live_note/runtime_daemon_main.py`

`AppService` 会拉起 detached daemon：

- `src/live_note/app/services.py`

当前本地拓扑：

1. GUI 或 CLI 提交 session/task 命令
2. `RuntimeDaemon` 持续轮询 `control.db`
3. daemon 启动 live task 或 queue task
4. task progress 回写 SQLite
5. GUI 刷新 projection

live 控制命令通过日志传递：

- `stop`
- `pause`
- `resume`

控制逻辑位于：

- `src/live_note/runtime/live_control.py`

## 远端传输平面

远端 HTTP / websocket 入口位于：

- `src/live_note/remote/api.py`
- `src/live_note/remote/live_gateway.py`

HTTP API 提供：

- health
- session 列表与详情
- artifacts 拉取
- session action 提交
- import task 提交
- task 查询与取消

websocket 提供：

- live start
- PCM ingest uplink
- pause / resume / stop command uplink
- 实时进度回推

远端 live session runner 位于：

- `src/live_note/remote/live_session.py`

它负责：

- 创建 remote live session
- 接收并转写 live 音频
- 写入 `live.ingest.pcm`
- 在 stop 后提交 `postprocess` durable handoff

## 同步平面

同步逻辑位于：

- `src/live_note/runtime/remote_projection_sync.py`
- `src/live_note/runtime/remote_task_projections.py`
- `src/live_note/runtime/remote_session_projections.py`
- `src/live_note/remote_sync.py`

同步平面的职责：

- 拉取远端 task 列表
- 更新 `remote_task_projections`
- 拉取远端 session artifacts
- 写入本地 workspace
- 写入本地 Obsidian
- 更新 `remote_session_projections`

时间字段职责：

- `remote_updated_at`：远端业务更新时间
- `last_seen_at`：本地最近一次观察到远端对象的时间
- `artifacts_synced_at`：本地最近一次同步远端工件成功的时间

## GUI 与 AppService

GUI 主入口位于：

- `src/live_note/app/gui.py`

服务入口位于：

- `src/live_note/app/services.py`

当前 GUI 职责：

- 发命令
- 读 projection
- 展示 live/task/session 状态
- 管理 attached / detached 的窗口侧状态

当前 `AppService` 职责：

- 访问 read model
- 提交 live/task 操作
- 启动本地 runtime daemon
- 同步远端 task projection

## 当前代码收口点

当前实现已经完成这些核心替换：

- SQLite 已经承载 session/task 真值
- session reducer 已经承载唯一状态机
- task supervisor 已经承载本地任务调度与恢复
- `live.ingest.pcm` 已经进入正式恢复路径
- GUI 已经读取 projection
- 本地 runtime daemon 已经从 GUI 生命周期中分离

当前剩余的主收口点位于远端执行面：

- `src/live_note/remote/task_runtime.py`
- `src/live_note/remote/task_commands.py`

当前远端执行结构是：

- 远端状态真值写入同一个 runtime control plane
- 远端任务执行循环仍由 `RemoteTaskHost` 驱动

下一步收口方向：

- 把远端 task 执行循环并到统一的 runtime execution model
- 保持 remote API 作为 command / query / artifact front door
- 保持 remote control.db 作为远端唯一权威状态源

## 代码落点索引

控制平面：

- `src/live_note/runtime/store/control_db.py`
- `src/live_note/runtime/store/session_repo.py`
- `src/live_note/runtime/store/task_repo.py`
- `src/live_note/runtime/store/log_repo.py`

状态机：

- `src/live_note/runtime/domain/session_state.py`
- `src/live_note/runtime/domain/task_state.py`

运行时：

- `src/live_note/runtime/supervisors/runtime_host.py`
- `src/live_note/runtime/supervisors/session_supervisor.py`
- `src/live_note/runtime/supervisors/task_supervisor.py`
- `src/live_note/runtime/supervisors/recovery_supervisor.py`

ingest：

- `src/live_note/runtime/ingest/audio_spool.py`
- `src/live_note/runtime/ingest/spool_reader.py`

workflow：

- `src/live_note/runtime/task_runners.py`
- `src/live_note/runtime/session_workflows.py`
- `src/live_note/runtime/workflow_support.py`
- `src/live_note/runtime/session_outputs.py`

投影：

- `src/live_note/runtime/read_model.py`
- `src/live_note/runtime/projections/session_summaries.py`
- `src/live_note/runtime/remote_task_projections.py`
- `src/live_note/runtime/remote_session_projections.py`

本地后台：

- `src/live_note/runtime/runtime_daemon.py`
- `src/live_note/app/services.py`
- `src/live_note/app/gui.py`

远端：

- `src/live_note/remote/api.py`
- `src/live_note/remote/live_gateway.py`
- `src/live_note/remote/live_session.py`
- `src/live_note/remote/task_commands.py`
- `src/live_note/remote/task_runtime.py`
