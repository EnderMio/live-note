# 远端托管任务设计

> 注：本文写于旧本机 JSON 队列时代。当前实现已切到 `runtime_v2` SQLite 控制平面，本机任务真相位于 `.live-note/runtime-v2/control.db`，不再使用 `task_queue.json`。

## 摘要

本设计把 `live-note` 的远端执行从“客户端驱动的长请求”收敛为“服务端托管任务 + 客户端附着查看”。目标是保证两类远端任务在客户端关闭后继续执行，并在客户端重新连接后恢复进度与结果同步：

- 远端导入
- 远端实时录音停止后的后台收尾任务（`postprocess`，包括补转写、离线精修、整理、导出）

本设计**不**保证纯本地任务在客户端关闭后继续执行，也**不**保证远端服务重启后任务可恢复。

## 背景与问题

当前产品的本地任务由 `runtime_v2` 控制平面驱动，GUI 关闭后本地运行项会被视为中断。远端导入虽然已能在服务端继续跑，但它仍是特例：

- 远端导入只有单独的 `/imports` 协议，没有统一任务模型
- 远端任务状态主要面向当前客户端进程，不是产品级托管实体
- 客户端重新打开后，无法系统性恢复远端任务进度
- 实时录音停止后的远端后台收尾仍缺少统一的 `task_id` 与重连入口

结果是：用户关闭客户端后，远端“实际上还在跑”，但产品层无法稳定表达“这条任务仍在继续，并且可以重新连接查看”。

## 目标

- 让远端任务成为服务端托管实体，而不是客户端线程的延伸
- 在不改变主导航的前提下，把远端任务纳入统一产品结构
- 让客户端重连后可恢复远端任务进度，并在完成时自动回写本地会话镜像
- 保持“记录库”仍是唯一的会话真相入口，任务区只负责展示当前运行与最近终态

## 非目标

- 不为纯本地任务提供客户端关闭后的继续执行保证
- 不引入手机端、Web 控制台或多客户端协同
- 不实现远端服务重启后的任务恢复
- 不把整套任务系统改造成统一分布式调度器

## 产品决策

### 信息架构

- 左侧导航保持 `新建记录 / 记录库 / 设置`
- 不新增“远端”一级导航
- `记录库` 顶部新增紧凑任务区，分为：
  - `本机任务`
  - `远端任务`
- 下方仍是统一的会话列表；远端任务完成后继续回写到本地会话镜像

### 用户心智

- 首页负责发起记录或导入
- `本机任务` 仍由当前客户端负责
- `远端任务` 由服务端负责；客户端只是查看、取消、同步结果
- 会话仍以本地 `.live-note/sessions/<session_id>/` 为最终镜像

### v1 保证

- 客户端关闭或断线，不影响远端任务继续执行
- 客户端重新打开后，可在 `记录库` 顶部重新看到远端任务状态
- 若远端服务进程重启，任务可能丢失；UI 必须明确暴露这一事实

## 领域模型

### 远端任务类型

统一远端任务注册表，首批支持四类任务：

- `import`
- `postprocess`
- `refine`
- `retranscribe`

其中 `postprocess` 用于远端实时录音停止后的后台收尾，可再带一个 `steps` 列表，例如：

- `retranscribe`
- `refine`
- `publish`

### 远端任务状态

- `queued`
- `running`
- `cancelling`
- `completed`
- `failed`
- `cancelled`

每条任务至少包含以下字段：

- `task_id`
- `server_id`
- `action`
- `label`
- `status`
- `stage`
- `message`
- `session_id`
- `request_id`
- `current`
- `total`
- `created_at`
- `updated_at`
- `result_version`
- `error`
- `can_cancel`

其中：

- `request_id` 对客户端主动创建的任务为必填，例如 `import / refine / retranscribe`
- `postprocess` 这类服务端内部派生任务可不带 `request_id`

### 本地附着记录

新增 `.live-note/remote_tasks.json`，它不是队列，而是本机对远端任务的“附着表”。每条记录至少包含：

- `remote_task_id`
- `server_id`
- `action`
- `label`
- `session_id`
- `request_id`
- `last_known_status`
- `attachment_state`
- `last_synced_result_version`
- `updated_at`
- `result_version`
- `created_at`
- `last_seen_at`
- `artifacts_synced_at`
- `last_error`

本地附着表不主导任务状态，只记录：

- 本机是否已见过该远端任务
- 本机是否已把该任务的终态产物同步回本地
- 本机当前是否仍能把该附着关系解析到远端托管任务

## 服务端设计

