# Repository Guidelines

## 项目结构与模块划分
本仓库实现一个通用音频记录与整理工具：支持实时采集和文件导入，统一走“切段/切块 -> `whisper.cpp` 转写 -> 写入 Obsidian -> LLM 整理”的链路。核心代码在 `src/live_note/`：

- `app/`：CLI 入口、会话编排、journal 恢复
- `audio/`：设备采集、VAD 分段、文件归一化与切块
- `transcribe/`：`whisper-server` 进程管理与 HTTP 调用
- `obsidian/`：Local REST API 客户端与 Markdown 渲染
- `tests/`：单元测试，覆盖 journal、渲染、CLI、导入、whisper/obsidian 客户端

会话数据保存在 `.live-note/sessions/<session_id>/`，包含 `session.toml`、`segments/`、`transcript.md`、`structured.md`、`logs.txt`。即使关闭 Obsidian 或 LLM，也要保持这套本地 journal 完整可恢复。

## 构建、测试与开发命令
- `make setup`：创建虚拟环境并安装开发依赖
- `make doctor`：检查 `ffmpeg`、`whisper-server`、模型路径、Obsidian 与 LLM 配置
- `make devices`：列出实时录音输入设备
- `make dev ARGS='--title 周会 --source 2 --kind meeting'`：启动实时转写
- `make import ARGS='--file ~/Recordings/demo.mp3 --kind lecture'`：导入音频文件
- `make finalize ARGS='--session 20260315-210500-周会'`：补转写并重写笔记
- `make test`：运行单元测试

## 代码风格与命名约定
使用 Python 3.12、4 空格缩进。模块、函数、变量统一 `snake_case`；类使用 `PascalCase`。新能力优先接入现有 coordinator 和 journal，不要绕开 `.live-note/sessions/` 直接写业务脚本。提交前运行 `ruff check src tests` 与 `python -m compileall src tests`。

## 测试要求
测试文件命名为 `test_*.py`。新增功能至少覆盖：

- 参数解析与命令分发
- 音频切块/分段边界
- `whisper.cpp` 或 Obsidian 请求失败后的回退路径
- 笔记渲染的 frontmatter 与链接格式

## 提交与合并请求
采用 Conventional Commits，例如 `feat: add file import pipeline`、`fix: handle empty whisper response`。PR 需说明改动链路、验证命令、配置变化；如果修改笔记模板或 Obsidian 输出结构，附一段示例 Markdown。

## 配置与安全
不要提交 `.env`、`config.toml`、模型文件、录音样本或真实会议内容。Obsidian Local REST API 默认走 `https://127.0.0.1:27124`，本地自签证书场景保持 `verify_ssl = false`。如果做本地优先改动，优先复用 `obsidian.enabled` 和 `llm.enabled`，不要新增分叉脚本。
