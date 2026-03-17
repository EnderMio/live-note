from __future__ import annotations

import unittest
from importlib.resources import files


class BrandingAssetTests(unittest.TestCase):
    def test_branding_assets_exist(self) -> None:
        package_root = files("live_note")
        svg_path = package_root / "assets" / "branding" / "live-note-a1.svg"
        png_path = package_root / "assets" / "branding" / "live-note-a1-256.png"

        self.assertTrue(svg_path.is_file())
        self.assertTrue(png_path.is_file())


if __name__ == "__main__":
    unittest.main()
