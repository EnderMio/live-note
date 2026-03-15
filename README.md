# live-note

`live-note` 是一个本地优先的课程、会议和一般音频内容记录工具。它现在同时提供桌面 GUI 和 CLI：支持实时采集麦克风/虚拟声卡音频，也支持导入音频或视频文件；统一调用 `whisper.cpp` 转写，并可选同步到 Obsidian、可选调用 LLM 生成整理稿。

## 当前范围

- v1 先支持 macOS
- 实时模式一次只接一个输入源，但已支持在 GUI 中选择和停止录音
- live 会话默认同时保存整场 `session.live.wav`，用于录音结束后的离线精修
- 文件导入支持 `mp3` / `m4a` / `wav` / `mp4` / `mov` / `mkv` 等本地媒体文件
- 导入会先用 `ffmpeg` 归一化，再按固定时长切块转写
- live 结束后默认会自动后台跑一次离线精修；失败时保留实时草稿继续出稿
- 本地 `.live-note/sessions/` 保存完整 journal，便于恢复与重放
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

中文课程、会议和播客内容，建议直接使用 `ggml-large-v3.bin` 或 `ggml-large-v3-turbo.bin`。`ggml-medium.bin` 更适合先验证流程或追求更快响应；`ggml-base.bin` 在中文长句、专有名词和连续口语场景下通常不够稳定。

## 常用命令

- `make gui`：启动桌面界面，包含首启向导、实时录音、文件导入和历史会话
- `make dev ARGS='--title 周会 --source 2 --kind meeting'`：实时转写
- `make import ARGS='--file ~/Recordings/demo.mp4 --title 产品复盘 --kind meeting'`：导入音频或视频文件
- `make finalize ARGS='--session 20260315-210500-产品复盘'`：只补转写缺失片段，并重写原文/整理稿
- `make retranscribe ARGS='--session 20260315-210500-产品复盘'`：按当前模型重转写全部片段，并重写原文/整理稿
- `make refine ARGS='--session 20260315-210500-产品复盘'`：对 live 会话的整场录音做离线精修，并重写原文/整理稿
- `make merge ARGS='--session 20260315-210500-上半场 --session 20260315-223000-下半场'`：把多条会话按开始时间顺序合并成一条新会话，原始会话保留
- `make test`：运行单元测试

## 离线精修

- `audio.save_session_wav = true` 时，live 会话目录会保留 `session.live.wav`
- `refine.enabled = true` 且 `refine.auto_after_live = true` 时，录音停止后会自动把 `session.live.wav` 切块并重新转写
- 离线精修成功后，`segments.jsonl` 会切换为精修后的 canonical transcript；原 live journal 会备份为 `segments.live.jsonl`
- 离线精修失败时，不会覆盖 live 草稿；最终原文和整理稿会明确标注当前仍基于实时草稿
- 历史会话和 CLI 都支持手动执行一次 `refine`
- 历史会话和 CLI 都支持把多条会话合并为一条新会话，适合应对录音中断、程序退出后重开导致的一次课程被拆成两条记录
- 如果多条 live 会话的整场录音采样率不一致，系统会继续完成文本合并，但跳过 `session.live.wav` 拼接；此时新会话不能直接再做整场离线精修

## LLM 配置

- `llm.base_url` 支持自定义任意兼容 OpenAI 的服务地址
- `llm.wire_api = "chat_completions"` 时，请求 `/chat/completions`
- `llm.wire_api = "responses"` 时，请求 `/responses`，并支持 SSE 流式聚合
- `llm.stream = true` 时，会聚合流式响应后再写入整理笔记；`false` 时走普通 JSON 返回
- `llm.requires_openai_auth = true` 时，优先读取 `OPENAI_API_KEY`，未设置时再回退到 `LLM_API_KEY`
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

- 首次启动如果没有 `config.toml`，会自动弹出向导，预填常见的 `ffmpeg`、`whisper-server` 和模型路径
- 设置页和首启向导都支持关闭 Obsidian 同步、LLM 整理或离线精修，适合先从本地模式开始
- “新建会话”页签分为“实时录音”和“导入文件”，默认走一键成稿
- “历史会话”支持多选；可直接把多条会话合并为一条新会话，也可打开本地原文/整理稿、离线精修并重写、重转写并重写、重新生成整理、重新同步 Obsidian
- “设置与诊断”可以保存配置、重新检测依赖、打开 `config.toml` / `.env`，并查看每项检查结果

## 语言设置

- 默认语言和会话级“语言覆盖”都支持 `auto`、`zh`、`en`、`ja`、`ko`，也允许直接输入其他 Whisper 语言代码，如 `fr`
- GUI 中的“自动识别 / 中英混合 / 多语言（auto）”实际等价于 `auto`
- 中英频繁切换、多人多语发言、语言不确定时，优先使用 `auto`
- 主体是中文、只是夹少量英文术语时，可直接用 `zh`
- 会话级“语言覆盖”选择“沿用默认设置”时，会继承设置页里的默认语言
- 不要填写 `zh,en`、`en+zh` 这类组合值；当前 `whisper.cpp` 接受的是单个语言代码或 `auto`
- 转写时会自动带上最近几段的上下文提示；中文场景会优先输出简体中文
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
- 即使关闭 Obsidian 或同步失败，也会先把 `transcript.md` 和 `structured.md` 保存在本地会话目录

如果暂时没有启用 LLM，系统会生成一个可手动补写的整理模板；后续补上配置后，可以再用 `finalize` 或 GUI 历史动作重新生成整理稿。
