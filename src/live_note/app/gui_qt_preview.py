from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from string import Template

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QIcon,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QStyleFactory,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from live_note.branding import brand_logo_svg_path

from .qt_backdrop import create_backdrop_controller


@dataclass(frozen=True, slots=True)
class ResizeHandleSpec:
    edges: Qt.Edge | Qt.Edges
    cursor: Qt.CursorShape
    rect: tuple[int, int, int, int]


class PreviewSessionStateKind(StrEnum):
    IDLE = "idle"
    RECORDING = "recording"
    PAUSED = "paused"
    BACKGROUND_FINISHING = "background_finishing"


@dataclass(frozen=True, slots=True)
class PreviewSessionState:
    kind: PreviewSessionStateKind
    eyebrow: str
    hero_title: str
    hero_description: str
    primary_action: str
    secondary_action: str | None
    tertiary_action: str | None
    status_badge: str
    status_badge_tone: str
    status_heading: str
    status_summary: str
    pulse_caption: str
    pulse_value: str
    session_note: str
    session_meta: tuple[tuple[str, str], ...]
    next_step_title: str
    next_step_detail: str
    recent_events: tuple[str, ...]


WINDOW_MARGIN = 18
HANDLE_SIZE = 10
DEFAULT_BODY_FONT = "Arial"
DEFAULT_DISPLAY_FONT = "Arial"
DEFAULT_MONO_FONT = "Menlo"


@dataclass(frozen=True, slots=True)
class TypographyPalette:
    body_family: str
    display_family: str
    mono_family: str


ACTIVE_TYPOGRAPHY = TypographyPalette(
    body_family=DEFAULT_BODY_FONT,
    display_family=DEFAULT_DISPLAY_FONT,
    mono_family=DEFAULT_MONO_FONT,
)
TYPOGRAPHY_READY = False

SURFACE_THEMES = {
    "clear_shell": {
        "bar_bg": "rgba(246, 248, 252, 0.78)",
        "bar_border": "rgba(255, 255, 255, 0.56)",
        "card_bg": "rgba(247, 249, 252, 0.88)",
        "card_border": "rgba(255, 255, 255, 0.46)",
        "accent_0": "rgba(250, 252, 255, 0.92)",
        "accent_1": "rgba(236, 243, 255, 0.86)",
        "accent_2": "rgba(227, 238, 255, 0.92)",
        "accent_border": "rgba(255, 255, 255, 0.50)",
        "inset_bg": "rgba(255, 255, 255, 0.94)",
        "inset_border": "rgba(232, 238, 246, 0.74)",
        "table_header_bg": "rgba(242, 246, 250, 0.92)",
        "nav_hover_bg": "rgba(255, 255, 255, 0.74)",
        "nav_checked_bg": "rgba(255, 255, 255, 0.94)",
        "segment_bg": "rgba(255, 255, 255, 0.80)",
        "segment_checked_bg": "rgba(255, 255, 255, 0.96)",
        "secondary_bg": "rgba(255, 255, 255, 0.82)",
        "secondary_hover_bg": "rgba(255, 255, 255, 0.94)",
        "ghost_hover_bg": "rgba(255, 255, 255, 0.72)",
    },
    "fallback": {
        "bar_bg": "rgba(248, 250, 252, 0.92)",
        "bar_border": "rgba(255, 255, 255, 0.66)",
        "card_bg": "rgba(249, 250, 252, 0.96)",
        "card_border": "rgba(237, 242, 248, 0.88)",
        "accent_0": "rgba(251, 252, 255, 0.98)",
        "accent_1": "rgba(239, 245, 255, 0.96)",
        "accent_2": "rgba(230, 240, 255, 0.98)",
        "accent_border": "rgba(229, 236, 247, 0.88)",
        "inset_bg": "rgba(255, 255, 255, 0.98)",
        "inset_border": "rgba(232, 238, 246, 0.92)",
        "table_header_bg": "rgba(244, 247, 251, 0.98)",
        "nav_hover_bg": "rgba(255, 255, 255, 0.86)",
        "nav_checked_bg": "rgba(255, 255, 255, 1.0)",
        "segment_bg": "rgba(255, 255, 255, 0.90)",
        "segment_checked_bg": "rgba(255, 255, 255, 1.0)",
        "secondary_bg": "rgba(255, 255, 255, 0.90)",
        "secondary_hover_bg": "rgba(255, 255, 255, 1.0)",
        "ghost_hover_bg": "rgba(255, 255, 255, 0.84)",
    },
}


def surface_mode_from_backdrop(active: bool, label: str) -> str:
    if active and label == "macOS Clear Shell":
        return "clear_shell"
    return "fallback"


PREVIEW_KIND_LABELS = {
    "generic": "通用记录",
    "meeting": "会议记录",
    "lecture": "课程记录",
}

PREVIEW_INPUT_MODE_LABELS = {
    "live": "现场记录",
    "file": "导入录音",
}


PREVIEW_SESSION_STATES = {
    PreviewSessionStateKind.IDLE: PreviewSessionState(
        kind=PreviewSessionStateKind.IDLE,
        eyebrow="",
        hero_title="开始记录",
        hero_description="",
        primary_action="开始记录",
        secondary_action=None,
        tertiary_action=None,
        status_badge="空闲",
        status_badge_tone="soft",
        status_heading="空闲",
        status_summary="",
        pulse_caption="待开始",
        pulse_value="Ready",
        session_note="",
        session_meta=(
            ("记录方式", "现场记录"),
            ("记录类型", "课程记录"),
            ("语言", "自动识别"),
            ("完成后", "原文与整理"),
        ),
        next_step_title="会话笔记",
        next_step_detail="会话笔记",
        recent_events=(
            "当前没有活动记录。",
            "你也可以先用导入录音模式验证现有文件。",
        ),
    ),
    PreviewSessionStateKind.RECORDING: PreviewSessionState(
        kind=PreviewSessionStateKind.RECORDING,
        eyebrow="",
        hero_title="记录中",
        hero_description="",
        primary_action="结束记录",
        secondary_action="暂停",
        tertiary_action=None,
        status_badge="录制",
        status_badge_tone="accent",
        status_heading="录制中",
        status_summary="",
        pulse_caption="记录中",
        pulse_value="00:12:41",
        session_note="",
        session_meta=(
            ("记录方式", "现场记录"),
            ("输入来源", "BlackHole 2ch"),
            ("当前结果", "即时原文"),
            ("完成后", "后台整理"),
        ),
        next_step_title="原文与整理",
        next_step_detail="原文与整理",
        recent_events=(
            "22:03:29 检测到静音并切段。",
            "22:03:33 即时草稿已经持续写入。",
        ),
    ),
    PreviewSessionStateKind.PAUSED: PreviewSessionState(
        kind=PreviewSessionStateKind.PAUSED,
        eyebrow="",
        hero_title="已暂停",
        hero_description="",
        primary_action="继续记录",
        secondary_action="结束并整理",
        tertiary_action=None,
        status_badge="已暂停",
        status_badge_tone="soft",
        status_heading="暂停",
        status_summary="",
        pulse_caption="暂停中",
        pulse_value="Paused",
        session_note="",
        session_meta=(
            ("记录方式", "现场记录"),
            ("已记录", "18 段原文"),
            ("整段录音", "已保留"),
            ("完成后", "后台整理"),
        ),
        next_step_title="原文与整理",
        next_step_detail="原文与整理",
        recent_events=(
            "22:04:01 用户点击暂停。",
            "暂停期间不会继续写入新的片段。",
        ),
    ),
    PreviewSessionStateKind.BACKGROUND_FINISHING: PreviewSessionState(
        kind=PreviewSessionStateKind.BACKGROUND_FINISHING,
        eyebrow="",
        hero_title="后台整理中",
        hero_description="",
        primary_action="开始下一条",
        secondary_action=None,
        tertiary_action="打开记录库",
        status_badge="整理中",
        status_badge_tone="success",
        status_heading="整理中",
        status_summary="",
        pulse_caption="整理中",
        pulse_value="Finishing",
        session_note="",
        session_meta=(
            ("记录方式", "现场记录"),
            ("当前结果", "后台整理"),
            ("完成后", "原文与整理"),
            ("整理进度", "18 / 21 段已完成"),
        ),
        next_step_title="后台整理",
        next_step_detail="后台整理",
        recent_events=(
            "22:05:29 检测到停止请求并关闭前台录音。",
            "22:05:33 已将离线精修与整理加入后台队列。",
        ),
    ),
}


