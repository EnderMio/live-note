from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import patch

from live_note.app.qt_backdrop import (
    FallbackBackdropController,
    MacOSBackdropController,
    create_backdrop_controller,
)


class QtBackdropTests(unittest.TestCase):
    def test_create_backdrop_controller_returns_fallback_off_macos(self) -> None:
        controller = create_backdrop_controller(platform="linux")

        self.assertIsInstance(controller, FallbackBackdropController)
        self.assertFalse(controller.state.active)
        self.assertIn("当前平台", controller.state.detail)

    def test_create_backdrop_controller_returns_fallback_without_pyobjc(self) -> None:
        with patch("live_note.app.qt_backdrop.find_spec", return_value=None):
            controller = create_backdrop_controller(platform="darwin")

        self.assertIsInstance(controller, FallbackBackdropController)
        self.assertIn("PyObjC", controller.state.detail)

    def test_create_backdrop_controller_returns_macos_controller_when_pyobjc_available(
        self,
    ) -> None:
        with patch("live_note.app.qt_backdrop.find_spec", return_value=object()):
            controller = create_backdrop_controller(platform="darwin")

        self.assertIsInstance(controller, MacOSBackdropController)

    def test_macos_controller_install_falls_back_when_bridge_fails(self) -> None:
        controller = MacOSBackdropController()

        cocoa = types.ModuleType("Cocoa")
        cocoa.NSAppearance = types.SimpleNamespace(appearanceNamed_=lambda name: object())
        cocoa.NSAppearanceNameVibrantLight = "light"
        cocoa.NSColor = types.SimpleNamespace(clearColor=lambda: object())
        cocoa.NSViewHeightSizable = 1
        cocoa.NSViewWidthSizable = 2
        cocoa.NSVisualEffectBlendingModeBehindWindow = 3
        cocoa.NSVisualEffectMaterialSidebar = 4
        cocoa.NSVisualEffectStateActive = 5
        cocoa.NSVisualEffectView = object()
        cocoa.NSWindowBelow = 6

        def fail_bridge(*, c_void_p: int) -> object:
            raise RuntimeError(f"bridge failed: {c_void_p}")

        objc = types.ModuleType("objc")
        objc.objc_object = fail_bridge

        class DummyWidget:
            def winId(self) -> int:
                return 1

        with patch.dict(sys.modules, {"Cocoa": cocoa, "objc": objc}):
            state = controller.install(DummyWidget())

        self.assertFalse(state.active)
        self.assertIsInstance(controller, MacOSBackdropController)
        self.assertEqual(state.label, "Qt Fallback")
        self.assertIn("bridge failed", state.detail)

    def test_macos_controller_install_succeeds_with_keyword_bridge(self) -> None:
        controller = MacOSBackdropController()

        class DummyLayer:
            def __init__(self) -> None:
                self.opaque = None

            def setOpaque_(self, value: bool) -> None:
                self.opaque = value

        class DummyHostView:
            pass

        class DummyWindow:
            def __init__(self, host_view: DummyHostView) -> None:
                self._host_view = host_view
                self.opaque = None
                self.background = None
                self.movable = None

            def contentView(self) -> DummyHostView:
                return self._host_view

            def setOpaque_(self, value: bool) -> None:
                self.opaque = value

            def setBackgroundColor_(self, value: object) -> None:
                self.background = value

            def setMovableByWindowBackground_(self, value: bool) -> None:
                self.movable = value

        class DummyNSView:
            def __init__(self, window: DummyWindow, superview: DummyHostView) -> None:
                self._window = window
                self._superview = superview
                self.wants_layer = None
                self._layer = DummyLayer()

            def window(self) -> DummyWindow:
                return self._window

            def superview(self) -> DummyHostView:
                return self._superview

            def setWantsLayer_(self, value: bool) -> None:
                self.wants_layer = value

            def layer(self) -> DummyLayer:
                return self._layer

        cocoa = types.ModuleType("Cocoa")
        cocoa.NSColor = types.SimpleNamespace(clearColor=lambda: "clear")

        host_view = DummyHostView()
        ns_window = DummyWindow(host_view)
        ns_view = DummyNSView(ns_window, host_view)
        objc_calls: list[int] = []

        def bridge(*, c_void_p: int) -> DummyNSView:
            objc_calls.append(c_void_p)
            return ns_view

        objc = types.ModuleType("objc")
        objc.objc_object = bridge

        class DummyWidget:
            def winId(self) -> int:
                return 1

        with patch.dict(sys.modules, {"Cocoa": cocoa, "objc": objc}):
            state = controller.install(DummyWidget())

        self.assertTrue(state.active)
        self.assertEqual(state.label, "macOS Clear Shell")
        self.assertEqual(objc_calls, [1])
        self.assertEqual(ns_window.opaque, False)
        self.assertEqual(ns_window.background, "clear")
        self.assertEqual(ns_window.movable, False)
        self.assertEqual(ns_view.wants_layer, True)
        self.assertEqual(ns_view.layer().opaque, False)
