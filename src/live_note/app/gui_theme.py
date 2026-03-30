from __future__ import annotations

import sys
from dataclasses import dataclass
from tkinter import Tk, ttk


@dataclass(frozen=True, slots=True)
class GuiPalette:
    app_bg: str
    surface_bg: str
    surface_alt_bg: str
    field_bg: str
    border: str
    text_primary: str
    text_secondary: str
    text_tertiary: str
    accent: str
    accent_active: str
    accent_text: str
    selection_bg: str
    progress_trough: str
    meter_no_signal: str
    meter_low: str
    meter_ok: str
    meter_high: str
    meter_clipping: str


@dataclass(frozen=True, slots=True)
class GuiMetrics:
    header_padding: tuple[int, int]
    page_padding: int
    section_padding: int
    section_gap: int
    inline_gap: int
    field_row_gap: int
    log_height: int


def default_gui_palette() -> GuiPalette:
    return GuiPalette(
        app_bg="#EEF2F7",
        surface_bg="#FBFCFE",
        surface_alt_bg="#F4F7FB",
        field_bg="#FFFFFF",
        border="#D7DEE8",
        text_primary="#1F2937",
        text_secondary="#5B6677",
        text_tertiary="#7B8797",
        accent="#2563EB",
        accent_active="#1D4ED8",
        accent_text="#FFFFFF",
        selection_bg="#E7EEF8",
        progress_trough="#DCE6F2",
        meter_no_signal="#C7D0DB",
        meter_low="#8DA3B8",
        meter_ok="#6E9D79",
        meter_high="#C99A56",
        meter_clipping="#C56A68",
    )


def default_gui_metrics() -> GuiMetrics:
    return GuiMetrics(
        header_padding=(20, 12),
        page_padding=14,
        section_padding=14,
        section_gap=12,
        inline_gap=8,
        field_row_gap=8,
        log_height=11,
    )


def _body_font(weight: str | None = None, size: int = 11) -> tuple[str, int] | tuple[str, int, str]:
    family = "SF Pro Text" if sys.platform == "darwin" else "TkDefaultFont"
    if weight:
        return (family, size, weight)
    return (family, size)


