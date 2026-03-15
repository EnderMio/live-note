from __future__ import annotations

import argparse
import sys
from pathlib import Path

from live_note.app.coordinator import (
    FileImportCoordinator,
    SessionCoordinator,
    finalize_session,
    merge_sessions,
    refine_session,
    retranscribe_session,
)
from live_note.app.services import AppService
from live_note.audio.capture import AudioCaptureError, list_input_devices
from live_note.config import load_config

KIND_CHOICES = ["generic", "meeting", "lecture"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="live-note")
    parser.add_argument("--config", default="config.toml", help="配置文件路径")

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("doctor", help="检查依赖、配置与外部服务")
    subparsers.add_parser("devices", help="列出输入设备")
    subparsers.add_parser("gui", help="启动桌面界面")

    start_parser = subparsers.add_parser("start", help="开始一场实时转写会话")
    start_parser.add_argument("--title", help="会话标题，例如 周会 / 机器学习导论")
    start_parser.add_argument("--course", dest="course_legacy", help=argparse.SUPPRESS)
    start_parser.add_argument("--source", required=True, help="输入设备编号或名称")
    start_parser.add_argument("--kind", choices=KIND_CHOICES, default="generic", help="内容类型")
    start_parser.add_argument("--profile", dest="profile_legacy", help=argparse.SUPPRESS)
    start_parser.add_argument("--lang", default=None, help="覆盖配置中的语言")

    import_parser = subparsers.add_parser("import", help="导入音频文件并生成转写与整理笔记")
    import_parser.add_argument("--file", required=True, help="待导入的音频文件路径")
    import_parser.add_argument("--title", default=None, help="会话标题，默认取文件名")
    import_parser.add_argument("--kind", choices=KIND_CHOICES, default="generic", help="内容类型")
    import_parser.add_argument("--lang", default=None, help="覆盖配置中的语言")

    finalize_parser = subparsers.add_parser("finalize", help="补转写缺失片段并重写指定会话")
    finalize_parser.add_argument("--session", required=True, help="会话 ID")
    retranscribe_parser = subparsers.add_parser(
        "retranscribe",
        help="按当前模型重转写全部片段并重写指定会话",
    )
    retranscribe_parser.add_argument("--session", required=True, help="会话 ID")
    refine_parser = subparsers.add_parser(
        "refine",
        help="对实时会话的整场录音执行离线精修并重写输出",
    )
    refine_parser.add_argument("--session", required=True, help="会话 ID")
    merge_parser = subparsers.add_parser(
        "merge",
        help="把多条会话按开始时间顺序合并为一条新会话",
    )
    merge_parser.add_argument(
        "--session",
        action="append",
        required=True,
        help="待合并的会话 ID，可重复提供",
    )
    merge_parser.add_argument("--title", default=None, help="新会话标题，默认自动生成")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "devices":
        return command_devices()
    if args.command == "gui":
        return launch_gui(Path(args.config))
    if args.command == "doctor":
        return command_doctor(Path(args.config))

    try:
        config = load_config(Path(args.config))
    except Exception as exc:
        print(exc, file=sys.stderr)
        return 1

    if args.command == "start":
        title = args.title or args.course_legacy
        if not title:
            print("start 命令需要提供 --title", file=sys.stderr)
            return 1
        runner = SessionCoordinator(
            config=config,
            title=title,
            source=args.source,
            kind=_resolve_kind(args.kind, args.profile_legacy),
            language=args.lang,
        )
        return runner.run()
    if args.command == "import":
        runner = FileImportCoordinator(
            config=config,
            file_path=args.file,
            title=args.title,
            kind=args.kind,
            language=args.lang,
        )
        return runner.run()
    if args.command == "finalize":
        return finalize_session(config, args.session)
    if args.command == "retranscribe":
        return retranscribe_session(config, args.session)
    if args.command == "refine":
        return refine_session(config, args.session)
    if args.command == "merge":
        return merge_sessions(config, args.session, title=args.title)
    return 1


def command_devices() -> int:
    try:
        devices = list_input_devices()
    except AudioCaptureError as exc:
        print(exc, file=sys.stderr)
        return 1

    if not devices:
        print("未发现可用输入设备。")
        return 1

    for device in devices:
        print(
            f"{device.index:>3}  {device.name}  "
            f"inputs={device.max_input_channels}  rate={device.default_samplerate:.0f}"
        )
    return 0


def command_doctor(config_path: Path) -> int:
    checks = AppService(config_path).doctor_checks()
    failed = False
    for check in checks:
        print(f"[{check.status}] {check.name}: {check.detail}")
        failed = failed or check.status == "FAIL"
    return 1 if failed else 0


def launch_gui(config_path: Path) -> int:
    from live_note.app.gui import launch_gui as launch_gui_impl

    return launch_gui_impl(config_path)


def _resolve_kind(kind: str, profile_legacy: str | None) -> str:
    if profile_legacy and kind == "generic":
        return "lecture"
    return kind
