from __future__ import annotations

import argparse
from pathlib import Path

from live_note.runtime.runtime_daemon import RuntimeDaemon


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="live-note-runtime-daemon")
    parser.add_argument("--config", default="config.toml", help="配置文件路径")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        daemon = RuntimeDaemon(config_path=Path(args.config))
        daemon.run_forever()
        return 0
    except RuntimeError as exc:
        message = str(exc).strip()
        if "runtime daemon already running" in message:
            return 0
        raise


if __name__ == "__main__":
    raise SystemExit(main())