def build_stylesheet(surface_mode: str) -> str:
    theme = SURFACE_THEMES[surface_mode]
    typography = ACTIVE_TYPOGRAPHY
    stylesheet = Template(
        """
QMainWindow, QWidget#WindowRoot {
    background: transparent;
    color: #233141;
    font-family: "$body_family";
}
QWidget {
    color: #233141;
    font-size: 14px;
    font-family: "$body_family";
}
QLabel {
    color: transparent;
    font-size: 14px;
}
QLabel#UsableHeading {
    font-family: "$display_family";
    font-size: 24px;
    font-weight: 650;
    color: #223042;
}
QLabel#UsableTitle {
    font-size: 16px;
    font-weight: 640;
    color: #223042;
}
QLabel#UsableMeta {
    font-size: 13px;
    font-weight: 560;
    color: #70849A;
}
QLabel#UsableBody {
    font-size: 14px;
    font-weight: 560;
    color: #44586D;
}
QLabel#UsableStatus {
    font-size: 14px;
    font-weight: 640;
    color: #2B3E52;
}
QLabel#UsableSetting {
    font-size: 14px;
    font-weight: 620;
    color: #2B3E52;
}
QLabel#UsableValue {
    font-size: 13px;
    font-weight: 560;
    color: #607488;
}
QLabel#UsablePill {
    border-radius: 999px;
    padding: 5px 10px;
    font-size: 11px;
    font-weight: 650;
}
QLabel#UsablePill[pillTone="accent"] {
    background: rgba(225, 234, 255, 0.98);
    color: #4B79D9;
}
QLabel#UsablePill[pillTone="soft"] {
    background: rgba(248, 250, 252, 0.98);
    color: #78889A;
    border: 1px solid rgba(233, 239, 246, 0.88);
}
QLabel#UsablePill[pillTone="success"] {
    background: rgba(236, 249, 241, 0.98);
    color: #2A9B63;
}
QLabel#UsablePill[pillTone="danger"] {
    background: rgba(252, 238, 242, 0.98);
    color: #C25B78;
}
QFrame#Shell {
    background: transparent;
    border: none;
}
QFrame#Surface[surfaceRole="bar"] {
    background: $bar_bg;
    border: 1px solid $bar_border;
    border-radius: 22px;
}
QFrame#Surface[surfaceRole="card"][surfaceTone="base"] {
    background: $card_bg;
    border: 1px solid $card_border;
    border-radius: 26px;
}
QFrame#Surface[surfaceRole="card"][surfaceTone="accent"] {
    background: qlineargradient(
        x1:0, y1:0, x2:1, y2:1,
        stop:0 $accent_0,
        stop:0.56 $accent_1,
        stop:1 $accent_2
    );
    border: 1px solid $accent_border;
    border-radius: 26px;
}
QFrame#Surface[surfaceRole="inset"] {
    background: $inset_bg;
    border: 1px solid $inset_border;
    border-radius: 18px;
}
QFrame#LibraryRow,
QFrame#PreferenceCard {
    background: $inset_bg;
    border: 1px solid $inset_border;
    border-radius: 22px;
}
QFrame#NoteSheet {
    background: rgba(255, 255, 255, 0.985);
    border: 1px solid rgba(229, 236, 246, 0.92);
    border-radius: 28px;
}
QFrame#InlineCallout,
QFrame#ActionStrip {
    background: rgba(246, 250, 255, 0.92);
    border: 1px solid rgba(223, 233, 247, 0.9);
    border-radius: 20px;
}
QFrame#SettingsNavItem {
    background: rgba(255, 255, 255, 0.72);
    border: 1px solid rgba(232, 238, 246, 0.72);
    border-radius: 20px;
}
QFrame#SettingsNavItem[selected="true"] {
    background: qlineargradient(
        x1:0, y1:0, x2:1, y2:1,
        stop:0 rgba(252, 253, 255, 0.98),
        stop:1 rgba(236, 244, 255, 0.98)
    );
    border: 1px solid rgba(205, 221, 248, 0.92);
}
QFrame#LibraryRow[selected="true"] {
    background: qlineargradient(
        x1:0, y1:0, x2:1, y2:1,
        stop:0 rgba(252, 253, 255, 0.98),
        stop:1 rgba(235, 243, 255, 0.98)
    );
    border: 1px solid rgba(205, 221, 248, 0.92);
}
QFrame#Divider {
    background: rgba(223, 231, 241, 0.76);
    border: none;
}
QLabel#IconPlate {
    background: rgba(250, 252, 255, 0.98);
    border: 1px solid rgba(219, 229, 242, 0.98);
    border-radius: 16px;
}
QLabel#IconPlate[pillTone="accent"] {
    background: rgba(226, 236, 255, 1.0);
    border-color: rgba(194, 214, 251, 1.0);
}
QLabel#IconPlate[pillTone="soft"] {
    background: rgba(246, 249, 252, 1.0);
    border-color: rgba(223, 231, 241, 1.0);
}
QLabel#IconPlate[pillTone="success"] {
    background: rgba(235, 248, 240, 1.0);
    border-color: rgba(206, 233, 217, 1.0);
}
QLabel#IconPlate[pillTone="danger"] {
    background: rgba(253, 239, 243, 1.0);
    border-color: rgba(244, 210, 221, 1.0);
}
QLabel#BrandTitle {
    font-family: "$display_family";
    font-size: 18px;
    font-weight: 650;
    color: transparent;
}
QLabel#PageHeading {
    font-family: "$display_family";
    font-size: 22px;
    font-weight: 650;
    color: transparent;
}
QLabel#HeroHeadline {
    font-family: "$display_family";
    font-size: 32px;
    font-weight: 650;
    color: transparent;
}
QLabel#SectionTitle {
    font-family: "$display_family";
    font-size: 21px;
    font-weight: 650;
    color: transparent;
}
QLabel#CardTitle {
    font-size: 18px;
    font-weight: 600;
    color: transparent;
}
QLabel#BodyStrong {
    font-size: 15px;
    font-weight: 600;
    color: transparent;
}
QLabel#PreviewBody {
    font-size: 15px;
    color: transparent;
    line-height: 1.52;
}
QLabel#NoteTitle {
    font-family: "$display_family";
    font-size: 28px;
    font-weight: 650;
    color: transparent;
}
QLabel#NoteLead {
    font-size: 16px;
    font-weight: 600;
    color: transparent;
    line-height: 1.58;
}
QLabel#NoteBody {
    font-size: 15px;
    color: transparent;
    line-height: 1.6;
}
QLabel#NoteSectionTitle {
    font-size: 13px;
    font-weight: 650;
    color: transparent;
}
QLabel#ListMeta {
    color: transparent;
    font-size: 13px;
}
QLabel#TechValue {
    font-family: "$body_family";
    font-size: 13px;
    font-weight: 600;
    color: transparent;
}
QLabel#MetricValue {
    font-size: 30px;
    font-weight: 650;
    color: transparent;
}
QLabel#Eyebrow {
    color: transparent;
    font-size: 11px;
    font-weight: 650;
    text-transform: uppercase;
}
QLabel#MetaKey {
    color: transparent;
    font-size: 12px;
    font-weight: 600;
}
QLabel#MutedText {
    color: transparent;
    font-size: 14px;
    font-weight: 580;
}
QLabel#Pill {
    border-radius: 999px;
    padding: 6px 11px;
    font-size: 11px;
    font-weight: 600;
    color: transparent;
}
QLabel#Pill[pillTone="accent"] {
    background: rgba(225, 234, 255, 0.98);
}
QLabel#Pill[pillTone="soft"] {
    background: rgba(248, 250, 252, 0.98);
    border: 1px solid rgba(233, 239, 246, 0.88);
}
QLabel#Pill[pillTone="success"] {
    background: rgba(236, 249, 241, 0.98);
}
QLabel#Pill[pillTone="danger"] {
    background: rgba(252, 238, 242, 0.98);
}
QPushButton#CloseButton,
QPushButton#MinimizeButton,
QPushButton#ZoomButton {
    border-radius: 6px;
    border: none;
}
QPushButton#CloseButton { background: #FF5F57; }
QPushButton#MinimizeButton { background: #FEBC2E; }
QPushButton#ZoomButton { background: #28C840; }
QPushButton#NavButton {
    background: transparent;
    border: 1px solid transparent;
    border-radius: 16px;
    min-width: 42px;
    max-width: 42px;
    min-height: 42px;
    max-height: 42px;
    padding: 0;
    color: transparent;
    font-weight: 600;
}
QPushButton#NavButton[showLabel="true"] {
    min-width: 80px;
    max-width: none;
    padding: 0 12px;
    color: #6A7C8F;
}
QPushButton#NavButton:hover {
    background: $nav_hover_bg;
    border-color: rgba(233, 239, 246, 0.74);
}
QPushButton#NavButton:checked {
    background: $nav_checked_bg;
    border-color: rgba(229, 236, 246, 0.88);
    color: #243244;
}
QPushButton#SegmentButton {
    background: $segment_bg;
    border: 1px solid rgba(233, 239, 246, 0.74);
    border-radius: 18px;
    min-width: 72px;
    max-width: 72px;
    min-height: 52px;
    max-height: 52px;
    padding: 0;
    color: transparent;
    font-size: 13px;
    font-weight: 700;
}
QPushButton#SegmentButton[showLabel="true"] {
    min-width: 94px;
    max-width: none;
    padding: 0 14px;
    color: #65788D;
}
QPushButton#SegmentButton:checked {
    background: $segment_checked_bg;
    border-color: rgba(214, 228, 255, 0.74);
    color: transparent;
}
QPushButton#SegmentButton[showLabel="true"]:checked {
    color: #243244;
}
QPushButton#PrimaryButton,
QPushButton#SecondaryButton,
QPushButton#DangerButton,
QPushButton#GhostButton {
    border-radius: 17px;
    padding: 0;
    font-size: 13px;
    font-weight: 600;
    color: transparent;
    min-width: 44px;
    max-width: 44px;
    min-height: 44px;
    max-height: 44px;
}
QPushButton#PrimaryButton[showLabel="true"],
QPushButton#SecondaryButton[showLabel="true"],
QPushButton#DangerButton[showLabel="true"],
QPushButton#GhostButton[showLabel="true"] {
    padding: 0 14px;
}
QToolButton#MoreButton {
    background: $secondary_bg;
    color: transparent;
    border: 1px solid rgba(232, 238, 246, 0.88);
    border-radius: 17px;
    padding: 0;
    font-size: 13px;
    font-weight: 600;
    min-width: 56px;
    max-width: 56px;
    min-height: 56px;
    max-height: 56px;
}
QToolButton#MoreButton[showLabel="true"] {
    min-width: 82px;
    max-width: none;
    padding: 0 14px;
    color: #617487;
}
QToolButton#MoreButton:hover {
    background: $secondary_hover_bg;
}
QToolButton#MoreButton[buttonContext="inline"] {
    min-width: 56px;
    max-width: 56px;
    min-height: 56px;
    max-height: 56px;
    padding: 0;
}
QToolButton#MoreButton::menu-indicator {
    image: none;
    width: 0;
}
QPushButton[buttonContext="stacked"] {
    text-align: center;
    min-width: 56px;
    max-width: 56px;
    min-height: 56px;
    max-height: 56px;
    font-size: 13px;
    font-weight: 600;
}
QPushButton[buttonContext="stacked"][showLabel="true"] {
    min-width: 92px;
    max-width: none;
    min-height: 50px;
    max-height: 50px;
    padding: 0 14px;
}
QPushButton[buttonScale="hero"] {
    min-width: 86px;
    max-width: 86px;
    min-height: 86px;
    max-height: 86px;
    padding: 0;
    font-size: 15px;
    border-radius: 43px;
}
QPushButton[buttonScale="hero"][showLabel="true"] {
    min-width: 180px;
    max-width: none;
    min-height: 58px;
    max-height: 58px;
    border-radius: 20px;
    padding: 0 18px;
}
QPushButton#PrimaryButton {
    background: qlineargradient(
        x1:0, y1:0, x2:1, y2:0,
        stop:0 rgba(124, 172, 255, 0.98),
        stop:1 rgba(93, 135, 246, 0.98)
    );
    color: white;
    border: none;
}
QPushButton#PrimaryButton:hover {
    background: #6E9BFA;
}
QPushButton#SecondaryButton {
    background: $secondary_bg;
    color: #42576D;
    border: 1px solid rgba(232, 238, 246, 0.88);
}
QPushButton#SecondaryButton:hover {
    background: $secondary_hover_bg;
}
QPushButton#DangerButton {
    background: rgba(255, 241, 244, 0.96);
    color: #B95C78;
    border: 1px solid rgba(245, 211, 220, 0.98);
}
QPushButton#DangerButton:hover {
    background: rgba(255, 233, 237, 1.0);
}
QPushButton#GhostButton {
    background: transparent;
    color: #6D7F92;
    border: 1px dashed rgba(220, 229, 240, 0.98);
}
QPushButton#GhostButton:hover {
    background: $ghost_hover_bg;
}
QTableWidget {
    background: transparent;
    border: none;
    font-size: 13px;
    gridline-color: transparent;
    selection-background-color: rgba(232, 241, 255, 0.96);
    selection-color: #223042;
}
QHeaderView::section {
    background: $table_header_bg;
    color: #8B9BAD;
    border: none;
    border-bottom: 1px solid rgba(232, 238, 246, 0.58);
    padding: 12px 10px;
    font-size: 11px;
    font-weight: 600;
}
QTableWidget#HistoryTable {
    background: $inset_bg;
    border: 1px solid $inset_border;
    border-radius: 18px;
}
QTableWidget::item {
    padding: 10px;
    border-bottom: 1px solid rgba(238, 243, 248, 0.48);
}
"""
    ).substitute(
        **theme,
        body_family=typography.body_family,
        display_family=typography.display_family,
        mono_family=typography.mono_family,
    )
    return stylesheet


def launch_gui_preview_qt() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("live-note Qt Preview")
    app.setStyle(QStyleFactory.create("Fusion"))
    configure_app_font(app)
    app.setWindowIcon(app_brand_icon())
    window = PreviewWindow()
    window.show()
    return app.exec()


def app_brand_icon() -> QIcon:
    icon = QIcon(str(brand_logo_svg_path()))
    if icon.isNull():
        return make_icon("new", size=26, color="#304255")
    return icon


def configure_app_font(app: QApplication) -> None:
    global ACTIVE_TYPOGRAPHY, TYPOGRAPHY_READY
    ACTIVE_TYPOGRAPHY = resolve_typography_palette()
    font = QFont(ACTIVE_TYPOGRAPHY.body_family)
    font.setPointSize(13)
    font.setStyleStrategy(QFont.PreferAntialias)
    app.setFont(font)
    TYPOGRAPHY_READY = True


def ensure_typography_configured() -> None:
    global TYPOGRAPHY_READY
    if TYPOGRAPHY_READY:
        return
    app = QApplication.instance()
    if app is None:
        return
    configure_app_font(app)


def resolve_typography_palette() -> TypographyPalette:
    available = set(QFontDatabase.families())
    general_font = QFontDatabase.systemFont(QFontDatabase.GeneralFont).family() or DEFAULT_BODY_FONT
    fixed_font = QFontDatabase.systemFont(QFontDatabase.FixedFont).family() or DEFAULT_MONO_FONT

    if sys.platform == "darwin":
        body = first_available_font(
            available,
            ("SF Pro Text", "PingFang SC", "Helvetica Neue", "Hiragino Sans GB"),
            general_font,
        )
        display = first_available_font(
            available,
            ("SF Pro Display", "SF Pro Text", "PingFang SC", "Helvetica Neue"),
            body,
        )
        mono = first_available_font(
            available,
            ("SF Mono", "Menlo", "Monaco", "JetBrains Mono"),
            fixed_font,
        )
        return TypographyPalette(body_family=body, display_family=display, mono_family=mono)

    if sys.platform.startswith("win"):
        body = first_available_font(
            available,
            ("Segoe UI Variable Text", "Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei"),
            general_font,
        )
        display = first_available_font(
            available,
            (
                "Segoe UI Variable Display",
                "Segoe UI Variable Text",
                "Segoe UI",
                "Microsoft YaHei UI",
            ),
            body,
        )
        mono = first_available_font(
            available,
            ("Cascadia Mono", "Consolas", "JetBrains Mono"),
            fixed_font,
        )
        return TypographyPalette(body_family=body, display_family=display, mono_family=mono)

    body = first_available_font(
        available,
        ("Noto Sans CJK SC", "Noto Sans SC", "Source Han Sans SC", "Inter", "DejaVu Sans"),
        general_font,
    )
    display = first_available_font(
        available,
        ("Noto Sans CJK SC", "Noto Sans SC", "Source Han Sans SC", "Inter", "DejaVu Sans"),
        body,
    )
    mono = first_available_font(
        available,
        ("JetBrains Mono", "DejaVu Sans Mono", "Noto Sans Mono", "Monospace"),
        fixed_font,
    )
    return TypographyPalette(body_family=body, display_family=display, mono_family=mono)


def first_available_font(available: set[str], candidates: tuple[str, ...], fallback: str) -> str:
    for family in candidates:
        if family in available:
            return family
    return fallback


def display_text_for_label(label: str) -> str:
    mapping = {
        "新建记录": "新建",
        "记录库": "记录库",
        "设置": "设置",
        "开始新记录": "新建",
        "现场记录": "实时",
        "导入录音": "导入",
        "开始记录": "开始记录",
        "结束记录": "结束",
        "暂停": "暂停",
        "继续记录": "继续",
        "结束并整理": "结束",
        "打开记录库": "记录库",
        "打开笔记": "打开",
        "重新整理": "整理",
        "刷新": "刷新",
        "合并记录": "合并",
        "更多操作": "更多",
    }
    return mapping.get(label, label)


