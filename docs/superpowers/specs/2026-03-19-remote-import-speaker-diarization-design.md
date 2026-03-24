# 远端导入支持说话人区分设计

## 摘要

本设计为 `remote.enabled = true` 的导入任务补上说话人区分能力。目标是在不新增独立任务类型的前提下，让远端导入在完成转写后自动进入“说话人区分 -> 发布”阶段，并把 `speaker_label` 与 `speaker_status` 一并回写到本地会话镜像和 Obsidian。

本设计只覆盖**远端导入**，不扩展到本地导入。

## 背景

当前说话人区分只挂在远端实时会话的后台收尾和远端 `refine` 上：

- 远端 live 停止后的 `_run_remote_postprocess(...)`
- 远端 `request_refine(...)`

远端导入虽然同样运行在服务端，但它走的是 `FileImportCoordinator` 链路，完成分块转写后会直接发布最终结果，因此：

- 导入任务不会进入说话人区分阶段
- `speaker_status` 往往保持 `disabled`
- 导入后的 transcript 不会带 `Speaker 1/2` 标签

## 目标

- 让远端导入在服务端自动支持说话人区分
- 不新增新的远端任务类型，仍保持单个 `import task`
- 保证说话人区分失败不会拖垮整条导入任务
- 让最终 artifacts 正确带回 `speaker_label` 与 `speaker_status`

## 非目标

- 不为本地导入增加说话人区分
- 不把“说话人区分”暴露为独立按钮或独立任务
- 不实现跨文件稳定说话人身份识别

## 用户可见行为

当远端导入且远端说话人区分已开启时，任务阶段变为：

- `uploading`
- `transcribing`
- `speaker`
- `publishing`
- `done`

最终效果：

- transcript 每条记录可带 `Speaker 1`、`Speaker 2` 等前缀
- 会话 `speaker_status` 为 `done`
- 若说话人区分失败，则保留原有转写和整理结果，但 `speaker_status = failed`

## 触发条件

仅当以下条件全部满足时才执行说话人区分：

- 桌面端启用了 `remote.enabled = true`
- 任务类型是导入文件
- 远端 `config.speaker.enabled = true`
- 远端配置了分割模型与嵌入模型
- 远端运行时安装了 `sherpa_onnx`
- 导入链路中有可用于 diarization 的整场 WAV

若任一条件不满足：

- 导入任务继续完成
- `speaker_status` 记为 `disabled` 或 `failed`

## 原理说明

当前说话人区分能力基于 `sherpa_onnx.OfflineSpeakerDiarization`：

1. 读取整场 `session.live.wav`
2. 用分割模型检测说话边界
3. 用嵌入模型提取每段音色 embedding
4. 用聚类把相近音色归为同一匿名说话人
5. 把 diarization 时间段对齐到 transcript entries，并写入 `speaker_label`

它识别的是“同一文件内有哪些不同说话人”，不是“这个人是谁”。

## 架构设计

### 远端导入阶段

远端导入保持 `import task` 不变，但内部阶段扩展为：

- `uploading`
- `transcribing`
- `speaker`
- `publishing`
- `done`

这样可以保持任务模型和 UI 一致，不必为说话人区分单独发明新动作类型。

### 整场 WAV 约束

当前 `apply_speaker_labels(...)` 依赖 `workspace.session_live_wav`。因此远端导入必须在转写完成后准备并保留一份规范整场 WAV 到会话目录：

- 规范路径固定为 `session.live.wav`
- 它可以由 `source.normalized.wav` 派生，但不能只停留在 `source.normalized.wav`

要求：

- 该文件对导入说话人区分是必需输入
- 它不应依赖 `importer.keep_normalized_audio`；即使用户不保留归一化中间文件，流程仍应为说话人区分准备 `session.live.wav`
- 若缺失，不应让导入失败，而应将 `speaker_status` 标为 `failed`

### 后处理步骤

建议把远端导入完成分块转写后统一进入一个后处理步骤：

1. 准备整场 WAV
2. 若允许，说话人区分
3. 发布原文和整理

实现目标是形成类似下面的职责边界：

- `transcribe_import_audio(...)`
- `postprocess_import_session(...)`
  - `apply_speaker_labels(...)`
  - `publish_final_outputs(...)`

不要求必须使用这些函数名，但要求“导入转写”和“导入后处理”边界清楚。

远端限定规则：

- 说话人区分逻辑只接在远端导入路径上
- 本地导入继续沿用现有共享 `FileImportCoordinator` 行为，不附带 speaker 后处理
- 若需要复用导入转写步骤，应由远端导入包装层决定是否进入 `postprocess_import_session(...)`，而不是让共享本地导入 coordinator 自动带上 speaker 分支

## 错误处理

- 说话人区分失败：
  - 不让整条导入任务失败
  - 记录日志
  - `speaker_status = failed`
  - 继续发布 transcript / structured

- 说话人区分关闭：
  - `speaker_status = disabled`
  - 不进入 `speaker` 阶段

- 缺少整场 WAV：
  - `speaker_status = failed`
  - 继续走发布

## 对外结果

远端导入最终返回的 artifacts 必须包含：

- 带 `speaker_label` 的 `entries`
- 更新后的 `speaker_status`

客户端不需要新增专门逻辑，只要沿用现有 artifacts 同步路径，即可把说话人标签落到本地镜像与 Obsidian。

## 验收

### 自动化测试

至少覆盖：

- 远端导入在 `speaker.enabled = true` 时会触发 `apply_speaker_labels(...)`
- 远端导入在 `speaker.enabled = false` 时保持现有行为
- 说话人区分失败不会让远端导入整体失败
- 返回 artifacts 时包含 `speaker_label` 与 `speaker_status`
- `speaker.enabled = false` 时不会进入 `speaker` 阶段
- `speaker.enabled = true` 但运行失败时会进入 `speaker` 阶段并以 `speaker_status = failed` 回退

### 手工验收

- 远端导入一个多人会议音频后，最终 transcript 中可看到 `Speaker 1/2`
- 若远端 speaker runtime 缺失，导入仍成功，但会话显示 `speaker_status = failed`

## 迁移策略

建议作为独立小步实现：

1. 先在远端导入链路中保留整场 WAV
2. 再把 `apply_speaker_labels(...)` 接到导入发布前
3. 最后补进度文案、测试与 README
