from __future__ import annotations

import argparse
import sys
from pathlib import Path

from live_note.app.services import AppService
from live_note.audio.capture import AudioCaptureError

KIND_CHOICES = ["generic", "meeting", "lecture"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="live-note")
    parser.add_argument("--config", default="config.toml", help="配置文件路径")

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("doctor", help="检查依赖、配置与外部服务")
    subparsers.add_parser("devices", help="列出输入设备")
    subparsers.add_parser("gui", help="启动桌面界面")
    subparsers.add_parser("serve", help="启动局域网远端转写服务")
    remote_deploy_parser = subparsers.add_parser(
        "remote-deploy",
        help="同步代码到远端机器，并安装/更新 launchd 常驻服务",
    )
    remote_deploy_parser.add_argument(
        "--host",
        required=True,
        help="远端 SSH 地址，例如 ender@mini.local",
    )
    remote_deploy_parser.add_argument("--remote-dir", default="~/live-note", help="远端项目目录")
    remote_deploy_parser.add_argument(
        "--data-dir",
        default="~/Library/Application Support/live-note",
        help="远端配置与 token 存放目录",
    )
    remote_deploy_parser.add_argument(
        "--config-path",
        default="~/Library/Application Support/live-note/config.remote.toml",
        help="远端 serve 配置文件路径",
    )
    remote_deploy_parser.add_argument(
        "--label",
        default="com.live-note.remote",
        help="launchd 服务标识",
    )
    remote_deploy_parser.add_argument(
        "--remote-home",
        default=None,
        help="远端用户 home 目录，默认按 /Users/<ssh-user> 推断",
    )
    remote_deploy_parser.add_argument(
        "--python-bin",
        default="python3",
        help="远端用于创建虚拟环境的 Python 命令",
    )
    remote_deploy_parser.add_argument(
        "--speaker",
        action="store_true",
        help="同时安装说话人区分依赖",
    )
    remote_deploy_parser.add_argument(
        "--speaker-pyannote",
        action="store_true",
        help="同时安装 pyannote 说话人区分依赖",
    )
    remote_deploy_parser.add_argument(
        "--funasr",
        action="store_true",
        help="同时安装 FunASR websocket runtime，并注册独立 launchd 服务",
    )
    remote_deploy_parser.add_argument(
        "--funasr-dir",
        default="~/live-note-funasr",
        help="远端 FunASR runtime 目录",
    )
    remote_deploy_parser.add_argument(
        "--funasr-label",
        default="com.live-note.funasr",
        help="FunASR launchd 服务标识",
    )
    remote_deploy_parser.add_argument(
        "--funasr-port",
        type=int,
        default=10095,
        help="FunASR websocket 监听端口",
    )
    remote_deploy_parser.add_argument(
        "--skip-deps",
        action="store_true",
        help="跳过远端依赖安装，仅同步代码与服务配置",
    )
    remote_deploy_parser.add_argument(
        "--no-start",
        action="store_true",
        help="只安装服务，不立即重启 launchd",
    )
    remote_deploy_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅打印将执行的命令，不真正连接远端",
    )

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
    config_path = Path(args.config)
    service = AppService(config_path)

    if args.command == "devices":
        return command_devices(service)
    if args.command == "gui":
        return launch_gui(config_path)
    if args.command == "serve":
        return launch_remote_server(config_path)
    if args.command == "remote-deploy":
        return launch_remote_deploy(args)
    if args.command == "doctor":
        return command_doctor(service)

    if args.command == "start":
        title = args.title or args.course_legacy
        if not title:
            print("start 命令需要提供 --title", file=sys.stderr)
            return 1
        return service.start_live_session(
            title=title,
            source=args.source,
            kind=_resolve_kind(args.kind, args.profile_legacy),
            language=args.lang,
        )
    if args.command == "import":
        return service.import_audio_file(
            file_path=args.file,
            title=args.title,
            kind=args.kind,
            language=args.lang,
        )
    if args.command == "finalize":
        return service.finalize(args.session)
    if args.command == "retranscribe":
        return service.retranscribe(args.session)
    if args.command == "refine":
        return service.refine(args.session)
    if args.command == "merge":
        return service.merge(args.session, title=args.title)
    return 1


def command_devices(service: AppService) -> int:
    try:
        devices = service.list_input_devices()
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


def command_doctor(service: AppService) -> int:
    checks = service.doctor_checks()
    failed = False
    for check in checks:
        print(f"[{check.status}] {check.name}: {check.detail}")
        failed = failed or check.status == "FAIL"
    return 1 if failed else 0


def launch_gui(config_path: Path) -> int:
    from live_note.app.gui import launch_gui as launch_gui_impl

    return launch_gui_impl(config_path)


def launch_remote_server(config_path: Path) -> int:
    from live_note.remote.server import serve_remote_app

    return serve_remote_app(config_path)


def launch_remote_deploy(args: argparse.Namespace) -> int:
    from live_note.remote.deploy import RemoteDeployOptions, deploy_remote_service

    return deploy_remote_service(
        project_root=Path.cwd(),
        options=RemoteDeployOptions(
            host=args.host,
            remote_dir=args.remote_dir,
            data_dir=args.data_dir,
            config_path=args.config_path,
            label=args.label,
            remote_home=args.remote_home,
            python_bin=args.python_bin,
            speaker=args.speaker,
            speaker_pyannote=args.speaker_pyannote,
            funasr=args.funasr,
            funasr_dir=args.funasr_dir,
            funasr_label=args.funasr_label,
            funasr_port=args.funasr_port,
            skip_deps=args.skip_deps,
            start_service=not args.no_start,
            dry_run=args.dry_run,
        ),
    )


def _resolve_kind(kind: str, profile_legacy: str | None) -> str:
    if profile_legacy and kind == "generic":
        return "lecture"
    return kind
