from __future__ import annotations

import sys
from dataclasses import dataclass
from importlib.util import find_spec
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget


@dataclass(slots=True)
class BackdropState:
    active: bool
    label: str
    detail: str


class BackdropController:
    def __init__(self) -> None:
        self.state = BackdropState(
            active=False,
            label="Qt Fallback",
            detail="使用透明外壳和半透明卡片，不依赖系统级毛玻璃。",
        )

    def install(self, widget: QWidget) -> BackdropState:
        return self.state


class FallbackBackdropController(BackdropController):
    def __init__(self, detail: str | None = None) -> None:
        super().__init__()
        if detail:
            self.state.detail = detail


class MacOSBackdropController(BackdropController):
    def __init__(self) -> None:
        super().__init__()
        self.state = BackdropState(
            active=False,
            label="macOS Clear Shell",
            detail="准备启用 macOS 原生透明外壳。",
        )

    def install(self, widget: QWidget) -> BackdropState:
        try:
            import objc
            from Cocoa import NSColor
        except Exception as exc:
            self.state = BackdropState(
                active=False,
                label="Qt Fallback",
                detail=f"PyObjC 不可用，已回退到 Qt 半透明卡片。{exc}",
            )
            return self.state

        try:
            widget.winId()
            ns_view = objc.objc_object(c_void_p=int(widget.winId()))
            ns_window = ns_view.window()
            ns_window.setOpaque_(False)
            ns_window.setBackgroundColor_(NSColor.clearColor())
            ns_window.setMovableByWindowBackground_(False)
            if hasattr(ns_view, "setWantsLayer_"):
                ns_view.setWantsLayer_(True)
                layer = ns_view.layer()
                if layer is not None:
                    layer.setOpaque_(False)
            self.state = BackdropState(
                active=True,
                label="macOS Clear Shell",
                detail="已启用 macOS 原生透明外壳；内容层由 Qt 卡片承接。",
            )
        except Exception as exc:
            self.state = BackdropState(
                active=False,
                label="Qt Fallback",
                detail=f"启用 macOS 透明外壳失败，已回退。{exc}",
            )
        return self.state


def create_backdrop_controller(platform: str | None = None) -> BackdropController:
    current_platform = platform or sys.platform
    if current_platform == "darwin" and find_spec("Cocoa") and find_spec("objc"):
        return MacOSBackdropController()
    if current_platform == "darwin":
        return FallbackBackdropController(
            "未安装 PyObjC，当前只启用 Qt 半透明外壳。"
            "安装 [gui] extra 后可启用 macOS 原生透明窗口外壳。"
        )
    return FallbackBackdropController(
        "当前平台使用 Qt 半透明浮层卡片；macOS 会额外启用原生透明窗口外壳。"
    )
