"""GitHub Actions用の本物のTesseractを使う小さなOCR確認テスト。"""
from __future__ import annotations

import sys
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import extract_banban as ocr
import run_banban_ocr as runner


class OcrSmokeTests(unittest.TestCase):
    def test_real_tesseract_reads_red_price(self):
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 42)
        canvas = Image.new("RGB", (155, 62), "white")
        ImageDraw.Draw(canvas).text((10, 2), "270", fill=(255, 0, 0), font=font)
        self.assertEqual(ocr.number(np.asarray(canvas), "red", "smoke"), 270)

    def test_price_order_allows_duplicate_amounts(self):
        self.assertTrue(ocr.prices_in_expected_order([100, 100, 150, 200]))
        self.assertTrue(ocr.prices_in_expected_order([100, 150, 150, 200]))
        self.assertFalse(ocr.prices_in_expected_order([100, 200, 150, 250]))
        self.assertFalse(ocr.prices_in_expected_order([100, 200]))

    def test_approval_pins_four_images_and_price_table(self):
        shop = {"regular": [], "solo": [], "specials": []}
        images = {role: str(i + 1) * 64 for i, role in enumerate(runner.ROLES)}
        expected = runner.review_identity(shop, images)
        with tempfile.TemporaryDirectory() as directory:
            approvals = Path(directory) / "approved.json"
            approvals.write_text(json.dumps({
                "schema_version": 1, "shops": {"bb-00075": expected}
            }), encoding="utf-8")
            with mock.patch.object(runner, "APPROVALS", approvals):
                self.assertTrue(runner.is_approved("bb-00075", expected))
                self.assertFalse(runner.is_approved(
                    "bb-00075", runner.review_identity(
                        {**shop, "specials": [{"name": "changed"}]}, images)))
                self.assertFalse(runner.is_approved(
                    "bb-00075", runner.review_identity(
                        shop, {**images, "weekday": "f" * 64})))
                self.assertFalse(runner.is_approved("bb-other", expected))

    def test_rejects_redirect_before_contacting_target(self):
        with self.assertRaises(ValueError):
            runner.NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://evil.example/")

    def test_rejects_untrusted_url_and_unrelated_paths(self):
        self.assertFalse(runner.allowed_url("http://raw.githubusercontent.com/a/b.png"))
        self.assertFalse(runner.allowed_url("https://localhost/admin"))
        self.assertTrue(runner.allowed_url("https://raw.githubusercontent.com/org/repo/main/weekday.png"))
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                runner.image_path(str(Path(directory) / "outside.png"), "bb-00075", "weekday", Path(directory))


if __name__ == "__main__":
    unittest.main()
