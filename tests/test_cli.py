from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from live_note.app.cli import main


class CliTests(unittest.TestCase):
    def test_import_command_dispatches_to_file_import_coordinator(self) -> None:
        runner = Mock()
        runner.run.return_value = 0

        with patch("live_note.app.cli.load_config", return_value=object()) as load_config_mock:
            with patch("live_note.app.cli.FileImportCoordinator", return_value=runner) as factory:
                exit_code = main(["import", "--file", "/tmp/demo.mp4", "--kind", "meeting"])

        self.assertEqual(0, exit_code)
        load_config_mock.assert_called_once()
        factory.assert_called_once_with(
            config=load_config_mock.return_value,
            file_path="/tmp/demo.mp4",
            title=None,
            kind="meeting",
            language=None,
        )
        runner.run.assert_called_once()

    def test_start_command_accepts_legacy_course_argument(self) -> None:
        runner = Mock()
        runner.run.return_value = 0

        with patch("live_note.app.cli.load_config", return_value=object()) as load_config_mock:
            with patch("live_note.app.cli.SessionCoordinator", return_value=runner) as factory:
                exit_code = main(
                    [
                        "start",
                        "--course",
                        "离散数学",
                        "--source",
                        "2",
                        "--profile",
                        "online",
                    ]
                )

        self.assertEqual(0, exit_code)
        factory.assert_called_once_with(
            config=load_config_mock.return_value,
            title="离散数学",
            source="2",
            kind="lecture",
            language=None,
        )
        runner.run.assert_called_once()

    def test_gui_command_dispatches_to_launcher(self) -> None:
        with patch("live_note.app.cli.launch_gui", return_value=0) as launch_gui_mock:
            exit_code = main(["--config", "/tmp/gui.toml", "gui"])

        self.assertEqual(0, exit_code)
        launch_gui_mock.assert_called_once_with(Path("/tmp/gui.toml"))

    def test_gui_preview_qt_command_dispatches_to_launcher(self) -> None:
        with patch("live_note.app.cli.launch_gui_preview_qt", return_value=0) as preview_mock:
            exit_code = main(["gui-preview-qt"])

        self.assertEqual(0, exit_code)
        preview_mock.assert_called_once_with()

    def test_doctor_uses_service_checks(self) -> None:
        service = Mock()
        service.doctor_checks.return_value = [
            SimpleNamespace(status="OK", name="config", detail="已加载 /tmp/config.toml"),
        ]

        with patch("live_note.app.cli.AppService", return_value=service):
            with patch("builtins.print") as print_mock:
                exit_code = main(["--config", "/tmp/config.toml", "doctor"])

        self.assertEqual(0, exit_code)
        service.doctor_checks.assert_called_once()
        print_mock.assert_called_once_with("[OK] config: 已加载 /tmp/config.toml")

    def test_retranscribe_command_dispatches_to_coordinator(self) -> None:
        with patch(
            "live_note.app.cli.load_config", return_value=object()
        ) as load_config_mock:
            with patch(
                "live_note.app.cli.retranscribe_session", return_value=0
            ) as retranscribe_mock:
                exit_code = main(["retranscribe", "--session", "20260315-210500-demo"])

        self.assertEqual(0, exit_code)
        load_config_mock.assert_called_once()
        retranscribe_mock.assert_called_once_with(
            load_config_mock.return_value,
            "20260315-210500-demo",
        )

    def test_refine_command_dispatches_to_coordinator(self) -> None:
        with patch("live_note.app.cli.load_config", return_value=object()) as load_config_mock:
            with patch("live_note.app.cli.refine_session", return_value=0) as refine_mock:
                exit_code = main(["refine", "--session", "20260315-210500-demo"])

        self.assertEqual(0, exit_code)
        load_config_mock.assert_called_once()
        refine_mock.assert_called_once_with(
            load_config_mock.return_value,
            "20260315-210500-demo",
        )

    def test_merge_command_dispatches_to_coordinator(self) -> None:
        with patch("live_note.app.cli.load_config", return_value=object()) as load_config_mock:
            with patch("live_note.app.cli.merge_sessions", return_value=0) as merge_mock:
                exit_code = main(
                    [
                        "merge",
                        "--session",
                        "20260315-210500-part1",
                        "--session",
                        "20260315-223000-part2",
                        "--title",
                        "产品周会（合并）",
                    ]
                )

        self.assertEqual(0, exit_code)
        load_config_mock.assert_called_once()
        merge_mock.assert_called_once_with(
            load_config_mock.return_value,
            ["20260315-210500-part1", "20260315-223000-part2"],
            title="产品周会（合并）",
        )
