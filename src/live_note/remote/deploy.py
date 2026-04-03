from __future__ import annotations

import base64
import getpass
import plistlib
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, replace
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
    speaker_pyannote: bool = False
    funasr: bool = False
    funasr_dir: str = "~/live-note-funasr"
    funasr_label: str = "com.live-note.funasr"
    funasr_repo_url: str = "https://github.com/alibaba/FunASR.git"
    funasr_host: str = "127.0.0.1"
    funasr_port: int = 10095
    funasr_ncpu: int = 4
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
    logs_dir = f"{data_dir_abs}/logs"
    funasr_dir_abs = _resolve_remote_path(options.funasr_dir, remote_home)
    funasr_repo_dir = f"{funasr_dir_abs}/FunASR"
    funasr_venv_dir = f"{funasr_dir_abs}/.venv"
    funasr_logs_dir = f"{funasr_dir_abs}/logs"
    funasr_launch_agent_path = f"{remote_home}/Library/LaunchAgents/{options.funasr_label}.plist"
    project_root_abs = project_root.resolve()
    prepare_directories = [
        remote_dir_abs,
        data_dir_abs,
        logs_dir,
        f"{remote_home}/Library/LaunchAgents",
    ]
    if options.funasr:
        prepare_directories.extend([funasr_dir_abs, funasr_logs_dir])

    commands = [
        DeployCommand(
            label="prepare_directories",
            argv=_ssh_shell_command(
                options,
                "mkdir -p " + " ".join(shlex.quote(item) for item in prepare_directories),
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
        install_extras = ["dev"]
        if options.speaker:
            install_extras.append("speaker")
        if options.speaker_pyannote:
            install_extras.extend(["speaker", "speaker-pyannote"])
        install_target = f".[{','.join(dict.fromkeys(install_extras))}]"
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

    if options.funasr:
        commands.append(
            DeployCommand(
                label="install_funasr_runtime",
                argv=_ssh_shell_command(
                    options,
                    " && ".join(
                        [
                            f"mkdir -p {shlex.quote(funasr_dir_abs)}",
                            (
                                f"if [ ! -d {shlex.quote(f'{funasr_repo_dir}/.git')} ]; then "
                                f"git clone --depth 1 "
                                f"{shlex.quote(options.funasr_repo_url)} "
                                f"{shlex.quote(funasr_repo_dir)}; fi"
                            ),
                            (
                                f"{shlex.quote(options.python_bin)} "
                                f"-m venv {shlex.quote(funasr_venv_dir)}"
                            ),
                            (
                                f"{shlex.quote(f'{funasr_venv_dir}/bin/pip')} install "
                                "--upgrade pip setuptools wheel"
                            ),
                            (
                                f"{shlex.quote(f'{funasr_venv_dir}/bin/pip')} install "
                                "-U modelscope funasr torch torchaudio"
                            ),
                            f"cd {shlex.quote(f'{funasr_repo_dir}/runtime/python/websocket')}",
                            (
                                f"{shlex.quote(f'{funasr_venv_dir}/bin/pip')} install "
                                "-r requirements_server.txt"
                            ),
                        ]
                    ),
                ),
            )
        )

        funasr_plist_payload = _funasr_launch_agent_plist(
            label=options.funasr_label,
            python_path=f"{funasr_venv_dir}/bin/python",
            script_path=f"{funasr_repo_dir}/runtime/python/websocket/funasr_wss_server.py",
            working_directory=f"{funasr_repo_dir}/runtime/python/websocket",
            host=options.funasr_host,
            port=options.funasr_port,
            ncpu=options.funasr_ncpu,
            stdout_path=f"{funasr_logs_dir}/launchd.out",
            stderr_path=f"{funasr_logs_dir}/launchd.err",
        )
        funasr_plist_b64 = base64.b64encode(funasr_plist_payload).decode("ascii")
        commands.append(
            DeployCommand(
                label="install_funasr_launch_agent",
                argv=_ssh_shell_command(
                    options,
                    "\n".join(
                        [
                            f"{shlex.quote(options.python_bin)} - <<'PY'",
                            "from base64 import b64decode",
                            "from pathlib import Path",
                            f"target = Path({funasr_launch_agent_path!r})",
                            "target.parent.mkdir(parents=True, exist_ok=True)",
                            f"target.write_bytes(b64decode({funasr_plist_b64!r}))",
                            "PY",
                        ]
                    ),
                ),
            )
        )

        if options.start_service:
            funasr_bootout_command = (
                f"launchctl bootout gui/$(id -u) {shlex.quote(funasr_launch_agent_path)} "
                ">/dev/null 2>&1 || true"
            )
            commands.append(
                DeployCommand(
                    label="restart_funasr_launch_agent",
                    argv=_ssh_shell_command(
                        options,
                        "\n".join(
                            [
                                funasr_bootout_command,
                                (
                                    "launchctl bootstrap gui/$(id -u) "
                                    f"{shlex.quote(funasr_launch_agent_path)}"
                                ),
                                (
                                    "launchctl kickstart -k "
                                    f"gui/$(id -u)/{shlex.quote(options.funasr_label)}"
                                ),
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
    resolved_options = options if options.dry_run else _resolve_python_bin(options)
    commands = build_remote_deploy_plan(project_root, resolved_options)
    execute = runner or _run_command
    for command in commands:
        if resolved_options.dry_run:
            print(f"[dry-run] {command.label}")
            print(" ".join(shlex.quote(part) for part in command.argv))
            continue
        execute(command)
    return 0


def _resolve_python_bin(
    options: RemoteDeployOptions,
    *,
    probe: Callable[[RemoteDeployOptions], str | None] | None = None,
) -> RemoteDeployOptions:
    if options.python_bin.strip() != "python3":
        return options
    python_bin = (probe or _probe_existing_remote_python_bin)(options)
    if not python_bin:
        return options
    return replace(options, python_bin=python_bin)


def _probe_existing_remote_python_bin(options: RemoteDeployOptions) -> str | None:
    remote_home = _resolve_remote_home(options)
    remote_dir_abs = _resolve_remote_path(options.remote_dir, remote_home)
    command = _ssh_shell_command(
        options,
        "\n".join(
            [
                f"if [ ! -x {shlex.quote(f'{remote_dir_abs}/.venv/bin/python')} ]; then",
                "  exit 0",
                "fi",
                f"{shlex.quote(f'{remote_dir_abs}/.venv/bin/python')} - <<'PY'",
                "import sys",
                'print(getattr(sys, "_base_executable", "") or sys.executable)',
                "PY",
            ]
        ),
    )
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return None
    candidate = completed.stdout.strip()
    if not candidate:
        return None
    return candidate.splitlines()[-1].strip() or None


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


def _funasr_launch_agent_plist(
    *,
    label: str,
    python_path: str,
    script_path: str,
    working_directory: str,
    host: str,
    port: int,
    ncpu: int,
    stdout_path: str,
    stderr_path: str,
) -> bytes:
    return plistlib.dumps(
        {
            "Label": label,
            "ProgramArguments": [
                python_path,
                script_path,
                "--host",
                host,
                "--port",
                str(port),
                "--certfile",
                "",
                "--keyfile",
                "",
                "--ngpu",
                "0",
                "--device",
                "cpu",
                "--ncpu",
                str(max(1, int(ncpu))),
            ],
            "WorkingDirectory": working_directory,
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": stdout_path,
            "StandardErrorPath": stderr_path,
        }
    )