class PreviewWindow(QMainWindow):
    CLOSED_WIDTH = 560
    OPEN_WIDTHS = {
        "history": 980,
        "settings": 922,
    }
    WINDOW_HEIGHT = 620
    DRAWER_WIDTHS = {
        "history": 404,
        "settings": 346,
    }

    def __init__(self) -> None:
        super().__init__()
        ensure_typography_configured()
        self.setWindowTitle("live-note Qt Preview")
        self.setWindowIcon(app_brand_icon())
        self.resize(self.CLOSED_WIDTH, self.WINDOW_HEIGHT)
        self.setMinimumSize(self.CLOSED_WIDTH, 560)
        self.setWindowFlag(Qt.FramelessWindowHint, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        self.nav_buttons: dict[str, QPushButton] = {}
        self.stack = QStackedWidget()
        self._active_panel: str | None = None
        self.drawer_frame: QFrame | None = None
        self.top_primary_button: QPushButton | None = None

        self.backdrop_controller = create_backdrop_controller()
        self.backdrop_state = self.backdrop_controller.state
        self._backdrop_installed = False
        self._resize_handles: list[ResizeHandle] = []
        self._surface_mode = "fallback"
        self.new_session_page: NewSessionPage | None = None
        self.history_page: HistoryPage | None = None
        self.settings_page: SettingsPage | None = None

        self.setStyleSheet(build_stylesheet(self._surface_mode))
        self._build_ui()
        self._build_resize_handles()

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("WindowRoot")
        outer = QVBoxLayout(root)
        outer.setContentsMargins(WINDOW_MARGIN, WINDOW_MARGIN, WINDOW_MARGIN, WINDOW_MARGIN)
        outer.setSpacing(0)

        shell = QFrame()
        shell.setObjectName("Shell")

        shell_layout = QVBoxLayout(shell)
        shell_layout.setContentsMargins(12, 10, 12, 12)
        shell_layout.setSpacing(10)
        shell_layout.addWidget(self._build_chrome())
        shell_layout.addWidget(self._build_workspace(), 1)

        outer.addWidget(shell, 1)
        self.setCentralWidget(root)
        self._close_panel()

    def _build_chrome(self) -> QWidget:
        frame = DragFrame(self)
        frame.setObjectName("Surface")
        frame.setProperty("surfaceRole", "bar")
        frame.setProperty("surfaceTone", "base")
        apply_shadow(frame, blur=22, y_offset=8, alpha=16)
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(14, 8, 14, 8)
        layout.setSpacing(10)

        identity = QHBoxLayout()
        identity.setSpacing(12)
        identity.addWidget(self._build_window_controls(), 0, Qt.AlignVCenter)

        logo = QLabel()
        logo.setPixmap(app_brand_icon().pixmap(26, 26))
        identity.addWidget(logo, 0, Qt.AlignVCenter)

        brand = QLabel("live-note")
        brand.setObjectName("UsableSetting")
        identity.addWidget(brand, 0, Qt.AlignVCenter)

        nav = QWidget()
        nav_layout = QHBoxLayout(nav)
        nav_layout.setContentsMargins(0, 0, 0, 0)
        nav_layout.setSpacing(8)
        for key, text in (("history", "记录库"), ("settings", "设置")):
            button = QPushButton(text)
            button.setObjectName("NavButton")
            button.setCheckable(True)
            button.clicked.connect(lambda checked=False, panel=key: self._toggle_panel(panel))
            apply_button_icon(button, text, show_text=True)
            self.nav_buttons[key] = button
            nav_layout.addWidget(button)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.top_primary_button = labeled_action_button(
            "开始新记录",
            "primary",
            size=18,
            display_text="新建",
        )
        self.top_primary_button.setFixedHeight(44)
        self.top_primary_button.clicked.connect(self._open_new_session)
        actions.addWidget(self.top_primary_button, 0, Qt.AlignRight)

        layout.addLayout(identity, 0)
        layout.addStretch(1)
        layout.addWidget(nav, 0, Qt.AlignCenter)
        layout.addStretch(1)
        layout.addLayout(actions, 0)
        return frame

    def _build_window_controls(self) -> QWidget:
        wrapper = QWidget()
        layout = QHBoxLayout(wrapper)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        close_button = circle_button("CloseButton", self.close)
        minimize_button = circle_button("MinimizeButton", self.showMinimized)
        zoom_button = circle_button("ZoomButton", self._toggle_maximized)
        layout.addWidget(close_button)
        layout.addWidget(minimize_button)
        layout.addWidget(zoom_button)
        return wrapper

    def _build_workspace(self) -> QWidget:
        wrapper = QWidget()
        layout = QHBoxLayout(wrapper)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        self.new_session_page = NewSessionPage(on_open_history=lambda: self._open_panel("history"))
        self.history_page = HistoryPage()
        self.settings_page = SettingsPage()
        self.stack.addWidget(self.history_page)
        self.stack.addWidget(self.settings_page)

        self.drawer_frame = QFrame()
        self.drawer_frame.setObjectName("DrawerFrame")
        self.drawer_frame.setStyleSheet(
            "QFrame#DrawerFrame { background: transparent; border: none; }"
        )
        self.drawer_frame.setFixedWidth(self.DRAWER_WIDTHS["history"])
        drawer_layout = QVBoxLayout(self.drawer_frame)
        drawer_layout.setContentsMargins(0, 0, 0, 0)
        drawer_layout.setSpacing(0)
        drawer_layout.addWidget(self.stack, 1)
        self.drawer_frame.hide()

        layout.addWidget(self.new_session_page, 1)
        layout.addWidget(self.drawer_frame, 0)
        return wrapper

    def _build_resize_handles(self) -> None:
        specs = [
            ResizeHandleSpec(
                Qt.TopEdge, Qt.SizeVerCursor, (HANDLE_SIZE, 0, -2 * HANDLE_SIZE, HANDLE_SIZE)
            ),
            ResizeHandleSpec(
                Qt.BottomEdge,
                Qt.SizeVerCursor,
                (HANDLE_SIZE, -HANDLE_SIZE, -2 * HANDLE_SIZE, HANDLE_SIZE),
            ),
            ResizeHandleSpec(
                Qt.LeftEdge, Qt.SizeHorCursor, (0, HANDLE_SIZE, HANDLE_SIZE, -2 * HANDLE_SIZE)
            ),
            ResizeHandleSpec(
                Qt.RightEdge,
                Qt.SizeHorCursor,
                (-HANDLE_SIZE, HANDLE_SIZE, HANDLE_SIZE, -2 * HANDLE_SIZE),
            ),
            ResizeHandleSpec(
                Qt.TopEdge | Qt.LeftEdge, Qt.SizeFDiagCursor, (0, 0, HANDLE_SIZE, HANDLE_SIZE)
            ),
            ResizeHandleSpec(
                Qt.TopEdge | Qt.RightEdge,
                Qt.SizeBDiagCursor,
                (-HANDLE_SIZE, 0, HANDLE_SIZE, HANDLE_SIZE),
            ),
            ResizeHandleSpec(
                Qt.BottomEdge | Qt.LeftEdge,
                Qt.SizeBDiagCursor,
                (0, -HANDLE_SIZE, HANDLE_SIZE, HANDLE_SIZE),
            ),
            ResizeHandleSpec(
                Qt.BottomEdge | Qt.RightEdge,
                Qt.SizeFDiagCursor,
                (-HANDLE_SIZE, -HANDLE_SIZE, HANDLE_SIZE, HANDLE_SIZE),
            ),
        ]
        for spec in specs:
            handle = ResizeHandle(self, spec.edges, spec.cursor)
            handle.hide()
            self._resize_handles.append(handle)

    def _toggle_panel(self, key: str) -> None:
        if self._active_panel == key:
            self._close_panel()
            return
        self._open_panel(key)

    def _open_panel(self, key: str) -> None:
        if self.drawer_frame is None:
            return
        page = {
            "history": self.history_page,
            "settings": self.settings_page,
        }[key]
        if page is None:
            return
        self.stack.setCurrentWidget(page)
        self.drawer_frame.setFixedWidth(self.DRAWER_WIDTHS[key])
        self.drawer_frame.show()
        self._active_panel = key
        self._sync_window_width(key)
        for name, button in self.nav_buttons.items():
            button.setChecked(name == key)

    def _close_panel(self) -> None:
        self._active_panel = None
        if self.drawer_frame is not None:
            self.drawer_frame.hide()
        self._sync_window_width(None)
        for button in self.nav_buttons.values():
            button.setChecked(False)

    def _toggle_maximized(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def _open_new_session(self) -> None:
        self._close_panel()
        if self.new_session_page is not None:
            self.new_session_page.reset_home()

    def _apply_surface_mode(self, surface_mode: str) -> None:
        if surface_mode == self._surface_mode:
            return
        self._surface_mode = surface_mode
        self.setStyleSheet(build_stylesheet(surface_mode))

    def _sync_window_width(self, panel: str | None) -> None:
        target_width = self.OPEN_WIDTHS[panel] if panel is not None else self.CLOSED_WIDTH
        self.setMinimumWidth(target_width)
        if self.isMaximized():
            return
        self.resize(target_width, max(self.height(), self.WINDOW_HEIGHT))

    def keyPressEvent(self, event: QKeyEvent) -> None:  # type: ignore[override]
        if event.key() == Qt.Key_Escape and self._active_panel is not None:
            self._close_panel()
            event.accept()
            return
        super().keyPressEvent(event)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._layout_resize_handles()

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        self._layout_resize_handles()
        if not self._backdrop_installed:
            self.backdrop_state = self.backdrop_controller.install(self)
            self._apply_surface_mode(
                surface_mode_from_backdrop(self.backdrop_state.active, self.backdrop_state.label)
            )
            if self.settings_page is not None:
                self.settings_page.set_backdrop_state(self.backdrop_state)
            self._backdrop_installed = True
        for handle in self._resize_handles:
            handle.show()

    def _layout_resize_handles(self) -> None:
        width = self.width()
        height = self.height()
        specs = [
            ResizeHandleSpec(
                Qt.TopEdge, Qt.SizeVerCursor, (HANDLE_SIZE, 0, width - 2 * HANDLE_SIZE, HANDLE_SIZE)
            ),
            ResizeHandleSpec(
                Qt.BottomEdge,
                Qt.SizeVerCursor,
                (HANDLE_SIZE, height - HANDLE_SIZE, width - 2 * HANDLE_SIZE, HANDLE_SIZE),
            ),
            ResizeHandleSpec(
                Qt.LeftEdge,
                Qt.SizeHorCursor,
                (0, HANDLE_SIZE, HANDLE_SIZE, height - 2 * HANDLE_SIZE),
            ),
            ResizeHandleSpec(
                Qt.RightEdge,
                Qt.SizeHorCursor,
                (width - HANDLE_SIZE, HANDLE_SIZE, HANDLE_SIZE, height - 2 * HANDLE_SIZE),
            ),
            ResizeHandleSpec(
                Qt.TopEdge | Qt.LeftEdge, Qt.SizeFDiagCursor, (0, 0, HANDLE_SIZE, HANDLE_SIZE)
            ),
            ResizeHandleSpec(
                Qt.TopEdge | Qt.RightEdge,
                Qt.SizeBDiagCursor,
                (width - HANDLE_SIZE, 0, HANDLE_SIZE, HANDLE_SIZE),
            ),
            ResizeHandleSpec(
                Qt.BottomEdge | Qt.LeftEdge,
                Qt.SizeBDiagCursor,
                (0, height - HANDLE_SIZE, HANDLE_SIZE, HANDLE_SIZE),
            ),
            ResizeHandleSpec(
                Qt.BottomEdge | Qt.RightEdge,
                Qt.SizeFDiagCursor,
                (width - HANDLE_SIZE, height - HANDLE_SIZE, HANDLE_SIZE, HANDLE_SIZE),
            ),
        ]
        for handle, spec in zip(self._resize_handles, specs):
            x, y, w, h = spec.rect
            handle.setGeometry(QRect(x, y, w, h))


class DragFrame(QFrame):
    def __init__(self, window: PreviewWindow):
        super().__init__(window)
        self._window = window

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton and self._window.windowHandle() is not None:
            self._window.windowHandle().startSystemMove()
            event.accept()
            return
        super().mousePressEvent(event)


class ResizeHandle(QWidget):
    def __init__(self, window: PreviewWindow, edges: Qt.Edge | Qt.Edges, cursor: Qt.CursorShape):
        super().__init__(window)
        self._window = window
        self._edges = edges
        self.setCursor(cursor)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet("background: transparent;")

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton and self._window.windowHandle() is not None:
            self._window.windowHandle().startSystemResize(self._edges)
            event.accept()
            return
        super().mousePressEvent(event)


class SessionPulse(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setFixedSize(132, 108)
        self._state = PREVIEW_SESSION_STATES[PreviewSessionStateKind.IDLE]

    def set_preview_state(self, state: PreviewSessionState) -> None:
        self._state = state
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        base_width = 220
        base_height = 182
        scale = min(self.width() / base_width, self.height() / base_height)
        x_offset = (self.width() - base_width * scale) / 2
        y_offset = (self.height() - base_height * scale) / 2
        painter.translate(x_offset, y_offset)
        painter.scale(scale, scale)

        palette = {
            PreviewSessionStateKind.IDLE: {
                "outer_0": QColor(198, 212, 236, 110),
                "outer_1": QColor(198, 212, 236, 28),
                "inner_0": QColor(255, 255, 255, 244),
                "inner_1": QColor(240, 245, 251, 210),
                "bubble": QColor(255, 255, 255, 96),
                "text": QColor("#6F8195"),
            },
            PreviewSessionStateKind.RECORDING: {
                "outer_0": QColor(132, 181, 255, 176),
                "outer_1": QColor(126, 192, 255, 60),
                "inner_0": QColor(255, 255, 255, 244),
                "inner_1": QColor(232, 241, 255, 212),
                "bubble": QColor(255, 255, 255, 104),
                "text": QColor("#4B79D9"),
            },
            PreviewSessionStateKind.PAUSED: {
                "outer_0": QColor(198, 184, 255, 124),
                "outer_1": QColor(198, 184, 255, 30),
                "inner_0": QColor(255, 255, 255, 242),
                "inner_1": QColor(241, 237, 255, 214),
                "bubble": QColor(255, 255, 255, 104),
                "text": QColor("#7966C5"),
            },
            PreviewSessionStateKind.BACKGROUND_FINISHING: {
                "outer_0": QColor(142, 221, 193, 142),
                "outer_1": QColor(142, 221, 193, 34),
                "inner_0": QColor(255, 255, 255, 244),
                "inner_1": QColor(235, 249, 242, 212),
                "bubble": QColor(255, 255, 255, 104),
                "text": QColor("#2A9B63"),
            },
        }[self._state.kind]

        outer = QRadialGradient(148, 70, 102)
        outer.setColorAt(0.0, palette["outer_0"])
        outer.setColorAt(0.66, palette["outer_1"])
        outer.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.setBrush(outer)
        painter.drawEllipse(48, 6, 154, 154)

        inner = QRadialGradient(118, 94, 78)
        inner.setColorAt(0.0, palette["inner_0"])
        inner.setColorAt(0.82, palette["inner_1"])
        inner.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.setBrush(inner)
        painter.drawEllipse(50, 24, 132, 132)

        painter.setBrush(palette["bubble"])
        painter.drawEllipse(170, 120, 32, 32)
        painter.drawEllipse(24, 108, 22, 22)

        icon_map = {
            PreviewSessionStateKind.IDLE: "record",
            PreviewSessionStateKind.RECORDING: "record",
            PreviewSessionStateKind.PAUSED: "pause",
            PreviewSessionStateKind.BACKGROUND_FINISHING: "spark",
        }
        icon = make_icon(icon_map[self._state.kind], size=28, color=palette["text"].name()).pixmap(
            28, 28
        )
        painter.drawPixmap(96, 76, icon)

        active_count = {
            PreviewSessionStateKind.IDLE: 1,
            PreviewSessionStateKind.RECORDING: 3,
            PreviewSessionStateKind.PAUSED: 2,
            PreviewSessionStateKind.BACKGROUND_FINISHING: 3,
        }[self._state.kind]
        for index in range(3):
            alpha = 210 if index < active_count else 70
            painter.setBrush(
                QColor(
                    palette["text"].red(),
                    palette["text"].green(),
                    palette["text"].blue(),
                    alpha,
                )
            )
            painter.drawRoundedRect(62 + index * 34, 138, 20, 8, 4, 4)


class LaunchHalo(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setFixedSize(220, 92)
        self._state = PREVIEW_SESSION_STATES[PreviewSessionStateKind.IDLE]

    def set_preview_state(self, state: PreviewSessionState) -> None:
        self._state = state
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)

        palette = {
            PreviewSessionStateKind.IDLE: ((208, 220, 241, 120), (246, 249, 255, 252)),
            PreviewSessionStateKind.RECORDING: ((138, 179, 255, 144), (236, 244, 255, 252)),
            PreviewSessionStateKind.PAUSED: ((186, 174, 244, 132), (244, 240, 255, 252)),
            PreviewSessionStateKind.BACKGROUND_FINISHING: (
                (145, 220, 190, 136),
                (238, 249, 243, 252),
            ),
        }[self._state.kind]

        glow = QRadialGradient(self.width() * 0.5, self.height() * 0.48, 78)
        glow.setColorAt(0.0, QColor(*palette[0]))
        glow.setColorAt(0.72, QColor(palette[0][0], palette[0][1], palette[0][2], 18))
        glow.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.setBrush(glow)
        painter.drawEllipse(44, 0, 132, 132)

        inner = QRadialGradient(self.width() * 0.5, self.height() * 0.55, 48)
        inner.setColorAt(0.0, QColor(*palette[1]))
        inner.setColorAt(0.78, QColor(245, 248, 252, 180))
        inner.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.setBrush(inner)
        painter.drawEllipse(68, 16, 84, 84)

        painter.setBrush(QColor(255, 255, 255, 110))
        painter.drawEllipse(150, 14, 18, 18)
        painter.drawEllipse(52, 54, 12, 12)


class IconSequence(QWidget):
    def __init__(
        self,
        kinds: tuple[str, ...] = (),
        *,
        tone: str = "soft",
        size: int = 14,
        max_per_row: int = 5,
        spacing: int = 8,
        centered: bool = False,
    ) -> None:
        super().__init__()
        self._tone = tone
        self._size = size
        self._max_per_row = max_per_row
        self._spacing = spacing
        self._centered = centered
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(spacing)
        self.set_sequence(kinds)

    def set_sequence(self, kinds: tuple[str, ...], *, tone: str | None = None) -> None:
        if tone is not None:
            self._tone = tone
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        if not kinds:
            self.hide()
            return
        self.show()
        for start in range(0, len(kinds), self._max_per_row):
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(self._spacing)
            if self._centered:
                row_layout.addStretch(1)
            for kind in kinds[start : start + self._max_per_row]:
                row_layout.addWidget(icon_plate(kind, self._tone, size=self._size))
            if self._centered:
                row_layout.addStretch(1)
            else:
                row_layout.addStretch(1)
            self._layout.addWidget(row)


class NewSessionPage(QWidget):
    def __init__(self, on_open_history: Callable[[], None]) -> None:
        super().__init__()
        self._on_open_history = on_open_history
        self.current_state = PREVIEW_SESSION_STATES[PreviewSessionStateKind.IDLE]
        self._selected_mode = "live"
        self._capture_title = "计量经济学 Week 3"
        self._capture_meta = "BlackHole 2ch"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 0, 8, 0)
        layout.setSpacing(0)
        layout.addStretch(1)
        layout.addWidget(self._build_stage_panel(), 0, Qt.AlignHCenter)
        layout.addStretch(1)

        self.reset_home()

    def _build_stage_panel(self) -> QWidget:
        hero = panel("card", "accent")
        hero.setMaximumWidth(396)
        hero.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        hero_layout = QVBoxLayout(hero)
        hero_layout.setContentsMargins(14, 14, 14, 14)
        hero_layout.setSpacing(0)
        hero_layout.addWidget(self._build_stage_sheet())
        return hero

    def _build_stage_sheet(self) -> QWidget:
        shell = styled_frame("NoteSheet")
        shell.setMaximumWidth(368)
        layout = QVBoxLayout(shell)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)

        self.state_badge = visible_pill("空闲", "accent")
        layout.addWidget(self.state_badge, 0, Qt.AlignHCenter)

        self.hero_title = QLabel()
        self.hero_title.setObjectName("UsableHeading")
        self.hero_title.setAlignment(Qt.AlignHCenter)
        self.hero_title.setWordWrap(True)
        layout.addWidget(self.hero_title)

        self.hero_description = QLabel()
        self.hero_description.setObjectName("UsableMeta")
        self.hero_description.setAlignment(Qt.AlignHCenter)
        self.hero_description.setWordWrap(True)
        layout.addWidget(self.hero_description)

        self.mode_group = QButtonGroup(self)
        segment_row = QHBoxLayout()
        segment_row.setSpacing(10)
        self.live_mode_button = QPushButton("现场记录")
        self.live_mode_button.setObjectName("SegmentButton")
        self.live_mode_button.setCheckable(True)
        self.live_mode_button.setFixedHeight(44)
        self.live_mode_button.setChecked(True)
        self.live_mode_button.clicked.connect(self._activate_live_mode)
        apply_button_icon(self.live_mode_button, "现场记录", show_text=True)
        self.file_mode_button = QPushButton("导入录音")
        self.file_mode_button.setObjectName("SegmentButton")
        self.file_mode_button.setCheckable(True)
        self.file_mode_button.setFixedHeight(44)
        self.file_mode_button.clicked.connect(self._activate_file_mode)
        apply_button_icon(self.file_mode_button, "导入录音", show_text=True)
        self.mode_group.addButton(self.live_mode_button)
        self.mode_group.addButton(self.file_mode_button)
        segment_row.addWidget(self.live_mode_button)
        segment_row.addWidget(self.file_mode_button)
        mode_shell = QWidget()
        mode_shell_layout = QHBoxLayout(mode_shell)
        mode_shell_layout.setContentsMargins(0, 0, 0, 0)
        mode_shell_layout.addStretch(1)
        mode_shell_layout.addLayout(segment_row)
        mode_shell_layout.addStretch(1)
        layout.addWidget(mode_shell)

        source_shell = styled_frame("ActionStrip")
        source_layout = QHBoxLayout(source_shell)
        source_layout.setContentsMargins(16, 14, 16, 14)
        source_layout.setSpacing(12)
        self.source_icon = icon_plate("mic", "accent", 18)
        source_layout.addWidget(self.source_icon, 0, Qt.AlignTop)

        source_copy = QVBoxLayout()
        source_copy.setSpacing(0)
        self.capture_summary_label = QLabel()
        self.capture_summary_label.setObjectName("UsableMeta")
        self.capture_summary_label.setWordWrap(True)
        source_copy.addWidget(self.capture_summary_label)
        source_layout.addLayout(source_copy, 1)

        self.capture_mode_badge = visible_pill("实时", "accent")
        source_layout.addWidget(self.capture_mode_badge, 0, Qt.AlignTop)
        layout.addWidget(source_shell)

        self.session_pulse = SessionPulse()
        self.session_pulse.setFixedSize(88, 72)
        layout.addWidget(self.session_pulse, 0, Qt.AlignHCenter)

        self.primary_action_button = labeled_action_button("开始记录", "primary", size=22)
        self.primary_action_button.setProperty("buttonScale", "hero")
        self.primary_action_button.setFixedHeight(58)
        refresh_widget_style(self.primary_action_button)
        self.primary_action_button.clicked.connect(self._handle_primary_action)
        layout.addWidget(self.primary_action_button, 0, Qt.AlignHCenter)

        action_shell = QWidget()
        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(10)
        self.secondary_action_button = labeled_action_button("暂停", "secondary")
        self.secondary_action_button.setFixedHeight(44)
        self.secondary_action_button.clicked.connect(self._handle_secondary_action)
        self.tertiary_action_button = labeled_action_button("导入录音", "ghost")
        self.tertiary_action_button.setFixedHeight(44)
        self.tertiary_action_button.clicked.connect(self._handle_tertiary_action)
        actions.addWidget(self.secondary_action_button)
        actions.addWidget(self.tertiary_action_button)
        action_shell.setLayout(actions)
        layout.addWidget(action_shell, 0, Qt.AlignHCenter)

        self.mode_hint = QLabel()
        self.mode_hint.setObjectName("UsableStatus")
        self.mode_hint.setAlignment(Qt.AlignHCenter)
        self.mode_hint.setWordWrap(True)
        layout.addWidget(self.mode_hint)
        return shell

    def reset_home(self) -> None:
        self._selected_mode = "live"
        self._capture_title = "计量经济学 Week 3"
        self._capture_meta = "BlackHole 2ch"
        self.set_preview_state(PreviewSessionStateKind.IDLE)

    def _activate_live_mode(self) -> None:
        self._selected_mode = "live"
        self._capture_title = "计量经济学 Week 3"
        self._capture_meta = "BlackHole 2ch"
        if self.current_state.kind == PreviewSessionStateKind.IDLE:
            self.set_preview_state(PreviewSessionStateKind.IDLE)

    def _activate_file_mode(self) -> None:
        self._selected_mode = "file"
        self._capture_title = "iPhone 录音 · 产品复盘"
        self._capture_meta = "review.m4a"
        if self.current_state.kind == PreviewSessionStateKind.IDLE:
            self.set_preview_state(PreviewSessionStateKind.IDLE)

    def _handle_primary_action(self) -> None:
        if (
            self.current_state.kind == PreviewSessionStateKind.IDLE
            and self._selected_mode == "file"
        ):
            self.set_preview_state(PreviewSessionStateKind.BACKGROUND_FINISHING)
            return
        transitions = {
            PreviewSessionStateKind.IDLE: PreviewSessionStateKind.RECORDING,
            PreviewSessionStateKind.RECORDING: PreviewSessionStateKind.BACKGROUND_FINISHING,
            PreviewSessionStateKind.PAUSED: PreviewSessionStateKind.RECORDING,
            PreviewSessionStateKind.BACKGROUND_FINISHING: PreviewSessionStateKind.RECORDING,
        }
        self.set_preview_state(transitions[self.current_state.kind])

    def _handle_secondary_action(self) -> None:
        transitions = {
            PreviewSessionStateKind.RECORDING: PreviewSessionStateKind.PAUSED,
            PreviewSessionStateKind.PAUSED: PreviewSessionStateKind.BACKGROUND_FINISHING,
        }
        next_state = transitions.get(self.current_state.kind)
        if next_state is not None:
            self.set_preview_state(next_state)

    def _handle_tertiary_action(self) -> None:
        if self.current_state.kind == PreviewSessionStateKind.IDLE:
            if self._selected_mode == "live":
                self._activate_file_mode()
            else:
                self._activate_live_mode()
            return
        if self.current_state.kind == PreviewSessionStateKind.BACKGROUND_FINISHING:
            self._on_open_history()

    def _sync_idle_primary_action(self) -> None:
        if self.current_state.kind != PreviewSessionStateKind.IDLE:
            return
        primary_label = "开始记录" if self._selected_mode == "live" else "导入录音"
        apply_button_icon(
            self.primary_action_button,
            primary_label,
            size=22,
            color="#FFFFFF",
            show_text=True,
        )

    def _update_button_copy(
        self,
        button: QPushButton,
        label: str,
        role: str,
    ) -> None:
        color_map = {
            "primary": "#FFFFFF",
            "secondary": "#42576D",
            "ghost": "#667B91",
        }
        apply_button_icon(
            button,
            label,
            size=22 if role == "primary" else 18,
            color=color_map[role],
            show_text=True,
        )

    def _state_tone(self, kind: PreviewSessionStateKind) -> str:
        if kind == PreviewSessionStateKind.IDLE:
            return "accent" if self._selected_mode == "live" else "soft"
        return {
            PreviewSessionStateKind.RECORDING: "accent",
            PreviewSessionStateKind.PAUSED: "soft",
            PreviewSessionStateKind.BACKGROUND_FINISHING: "success",
        }[kind]

    def _refresh_capture_preview(self) -> None:
        tone = self._state_tone(self.current_state.kind)
        icon_kind = "mic" if self._selected_mode == "live" else "import"
        mode_text = "实时" if self._selected_mode == "live" else "导入"
        set_icon_plate(self.source_icon, icon_kind, tone, size=18)
        self.capture_summary_label.setText(f"{self._capture_title} · {self._capture_meta}")
        set_pill_state(self.capture_mode_badge, mode_text, tone)

    def _status_card_text(self, state: PreviewSessionState) -> str:
        mapping = {
            PreviewSessionStateKind.IDLE: "空闲",
            PreviewSessionStateKind.RECORDING: "录制中",
            PreviewSessionStateKind.PAUSED: "已暂停",
            PreviewSessionStateKind.BACKGROUND_FINISHING: "整理中",
        }
        return mapping[state.kind]

    def _hero_title_text(self, state: PreviewSessionState) -> str:
        if state.kind == PreviewSessionStateKind.IDLE and self._selected_mode == "file":
            return "导入一段录音"
        return {
            PreviewSessionStateKind.IDLE: "开始记录",
            PreviewSessionStateKind.RECORDING: "记录中",
            PreviewSessionStateKind.PAUSED: "已暂停",
            PreviewSessionStateKind.BACKGROUND_FINISHING: "已交给后台",
        }[state.kind]

    def _hero_description_text(self, state: PreviewSessionState) -> str:
        if state.kind == PreviewSessionStateKind.IDLE and self._selected_mode == "file":
            return "支持 mp3、m4a、wav，导入后沿用同一条整理与导出流程。"
        return {
            PreviewSessionStateKind.IDLE: "先写原文，停止后自动转入后台整理。",
            PreviewSessionStateKind.RECORDING: "原文正在持续写入，完整录音会一并保留。",
            PreviewSessionStateKind.PAUSED: "已暂停，现在可以继续，也可以直接结束。",
            PreviewSessionStateKind.BACKGROUND_FINISHING: "这条记录已离开前台，后台会继续整理。",
        }[state.kind]

    def _status_line_text(self, state: PreviewSessionState) -> str:
        if state.kind == PreviewSessionStateKind.IDLE and self._selected_mode == "file":
            return "导入后会先完成原文，再继续整理。"
        return {
            PreviewSessionStateKind.IDLE: "开始后自动写原文，结束后自动转后台。",
            PreviewSessionStateKind.RECORDING: "正在记录，可直接暂停或结束。",
            PreviewSessionStateKind.PAUSED: "暂停期间不会继续写入新片段。",
            PreviewSessionStateKind.BACKGROUND_FINISHING: "后台整理中，现在可以开始下一条。",
        }[state.kind]

    def set_preview_state(self, kind: PreviewSessionStateKind) -> None:
        state = PREVIEW_SESSION_STATES[kind]
        self.current_state = state

        set_pill_state(self.state_badge, self._status_card_text(state), self._state_tone(kind))
        self.hero_title.setText(self._hero_title_text(state))
        self.hero_description.setText(self._hero_description_text(state))

        self._update_button_copy(self.primary_action_button, state.primary_action, "primary")

        secondary_label = {
            PreviewSessionStateKind.RECORDING: "暂停",
            PreviewSessionStateKind.PAUSED: "结束并整理",
        }.get(kind)
        self.secondary_action_button.setVisible(bool(secondary_label))
        if secondary_label is not None:
            self._update_button_copy(self.secondary_action_button, secondary_label, "secondary")

        tertiary_label = None
        if kind == PreviewSessionStateKind.IDLE:
            tertiary_label = "导入录音" if self._selected_mode == "live" else "现场记录"
        elif kind == PreviewSessionStateKind.BACKGROUND_FINISHING:
            tertiary_label = "打开记录库"
        self.tertiary_action_button.setVisible(bool(tertiary_label))
        if tertiary_label is not None:
            self._update_button_copy(self.tertiary_action_button, tertiary_label, "ghost")

        self.live_mode_button.setEnabled(kind == PreviewSessionStateKind.IDLE)
        self.file_mode_button.setEnabled(kind == PreviewSessionStateKind.IDLE)
        if kind == PreviewSessionStateKind.IDLE:
            self.live_mode_button.setChecked(self._selected_mode == "live")
            self.file_mode_button.setChecked(self._selected_mode == "file")
            self._sync_idle_primary_action()

        self.mode_hint.setText(self._status_line_text(state))
        self.session_pulse.set_preview_state(state)
        self.session_pulse.setVisible(kind != PreviewSessionStateKind.IDLE)
        self._refresh_capture_preview()


class HistoryPage(QWidget):
    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        shell = panel("card")
        shell_layout = QVBoxLayout(shell)
        shell_layout.setContentsMargins(14, 14, 14, 14)
        shell_layout.setSpacing(0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        scroll.viewport().setStyleSheet("background: transparent;")
        scroll.setWidget(self._workspace_card())
        shell_layout.addWidget(scroll, 1)
        layout.addWidget(shell, 1)

    def _workspace_card(self) -> QWidget:
        frame = QWidget()
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        header = QWidget()
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(6, 6, 6, 0)
        header_layout.setSpacing(8)
        title = QLabel("记录库")
        title.setObjectName("UsableHeading")
        subtitle = QLabel("回看已完成的记录，必要时再重新整理、合并或导出。")
        subtitle.setObjectName("UsableMeta")
        subtitle.setWordWrap(True)
        header_layout.addWidget(title)
        header_layout.addWidget(subtitle)
        header_layout.addWidget(self._toolbar_row())
        layout.addWidget(header)

        layout.addWidget(self._library_list())
        layout.addWidget(self._note_preview())
        layout.addStretch(1)
        return frame

    def _toolbar_row(self) -> QWidget:
        shell = QWidget()
        layout = QHBoxLayout(shell)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        refresh_button = labeled_action_button("刷新", "ghost")
        refresh_button.setFixedHeight(38)
        merge_button = labeled_action_button("合并记录", "secondary")
        merge_button.setFixedHeight(38)
        layout.addWidget(refresh_button)
        layout.addWidget(merge_button)
        layout.addStretch(1)
        return shell

    def _library_list(self) -> QWidget:
        shell = panel("card")
        shell.setMaximumHeight(272)
        layout = QVBoxLayout(shell)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        heading = QLabel("最近记录")
        heading.setObjectName("UsableTitle")
        layout.addWidget(heading)

        list_layout = QVBoxLayout()
        list_layout.setSpacing(6)
        list_layout.addWidget(
            library_item(
                "宏观经济学 Week 3",
                "已完成",
                "success",
                "课程记录 · 导入录音 · 2026-03-16 19:30",
                "已生成原文与整理，适合直接回顾课程重点。",
                "42 段 · large-v3",
                selected=False,
            )
        )
        list_layout.addWidget(
            library_item(
                "产品周会",
                "整理中",
                "accent",
                "会议记录 · 现场记录 · 2026-03-16 14:05",
                "原文已经可回看，整理笔记和行动项仍在后台补全。",
                "18 / 21 段 · 预计 2 分钟",
                selected=True,
            )
        )
        list_layout.addWidget(
            library_item(
                "投资研究播客",
                "待复查",
                "danger",
                "通用记录 · 导入录音 · 2026-03-15 22:11",
                "有少量背景噪声片段，适合在整理完成后再做一次精修。",
                "31 段 · 噪声提示",
                selected=False,
            )
        )
        layout.addLayout(list_layout)
        return shell

    def _note_preview(self) -> QWidget:
        note = styled_frame("NoteSheet")
        note_layout = QVBoxLayout(note)
        note_layout.setContentsMargins(14, 14, 14, 14)
        note_layout.setSpacing(10)

        note_header = QHBoxLayout()
        note_header.setSpacing(10)
        meta = QLabel("2026-03-16 14:05 · BlackHole 2ch")
        meta.setObjectName("UsableMeta")
        meta.setWordWrap(True)
        note_header.addWidget(meta, 1)
        note_header.addWidget(visible_pill("整理中", "accent"), 0, Qt.AlignTop)
        note_layout.addLayout(note_header)

        title = QLabel("产品周会")
        title.setObjectName("UsableHeading")
        title.setWordWrap(True)
        note_layout.addWidget(title)

        lead = QLabel("原文已经可回看，系统正在后台补全整理摘要与行动项。")
        lead.setObjectName("UsableBody")
        lead.setWordWrap(True)
        note_layout.addWidget(lead)
        note_layout.addWidget(divider())

        excerpt_title = QLabel("原文摘录")
        excerpt_title.setObjectName("UsableSetting")
        excerpt_body = QLabel(
            "[00:12:18] 这轮先不做移动端实时，我们统一走录音后导入。\n"
            "[00:13:42] large-v3 继续保留为默认模型，优先保证课程和会议里的准确率。"
        )
        excerpt_body.setObjectName("UsableBody")
        excerpt_body.setWordWrap(True)
        note_layout.addWidget(excerpt_title)
        note_layout.addWidget(excerpt_body)
        note_layout.addWidget(divider())

        structured_title = QLabel("整理")
        structured_title.setObjectName("UsableSetting")
        structured_body = QLabel(
            "1. 本周发布继续收敛到桌面端本地优先。\n"
            "2. 移动端早期方案保持“录音后导入”，不再单独做实时采集。\n"
            "3. 默认模型继续使用 large-v3，后续再补离线精修链路。"
        )
        structured_body.setObjectName("UsableBody")
        structured_body.setWordWrap(True)
        note_layout.addWidget(structured_title)
        note_layout.addWidget(structured_body)
        note_layout.addWidget(divider())

        action_shell = styled_frame("ActionStrip")
        action_layout = QHBoxLayout(action_shell)
        action_layout.setContentsMargins(14, 12, 14, 12)
        action_layout.setSpacing(10)
        action_layout.addWidget(labeled_action_button("打开笔记", "primary"), 0)
        action_layout.addWidget(labeled_action_button("重新整理", "secondary"), 0)
        more_button = labeled_more_actions_button(
            "更多操作",
            [
                "查看原文",
                "查看整理",
                "重新生成原文",
                "提高准确度",
                "重新导出",
            ],
        )
        more_button.setProperty("buttonContext", "inline")
        refresh_widget_style(more_button)
        action_layout.addWidget(more_button, 0)
        action_layout.addStretch(1)
        note_layout.addWidget(action_shell)

        return note


class SettingsPage(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._diagnostics_expanded = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        shell = panel("card")
        shell_layout = QVBoxLayout(shell)
        shell_layout.setContentsMargins(14, 14, 14, 14)
        shell_layout.setSpacing(0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        scroll.viewport().setStyleSheet("background: transparent;")
        scroll.setWidget(self._workspace_card())
        shell_layout.addWidget(scroll, 1)
        layout.addWidget(shell, 1)

    def _workspace_card(self) -> QWidget:
        frame = QWidget()
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        summary = QWidget()
        summary_layout = QVBoxLayout(summary)
        summary_layout.setContentsMargins(6, 6, 6, 0)
        summary_layout.setSpacing(8)
        title = QLabel("设置")
        title.setObjectName("UsableHeading")
        subtitle = QLabel("本地记录默认可用；导出与智能整理按需开启。")
        subtitle.setObjectName("UsableMeta")
        subtitle.setWordWrap(True)
        summary_layout.addWidget(title)
        summary_layout.addWidget(subtitle)
        summary_layout.addWidget(self._preferences_sheet())
        layout.addWidget(summary)

        diagnostics_toggle_row = QWidget()
        diagnostics_toggle_layout = QHBoxLayout(diagnostics_toggle_row)
        diagnostics_toggle_layout.setContentsMargins(0, 0, 0, 0)
        diagnostics_toggle_layout.setSpacing(0)
        self.diagnostics_toggle = labeled_action_button("设置", "ghost", display_text="诊断")
        self.diagnostics_toggle.setAccessibleName("诊断")
        self.diagnostics_toggle.setToolTip("诊断")
        self.diagnostics_toggle.setFixedHeight(42)
        self.diagnostics_toggle.clicked.connect(self._toggle_diagnostics)
        diagnostics_toggle_layout.addWidget(self.diagnostics_toggle, 0, Qt.AlignLeft)
        layout.addWidget(diagnostics_toggle_row)

        self.diagnostics_body = panel("card")
        diagnostics_layout = QVBoxLayout(self.diagnostics_body)
        diagnostics_layout.setContentsMargins(14, 14, 14, 14)
        diagnostics_layout.setSpacing(12)
        diagnostics_layout.addWidget(self._doctor_panel())
        self.diagnostics_body.hide()
        layout.addWidget(self.diagnostics_body)
        layout.addStretch(1)
        return frame

    def _preferences_sheet(self) -> QWidget:
        sheet = styled_frame("NoteSheet")
        layout = QVBoxLayout(sheet)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)
        for index, (label, value, tone) in enumerate(
            (
                ("Whisper", "large-v3 · whisper-server · ffmpeg", "success"),
                ("Obsidian", "可选开启 · Local REST API", "soft"),
                ("LLM", "可选开启 · Base URL / stream", "soft"),
            )
        ):
            layout.addWidget(status_row(label, label, value, tone))
            if index < 2:
                layout.addWidget(divider())
        return sheet

    def _doctor_panel(self) -> QWidget:
        doctor = QWidget()
        doctor_layout = QVBoxLayout(doctor)
        doctor_layout.setContentsMargins(0, 0, 0, 0)
        doctor_layout.setSpacing(12)
        title = QLabel("诊断")
        title.setObjectName("UsableTitle")
        subtitle = QLabel("只有需要排查时再看这些细节。")
        subtitle.setObjectName("UsableMeta")
        subtitle.setWordWrap(True)
        doctor_layout.addWidget(title)
        doctor_layout.addWidget(subtitle)

        window_shell_row = QHBoxLayout()
        window_shell_row.setSpacing(10)
        self.window_shell_icon = icon_plate("window", "soft", 18)
        self.window_shell_badge = visible_pill("窗口外壳", "soft")
        self.window_shell_detail = QLabel("等待窗口初始化后更新。")
        self.window_shell_detail.setObjectName("UsableValue")
        self.window_shell_detail.setWordWrap(True)
        shell_copy = QVBoxLayout()
        shell_copy.setContentsMargins(0, 0, 0, 0)
        shell_copy.setSpacing(4)
        shell_copy.addWidget(self.window_shell_badge, 0, Qt.AlignLeft)
        shell_copy.addWidget(self.window_shell_detail)
        window_shell_row.addWidget(self.window_shell_icon, 0, Qt.AlignTop)
        window_shell_row.addLayout(shell_copy, 1)
        doctor_layout.addLayout(window_shell_row)
        doctor_layout.addWidget(divider())

        for index, (badge_text, label, value, tone) in enumerate(
            (
                ("设置", "配置文件", "已加载 config.toml", "success"),
                ("本地环境", "录音链路", "whisper-server 与模型已就绪", "success"),
                ("导出", "Obsidian 同步", "已连通 Local REST API", "success"),
                ("整理", "Base URL", "支持自定义地址与 stream 模式", "soft"),
            )
        ):
            doctor_layout.addWidget(status_row(badge_text, label, value, tone))
            if index < 3:
                doctor_layout.addWidget(divider())
        return doctor

    def _toggle_diagnostics(self) -> None:
        self._diagnostics_expanded = not self._diagnostics_expanded
        self.diagnostics_body.setVisible(self._diagnostics_expanded)
        self.diagnostics_toggle.setText("收起诊断" if self._diagnostics_expanded else "诊断")

    def set_backdrop_state(self, state) -> None:
        tone = "success" if state.active else "soft"
        set_pill_state(self.window_shell_badge, state.label, tone)
        self.window_shell_detail.setText(state.detail)
        set_icon_plate(self.window_shell_icon, "window", tone, size=18)


def panel(role: str = "card", tone: str = "base") -> QFrame:
    frame = QFrame()
    frame.setObjectName("Surface")
    frame.setProperty("surfaceRole", role)
    frame.setProperty("surfaceTone", tone)
    frame.setAttribute(Qt.WA_StyledBackground, True)
    frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
    return frame


def styled_frame(object_name: str) -> QFrame:
    frame = QFrame()
    frame.setObjectName(object_name)
    frame.setAttribute(Qt.WA_StyledBackground, True)
    return frame


def apply_shadow(widget: QWidget, blur: int, y_offset: int, alpha: int) -> None:
    shadow = QGraphicsDropShadowEffect(widget)
    shadow.setBlurRadius(blur)
    shadow.setOffset(0, y_offset)
    shadow.setColor(QColor(74, 97, 125, alpha))
    widget.setGraphicsEffect(shadow)


def circle_button(object_name: str, callback) -> QPushButton:
    button = QPushButton()
    button.setObjectName(object_name)
    button.setFixedSize(QSize(12, 12))
    button.clicked.connect(callback)
    button.setCursor(Qt.ArrowCursor)
    return button


def section(title: str, subtitle: str) -> QWidget:
    wrapper = QWidget()
    layout = QVBoxLayout(wrapper)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(3)
    head = QLabel(title)
    head.setObjectName("SectionTitle")
    desc = QLabel(subtitle)
    desc.setObjectName("MutedText")
    desc.setWordWrap(True)
    layout.addWidget(head)
    layout.addWidget(desc)
    return wrapper


def pill(text: str, tone: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("Pill")
    label.setProperty("pillTone", tone)
    label.setAlignment(Qt.AlignCenter)
    return label


def visible_pill(text: str, tone: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("UsablePill")
    label.setProperty("pillTone", tone)
    label.setAlignment(Qt.AlignCenter)
    return label


def set_pill_state(label: QLabel, text: str, tone: str) -> None:
    label.setText(text)
    label.setProperty("pillTone", tone)
    refresh_widget_style(label)


def refresh_widget_style(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


def divider(vertical: bool = False) -> QFrame:
    line = QFrame()
    line.setObjectName("Divider")
    if vertical:
        line.setFixedWidth(1)
        line.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
    else:
        line.setFixedHeight(1)
        line.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    return line


def compact_meta_text(meta: str) -> str:
    parts = [part.strip() for part in meta.split("·") if part.strip()]
    if len(parts) >= 2:
        return " · ".join(parts[-2:])
    return meta


def summary_note_card(title: str, detail: str, tone: str) -> tuple[QWidget, QLabel]:
    frame = QFrame()
    frame.setObjectName("PreferenceCard")
    frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(16, 16, 16, 16)
    layout.setSpacing(10)
    layout.addWidget(pill(title, tone), 0, Qt.AlignLeft)
    label = QLabel(detail)
    label.setObjectName("PreviewBody")
    label.setWordWrap(True)
    layout.addWidget(label)
    return frame, label


def compact_note_card(title: str, detail: str, tone: str) -> tuple[QWidget, QLabel]:
    frame = QFrame()
    frame.setObjectName("PreferenceCard")
    frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(14, 14, 14, 14)
    layout.setSpacing(8)
    layout.addWidget(pill(title, tone), 0, Qt.AlignLeft)
    label = QLabel(detail)
    label.setObjectName("PreviewBody")
    label.setWordWrap(True)
    layout.addWidget(label)
    return frame, label


def summary_strip_item(title: str, detail_label: QLabel, tone: str) -> QWidget:
    wrapper = QWidget()
    layout = QVBoxLayout(wrapper)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)
    layout.addWidget(pill(title, tone), 0, Qt.AlignLeft)
    layout.addWidget(detail_label)
    return wrapper


def sidebar_summary_card(title: str, lead: str, detail: str) -> QWidget:
    frame = QFrame()
    frame.setObjectName("PreferenceCard")
    frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(14, 14, 14, 14)
    layout.setSpacing(8)
    layout.addWidget(pill(title, "soft"), 0, Qt.AlignLeft)
    lead_label = QLabel(lead)
    lead_label.setObjectName("BodyStrong")
    lead_label.setWordWrap(True)
    detail_label = QLabel(detail)
    detail_label.setObjectName("MutedText")
    detail_label.setWordWrap(True)
    layout.addWidget(lead_label)
    layout.addWidget(detail_label)
    return frame


def settings_nav_item(
    title: str,
    badge_text: str,
    detail: str,
    tone: str,
    *,
    selected: bool,
) -> QWidget:
    frame = QFrame()
    frame.setObjectName("SettingsNavItem")
    frame.setProperty("selected", selected)
    refresh_widget_style(frame)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(16, 16, 16, 16)
    layout.setSpacing(10)
    layout.addWidget(
        icon_plate(icon_kind_for_label(title), tone if selected else "soft", 26),
        0,
        Qt.AlignCenter,
    )
    title_label = QLabel(title)
    title_label.setObjectName("UsableTitle")
    title_label.setAlignment(Qt.AlignCenter)
    title_label.setText(
        {
            "录音与转写": "Whisper",
            "导出与同步": "Obsidian",
            "智能整理": "LLM",
        }.get(title, title)
    )
    layout.addWidget(title_label)
    detail_label = QLabel(detail)
    detail_label.setObjectName("UsableMeta")
    detail_label.setAlignment(Qt.AlignCenter)
    detail_label.setWordWrap(True)
    layout.addWidget(detail_label)
    return frame


def value_text_role(label: str, value: str) -> str:
    normalized_label = label.casefold()
    normalized_value = value.casefold()
    technical_labels = {
        "session id",
        "base url",
        "服务地址",
        "同步地址",
        "转写服务",
        "音频工具",
        "whisper-server",
        "ffmpeg",
        "模型",
        "配置文件",
        "local rest api",
        "llm api key",
        "协议",
    }
    technical_markers = (
        "https://",
        "http://",
        "~/",
        "/opt/",
        "/bin/",
        "config.toml",
        "whisper-server",
        "ggml-",
        "chat_completions",
        "responses",
        "gpt-",
    )
    if normalized_label in technical_labels:
        return "TechValue"
    if any(marker in normalized_value for marker in technical_markers):
        return "TechValue"
    if value.startswith("/") or value.startswith("~"):
        return "TechValue"
    return "BodyStrong"


def fact_row(label: str, value: str) -> tuple[QWidget, QLabel, QLabel]:
    wrapper = QWidget()
    layout = QHBoxLayout(wrapper)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(12)
    caption = QLabel(label)
    caption.setObjectName("MetaKey")
    caption.setFixedWidth(88)
    detail = QLabel(value)
    detail.setObjectName(value_text_role(label, value))
    detail.setWordWrap(True)
    layout.addWidget(caption, 0, Qt.AlignTop)
    layout.addWidget(detail, 1)
    return wrapper, caption, detail


def preference_fact_row(label: str, value: str) -> tuple[QWidget, QLabel, QLabel]:
    wrapper = QWidget()
    layout = QHBoxLayout(wrapper)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(10)
    caption = QLabel(label)
    caption.setObjectName("MetaKey")
    caption.setFixedWidth(76)
    detail = QLabel(value)
    detail.setObjectName("PreviewBody")
    detail.setWordWrap(True)
    layout.addWidget(caption, 0, Qt.AlignTop)
    layout.addWidget(detail, 1)
    return wrapper, caption, detail


def build_event_row(text: str) -> tuple[QWidget, QLabel]:
    wrapper = QWidget()
    layout = QHBoxLayout(wrapper)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(10)
    bullet = QLabel("•")
    bullet.setObjectName("MetaKey")
    bullet.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
    bullet.setFixedWidth(10)
    label = QLabel(text)
    label.setObjectName("MutedText")
    label.setWordWrap(True)
    layout.addWidget(bullet, 0, Qt.AlignTop)
    layout.addWidget(label, 1)
    return wrapper, label


def icon_fact_row(label: str, value: str, tone: str) -> QWidget:
    wrapper = QWidget()
    wrapper.setAccessibleName(f"{label}: {value}")
    layout = QHBoxLayout(wrapper)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(12)
    layout.addWidget(icon_plate(icon_kind_for_label(label), tone, 18), 0, Qt.AlignTop)
    text = QVBoxLayout()
    text.setContentsMargins(0, 0, 0, 0)
    text.setSpacing(2)
    caption = QLabel(label)
    caption.setObjectName("UsableSetting")
    detail = QLabel(value)
    detail.setObjectName("UsableValue")
    detail.setWordWrap(True)
    text.addWidget(caption)
    text.addWidget(detail)
    layout.addLayout(text, 1)
    return wrapper


def icon_strip_card(
    texts: tuple[str, ...],
    tone: str,
    *,
    role: str = "PreferenceCard",
    size: int = 12,
    max_per_row: int = 5,
) -> QWidget:
    frame = QFrame()
    frame.setObjectName(role)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(14, 14, 14, 14)
    layout.setSpacing(10)
    layout.addWidget(
        IconSequence(
            icon_kinds_from_text(*texts, limit=max_per_row * 2),
            tone=tone,
            size=size,
            max_per_row=max_per_row,
            centered=True,
        )
    )
    for text in texts:
        semantic_label = QLabel(text)
        semantic_label.hide()
        layout.addWidget(semantic_label)
    return frame


def note_fact_item(label: str, value: str) -> QWidget:
    wrapper = QWidget()
    layout = QVBoxLayout(wrapper)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(3)
    caption = QLabel(label)
    caption.setObjectName("MetaKey")
    detail = QLabel(value)
    detail.setObjectName(value_text_role(label, value))
    detail.setWordWrap(True)
    layout.addWidget(caption)
    layout.addWidget(detail)
    return wrapper


def library_item(
    title: str,
    status: str,
    tone: str,
    meta: str,
    excerpt: str,
    footer: str,
    *,
    selected: bool,
) -> QWidget:
    frame = styled_frame("LibraryRow")
    frame.setProperty("selected", selected)
    refresh_widget_style(frame)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(12, 12, 12, 12)
    layout.setSpacing(6)

    heading = QHBoxLayout()
    heading.setSpacing(8)
    heading.addWidget(icon_plate("note", tone, 16), 0, Qt.AlignTop)
    title_label = QLabel(title)
    title_label.setObjectName("UsableSetting")
    title_label.setWordWrap(True)
    heading.addWidget(title_label, 1)
    heading.addWidget(visible_pill(status, tone), 0, Qt.AlignTop)
    layout.addLayout(heading)

    meta_label = QLabel(meta)
    meta_label.setObjectName("UsableMeta")
    meta_label.setWordWrap(True)
    meta_label.setText(compact_meta_text(meta))
    layout.addWidget(meta_label)

    semantic_excerpt = QLabel(excerpt)
    semantic_excerpt.hide()
    layout.addWidget(semantic_excerpt)
    semantic_footer = QLabel(footer)
    semantic_footer.hide()
    layout.addWidget(semantic_footer)
    return frame


def status_row(badge_text: str, label: str, value: str, tone: str) -> QWidget:
    wrapper = QWidget()
    layout = QHBoxLayout(wrapper)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(10)
    layout.addWidget(icon_plate(icon_kind_for_label(label), tone, 18), 0, Qt.AlignTop)
    content = QVBoxLayout()
    content.setContentsMargins(0, 0, 0, 0)
    content.setSpacing(2)
    head = QLabel(label)
    head.setObjectName("UsableSetting")
    detail = QLabel(value)
    detail.setObjectName("UsableValue")
    detail.setWordWrap(True)
    content.addWidget(head)
    content.addWidget(detail)
    layout.addLayout(content, 1)
    return wrapper


def dock_card(title: str, detail: str, tone: str) -> tuple[QWidget, QLabel, QLabel, IconSequence]:
    frame = QFrame()
    frame.setObjectName("PreferenceCard")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(16, 16, 16, 16)
    layout.setSpacing(12)
    title_label = icon_plate(icon_kind_for_label(title), tone, size=24)
    detail_label = QLabel(detail)
    detail_label.setObjectName("UsableMeta")
    detail_label.setAlignment(Qt.AlignCenter)
    detail_label.setWordWrap(True)
    signature = IconSequence(
        icon_kinds_from_text(title, detail),
        tone=tone,
        size=14,
        max_per_row=4,
        centered=True,
    )
    layout.addWidget(title_label, 0, Qt.AlignCenter)
    layout.addWidget(detail_label)
    layout.addWidget(signature, 0, Qt.AlignCenter)
    return frame, title_label, detail_label, signature


def settings_group(title: str, badge_text: str, tone: str, rows: list[tuple[str, str]]) -> QWidget:
    wrapper = QWidget()
    layout = QVBoxLayout(wrapper)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(10)
    heading = QHBoxLayout()
    heading.setSpacing(10)
    heading.addWidget(icon_plate(icon_kind_for_label(title), tone, 18), 0, Qt.AlignTop)
    title_label = QLabel(title)
    title_label.setObjectName("UsableTitle")
    heading.addWidget(title_label, 1)
    heading.addStretch(1)
    heading.addWidget(visible_pill(badge_text, tone), 0, Qt.AlignRight)
    layout.addLayout(heading)
    for index, (label, value) in enumerate(rows):
        layout.addWidget(icon_fact_row(label, value, tone))
        if index < len(rows) - 1:
            layout.addWidget(divider())
    return wrapper


def preference_sheet_section(
    title: str,
    summary: str,
    badge_text: str,
    tone: str,
    rows: list[tuple[str, str]],
    footer: str,
) -> QWidget:
    wrapper = QWidget()
    layout = QVBoxLayout(wrapper)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(10)

    heading = QHBoxLayout()
    heading.setSpacing(10)
    title_label = QLabel(title)
    title_label.setObjectName("CardTitle")
    heading.addWidget(title_label)
    heading.addStretch(1)
    heading.addWidget(pill(badge_text, tone), 0, Qt.AlignRight)
    layout.addLayout(heading)

    summary_label = QLabel(summary)
    summary_label.setObjectName("MutedText")
    summary_label.setWordWrap(True)
    layout.addWidget(summary_label)
    layout.addWidget(divider())
    rows_layout = QVBoxLayout()
    rows_layout.setSpacing(8)
    for index, (label, value) in enumerate(rows):
        row, _, _ = preference_fact_row(label, value)
        rows_layout.addWidget(row)
        if index < len(rows) - 1:
            rows_layout.addWidget(divider())
    layout.addLayout(rows_layout)

    layout.addWidget(divider())
    footer_label = QLabel(footer)
    footer_label.setObjectName("MutedText")
    footer_label.setWordWrap(True)
    layout.addWidget(footer_label)
    return wrapper


def preference_card(
    title: str,
    summary: str,
    badge_text: str,
    tone: str,
    rows: list[tuple[str, str]],
    footer: str,
) -> QWidget:
    frame = QFrame()
    frame.setObjectName("PreferenceCard")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(18, 18, 18, 18)
    layout.setSpacing(10)

    heading = QHBoxLayout()
    heading.setSpacing(10)
    title_label = QLabel(title)
    title_label.setObjectName("CardTitle")
    heading.addWidget(title_label)
    heading.addStretch(1)
    heading.addWidget(pill(badge_text, tone), 0, Qt.AlignRight)
    layout.addLayout(heading)

    summary_label = QLabel(summary)
    summary_label.setObjectName("MutedText")
    summary_label.setWordWrap(True)
    layout.addWidget(summary_label)
    layout.addWidget(divider())

    for index, (label, value) in enumerate(rows):
        row, _, _ = fact_row(label, value)
        layout.addWidget(row)
        if index < len(rows) - 1:
            layout.addWidget(divider())

    layout.addWidget(divider())
    footer_label = QLabel(footer)
    footer_label.setObjectName("MutedText")
    footer_label.setWordWrap(True)
    layout.addWidget(footer_label)
    return frame


def config_detail_box(label: str, value: str) -> tuple[QWidget, QLabel]:
    frame = panel("inset")
    frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(14, 14, 14, 14)
    layout.setSpacing(4)
    caption = QLabel(label)
    caption.setObjectName("MutedText")
    detail = QLabel(value)
    detail.setWordWrap(True)
    layout.addWidget(caption)
    layout.addWidget(detail)
    return frame, detail


def metric_card(title: str, value: str, detail: str, role: str = "inset") -> QWidget:
    frame = panel(role)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(18, 18, 18, 18)
    layout.setSpacing(6)
    caption = QLabel(title)
    caption.setObjectName("MutedText")
    number = QLabel(value)
    number.setObjectName("MetricValue")
    desc = QLabel(detail)
    desc.setObjectName("MutedText")
    desc.setWordWrap(True)
    layout.addWidget(caption)
    layout.addWidget(number)
    layout.addWidget(desc)
    return frame


def detail_box(label: str, value: str) -> QWidget:
    frame = panel("inset")
    frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(14, 14, 14, 14)
    layout.setSpacing(4)
    caption = QLabel(label)
    caption.setObjectName("MutedText")
    detail = QLabel(value)
    detail.setWordWrap(True)
    layout.addWidget(caption)
    layout.addWidget(detail)
    return frame


def detail_line(label: str, value: str) -> QWidget:
    wrapper = QWidget()
    layout = QVBoxLayout(wrapper)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(2)
    caption = QLabel(label)
    caption.setObjectName("MutedText")
    detail = QLabel(value)
    detail.setWordWrap(True)
    layout.addWidget(caption)
    layout.addWidget(detail)
    return wrapper


KEYWORD_ICON_KINDS = (
    ("新建", "new"),
    ("开始", "record"),
    ("现场", "live"),
    ("录音", "capture"),
    ("录制", "record"),
    ("导入", "import"),
    ("暂停", "pause"),
    ("继续", "play"),
    ("完成", "result"),
    ("后台", "spark"),
    ("整理", "spark"),
    ("摘要", "spark"),
    ("行动项", "spark"),
    ("导出", "export"),
    ("同步", "export"),
    ("obsidian", "export"),
    ("合并", "merge"),
    ("刷新", "refresh"),
    ("复查", "refresh"),
    ("课程", "note"),
    ("会议", "library"),
    ("记录", "note"),
    ("模型", "chip"),
    ("服务", "server"),
    ("音频", "wave"),
    ("ffmpeg", "wave"),
    ("目录", "folder"),
    ("session", "folder"),
    ("配置", "settings"),
    ("窗口", "window"),
    ("外壳", "window"),
    ("api", "link"),
    ("url", "link"),
    ("https://", "link"),
    ("http://", "link"),
)


def icon_kinds_from_text(*parts: str, limit: int = 4) -> tuple[str, ...]:
    kinds: list[str] = []
    text = " ".join(part.casefold() for part in parts if part)
    for keyword, kind in KEYWORD_ICON_KINDS:
        if keyword.casefold() in text and kind not in kinds:
            kinds.append(kind)
        if len(kinds) >= limit:
            break
    if not kinds:
        return ("dot",)
    return tuple(kinds[:limit])


def icon_kind_for_label(label: str) -> str:
    text = label.strip()
    mapping = {
        "新建记录": "new",
        "记录库": "library",
        "设置": "settings",
        "开始新记录": "new",
        "现场记录": "live",
        "导入录音": "import",
        "开始记录": "record",
        "开始下一条": "record",
        "结束记录": "stop",
        "暂停": "pause",
        "继续记录": "play",
        "结束并整理": "stop",
        "打开记录库": "library",
        "打开笔记": "note",
        "重新整理": "refresh",
        "刷新": "refresh",
        "合并记录": "merge",
        "更多操作": "more",
        "已完成": "result",
        "整理中": "spark",
        "待复查": "refresh",
        "当前": "current",
        "这条记录": "capture",
        "完成后": "result",
        "录音与转写": "mic",
        "导出与同步": "export",
        "智能整理": "spark",
        "Whisper": "mic",
        "Obsidian": "export",
        "LLM": "spark",
        "模型": "chip",
        "转写服务": "server",
        "音频工具": "wave",
        "会话目录": "folder",
        "配置文件": "settings",
        "录音链路": "mic",
        "Obsidian 同步": "export",
        "窗口外壳": "window",
        "Base URL": "link",
        "服务地址": "link",
    }
    return mapping.get(text, "dot")


def make_icon(kind: str, size: int = 20, color: str = "#34495D") -> QIcon:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor(color))
    pen.setWidthF(max(1.9, size * 0.1))
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)

    def rr(x: float, y: float, w: float, h: float, radius: int = 4) -> None:
        painter.drawRoundedRect(
            int(size * x),
            int(size * y),
            int(size * w),
            int(size * h),
            radius,
            radius,
        )

    def dl(x1: float, y1: float, x2: float, y2: float) -> None:
        painter.drawLine(int(size * x1), int(size * y1), int(size * x2), int(size * y2))

    def de(x: float, y: float, w: float, h: float) -> None:
        painter.drawEllipse(int(size * x), int(size * y), int(size * w), int(size * h))

    if kind == "new":
        de(0.2, 0.2, 0.6, 0.6)
        dl(0.5, 0.32, 0.5, 0.68)
        dl(0.32, 0.5, 0.68, 0.5)
    elif kind == "library":
        rr(0.18, 0.2, 0.64, 0.16, radius=3)
        rr(0.18, 0.42, 0.64, 0.16, radius=3)
        rr(0.18, 0.64, 0.64, 0.16, radius=3)
    elif kind == "settings":
        dl(0.22, 0.3, 0.78, 0.3)
        dl(0.22, 0.5, 0.78, 0.5)
        dl(0.22, 0.7, 0.78, 0.7)
        de(0.3, 0.24, 0.12, 0.12)
        de(0.56, 0.44, 0.12, 0.12)
        de(0.42, 0.64, 0.12, 0.12)
    elif kind == "live":
        de(0.34, 0.22, 0.32, 0.36)
        dl(0.5, 0.58, 0.5, 0.76)
        dl(0.34, 0.76, 0.66, 0.76)
    elif kind == "import":
        rr(0.24, 0.58, 0.52, 0.16, radius=3)
        dl(0.5, 0.22, 0.5, 0.58)
        dl(0.38, 0.42, 0.5, 0.58)
        dl(0.62, 0.42, 0.5, 0.58)
    elif kind == "record":
        painter.setBrush(QColor(color))
        de(0.28, 0.28, 0.44, 0.44)
    elif kind == "stop":
        rr(0.28, 0.28, 0.44, 0.44)
    elif kind == "pause":
        dl(0.38, 0.28, 0.38, 0.72)
        dl(0.62, 0.28, 0.62, 0.72)
    elif kind == "play":
        path = QPainterPath()
        path.moveTo(size * 0.36, size * 0.26)
        path.lineTo(size * 0.7, size * 0.5)
        path.lineTo(size * 0.36, size * 0.74)
        path.closeSubpath()
        painter.fillPath(path, QColor(color))
    elif kind == "note":
        rr(0.24, 0.18, 0.52, 0.64)
        dl(0.34, 0.4, 0.66, 0.4)
        dl(0.34, 0.56, 0.66, 0.56)
    elif kind == "refresh":
        path = QPainterPath()
        path.arcMoveTo(size * 0.22, size * 0.22, size * 0.56, size * 0.56, 20)
        path.arcTo(size * 0.22, size * 0.22, size * 0.56, size * 0.56, 20, 250)
        painter.drawPath(path)
        dl(0.62, 0.2, 0.76, 0.24)
        dl(0.62, 0.2, 0.66, 0.34)
    elif kind == "merge":
        dl(0.26, 0.34, 0.46, 0.34)
        dl(0.54, 0.66, 0.74, 0.66)
        dl(0.46, 0.34, 0.58, 0.5)
        dl(0.54, 0.66, 0.42, 0.5)
    elif kind == "current":
        de(0.28, 0.28, 0.44, 0.44)
        dl(0.5, 0.16, 0.5, 0.26)
    elif kind == "capture":
        rr(0.24, 0.24, 0.52, 0.52, radius=5)
        de(0.4, 0.4, 0.2, 0.2)
    elif kind == "result":
        rr(0.24, 0.18, 0.52, 0.64)
        dl(0.34, 0.62, 0.44, 0.72)
        dl(0.44, 0.72, 0.68, 0.42)
    elif kind == "mic":
        de(0.34, 0.2, 0.32, 0.42)
        dl(0.5, 0.62, 0.5, 0.78)
        dl(0.36, 0.78, 0.64, 0.78)
    elif kind == "export":
        rr(0.22, 0.26, 0.56, 0.18, radius=3)
        dl(0.5, 0.78, 0.5, 0.42)
        dl(0.38, 0.62, 0.5, 0.78)
        dl(0.62, 0.62, 0.5, 0.78)
    elif kind == "spark":
        path = QPainterPath()
        path.moveTo(size * 0.5, size * 0.16)
        path.lineTo(size * 0.58, size * 0.42)
        path.lineTo(size * 0.84, size * 0.5)
        path.lineTo(size * 0.58, size * 0.58)
        path.lineTo(size * 0.5, size * 0.84)
        path.lineTo(size * 0.42, size * 0.58)
        path.lineTo(size * 0.16, size * 0.5)
        path.lineTo(size * 0.42, size * 0.42)
        path.closeSubpath()
        painter.fillPath(path, QColor(color))
    elif kind == "more":
        painter.setBrush(QColor(color))
        de(0.24, 0.44, 0.1, 0.1)
        de(0.45, 0.44, 0.1, 0.1)
        de(0.66, 0.44, 0.1, 0.1)
    elif kind == "chip":
        rr(0.24, 0.24, 0.52, 0.52, radius=3)
        dl(0.5, 0.14, 0.5, 0.24)
        dl(0.5, 0.76, 0.5, 0.86)
        dl(0.14, 0.5, 0.24, 0.5)
        dl(0.76, 0.5, 0.86, 0.5)
    elif kind == "server":
        rr(0.22, 0.22, 0.56, 0.16, radius=3)
        rr(0.22, 0.42, 0.56, 0.16, radius=3)
        rr(0.22, 0.62, 0.56, 0.16, radius=3)
        de(0.64, 0.26, 0.06, 0.06)
        de(0.64, 0.46, 0.06, 0.06)
        de(0.64, 0.66, 0.06, 0.06)
    elif kind == "wave":
        path = QPainterPath()
        path.moveTo(size * 0.16, size * 0.56)
        path.cubicTo(size * 0.28, size * 0.24, size * 0.38, size * 0.78, size * 0.5, size * 0.46)
        path.cubicTo(size * 0.62, size * 0.14, size * 0.72, size * 0.72, size * 0.84, size * 0.4)
        painter.drawPath(path)
    elif kind == "folder":
        path = QPainterPath()
        path.moveTo(size * 0.18, size * 0.34)
        path.lineTo(size * 0.34, size * 0.34)
        path.lineTo(size * 0.42, size * 0.24)
        path.lineTo(size * 0.82, size * 0.24)
        path.lineTo(size * 0.82, size * 0.76)
        path.lineTo(size * 0.18, size * 0.76)
        path.closeSubpath()
        painter.drawPath(path)
    elif kind == "window":
        rr(0.18, 0.2, 0.64, 0.6)
        dl(0.18, 0.34, 0.82, 0.34)
        de(0.26, 0.25, 0.06, 0.06)
        de(0.38, 0.25, 0.06, 0.06)
    elif kind == "link":
        painter.drawArc(
            int(size * 0.18),
            int(size * 0.34),
            int(size * 0.3),
            int(size * 0.28),
            30 * 16,
            300 * 16,
        )
        painter.drawArc(
            int(size * 0.52),
            int(size * 0.34),
            int(size * 0.3),
            int(size * 0.28),
            210 * 16,
            300 * 16,
        )
        dl(0.42, 0.48, 0.58, 0.48)
    else:
        painter.setBrush(QColor(color))
        de(0.4, 0.4, 0.2, 0.2)

    painter.end()
    return QIcon(pixmap)


def apply_button_icon(
    button: QPushButton | QToolButton,
    label: str,
    size: int = 18,
    *,
    color: str = "#34495D",
    show_text: bool = False,
    display_text: str | None = None,
) -> None:
    button.setAccessibleName(label)
    button.setProperty("semanticLabel", label)
    button.setProperty("showLabel", show_text)
    button.setToolTip(label)
    button.setText((display_text or display_text_for_label(label)) if show_text else "")
    button.setIcon(make_icon(icon_kind_for_label(label), size=size, color=color))
    button.setIconSize(QSize(size, size))
    if isinstance(button, QToolButton):
        button.setToolButtonStyle(
            Qt.ToolButtonTextBesideIcon if show_text else Qt.ToolButtonIconOnly
        )
    refresh_widget_style(button)


def set_icon_plate(chip: QLabel, kind: str, tone: str, *, size: int | None = None) -> None:
    icon_size = size or int(chip.property("iconSize") or 18)
    color_map = {
        "accent": "#4F7FE8",
        "soft": "#50657A",
        "success": "#2F9C66",
        "danger": "#C56784",
    }
    chip.setProperty("iconSize", icon_size)
    chip.setProperty("pillTone", tone)
    chip.setProperty("iconKind", kind)
    chip.setPixmap(
        make_icon(kind, size=icon_size, color=color_map.get(tone, "#34495D")).pixmap(
            icon_size,
            icon_size,
        )
    )
    refresh_widget_style(chip)


def icon_plate(kind: str, tone: str, size: int = 28) -> QLabel:
    chip = QLabel()
    chip.setAlignment(Qt.AlignCenter)
    chip.setObjectName("IconPlate")
    chip.setFixedSize(max(size + 18, 30), max(size + 18, 30))
    set_icon_plate(chip, kind, tone, size=size)
    return chip


def action_button(text: str, role: str) -> QPushButton:
    mapping = {
        "primary": "PrimaryButton",
        "secondary": "SecondaryButton",
        "danger": "DangerButton",
        "ghost": "GhostButton",
    }
    button = QPushButton(text)
    button.setObjectName(mapping[role])
    size = 26 if role == "primary" else 18
    color_map = {
        "primary": "#FFFFFF",
        "secondary": "#42576D",
        "danger": "#B95C78",
        "ghost": "#667B91",
    }
    apply_button_icon(button, text, size=size, color=color_map[role])
    return button


def labeled_action_button(
    text: str,
    role: str,
    *,
    size: int | None = None,
    display_text: str | None = None,
) -> QPushButton:
    button = action_button(text, role)
    role_color = {
        "primary": "#FFFFFF",
        "secondary": "#42576D",
        "danger": "#B95C78",
        "ghost": "#667B91",
    }[role]
    apply_button_icon(
        button,
        text,
        size=size or (22 if role == "primary" else 18),
        color=role_color,
        show_text=True,
        display_text=display_text,
    )
    return button


def stacked_action_button(text: str, role: str, *, show_text: bool = False) -> QPushButton:
    button = action_button(text, role)
    button.setProperty("buttonContext", "stacked")
    if show_text:
        role_color = {
            "primary": "#FFFFFF",
            "secondary": "#42576D",
            "danger": "#B95C78",
            "ghost": "#667B91",
        }[role]
        apply_button_icon(button, text, size=18, color=role_color, show_text=True)
    refresh_widget_style(button)
    button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    return button


def more_actions_button(text: str, entries: list[str]) -> QToolButton:
    button = QToolButton()
    button.setObjectName("MoreButton")
    apply_button_icon(button, text)
    button.setPopupMode(QToolButton.InstantPopup)
    button.setToolButtonStyle(Qt.ToolButtonIconOnly)
    button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    menu = QMenu(button)
    for entry in entries:
        menu.addAction(entry)
    button.setMenu(menu)
    return button


def labeled_more_actions_button(text: str, entries: list[str]) -> QToolButton:
    button = more_actions_button(text, entries)
    apply_button_icon(
        button,
        text,
        size=18,
        color="#667B91",
        show_text=True,
    )
    return button


def flow_line(number: str, title: str, detail: str, tone: str) -> QWidget:
    wrapper = QWidget()
    layout = QHBoxLayout(wrapper)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(12)
    layout.addWidget(pill(number, tone), 0, Qt.AlignTop)

    text = QVBoxLayout()
    text.setSpacing(4)
    headline = QLabel(title)
    headline.setObjectName("CardTitle")
    desc = QLabel(detail)
    desc.setObjectName("MutedText")
    desc.setWordWrap(True)
    text.addWidget(headline)
    text.addWidget(desc)
    layout.addLayout(text, 1)
    return wrapper


def flow_step(number: str, title: str, detail: str, tone: str) -> QWidget:
    frame = panel("inset")
    layout = QHBoxLayout(frame)
    layout.setContentsMargins(14, 14, 14, 14)
    layout.setSpacing(12)
    layout.addWidget(pill(number, tone), 0, Qt.AlignTop)

    text = QVBoxLayout()
    text.setSpacing(4)
    headline = QLabel(title)
    headline.setObjectName("CardTitle")
    desc = QLabel(detail)
    desc.setObjectName("MutedText")
    desc.setWordWrap(True)
    text.addWidget(headline)
    text.addWidget(desc)
    layout.addLayout(text, 1)
    return frame


def note_card(title: str, detail: str, tone: str) -> QWidget:
    frame = panel("inset")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(14, 14, 14, 14)
    layout.setSpacing(8)
    layout.addWidget(pill(title, tone), 0, Qt.AlignLeft)
    text = QLabel(detail)
    text.setWordWrap(True)
    layout.addWidget(text)
    return frame