def apply_visual_theme(root: Tk) -> tuple[GuiPalette, GuiMetrics]:
    palette = default_gui_palette()
    metrics = default_gui_metrics()
    style = ttk.Style(root)
    style.theme_use("clam")
    root.configure(bg=palette.app_bg)

    style.configure("TFrame", background=palette.surface_bg)
    style.configure("Header.TFrame", background=palette.app_bg)
    style.configure("Toolbar.TFrame", background=palette.surface_bg)
    style.configure("TLabel", background=palette.surface_bg, foreground=palette.text_primary)
    style.configure(
        "Header.TLabel",
        background=palette.app_bg,
        foreground=palette.text_secondary,
        font=_body_font(size=11),
    )
    style.configure(
        "BrandTitle.TLabel",
        background=palette.app_bg,
        foreground=palette.text_primary,
        font=_body_font("bold", 20),
    )
    style.configure(
        "Status.TLabel",
        background=palette.app_bg,
        foreground=palette.text_secondary,
        font=_body_font("bold", 11),
    )
    style.configure(
        "SectionTitle.TLabel",
        background=palette.surface_bg,
        foreground=palette.text_primary,
        font=_body_font("bold", 11),
    )
    style.configure(
        "Hint.TLabel",
        background=palette.surface_bg,
        foreground=palette.text_tertiary,
        font=_body_font(size=10),
    )
    style.configure(
        "Subtle.TLabel",
        background=palette.surface_bg,
        foreground=palette.text_secondary,
        font=_body_font(size=11),
    )

    style.configure(
        "App.TNotebook",
        background=palette.app_bg,
        borderwidth=0,
        tabmargins=(0, 0, 0, 0),
    )
    style.configure(
        "App.TNotebook.Tab",
        background=palette.surface_alt_bg,
        foreground=palette.text_secondary,
        borderwidth=0,
        padding=(14, 8),
        font=_body_font(size=11),
    )
    style.map(
        "App.TNotebook.Tab",
        background=[
            ("selected", palette.surface_bg),
            ("active", palette.surface_bg),
        ],
        foreground=[
            ("selected", palette.text_primary),
            ("active", palette.text_primary),
        ],
    )

    style.configure(
        "Section.TLabelframe",
        background=palette.surface_bg,
        bordercolor=palette.border,
        borderwidth=1,
        relief="solid",
    )
    style.configure(
        "Section.TLabelframe.Label",
        background=palette.surface_bg,
        foreground=palette.text_primary,
        font=_body_font("bold", 11),
    )

    style.configure(
        "TButton",
        background=palette.surface_alt_bg,
        foreground=palette.text_primary,
        bordercolor=palette.border,
        borderwidth=1,
        focusthickness=0,
        focuscolor=palette.surface_alt_bg,
        padding=(12, 7),
        font=_body_font(size=11),
    )
    style.map(
        "TButton",
        background=[("active", palette.field_bg), ("pressed", palette.field_bg)],
        bordercolor=[("active", palette.border)],
    )
    style.configure(
        "Primary.TButton",
        background=palette.accent,
        foreground=palette.accent_text,
        bordercolor=palette.accent,
        focuscolor=palette.accent,
        padding=(14, 7),
        font=_body_font("bold", 11),
    )
    style.map(
        "Primary.TButton",
        background=[("active", palette.accent_active), ("pressed", palette.accent_active)],
        bordercolor=[("active", palette.accent_active)],
    )

    style.configure(
        "TCheckbutton",
        background=palette.surface_bg,
        foreground=palette.text_primary,
        font=_body_font(size=11),
    )
    style.map(
        "TCheckbutton",
        background=[("active", palette.surface_bg)],
        foreground=[("disabled", palette.text_tertiary)],
    )

    style.configure(
        "TEntry",
        fieldbackground=palette.field_bg,
        foreground=palette.text_primary,
        bordercolor=palette.border,
        lightcolor=palette.border,
        darkcolor=palette.border,
        insertcolor=palette.text_primary,
        padding=(8, 6),
    )
    style.configure(
        "TCombobox",
        fieldbackground=palette.field_bg,
        foreground=palette.text_primary,
        bordercolor=palette.border,
        lightcolor=palette.border,
        darkcolor=palette.border,
        padding=(8, 6),
        arrowsize=14,
    )
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", palette.field_bg)],
        background=[("readonly", palette.field_bg)],
        foreground=[("readonly", palette.text_primary)],
        selectbackground=[("readonly", palette.selection_bg)],
        selectforeground=[("readonly", palette.text_primary)],
    )

    style.configure(
        "App.Treeview",
        background=palette.field_bg,
        fieldbackground=palette.field_bg,
        foreground=palette.text_primary,
        bordercolor=palette.border,
        rowheight=28,
        relief="solid",
    )
    style.map(
        "App.Treeview",
        background=[("selected", palette.selection_bg)],
        foreground=[("selected", palette.text_primary)],
    )
    style.configure(
        "App.Treeview.Heading",
        background=palette.surface_alt_bg,
        foreground=palette.text_secondary,
        bordercolor=palette.border,
        relief="flat",
        padding=(8, 7),
        font=_body_font("bold", 10),
    )

    style.configure(
        "App.Horizontal.TProgressbar",
        background=palette.accent,
        troughcolor=palette.progress_trough,
        borderwidth=0,
        lightcolor=palette.accent,
        darkcolor=palette.accent,
    )
    for name, color in [
        ("NoSignal", palette.meter_no_signal),
        ("Low", palette.meter_low),
        ("OK", palette.meter_ok),
        ("High", palette.meter_high),
        ("Clipping", palette.meter_clipping),
    ]:
        style.configure(
            f"InputMeter.{name}.Horizontal.TProgressbar",
            background=color,
            troughcolor=palette.progress_trough,
            borderwidth=0,
            lightcolor=color,
            darkcolor=color,
        )
        style.configure(
            f"InputMeter.{name}.TLabel",
            background=palette.surface_bg,
            foreground=color,
            font=_body_font("bold", 10),
        )
    style.configure(
        "App.Vertical.TScrollbar",
        background=palette.surface_alt_bg,
        troughcolor=palette.app_bg,
        bordercolor=palette.app_bg,
        arrowcolor=palette.text_secondary,
    )

    return palette, metrics
