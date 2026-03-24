# live-note

<p align="center">
  <img src="src/live_note/assets/branding/live-note-a1.svg" alt="live-note logo" width="108">
</p>

`live-note` 是一个本地优先的个人记录工作台，用来记录课程、会议和一般音频内容。它现在同时提供桌面 GUI 和 CLI：支持现场记录，也支持导入已有录音或视频；统一调用 `whisper.cpp` 生成原文，并按设置继续整理和导出。

更多实现取舍与恢复策略见 [docs/technical-qa.md](docs/technical-qa.md)。

## 当前范围

- v1 先支持 macOS
- 实时模式一次只接一个输入源，但已支持在 GUI 中选择、暂停、继续和停止录音
- 实时模式支持按本次会话设置“自动停止（分钟）”；留空或 `0` 关闭，暂停时计时也会同步暂停
- live 会话默认同时保存整场 `session.live.wav`，用于录音结束后的离线精修
- 新建会话页可按本次 live 单独选择“停止后自动离线精修”，不必强制沿用全局默认
- 文件导入支持 `mp3` / `m4a` / `wav` / `mp4` / `mov` / `mkv` 等本地媒体文件
- 导入会先用 `ffmpeg` 归一化，再按固定时长切块转写
- 开启远端模式后，实时录音和文件导入都会优先交给远端服务执行；桌面端负责上传、控制和回写结果
- live 停止后会先释放录音前台，再在后台继续补转写、离线精修和整理；失败时保留实时草稿继续出稿
- 本地 `.live-note/sessions/` 保存完整 journal，便于恢复与重放
- 正式版 GUI 支持离线任务队列：导入、重转写、离线精修、合并、重新生成整理、重新同步都可连续排队
- Obsidian 和 LLM 都可以单独关闭，先以“仅本地转写”跑通也成立
- 桌面端内置首启向导、历史会话列表和重试动作
- 手机工作流先采用“手机录音/导出文件 -> 同步到电脑 -> `import` 导入”

## 快速开始

1. 安装 `ffmpeg`
2. 编译 `whisper.cpp`，确认 `whisper-server` 可执行；如果你放在 `~/whisper.cpp`，可直接参考 `config.example.toml`
3. 如需同步到 Obsidian，安装并启用 Obsidian Local REST API 插件，保持 `https://127.0.0.1:27124`
4. 复制 `config.example.toml` 为 `config.toml`
5. 新建 `.env`，按需填入 `OBSIDIAN_API_KEY`、`LLM_API_KEY`；如果你的服务要求 OpenAI 风格鉴权变量名，可改填 `OPENAI_API_KEY`
6. 运行 `make doctor`
7. 运行 `make gui` 打开桌面界面；如需命令行模式，再运行 `make devices` 查看实时输入设备

如果你准备在远端机器上启用 sherpa 说话人区分，额外运行 `make setup-speaker`。如果准备启用 `pyannote` 后端，运行 `make setup-speaker-pyannote`。

说明：如果项目根目录存在 `.venv/bin/python`，`make` 会优先使用它，减少系统 Python 与项目依赖不一致导致的环境偏差。

中文课程、会议和播客内容，建议直接使用 `ggml-large-v3.bin` 或 `ggml-large-v3-turbo.bin`。`ggml-medium.bin` 更适合先验证流程或追求更快响应；`ggml-base.bin` 在中文长句、专有名词和连续口语场景下通常不够稳定。

## 常用命令

- `make gui`：启动桌面界面，包含首启向导、实时录音、文件导入和历史会话
- `make serve`：启动局域网远端转写服务，适合放在 Mac mini 等常电机器上
- `make deploy-remote ARGS='--host ender@mini.local'`：把当前代码同步到远端，并安装或更新 `launchd` 常驻服务
- `make setup-speaker`：安装 sherpa 说话人区分依赖
- `make setup-speaker-pyannote`：安装 `pyannote.audio` 说话人区分依赖
- `make dev ARGS='--title 周会 --source 2 --kind meeting'`：实时转写
- `make import ARGS='--file ~/Recordings/demo.mp4 --title 产品复盘 --kind meeting'`：导入音频或视频文件
- `make finalize ARGS='--session 20260315-210500-产品复盘'`：只补转写缺失片段，并重写原文/整理稿
- `make retranscribe ARGS='--session 20260315-210500-产品复盘'`：按当前模型重转写全部片段，并重写原文/整理稿
- `make refine ARGS='--session 20260315-210500-产品复盘'`：对 live 会话的整场录音做离线精修，并重写原文/整理稿
- `make merge ARGS='--session 20260315-210500-上半场 --session 20260315-223000-下半场'`：把多条会话按开始时间顺序合并成一条新会话，原始会话保留
- `make test`：运行单元测试

