from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from live_note.app.gui import (
    LiveNoteGui,
    _language_code_to_display,
    _normalize_language_value,
    _optional_language_override,
)


class GuiLanguageTests(unittest.TestCase):
    def test_normalize_language_value_maps_mixed_language_label_to_auto(self) -> None:
        self.assertEqual("auto", _normalize_language_value("自动识别 / 中英混合 / 多语言（auto）"))

    def test_normalize_language_value_allows_custom_language_code(self) -> None:
        self.assertEqual("fr", _normalize_language_value("fr"))

    def test_optional_language_override_uses_none_for_default_label(self) -> None:
        self.assertIsNone(_optional_language_override("沿用默认设置"))

    def test_language_code_to_display_returns_known_label(self) -> None:
        self.assertEqual("中文（zh）", _language_code_to_display("zh", allow_blank=False))


class GuiHistoryTests(unittest.TestCase):
    def test_selected_summary_skips_prompt_when_prompt_disabled(self) -> None:
        gui = LiveNoteGui.__new__(LiveNoteGui)
        gui.history_tree = SimpleNamespace(selection=lambda: ())
        gui.history_rows = {}

        with patch("live_note.app.gui.messagebox.showinfo") as showinfo_mock:
            summary = gui._selected_summary(prompt=False)

        self.assertIsNone(summary)
        showinfo_mock.assert_not_called()

    def test_selected_summary_prompts_when_requested(self) -> None:
        gui = LiveNoteGui.__new__(LiveNoteGui)
        gui.history_tree = SimpleNamespace(selection=lambda: ())
        gui.history_rows = {}

        with patch("live_note.app.gui.messagebox.showinfo") as showinfo_mock:
            summary = gui._selected_summary()

        self.assertIsNone(summary)
        showinfo_mock.assert_called_once_with("请选择会话", "请先从历史列表中选择一条会话。")

    def test_selected_summaries_require_multiple_items_when_requested(self) -> None:
        gui = LiveNoteGui.__new__(LiveNoteGui)
        gui.history_tree = SimpleNamespace(selection=lambda: ("one",))
        gui.history_rows = {"one": SimpleNamespace(session_id="one")}

        with patch("live_note.app.gui.messagebox.showinfo") as showinfo_mock:
            summaries = gui._selected_summaries(min_count=2)

        self.assertIsNone(summaries)
        showinfo_mock.assert_called_once_with("请选择会话", "请先从历史列表中选择至少 2 条会话。")

    def test_selected_summaries_return_all_selected_rows(self) -> None:
        gui = LiveNoteGui.__new__(LiveNoteGui)
        first = SimpleNamespace(session_id="one")
        second = SimpleNamespace(session_id="two")
        gui.history_tree = SimpleNamespace(selection=lambda: ("one", "two"))
        gui.history_rows = {"one": first, "two": second}

        summaries = gui._selected_summaries(prompt=False, min_count=2)

        self.assertEqual([first, second], summaries)
