from __future__ import annotations

import os
import unittest
from importlib.util import find_spec
from types import SimpleNamespace

if find_spec("PySide6") is None:
    raise unittest.SkipTest("PySide6 未安装")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication, QFrame, QPushButton, QTableWidget, QToolButton

from live_note.app.gui_qt_preview import (
    HistoryPage,
    NewSessionPage,
    PreviewSessionStateKind,
    PreviewWindow,
    value_text_role,
)


class GuiQtPreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_preview_window_starts_with_home_and_closed_drawer(self) -> None:
        window = PreviewWindow()

        self.assertTrue(window.drawer_frame.isHidden())
        self.assertIsNone(window._active_panel)
        self.assertFalse(window.top_primary_button.isHidden())
        self.assertEqual(window.new_session_page.current_state.kind, PreviewSessionStateKind.IDLE)
        self.assertTrue(all(not button.isChecked() for button in window.nav_buttons.values()))
        self.assertFalse(window.windowIcon().isNull())

        window.close()

    def test_nav_button_toggles_drawer_open_and_closed(self) -> None:
        window = PreviewWindow()
        history_button = window.nav_buttons["history"]

        history_button.click()

        self.assertFalse(window.drawer_frame.isHidden())
        self.assertEqual(window._active_panel, "history")
        self.assertEqual(window.stack.currentWidget(), window.history_page)
        self.assertTrue(history_button.isChecked())

        history_button.click()

        self.assertTrue(window.drawer_frame.isHidden())
        self.assertIsNone(window._active_panel)
        self.assertFalse(history_button.isChecked())

        window.close()

    def test_top_primary_button_returns_to_home_and_resets_state(self) -> None:
        window = PreviewWindow()
        window.new_session_page.set_preview_state(PreviewSessionStateKind.RECORDING)
        window.nav_buttons["settings"].click()

        self.assertEqual(window._active_panel, "settings")

        window.top_primary_button.click()

        self.assertTrue(window.drawer_frame.isHidden())
        self.assertIsNone(window._active_panel)
        self.assertEqual(window.new_session_page.current_state.kind, PreviewSessionStateKind.IDLE)
        self.assertTrue(window.new_session_page.live_mode_button.isChecked())

        window.close()

    def test_escape_closes_active_drawer(self) -> None:
        window = PreviewWindow()
        window.nav_buttons["settings"].click()

        event = QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier)
        window.keyPressEvent(event)

        self.assertTrue(window.drawer_frame.isHidden())
        self.assertIsNone(window._active_panel)

        window.close()

    def test_new_session_page_primary_and_secondary_actions_cycle_preview_states(self) -> None:
        page = NewSessionPage(on_open_history=lambda: None)

        page.primary_action_button.click()
        self.assertEqual(page.current_state.kind, PreviewSessionStateKind.RECORDING)
        self.assertFalse(page.secondary_action_button.isHidden())
        self.assertEqual(page.primary_action_button.accessibleName(), "结束记录")

        page.secondary_action_button.click()
        self.assertEqual(page.current_state.kind, PreviewSessionStateKind.PAUSED)
        self.assertEqual(page.primary_action_button.accessibleName(), "继续记录")
        self.assertEqual(page.secondary_action_button.accessibleName(), "结束并整理")

        page.secondary_action_button.click()
        self.assertEqual(page.current_state.kind, PreviewSessionStateKind.BACKGROUND_FINISHING)
        self.assertTrue(page.secondary_action_button.isHidden())
        self.assertFalse(page.tertiary_action_button.isHidden())
        self.assertEqual(page.tertiary_action_button.accessibleName(), "打开记录库")

    def test_idle_file_mode_switches_primary_action_to_import(self) -> None:
        page = NewSessionPage(on_open_history=lambda: None)

        page.file_mode_button.click()

        self.assertTrue(page.file_mode_button.isChecked())
        self.assertEqual(page.primary_action_button.accessibleName(), "导入录音")
        self.assertEqual(page.tertiary_action_button.accessibleName(), "现场记录")
        self.assertIn("导入后", page.mode_hint.text())

    def test_new_session_page_idle_state_keeps_single_start_entry(self) -> None:
        page = NewSessionPage(on_open_history=lambda: None)

        self.assertTrue(page.secondary_action_button.isHidden())
        self.assertFalse(page.tertiary_action_button.isHidden())
        self.assertEqual(page.primary_action_button.property("buttonScale"), "hero")
        self.assertEqual(page.state_badge.text(), "空闲")

    def test_background_finishing_tertiary_action_opens_history(self) -> None:
        calls: list[str] = []
        page = NewSessionPage(on_open_history=lambda: calls.append("history"))
        page.set_preview_state(PreviewSessionStateKind.BACKGROUND_FINISHING)

        page.tertiary_action_button.click()

        self.assertEqual(calls, ["history"])

    def test_history_page_uses_two_primary_actions_and_more_menu(self) -> None:
        page = HistoryPage()

        push_texts = {button.accessibleName() for button in page.findChildren(QPushButton)}
        tool_texts = {button.accessibleName() for button in page.findChildren(QToolButton)}

        self.assertIn("打开笔记", push_texts)
        self.assertIn("重新整理", push_texts)
        self.assertIn("更多操作", tool_texts)

    def test_history_page_uses_library_cards_instead_of_table(self) -> None:
        page = HistoryPage()

        library_rows = [
            frame for frame in page.findChildren(QFrame) if frame.objectName() == "LibraryRow"
        ]

        self.assertEqual(page.findChildren(QTableWidget), [])
        self.assertEqual(len(library_rows), 3)
        self.assertTrue(any(bool(frame.property("selected")) for frame in library_rows))

    def test_settings_page_receives_backdrop_state_after_install(self) -> None:
        window = PreviewWindow()
        state = SimpleNamespace(
            active=True,
            label="macOS Clear Shell",
            detail="已启用 macOS 原生透明窗口外壳；内容层仍由 Qt 半透明卡片负责。",
        )

        self.assertTrue(window.settings_page.diagnostics_body.isHidden())
        window.settings_page.set_backdrop_state(state)

        self.assertEqual(window.settings_page.window_shell_badge.text(), "macOS Clear Shell")
        self.assertEqual(window.settings_page.window_shell_badge.property("pillTone"), "success")
        self.assertIn("透明窗口外壳", window.settings_page.window_shell_detail.text())

        window.close()

    def test_value_text_role_marks_structured_technical_values(self) -> None:
        self.assertEqual(value_text_role("Base URL", "https://127.0.0.1:27124"), "TechValue")
        self.assertEqual(value_text_role("Session ID", "20260316-140500-产品周会"), "TechValue")
        self.assertEqual(value_text_role("标题", "计量经济学 Week 3"), "BodyStrong")
