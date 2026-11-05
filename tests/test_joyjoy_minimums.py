"""JOYJOY公式最低料金を確定額として扱わないための回帰テスト。"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from update_joyjoy_prices import minimum_price

SOURCE = Path(__file__).resolve().parents[1] / "source/joyjoy_prices.json"


class JoyjoyMinimumTests(unittest.TestCase):
    def test_only_explicit_from_price_is_accepted(self):
        for text in ("￥450～", "450円～", "450〜", "¥450~"):
            value, original = minimum_price(text)
            self.assertEqual(value, 450)
            self.assertTrue(original)
        for text in ("450円", "450", "450-500円", "お問い合わせ", "無料"):
            with self.assertRaises(ValueError, msg=text):
                minimum_price(text)

    def test_initial_rates_remain_minimums_not_fixed_amounts(self):
        shops = json.loads(SOURCE.read_text(encoding="utf-8"))["shops"]
        self.assertEqual(len(shops), 107)
        for shop_id, shop in shops.items():
            rates = shop["rates"]
            self.assertEqual({rate["period"] for rate in rates},
                             {"daytime", "nighttime", "freetime"}, shop_id)
            for rate in rates:
                self.assertIn(rate["unit"], ("per_person_30_minutes", "per_person_per_freetime"))
                for window in rate["windows"]:
                    minimum, _ = minimum_price(window["price_text"])
                    self.assertEqual(minimum, window["minimum_yen"], shop_id)


if __name__ == "__main__":
    unittest.main()
