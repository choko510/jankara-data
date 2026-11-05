import json
import unittest
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "source" / "banban_prices.json"


class BanBanPriceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shop = json.loads(PATH.read_text(encoding="utf-8"))["shops"]["bb-00075"]

    def test_all_four_images_represented(self):
        shop = self.shop
        self.assertEqual(len(shop["regular"]), 3)
        self.assertEqual(len(shop["solo"]), 3)
        self.assertEqual(set(shop["source_images"]), {"weekday", "weekend", "solo_weekday", "solo_weekend"})

    def test_four_price_categories_and_solo(self):
        for mode in ("regular", "solo"):
            for table in self.shop[mode][:2]:
                self.assertEqual(len(table["plans"]), 3)
                for plan in table["plans"]:
                    self.assertEqual(len(plan["days"]), 4)
                    for day in plan["days"]:
                        self.assertEqual(set(day["prices"]), {"学生会員", "学生", "会員", "シニア", "一般"})

    def test_distinct_member_and_nonmember(self):
        weekday = self.shop["regular"][0]["plans"][0]["days"][-1]["prices"]
        self.assertEqual(weekday["学生"], 270)
        self.assertEqual(weekday["シニア"], 280)
        self.assertEqual(weekday["会員"], 300)
        self.assertEqual(weekday["一般"], 830)

    def test_hito_is_dedicated_table(self):
        weekday = self.shop["solo"][0]["plans"][0]["days"][-1]["prices"]
        self.assertEqual(weekday["学生"], 300)
        self.assertEqual(weekday["会員"], 350)
        self.assertEqual(weekday["一般"], 880)
        self.assertEqual(self.shop["solo"][1]["plans"][2]["days"][-1]["prices"]["会員"], 2140)

    def test_morning_member_only(self):
        regular = self.shop["regular"][2]["plans"][0]
        solo = self.shop["solo"][2]["plans"][0]
        self.assertEqual(regular["last_entry"], "12:00")
        self.assertEqual(regular["days"][-1]["prices"]["会員"], 450)
        self.assertEqual(solo["days"][-1]["prices"]["会員"], 700)
        for mode in ("regular", "solo"):
            self.assertNotIn("一般", self.shop[mode][2]["plans"][0]["days"][0]["prices"])

    def test_school_plan_not_regular_quote(self):
        offer = self.shop["specials"][0]
        self.assertEqual(offer["eligible"], "高校生以下2名以上")
        self.assertEqual(offer["days"][-1]["price"], 770)
        self.assertEqual(offer["days"][0]["price"], 1100)
        self.assertTrue(all(plan["name"] != "学バン" for t in self.shop["regular"] for plan in t["plans"]))


if __name__ == "__main__":
    unittest.main()
