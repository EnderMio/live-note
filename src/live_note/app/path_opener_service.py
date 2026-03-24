from __future__ import annotations

from collections.abc import Callable
from pathlib import Path


class PathOpenerService:
    def __init__(self, *, run: Callable[..., object]):
        self._run = run

    def open_path(self, path: Path) -> None:
        self._run(["open", str(path)], check=False)
