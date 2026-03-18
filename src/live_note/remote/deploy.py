from __future__ import annotations

import base64
import getpass
import plistlib
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RemoteDeployOptions:
    host: str
    remote_dir: str = "~/live-note"
    data_dir: str = "~/Library/Application Support/live-note"
    config_path: str = "~/Library/Application Support/live-note/config.remote.toml"
    label: str = "com.live-note.remote"
    remote_home: str | None = None
    python_bin: str = "python3"
    ssh_bin: str = "ssh"
    rsync_bin: str = "rsync"
    speaker: bool = False
    skip_deps: bool = False
    start_service: bool = True
    dry_run: bool = False


@dataclass(frozen=True, slots=True)
class DeployCommand:
    label: str
    argv: list[str]


def build_remote_deploy_plan(
    project_root: Path,
    options: RemoteDeployOptions,
) -> list[DeployCommand]:
    remote_home = _resolve_remote_home(options)
    remote_dir_abs = _resolve_remote_path(options.remote_dir, remote_home)
    data_dir_abs = _resolve_remote_path(options.data_dir, remote_home)
    config_path_abs = _resolve_remote_path(options.config_path, remote_home)
    launch_agent_path = f"{remote_home}/Library/LaunchAgents/{options.label}.plist"
    logs_dir = f"{remote_dir_abs}/logs"
    project_root_abs = project_root.resolve()

    commands = [
        DeployCommand(
            label="prepare_directories",
            argv=_ssh_shell_command(
                options,
                "mkdir -p "
                f"{shlex.quote(remote_dir_abs)} "
                f"{shlex.quote(data_dir_abs)} "
                f"{shlex.quote(logs_dir)} "
                f"{shlex.quote(f'{remote_home}/Library/LaunchAgents')}",
            ),
        ),
        DeployCommand(
            label="sync_code",
            argv=[
                options.rsync_bin,
                "-az",
                "--delete",
                "--exclude=.git/",
                "--exclude=.venv/",
                "--exclude=.live-note/",
                "--exclude=.superpowers/",
                "--exclude=__pycache__/",
                "--exclude=*.pyc",
                "--exclude=.DS_Store",
                "--exclude=config.toml",
                "--exclude=.env",
                "--exclude=logs/",
                f"{project_root_abs}/",
                f"{options.host}:{options.remote_dir.rstrip('/')}/",
            ],
        ),
    ]

    if not options.skip_deps:
        install_target = ".[dev,speaker]" if options.speaker else ".[dev]"
        commands.append(
            DeployCommand(
                label="install_dependencies",
                argv=_ssh_shell_command(
                    options,
                    " && ".join(
                        [
                            f"cd {shlex.quote(remote_dir_abs)}",
                            f"{shlex.quote(options.python_bin)} -m venv .venv",
                            ".venv/bin/pip install --upgrade pip",
                            f".venv/bin/pip install -e {shlex.quote(install_target)}",
                        ]
                    ),
                ),
            )
        )

    commands.append(
        DeployCommand(
            label="prepare_config",
            argv=_ssh_shell_command(
                options,
                " && ".join(
                    [
                        f"mkdir -p {shlex.quote(Path(config_path_abs).parent.as_posix())}",
                        f"if [ ! -f {shlex.quote(config_path_abs)} ]; then "
                        f"cp {shlex.quote(f'{remote_dir_abs}/config.remote.example.toml')} "
                        f"{shlex.quote(config_path_abs)}; fi",
                    ]
                ),
            ),
        )
    )

    plist_payload = _launch_agent_plist(
        label=options.label,
        remote_dir=remote_dir_abs,
        config_path=config_path_abs,
        stdout_path=f"{logs_dir}/launchd.out",
        stderr_path=f"{logs_dir}/launchd.err",
    )
    plist_b64 = base64.b64encode(plist_payload).decode("ascii")
    commands.append(
        DeployCommand(
            label="install_launch_agent",
            argv=_ssh_shell_command(
                options,
                "\n".join(
                    [
                        f"{shlex.quote(options.python_bin)} - <<'PY'",
                        "from base64 import b64decode",
                        "from pathlib import Path",
                        f"target = Path({launch_agent_path!r})",
                        "target.parent.mkdir(parents=True, exist_ok=True)",
                        f"target.write_bytes(b64decode({plist_b64!r}))",
                        "PY",
                    ]
                ),
            ),
        )
    )

    if options.start_service:
        bootout_command = (
            f"launchctl bootout gui/$(id -u) {shlex.quote(launch_agent_path)} "
            ">/dev/null 2>&1 || true"
        )
        commands.append(
            DeployCommand(
                label="restart_launch_agent",
                argv=_ssh_shell_command(
                    options,
                    "\n".join(
                        [
                            bootout_command,
                            f"launchctl bootstrap gui/$(id -u) {shlex.quote(launch_agent_path)}",
                            f"launchctl kickstart -k gui/$(id -u)/{shlex.quote(options.label)}",
                        ]
                    ),
                ),
            )
        )

    return commands


def deploy_remote_service(
    *,
    project_root: Path,
    options: RemoteDeployOptions,
    runner: Callable[[DeployCommand], None] | None = None,
) -> int:
    commands = build_remote_deploy_plan(project_root, options)
    execute = runner or _run_command
    for command in commands:
        if options.dry_run:
            print(f"[dry-run] {command.label}")
            print(" ".join(shlex.quote(part) for part in command.argv))
            continue
        execute(command)
    return 0


def _run_command(command: DeployCommand) -> None:
    subprocess.run(command.argv, check=True)


def _ssh_shell_command(options: RemoteDeployOptions, script: str) -> list[str]:
    return [
        options.ssh_bin,
        options.host,
        f"sh -lc {shlex.quote(script)}",
    ]


def _resolve_remote_home(options: RemoteDeployOptions) -> str:
    if options.remote_home:
        return options.remote_home.rstrip("/")
    user = options.host.split("@", 1)[0] if "@" in options.host else getpass.getuser()
    return f"/Users/{user}"


def _resolve_remote_path(path: str, remote_home: str) -> str:
    stripped = path.strip()
    if not stripped:
        return remote_home
    if stripped == "~":
        return remote_home
    if stripped.startswith("~/"):
        return f"{remote_home}/{stripped[2:]}"
    if stripped.startswith("/"):
        return stripped.rstrip("/")
    return f"{remote_home}/{stripped.rstrip('/')}"


def _launch_agent_plist(
    *,
    label: str,
    remote_dir: str,
    config_path: str,
    stdout_path: str,
    stderr_path: str,
) -> bytes:
    return plistlib.dumps(
        {
            "Label": label,
            "ProgramArguments": [
                f"{remote_dir}/.venv/bin/python",
                "-m",
                "live_note",
                "--config",
                config_path,
                "serve",
            ],
            "WorkingDirectory": remote_dir,
            "EnvironmentVariables": {
                "PYTHONPATH": "src",
            },
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": stdout_path,
            "StandardErrorPath": stderr_path,
        }
    )