## 远端模式（Mac mini）

目标是把高功耗实时转写挪到局域网里的另一台机器上跑，桌面端只负责录音、控制和回写 Obsidian。

1. 在 Mac mini 上准备好 `ffmpeg`、`whisper.cpp`、模型文件和同一份项目代码
2. 在本机执行 `make deploy-remote ARGS='--host ender@mini.local'`。它会用 `rsync` 同步仓库、在远端建立 `.venv`、安装依赖，并写入 `~/Library/LaunchAgents/com.live-note.remote.plist`
3. 如果要把实时稿切到 FunASR，可直接执行 `make deploy-remote ARGS='--host ender@mini.local --funasr'`。这会额外在远端创建 `~/live-note-funasr/`、拉取官方 `FunASR` 仓库、安装 `modelscope + funasr`，并注册 `~/Library/LaunchAgents/com.live-note.funasr.plist`
4. 首次部署时，命令只会在远端缺少配置时自动复制 [config.remote.example.toml](config.remote.example.toml) 到 `~/Library/Application Support/live-note/config.remote.toml`，不会覆盖你已经调整过的远端配置
5. 登录 Mac mini，编辑 `~/Library/Application Support/live-note/config.remote.toml`；在这份文件里保持 `remote.enabled = false`，配置 `[serve]`、`[whisper]`，并按需打开 `[funasr].enabled` 与 `[speaker]`
6. 如需启用 sherpa 说话人区分，可执行 `make deploy-remote ARGS='--host ender@mini.local --speaker'`；如需启用 `pyannote`，执行 `make deploy-remote ARGS='--host ender@mini.local --speaker-pyannote'`
7. 在桌面端把 `[remote]` 打开，例如：

```toml
[remote]
enabled = true
base_url = "http://mini.local:8765"
api_token = "your-remote-token"
upload_timeout_seconds = 180
live_chunk_ms = 240
```

补充说明：

- 默认远端 live 链路仍可继续使用 `whisper.cpp + 分段`
- 本机 `make import ...` 和 GUI 的“导入录音”在 `remote.enabled = true` 时也会自动走远端，不再使用本机 `ffmpeg/whisper.cpp`
- 远端现在统一维护 `import / postprocess / refine / retranscribe` 四类托管任务；文件导入、远端 live 停止后的后台整理，以及历史页里对远端会话发起的“离线精修 / 重转写”，都会先注册成远端任务再异步执行
- 这些远端托管任务现在由服务端按严格 FIFO 串行执行；同一时刻只会处理 1 个重任务，后续任务会在“远端任务”区显示为 `排队中`
- 当前远端导入协议仍保留 `/api/v1/imports` 入口，但它内部也会注册为统一远端任务；桌面端上传原始文件后，会轮询 `/api/v1/tasks` 与 `/api/v1/tasks/{task_id}`，并按 `result_version` 自动回写本地镜像
- 因为远端导入使用 HTTP 直接上传，第一次点击导入后会先经历一个“上传音频到远端”的阶段；大文件在这个阶段不会立即出现分段进度
- 大文件上传超时时，可提高 `[remote].upload_timeout_seconds`；默认 `180` 秒
- 远端导入拿到 `session_id` 后，会持续把原文快照回写到本机会话目录，并按本机 Obsidian 设置增量同步；不必等整场导入完成才看到 transcript
- 如果本机代码已升级到支持远端导入，但 Mac mini 还没重新执行 `make deploy-remote ...`，桌面端会直接提示“远端服务版本过旧”，需要先更新远端服务
- `remote-deploy --funasr` 只负责把 FunASR websocket runtime 部署到远端，并注册 `com.live-note.funasr`；它默认按本机回环口的明文 `ws://127.0.0.1:10095` 启动，不启用 TLS；真正切换到 FunASR 仍要把远端 `config.remote.toml` 的 `[funasr].enabled = true` 打开
- `remote-deploy` 在 `--python-bin` 保持默认 `python3` 时，会先尝试复用远端现有 `.venv` 的 base Python，避免把已经运行的 conda / pyenv 环境意外重建成系统 Python
- 如果远端默认 `python3` 低于 3.10，官方 FunASR websocket server 会因语法不兼容而启动失败；这类机器上部署时请显式追加 `--python-bin /opt/miniconda3/bin/python3`
- 如果把远端配置中的 `[funasr].enabled = true` 打开，远端实时稿会改走 FunASR WebSocket；停止录音后仍会按当前 `[whisper]` 配置执行最终精修
- GUI 设置页里的 FunASR 项只负责保存桌面端的远端配置模板，不会直接切换已运行的 Mac mini 服务；实际后端以“设置与诊断”里的 `backend=...` 为准，切换仍需修改远端 `config.remote.toml` 并重启服务
- 远端运行配置不要直接放在仓库根目录；否则后续 `rsync --delete`、拉新分支或重建工作区时容易被覆盖
- `remote-deploy` 默认会重启远端 `launchd` 服务；如果你只想更新代码不重启，可追加 `--no-start`
- 远端会话完成后，桌面端会把远端 artifacts 回写成本地 `.live-note/sessions/<session_id>/` 镜像，再按本地设置同步到 Obsidian
- 远端会话的本地镜像会沿用服务端返回的笔记路径，不会因为本地重连或最终同步再额外创建一份空白原文笔记
- 远端导入同样会回写成本地镜像，所以历史列表、离线任务、重转写和重新导出仍然按本地会话工作
- 客户端关闭或断线时，远端托管任务会继续在服务端执行；重新打开 GUI 后，可在“记录库”顶部的“远端任务”区重新附着查看进度，并在结果同步失败时手动点击“重试同步”
- 远端任务附着信息保存在本地 `.live-note/remote_tasks.json`；它不是执行队列，只用于记录“我正在关注哪些服务端任务”以及这些任务的同步状态。本机“任务队列”负责提交顺序，真正的远端执行顺序以服务端队列为准
- v1 不做“服务端重启后任务恢复”。如果 Mac mini 上的远端服务被重启，桌面端会把原先仍在活动中的附着记录标成“已丢失 / 服务端已重置，任务无法恢复”；已经完成、失败或取消的历史任务会保留原状态，不会被误标成丢失
- 远端会话标题可以直接使用中文；桌面端会正确编码带中文的 `session_id` 请求路径，避免完成后同步 artifacts 失败
- 建议为 `[serve].api_token` 和 `[remote].api_token` 配同一值，不要在无认证状态下直接暴露到更大网络
- Mac mini 场景建议使用 `launchd` 常驻 `make serve`；至少保证系统重启后服务能自动恢复
- 远端机器上运行 `make doctor` 时，会按当前 `speaker.backend` 检查对应依赖：`sherpa_onnx` 或 `pyannote.audio`

