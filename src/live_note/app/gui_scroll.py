from __future__ import annotations

import tkinter as tk


def bind_mousewheel_scrolling(root: tk.Misc, canvas: tk.Canvas) -> None:
    def _blocks_outer_scroll(widget: tk.Misc) -> bool:
        widget_name = str(getattr(widget, "widgetName", "")).lower()
        if widget_name in {"canvas", "text", "listbox", "ttk::treeview"}:
            return True
        yview = getattr(widget, "yview", None)
        return callable(yview)

    def _is_descendant_of_canvas(widget: tk.Misc | None) -> bool:
        current = widget
        while current is not None:
            if current is canvas:
                return True
            if _blocks_outer_scroll(current):
                return False
            current = getattr(current, "master", None)
        return False

    def _scroll(event: tk.Event[tk.Misc]) -> None:
        try:
            widget_under_pointer = canvas.winfo_containing(event.x_root, event.y_root)
        except (tk.TclError, KeyError):
            return
        if not _is_descendant_of_canvas(widget_under_pointer):
            return
        if getattr(event, "num", None) == 4:
            delta = -1
        elif getattr(event, "num", None) == 5:
            delta = 1
        elif getattr(event, "delta", 0):
            delta = -1 if event.delta > 0 else 1
        else:
            return
        canvas.yview_scroll(delta, "units")

    root.bind_all("<MouseWheel>", _scroll, add="+")
    root.bind_all("<Button-4>", _scroll, add="+")
    root.bind_all("<Button-5>", _scroll, add="+")
