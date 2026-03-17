from __future__ import annotations

from importlib.resources import files
from pathlib import Path


def brand_asset_path(*parts: str) -> Path:
    resource = files("live_note")
    for part in ("assets", "branding", *parts):
        resource = resource / part
    return Path(str(resource))


def brand_logo_svg_path() -> Path:
    return brand_asset_path("live-note-a1.svg")


def brand_logo_png_path() -> Path:
    return brand_asset_path("live-note-a1-256.png")