## 说话人区分

- 说话人区分适合课程、会议、多发言人播客这类录音；默认关闭，可在 GUI 新建页按本次会话勾选“本次启用说话人区分”
- 当前支持本机 live 停止后的后处理、远端 live 停止后的后处理，以及文件导入后的后处理（本机或远端）
- 本地导入会直接对归一化后的 `source.normalized.wav` 做区分；远端导入则在服务端完成同一流程，再把带 `Speaker 1/2/...` 标签的结果同步回本机
- 文件导入在开启 speaker 后处理时，会自动把切块时长收紧到 `15s`，减少多人长段混说时整块只贴一个 speaker 的偏差
- `[speaker].backend = "sherpa_onnx"` 时，填写 `segmentation_model` 与 `embedding_model`，并先运行 `make setup-speaker`
- `[speaker].backend = "pyannote"` 时，填写 `pyannote_model`，并先运行 `make setup-speaker-pyannote` 或远端部署时追加 `--speaker-pyannote`
- `pyannote/speaker-diarization-community-1` 首次加载会从 Hugging Face 拉模型；未提供可用 token 时通常会直接返回 `401 Unauthorized`
- `pyannote` 模型如果需要 Hugging Face 访问令牌，可在 `.env` 或远端环境里配置 `PYANNOTE_AUTH_TOKEN=...`
- 如果远端机器本身访问 Hugging Face 很慢或超时，需要先解决网络链路；模型缓存完成后，后续 diarization 就不再依赖这一步下载
- 已知说话人数时，可额外设置 `[speaker].expected_speakers = 3` 这类提示，减少自动聚类把三人会议拆成十几个 speaker 的情况
- 如果你希望“导入录音”也区分说话人，而导入任务本身是走远端模式执行，那么模型和依赖也必须部署在远端机器上
- 如果缺依赖或模型不可用，本次会话不会整体失败，但 `speaker_status` 会标成 `failed` 或 `disabled`，原文仍会照常输出
- 说话人推理会在独立子进程中执行，避免远端 `serve` 进程在 diarization 期间长时间失去响应
- 远端导入在说话人阶段被取消时，现在会直接终止 diarization 子进程，不必再等整段推理自然结束
- 任务进入说话人阶段后，会看到 `speaker 1/3 -> 3/3` 的进度；`speaker_status` 会明确落成 `running / done / failed`
- speaker 标签会按累计重叠时长归一化成连续的 `Speaker 1`、`Speaker 2`……；匹配时优先看整段总重叠，而不是只看中点是否落在某个短插话里
- 最终 transcript 不再只保留固定 chunk；speaker 后处理会按 diarization turn 再结合句号/停顿符号，把一大块转写尽量收敛成更细的发言条目
- 这一步仍然是启发式对齐，不是字级时间戳；如果一段里多人抢话很多，想要更稳定的结果，优先把导入切块调短，或给出 `expected_speakers`

