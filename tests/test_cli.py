from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from live_note.app.cli import build_parser, main


class CliTests(unittest.TestCase):
    def test_import_command_dispatches_via_app_service(self) -> None:
        service = Mock()
        service.import_audio_file.return_value = 0

        with patch("live_note.app.cli.AppService", return_value=service) as service_factory:
            exit_code = main(["import", "--file", "/tmp/demo.mp4", "--kind", "meeting"])

        self.assertEqual(0, exit_code)
        service_factory.assert_called_once_with(Path("config.toml"))
        service.import_audio_file.assert_called_once_with(
            file_path="/tmp/demo.mp4",
            title=None,
            kind="meeting",
            language=None,
        )

    def test_devices_command_dispatches_via_app_service(self) -> None:
        service = Mock()
        service.list_input_devices.return_value = [
            SimpleNamespace(index=1, name="Mic", max_input_channels=2, default_samplerate=48000.0)
        ]

        with patch("live_note.app.cli.AppService", return_value=service) as service_factory:
            with patch("builtins.print") as print_mock:
                exit_code = main(["devices"])

        self.assertEqual(0, exit_code)
        service_factory.assert_called_once_with(Path("config.toml"))
        service.list_input_devices.assert_called_once_with()
        print_mock.assert_called_once_with("  1  Mic  inputs=2  rate=48000")

    def test_start_command_dispatches_via_app_service(self) -> None:
        service = Mock()
        service.start_live_session.return_value = 0

        with patch("live_note.app.cli.AppService", return_value=service) as service_factory:
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
        service_factory.assert_called_once_with(Path("config.toml"))
        service.start_live_session.assert_called_once_with(
            title="离散数学",
            source="2",
            kind="lecture",
            language=None,
        )

    def test_gui_command_dispatches_to_launcher(self) -> None:
        with patch("live_note.app.cli.launch_gui", return_value=0) as launch_gui_mock:
            exit_code = main(["--config", "/tmp/gui.toml", "gui"])

        self.assertEqual(0, exit_code)
        launch_gui_mock.assert_called_once_with(Path("/tmp/gui.toml"))

    def test_gui_preview_qt_command_is_not_available(self) -> None:
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["gui-preview-qt"])

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

    def test_serve_command_dispatches_to_launcher(self) -> None:
        with patch("live_note.app.cli.launch_remote_server", return_value=0) as serve_mock:
            exit_code = main(["--config", "/tmp/live-note.toml", "serve"])

        self.assertEqual(0, exit_code)
        serve_mock.assert_called_once_with(Path("/tmp/live-note.toml"))

    def test_remote_deploy_command_dispatches_to_launcher(self) -> None:
        with patch("live_note.app.cli.launch_remote_deploy", return_value=0) as deploy_mock:
            exit_code = main(
                [
                    "remote-deploy",
                    "--host",
                    "ender@172.21.0.159",
                    "--speaker",
                    "--speaker-pyannote",
                    "--funasr",
                    "--funasr-port",
                    "11095",
                    "--skip-deps",
                    "--dry-run",
                ]
            )

        self.assertEqual(0, exit_code)
        deploy_mock.assert_called_once()
        args = deploy_mock.call_args.args[0]
        self.assertEqual("ender@172.21.0.159", args.host)
        self.assertTrue(args.speaker)
        self.assertTrue(args.speaker_pyannote)
        self.assertTrue(args.funasr)
        self.assertEqual(11095, args.funasr_port)
        self.assertTrue(args.skip_deps)
        self.assertTrue(args.dry_run)

    def test_finalize_command_dispatches_via_app_service(self) -> None:
        service = Mock()
        service.finalize.return_value = 0

        with patch("live_note.app.cli.AppService", return_value=service) as service_factory:
            exit_code = main(["finalize", "--session", "20260315-210500-demo"])

        self.assertEqual(0, exit_code)
        service_factory.assert_called_once_with(Path("config.toml"))
        service.finalize.assert_called_once_with("20260315-210500-demo")

    def test_retranscribe_command_dispatches_via_app_service(self) -> None:
        service = Mock()
        service.retranscribe.return_value = 0

        with patch("live_note.app.cli.AppService", return_value=service) as service_factory:
            exit_code = main(["retranscribe", "--session", "20260315-210500-demo"])

        self.assertEqual(0, exit_code)
        service_factory.assert_called_once_with(Path("config.toml"))
        service.retranscribe.assert_called_once_with("20260315-210500-demo")

    def test_refine_command_dispatches_via_app_service(self) -> None:
        service = Mock()
        service.refine.return_value = 0

        with patch("live_note.app.cli.AppService", return_value=service) as service_factory:
            exit_code = main(["refine", "--session", "20260315-210500-demo"])

        self.assertEqual(0, exit_code)
        service_factory.assert_called_once_with(Path("config.toml"))
        service.refine.assert_called_once_with("20260315-210500-demo")

    def test_merge_command_dispatches_via_app_service(self) -> None:
        service = Mock()
        service.merge.return_value = 0

        with patch("live_note.app.cli.AppService", return_value=service) as service_factory:
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
        service_factory.assert_called_once_with(Path("config.toml"))
        service.merge.assert_called_once_with(
            ["20260315-210500-part1", "20260315-223000-part2"],
            title="产品周会（合并）",
        )
