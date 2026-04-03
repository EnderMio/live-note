from __future__ import annotations

import base64
import plistlib
import tempfile
import unittest
from pathlib import Path

from live_note.remote.deploy import (
    RemoteDeployOptions,
    _resolve_python_bin,
    build_remote_deploy_plan,
    deploy_remote_service,
)


class RemoteDeployTests(unittest.TestCase):
    def test_build_plan_includes_speaker_dependencies_and_launch_agent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            options = RemoteDeployOptions(
                host="ender@172.21.0.159",
                remote_dir="~/live-note",
                data_dir="~/Library/Application Support/live-note",
                config_path="~/Library/Application Support/live-note/config.remote.toml",
                label="com.live-note.remote",
                speaker=True,
            )

            commands = build_remote_deploy_plan(root, options)

        labels = [item.label for item in commands]
        self.assertEqual(
            [
                "prepare_directories",
                "sync_code",
                "install_dependencies",
                "prepare_config",
                "install_launch_agent",
                "restart_launch_agent",
            ],
            labels,
        )
        prepare_command = next(item for item in commands if item.label == "prepare_directories")
        self.assertEqual(["ssh", "ender@172.21.0.159"], prepare_command.argv[:2])
        self.assertEqual(3, len(prepare_command.argv))
        self.assertIn("sh -lc", prepare_command.argv[2])
        self.assertIn("Application Support/live-note", prepare_command.argv[2])
        install_command = next(item for item in commands if item.label == "install_dependencies")
        self.assertEqual(3, len(install_command.argv))
        self.assertIn(".venv/bin/pip install -e", install_command.argv[2])
        self.assertIn(".[dev,speaker]", install_command.argv[2])
        service_command = next(item for item in commands if item.label == "install_launch_agent")
        self.assertEqual(3, len(service_command.argv))
        self.assertIn("com.live-note.remote", service_command.argv[2])
        encoded = service_command.argv[2].split("b64decode(", 1)[1].split(")", 1)[0].strip("'\"")
        plist_payload = plistlib.loads(base64.b64decode(encoded))
        self.assertEqual("com.live-note.remote", plist_payload["Label"])
        self.assertIn("config.remote.toml", plist_payload["ProgramArguments"][4])
        self.assertIn(
            "Application Support/live-note/logs/launchd.out", plist_payload["StandardOutPath"]
        )

    def test_build_plan_can_install_pyannote_speaker_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            options = RemoteDeployOptions(
                host="ender@172.21.0.159",
                speaker_pyannote=True,
            )

            commands = build_remote_deploy_plan(root, options)

        install_command = next(item for item in commands if item.label == "install_dependencies")
        self.assertIn(".[dev,speaker,speaker-pyannote]", install_command.argv[2])

    def test_deploy_remote_service_executes_plan_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            options = RemoteDeployOptions(
                host="ender@172.21.0.159",
                skip_deps=True,
                start_service=False,
            )
            executed: list[tuple[str, list[str]]] = []

            exit_code = deploy_remote_service(
                project_root=root,
                options=options,
                runner=lambda command: executed.append((command.label, command.argv)),
            )

        self.assertEqual(0, exit_code)
        self.assertEqual(
            ["prepare_directories", "sync_code", "prepare_config", "install_launch_agent"],
            [label for label, _ in executed],
        )
        rsync_argv = dict(executed)["sync_code"]
        self.assertEqual("rsync", rsync_argv[0])
        self.assertIn("--delete", rsync_argv)
        self.assertTrue(rsync_argv[-2].endswith("/"))
        self.assertEqual("ender@172.21.0.159:~/live-note/", rsync_argv[-1])

    def test_build_plan_can_install_optional_funasr_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            options = RemoteDeployOptions(
                host="ender@172.21.0.159",
                funasr=True,
                funasr_dir="~/live-note-funasr",
                funasr_label="com.live-note.funasr",
                funasr_port=10095,
            )

            commands = build_remote_deploy_plan(root, options)

        labels = [item.label for item in commands]
        self.assertEqual(
            [
                "prepare_directories",
                "sync_code",
                "install_dependencies",
                "prepare_config",
                "install_funasr_runtime",
                "install_funasr_launch_agent",
                "restart_funasr_launch_agent",
                "install_launch_agent",
                "restart_launch_agent",
            ],
            labels,
        )
        prepare_command = next(item for item in commands if item.label == "prepare_directories")
        self.assertIn("live-note-funasr", prepare_command.argv[2])
        runtime_command = next(item for item in commands if item.label == "install_funasr_runtime")
        self.assertIn("https://github.com/alibaba/FunASR.git", runtime_command.argv[2])
        self.assertNotIn("pull --ff-only", runtime_command.argv[2])
        self.assertIn("pip install --upgrade pip setuptools wheel", runtime_command.argv[2])
        self.assertIn(
            "pip install -U modelscope funasr torch torchaudio",
            runtime_command.argv[2],
        )
        self.assertIn("requirements_server.txt", runtime_command.argv[2])
        launch_command = next(
            item for item in commands if item.label == "install_funasr_launch_agent"
        )
        encoded = launch_command.argv[2].split("b64decode(", 1)[1].split(")", 1)[0].strip("'\"")
        plist_payload = plistlib.loads(base64.b64decode(encoded))
        self.assertEqual("com.live-note.funasr", plist_payload["Label"])
        self.assertEqual("127.0.0.1", plist_payload["ProgramArguments"][3])
        self.assertEqual("10095", plist_payload["ProgramArguments"][5])
        self.assertTrue(plist_payload["ProgramArguments"][1].endswith("funasr_wss_server.py"))
        self.assertIn("--certfile", plist_payload["ProgramArguments"])
        self.assertIn("--keyfile", plist_payload["ProgramArguments"])

    def test_resolve_python_bin_prefers_existing_remote_venv_base_python(self) -> None:
        options = RemoteDeployOptions(
            host="ender@172.21.0.159",
            python_bin="python3",
        )

        resolved = _resolve_python_bin(
            options,
            probe=lambda _: "/opt/miniconda3/bin/python3.13",
        )

        self.assertEqual("/opt/miniconda3/bin/python3.13", resolved.python_bin)

    def test_resolve_python_bin_keeps_explicit_python_choice(self) -> None:
        options = RemoteDeployOptions(
            host="ender@172.21.0.159",
            python_bin="/opt/homebrew/bin/python3.13",
        )

        resolved = _resolve_python_bin(
            options,
            probe=lambda _: "/opt/miniconda3/bin/python3.13",
        )

        self.assertEqual("/opt/homebrew/bin/python3.13", resolved.python_bin)