## 离线精修

- `audio.save_session_wav = true` 时，live 会话目录会保留 `session.live.wav`
- `refine.enabled = true` 且 `refine.auto_after_live = true` 时，GUI 新建页默认会勾选“本次停止后自动离线精修”
- 开始录音前，可在 GUI 新建页按这一次会话临时取消自动精修；这个选择会同时传给本机或远端 live 服务
- 仍然勾选自动精修时，录音停止后会自动把 `session.live.wav` 切块并重新转写
- 离线精修成功后，`segments.jsonl` 会切换为精修后的 canonical transcript；原 live journal 会备份为 `segments.live.jsonl`
- 离线精修失败时，不会覆盖 live 草稿；最终原文和整理稿会明确标注当前仍基于实时草稿
- 历史会话和 CLI 都支持手动执行一次 `refine`
- 历史会话和 CLI 都支持把多条会话合并为一条新会话，适合应对录音中断、程序退出后重开导致的一次课程被拆成两条记录
- 如果多条 live 会话的整场录音采样率不一致，系统会继续完成文本合并，但跳过 `session.live.wav` 拼接；此时新会话不能直接再做整场离线精修
- 历史会话里的“离线精修并重写”只会对保留了 `session.live.wav` 的会话启用；合并后如果没有成功拼出整场录音，该动作会自动禁用
- 对较早期的 live 会话，如果当时没有留下 `session.live.wav`，但 `segments.jsonl` 和 `segments/*.wav` 仍完整，系统会先按时间轴补静音回拼整场录音，再继续离线精修

## LLM 配置

- `llm.base_url` 支持自定义任意兼容 OpenAI 的服务地址
- `llm.wire_api = "chat_completions"` 时，请求 `/chat/completions`
- `llm.wire_api = "responses"` 时，请求 `/responses`，并支持 SSE 流式聚合
- `llm.stream = true` 时，会聚合流式响应后再写入整理笔记；`false` 时走普通 JSON 返回
- `llm.requires_openai_auth = true` 时，优先读取 `OPENAI_API_KEY`，未设置时再回退到 `LLM_API_KEY`
- GUI 里的单个 “LLM API Key” 输入框会始终写入 `LLM_API_KEY`；启用 OpenAI 鉴权时也会同步写入 `OPENAI_API_KEY`，避免旧值覆盖新值
- 首启向导和“设置与诊断”页都可以直接配置 `Base URL`、模型名、协议和 `Stream` 开关

示例：

```toml
[llm]
enabled = true
base_url = "https://api-vip.codex-for.me/v1"
model = "gpt-4.1-mini"
stream = true
wire_api = "responses"
requires_openai_auth = true
```

## GUI 说明

