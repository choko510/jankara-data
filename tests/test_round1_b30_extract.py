"""ROUND1 B-30変換の基礎テスト。画像がなくても曜日・区分の対応を検証する。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ocr import run_round1_b30 as runner

try:
    from scripts.extract_round1_b30 import ReadError, to_tables
except ImportError:
    ReadError = None
    to_tables = None


@unittest.skipIf(to_tables is None, "OCR画像用ライブラリをインストールしていません")
class TestB30Extract(unittest.TestCase):
    @staticmethod
    def cell(kind, column, start, end, yen, morning=False):
        return {"kind": kind, "column": column, "start": start, "end": end,
                "yen": yen, "morning": morning}

    def test_day_and_customer_mapping(self):
        cells = [
            self.cell("weekday", "student_slot", "開店", "13:00", 50),
            self.cell("weekday", "general_slot", "開店", "13:00", 50),
            self.cell("friday", "student_slot", "開店", "13:00", 50),
            self.cell("saturday", "student_slot", "6:00", "9:00", 320),
            self.cell("sunday", "student_slot", "6:00", "9:00", 320),
        ]
        slots = to_tables(cells)[0]
        self.assertEqual(slots["band"], "30分")
        weekday = next(p for p in slots["plans"] if p["hours"] == "開店～13:00")
        price = {row["day"]: row["prices"] for row in weekday["days"]}
        self.assertEqual(price["月火水木"]["学生"], 50)
        self.assertEqual(price["月火水木"]["シニア"], 50)
        self.assertEqual(price["月火水木"]["一般"], 50)
        self.assertEqual(price["金祝前"]["学生会員"], 50)
        self.assertEqual(price["土"], {})
        self.assertEqual(price["日祝"], {})

    def test_combined_free_cell_preserves_entry_range(self):
        free = to_tables([self.cell("weekday", "student_free", "開店", "18:00", 480, True)])[1]
        self.assertEqual(free["plans"][0]["hours"], "開店～18:00")
        self.assertEqual(free["plans"][0]["name"], "朝割フリータイム")

    def test_conflicting_prices_are_rejected(self):
        a = self.cell("weekday", "student_slot", "13:00", "18:00", 150)
        b = self.cell("weekday", "student_slot", "13:00", "18:00", 999)
        with self.assertRaises(ReadError):
            to_tables([a, b])



class TestRunnerReviewGate(unittest.TestCase):
    def test_unapproved_target_marks_action_failed_without_publishing(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config = base / "targets.json"
            output = base / "round1.json"
            work = base / "work"
            status = work / "status.json"
            config.write_text(json.dumps({"schema_version": 1, "shops": {
                "r1-01234": {
                    "images": {day: f"https://www.round1.co.jp/{day}.png" for day in runner.DAYS},
                    "verified": "",
                }
            }}), encoding="utf-8")
            argv = ["run_round1_b30.py", "--config", str(config), "--output", str(output),
                    "--work", str(work), "--status", str(status)]
            def fake_fetch(_session, _url, path):
                path.write_bytes(b"image")
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(runner, "fetch_image", side_effect=fake_fetch), \
                 mock.patch.object(runner, "run_extractor", return_value=None):
                exit_code = runner.main()
            self.assertEqual(exit_code, 1)
            self.assertFalse(output.exists())
            report = json.loads(status.read_text(encoding="utf-8"))
            self.assertFalse(report["publish"])
            self.assertEqual(report["pending"], ["r1-01234"])


if __name__ == "__main__":
    unittest.main()