### 统一任务注册表

远端服务从“仅导入任务管理器”升级为统一任务注册表。导入、后台收尾、离线精修、重转写都注册为同一种远端任务实体。

要求：

- 支持按 `task_id` 查询详情
- 支持列出活动任务与最近终态任务
- 支持取消支持取消的任务
- 支持按“会话修改类任务”做互斥，避免多个活动任务并发修改同一会话 artifacts
- 支持按客户端提供的 `request_id` 做幂等创建，避免“服务端已接收、客户端未拿到 task_id”时无法重新附着
- 会话修改类任务定义为：`postprocess / refine / retranscribe`
- 若某会话已存在任一活动中的会话修改类任务，则新的 `postprocess / refine / retranscribe` 请求必须返回该既有任务作为规范任务，不允许并发修改同一会话产物

### API

新增或统一以下接口：

- `GET /api/v1/tasks`
  - 返回 `server_id`
  - 返回活动任务
  - 返回最近终态任务（例如最近 50 条）
- `GET /api/v1/tasks/{task_id}`
- `POST /api/v1/tasks/{task_id}/actions/cancel`

创建入口按动作分别暴露，但返回统一任务实体：

- `POST /api/v1/imports`
  - 客户端必须传 `request_id`
  - 服务端按 `request_id` 幂等创建或返回已存在任务
- `POST /api/v1/sessions/{session_id}/actions/refine`
  - 客户端应传 `request_id`
  - 语义改为“创建远端 refine task 并立即返回”
- `POST /api/v1/sessions/{session_id}/actions/retranscribe`
  - 客户端应传 `request_id`
  - 语义改为“创建远端 retranscribe task 并立即返回”

兼容策略：

- 现有 `POST /api/v1/imports` 保留为导入创建入口
- 但导入任务内部必须注册到统一任务表，并返回标准任务载荷
- artifacts 继续走现有 `session` artifacts 接口，任务注册表不承担会话产物读取职责

`server_id` 合约：

- `server_id` 表示“当前远端任务注册表实例”的身份
- 在 v1 中，任务注册表不做跨服务重启持久化，因此每次远端服务进程启动都必须生成新的 `server_id`
- 客户端一旦发现附着记录中的 `server_id` 与当前服务返回值不一致，即可把仍未正常收尾的远端附着记录判为 `lost`

### 远端实时链路

远端 live 任务在收到“停止录音”后：

1. 立即结束前台录音会话
2. 若后续需要后台补转写、离线精修、整理或导出，则创建 `postprocess task`
3. 尽快把 `task_id + session_id` 发回客户端
4. 即使客户端随后关闭，服务端任务仍继续执行

若客户端在收到 `task_id` 前就断开，客户端重连时必须能通过 `session_id + action` 找回相关任务。

## 客户端设计

### 启动恢复流程

客户端启动时：

1. 读取 `.live-note/remote_tasks.json`
2. 若远端可达，调用 `GET /api/v1/tasks`
3. 按 `server_id + task_id` 合并本地附着记录
4. 对活动任务更新顶部“远端任务”区
5. 对 `completed` 且 `result_version` 尚未同步的任务，拉取 artifacts 并回写本地会话
6. 更新 `artifacts_synced_at`

若客户端此前没有拿到 `task_id`：

- `import`：按 `server_id + request_id` 做兜底匹配
- `postprocess / refine / retranscribe`：按 `server_id + session_id + action` 做兜底匹配

其中 `attachment_state` 为本地派生状态，取值至少包括：

- `attached`
- `awaiting_rebind`
- `lost`

判定规则：

- 远端仍能查到同一个 `server_id + task_id`：`attached`
- 客户端有附着记录，但当前缺少 `task_id`，仍在尝试按 `request_id` 或 `session_id + action` 找回：`awaiting_rebind`
- 原先可见的远端活动任务现在不存在，或 `server_id` 发生变化：`lost`

`lost` 不是远端任务状态，而是客户端附着状态；UI 对应文案为“服务端已重置，任务无法恢复”。

本地附着记录清理规则：

- 已 `completed` 且 `last_synced_result_version >= result_version` 的记录，可在超过保留窗口后清理
- 已 `failed` 或 `cancelled` 的记录，可在用户确认后或超过保留窗口后清理
- 已进入终态且被服务端从“最近终态任务”窗口移除，不应被判定为 `lost`
- 只有“原先仍应处于活动或待重绑阶段”的任务消失，才可判定为 `lost`

### 轮询策略