- 当前只保留一套正式桌面 GUI：`Tkinter/ttk`
- 当前桌面界面已收敛为更接近系统偏好设置的浅色主题：更轻的边框、更紧凑的留白、更统一的按钮与表格样式
- GUI 进度条动画已降到低频率，事件轮询也会在空闲时自动降频，减少空转时的 CPU 占用
- 顶栏会直接显示当前转写执行位置：`本机`、`远端服务（已连接）` 或 `远端服务（未连通）`，不必开始录音也能确认是否已切到远端
- 正式版 GUI 的离线动作统一进入 `.live-note/task_queue.json` 持久队列，按严格 FIFO 串行执行；关闭应用后，未开始的排队任务会在下次启动时恢复
- 历史页里的“取消所选”现在同时支持两类导入任务：尚未开始的排队导入，以及当前正在运行的导入；运行中的导入会发送取消请求并在最近的安全边界停止
- 如果应用上次退出时某个离线任务仍处于运行中，它会在下次启动时被标记为“中断”，并从待执行队列中移除，不会自动重跑
- 首次启动如果没有 `config.toml`，会自动弹出向导，预填常见的 `ffmpeg`、`whisper-server` 和模型路径
- 设置页和首启向导都支持关闭 Obsidian 同步、LLM 整理或离线精修，适合先从本地模式开始
- 设置页现在也可直接查看和修改说话人区分后端、模型与阈值；切到 `pyannote` 时，可直接填写模型名，访问令牌仍通过 `.env` 中的 `PYANNOTE_AUTH_TOKEN` 提供
- “新建记录”页签分为“现场记录”和“导入录音”；界面始终只保留一个主按钮，避免开始动作重复出现
- 现场记录支持“暂停 / 继续 / 结束并整理”；暂停期间不会继续累计记录内容
- 现场记录支持按本次会话设置自动停止分钟数；到点后行为等同于点击“停止录音”，暂停期间不会继续倒计时
- 暂停后再继续时，会重置实时转写使用的最近上下文，避免旧上下文把恢复后的弱语音或噪声一路带偏
- 点击“结束记录”后，界面会尽快回到可再次开始的状态；上一条记录会在后台继续补全原文、整理和导出
- 文件导入走队列时，“新建会话”页的运行状态面板也会显示当前进度，不必切到历史页才能看到处理状态
- 如果队列里已有离线任务正在运行，这时开始新的实时录音会允许短暂重叠；但队列不会再启动下一项，直到录音及其后台收尾结束
- 历史页顶部会显示当前队列进度、待执行列表，并支持取消尚未开始的排队任务
- 当远端模式开启时，记录库顶部还会显示“远端任务”区：运行中的任务会展示阶段和进度，完成但尚未同步的任务会显示“待同步 / 同步失败”，第二按钮会切换成“重试同步”
- 如果某条旧会话的 `session.toml` 或 `segments.jsonl` 损坏，历史页会把它隔离显示为 `broken`，不会拖垮整个列表
- “设置”页改成更接近偏好设置的结构：左侧是能力分组，中间只保留今天真正会用到的设置，右侧用一列就绪状态说明本机是否可直接开始
- 如果关闭窗口时仍有后台任务，进程会继续运行直到这些任务完成

## 语言设置

- 默认语言和会话级“语言覆盖”都支持 `auto`、`zh`、`en`、`ja`、`ko`，也允许直接输入其他 Whisper 语言代码，如 `fr`
- GUI 中的“自动识别 / 中英混合 / 多语言（auto）”实际等价于 `auto`
- 中英频繁切换、多人多语发言、语言不确定时，优先使用 `auto`
- 主体是中文、只是夹少量英文术语时，可直接用 `zh`
- 会话级“语言覆盖”选择“沿用默认设置”时，会继承设置页里的默认语言
- 不要填写 `zh,en`、`en+zh` 这类组合值；当前 `whisper.cpp` 接受的是单个语言代码或 `auto`
- 转写时会自动带上最近几段的上下文提示；`zh`/`zh-*` 会明确要求简体中文，`auto` 也会要求“中文统一用简体、其他语言保持原样”
- `auto` 仍适合中英混合和多语言内容；它会尽量保留原始语言切换，但中文内容会统一收敛为简体中文
- 简繁转换依赖 `opencc-python-reimplemented`；如果环境缺失，可重新运行 `pip install -e .`
- 对静音、背景噪声段里常见的“谢谢观看 / 欢迎订阅 / 谢谢大家”这类片尾幻觉，会做一层保守抑制
- 如果旧会话是在较小模型或旧参数下生成的，可在历史会话里使用“重转写并重写”，或运行 `make retranscribe ARGS='--session ...'`

## 输出说明

- 原文笔记默认写到 `Sessions/Transcripts/YYYY-MM-DD/*.md`
- 整理笔记默认写到 `Sessions/Summaries/YYYY-MM-DD/*.md`
- live 会话目录默认还会包含 `session.live.wav`、`segments.live.jsonl`（首次精修成功后）和 `refined/*.wav`
- `kind` 当前支持 `generic`、`meeting`、`lecture`
- `input_mode` 会记录为 `live` 或 `file`
- `transcript.md` 会标注 `transcript_source`、`refine_status`，并在末尾列出“待复核段落”
- 合并会话会生成一个新的会话目录；如果原始会话都有 `session.live.wav` 且采样率兼容，还会自动拼出新的整场录音，便于后续再做一次统一精修
- 如果某条原始 `session.live.wav` 已损坏，系统会继续完成文本合并，但跳过整场录音拼接
- 即使关闭 Obsidian 或同步失败，也会先把 `transcript.md` 和 `structured.md` 保存在本地会话目录

如果暂时没有启用 LLM，系统会生成一个可手动补写的整理模板；后续补上配置后，可以再用 `finalize` 或 GUI 历史动作重新生成整理稿。
