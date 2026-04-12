from __future__ import annotations

from collections.abc import Callable
from tkinter import messagebox

import pytest


def _unexpected_messagebox(name: str) -> Callable[..., object]:
    def _raise(*_args: object, **_kwargs: object) -> object:
        raise AssertionError(
            f"unexpected GUI messagebox.{name} call in test; patch it explicitly"
        )

    return _raise


@pytest.fixture(autouse=True)
def _fail_on_real_gui_messageboxes(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("showinfo", "showwarning", "showerror", "askyesno"):
        monkeypatch.setattr(messagebox, name, _unexpected_messagebox(name))