- 当前页面是 `记录库` 且有活动远端任务：1 到 2 秒轮询一次
- 当前不在 `记录库` 但仍有活动远端任务：5 秒一次
- 无活动远端任务：15 秒一次

客户端不在任务列表接口上做无限历史轮询；最近终态任务仅用于短期确认，不替代会话历史。
当活动中的 `import` 任务 `result_version` 递增时，客户端应继续同步原文快照，而不是只在终态时同步。

### 远端不可达

若远端暂时不可达：

- 不清空远端任务区
- 明确显示“远端暂不可达，显示的是上次已知状态”
- 不把任务自动标为失败

## 记录库任务区交互

### 展示规则

- 任务区位于 `记录库` 顶部
- 默认只展开有内容的分组
- 无任务时折叠为一行提示

每条远端任务展示：

- 标题
- 动作类型
- 当前阶段文案
- 进度条
- 最近更新时间
- 关联记录入口

### 操作规则

显性操作最多两个：

- `查看记录`
- `取消`

行为：

- 有 `session_id` 时，`查看记录` 直接选中对应会话
- 无 `session_id` 时，仅展示任务状态，不强行跳转
- `can_cancel = false` 时，不显示取消按钮
- 取消后 UI 先进入 `正在取消`
- 最终转为 `已取消` 或 `取消失败`

### 终态策略

- `completed`：显示“已完成，已同步到记录库”或“已完成，等待同步”
- `failed`：显示远端任务失败摘要
- `cancelled`：显示“已取消”
- `completed` 但 artifacts 未成功回写：显示“已完成，结果同步失败”，并允许 `重试同步`

终态任务仅保留短期可见性，用于确认刚刚结束了什么；长期沉淀仍进入会话列表，而不是把任务区做成第二套历史页。

## 同步合约

`result_version` 是服务端针对“该任务当前可见产物版本”的单调递增整数，规则如下：

- 初始值为 `0`
- 每当该任务关联的会话 artifacts 发生对客户端可见的变化时递增
- `GET /api/v1/tasks` 与 `GET /api/v1/tasks/{task_id}` 返回权威 `result_version`
- 客户端以 `last_synced_result_version < result_version` 作为需要重新拉取 artifacts 的唯一判定

对于 v1：

- `import`：分段原文快照可推动 `result_version` 递增
- `postprocess / refine / retranscribe`：仅在新稿或最终产物可见变化时递增

## 错误处理

- 远端任务完成但 artifacts 拉取失败：标记为“已完成，结果同步失败”
- 客户端重复提交同一会话的会话修改类任务：服务端返回已有活动任务
- 客户端关闭前未保存附着记录：下次启动按 `request_id` 或 `session_id + action` 尝试补绑定
- 远端服务重启导致任务丢失，或 `server_id` 与附着记录不一致：本地附着状态转为 `lost`，并显示“服务端已重置，任务无法恢复”

## 测试与验收

### 自动化测试

至少覆盖：

- 统一任务注册表的创建、查询、取消、去重
- `GET /api/v1/tasks` 的活动任务与最近终态返回
- 客户端附着表的加载、合并、去重、同步标记
- 客户端重启后恢复远端任务显示
- `completed` 任务在未同步 artifacts 时自动拉取并回写本地
- 远端不可达时保留上次已知状态
- 同一会话重复请求 `refine/retranscribe` 时复用活动任务
- 活动 `postprocess` 存在时，手动 `refine/retranscribe` 会附着到该任务
- 任一活动会话修改类任务存在时，其他会话修改类请求都会复用该活动任务
- 最近终态任务老化移除后，不会把已同步终态任务误判为 `lost`
- `request_id` 幂等重绑与 `server_id` 变更后的 `lost` 判定

### 手工验收

- 远端导入开始后关闭客户端，任务继续执行；重新打开客户端后可看到正确进度
- 远端实时录音停止后进入后台收尾，关闭客户端不影响任务继续；重新打开后能看到 `postprocess` 任务
- 任务完成后会自动回写本地会话目录，并出现在统一会话列表
- 远端暂时不可达时，任务区保留任务且不会误报失败
- 服务端重启后，任务区明确提示任务无法恢复

## 迁移策略

建议分三步落地：

1. 先把远端导入任务接入统一任务注册表，并增加 `GET /api/v1/tasks`
2. 再把 `refine / retranscribe / postprocess` 接入统一任务模型
3. 最后在 GUI 的 `记录库` 顶部接入“远端任务”区与重连逻辑

这样可以先验证“客户端关闭后远端导入继续执行”的最小闭环，再扩到实时录音停止后的后台收尾。
