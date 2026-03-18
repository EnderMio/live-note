from __future__ import annotations

import base64
import plistlib
import tempfile
import unittest
from pathlib import Path

from live_note.remote.deploy import (
    RemoteDeployOptions,
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
        self.assertIn("launchd.out", plist_payload["StandardOutPath"])

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
