#!/usr/bin/env python3
"""BanBan料金表4枚から通常料金・ヒトカラ料金を抽出して source/banban_prices.json に保存する。

使用例:
python scripts/extract_banban.py --shop-id bb-00075 \
  --weekday 29995.png --weekend 29996.png \
  --solo-weekday 29997.png --solo-weekend 29998.png \
  --output source/banban_prices.json

依存: pip install -r requirements-banban.txt
OS側: tesseract コマンドが必要。読み取りに失敗したときは既存データを変更しない。
画像は1240x1754のBanBan料金表の同一レイアウト(比例拡大縮小可)を想定する。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image
import pytesseract


class ExtractionError(ValueError):
    pass


CUSTOMERS = ["学生会員", "学生", "会員", "シニア", "一般"]
# 画像の列: 学生、シニア、一般(会員)、非会員。
KEYS = ("学生会員", "シニア", "会員", "一般")


def bands(mask: np.ndarray) -> list[int]:
    h, w = mask.shape
    rows = np.flatnonzero(mask[:, int(w * .07):int(w * .93)].mean(axis=1) > .65)
    found = []
    previous = -100
    for row in rows:
        if int(row) != previous + 1:
            found.append([int(row)])
        else:
            found[-1].append(int(row))
        previous = int(row)
    return [group[0] for group in found if len(group) > w / 60]


def crop(image: np.ndarray, x1: float, y1: float, x2: float, y2: float, scale: float):
    x1, y1, x2, y2 = [round(v * scale) for v in (x1, y1, x2, y2)]
    if not (0 <= x1 < x2 <= image.shape[1] and 0 <= y1 < y2 <= image.shape[0]):
        raise ExtractionError("セルの切り出し位置が画像からはみ出しています")
    return image[y1:y2, x1:x2]


def number(region: np.ndarray, color: str, label: str) -> int:
    r, g, b = [region[:, :, i].astype(np.int32) for i in range(3)]
    if color == "red":
        mask = (r > 175) & (g < 125) & (b < 125)
    elif color == "purple":
        mask = (r > 70) & (r < 180) & (g < 135) & (b > 105)
    else:
        mask = (r < 100) & (g < 145) & (b > 125)
    if mask.sum() < 25:
        raise ExtractionError(f"{label}: 数字の色を検出できません")
    image = Image.fromarray(np.where(mask, 0, 255).astype("uint8"))
    image = image.resize((image.width * 3, image.height * 3), Image.Resampling.NEAREST)
    values = []
    for psm in (7, 6):
        result = pytesseract.image_to_string(
            image, config=f"--psm {psm} -c tessedit_char_whitelist=0123456789"
        ).strip()
        if not re.fullmatch(r"\d{3,5}", result):
            raise ExtractionError(f"{label}: OCR結果を確認できません: {result!r}")
        values.append(int(result))
    if values[0] != values[1]:
        raise ExtractionError(f"{label}: OCR結果が一致しません: {values}")
    if not 100 <= values[0] <= 9000:
        raise ExtractionError(f"{label}: 金額が異常です: {values[0]}")
    return values[0]


def prices_in_expected_order(values: list[int]) -> bool:
    # 同額は公式表で正常。金額の区分が逆転している場合のみOCR疑義。
    return len(values) == 4 and all(a <= b for a, b in zip(values, values[1:]))


def read_page(path: Path, solo: bool) -> dict:
    with Image.open(path) as file:
        image = np.asarray(file.convert("RGB"))
    height, width = image.shape[:2]
    factor = width / 1240
    if width < 800 or not 1.38 < height / width < 1.46:
        raise ExtractionError(f"{path}: 料金表の縦横比または解像度が想定外です")
    r, g, b = [image[:, :, i].astype(np.int32) for i in range(3)]
    ys = bands((r > 225) & (g > 125) & (g < 245) & (b < 65))
    ps = bands((r > 80) & (r < 170) & (g < 110) & (b > 165))
    bs = bands((r < 70) & (g < 120) & (b > 150))
    if len(ys) != 2 or len(ps) != 1:
        raise ExtractionError(f"{path}: 昼・夜料金または朝得の位置を検出できません")
    if (not solo and len(bs) != 1) or (solo and bs):
        raise ExtractionError(f"{path}: 通常料金とヒトカラの画像指定が違う疑いがあります")
    ys = [round(y / factor) for y in ys]
    p = round(ps[0] / factor)
    if not (350 <= ys[0] <= 580 and 800 <= ys[1] <= 1090 and 100 <= p <= 260):
        raise ExtractionError(f"{path}: 表の配置が想定と異なります")
    tables = []
    for ti, y in enumerate(ys):
        rows = []
        for ri in range(3):
            values = []
            for ci in range(4):
                x = 307 + 218 * ci
                cell = crop(image, x, y + 96 + 80 * ri, x + 185, y + 173 + 80 * ri, factor)
                values.append(number(cell, "red", f"{path.name}: {ti}/{ri}/{ci}"))
            # 同じ金額の区分は公式表でもあり得る。列順だけ確認する。
            if not prices_in_expected_order(values):
                raise ExtractionError(f"{path}: 列の読み違いの疑い: {values}")
            rows.append(values)
        tables.append(rows)
    morning = number(crop(image, 750, p + 91, 940, p + 151, factor), "purple", f"{path.name}:朝得")
    school = None
    if not solo:
        blue = round(bs[0] / factor)
        if not 1200 <= blue <= 1300:
            raise ExtractionError(f"{path}: 学バンの配置が想定外です")
        school = number(crop(image, 745, blue + 71, 955, blue + 139, factor), "blue", f"{path.name}:学バン")
    return {"tables": tables, "morning": morning, "school": school}


def prices(values: list[int]) -> dict:
    result = dict(zip(KEYS, values, strict=True))
    result["学生"] = result["学生会員"]
    return {name: result[name] for name in CUSTOMERS}


def day_rows(weekday: list[int], holiday: list[int], night=False) -> list[dict]:
    w, h = prices(weekday), prices(holiday)
    if night:
        return [{"day": name, "prices": w if name == "月火水木" else h}
                for name in ("祝前", "祝", "金土日", "月火水木")]
    return [{"day": name, "prices": w if name in ("祝前", "月火水木金") else h}
            for name in ("祝", "祝前", "土日", "月火水木金")]


def plans(weekday: dict, holiday: dict, weekday_morning: int, holiday_morning: int):
    output = []
    for index, band in enumerate(("昼料金", "夜料金")):
        rows = []
        for ri, name in enumerate(("30分", "3時間パック", "フリータイム")):
            night = index == 1
            rows.append({
                "name": name,
                "hours": "18:00～05:00" if night else ("10:00～20:00" if ri == 2 else "10:00～18:00"),
                "last_entry": "18:00" if not night and ri == 2 else "",
                "days": day_rows(weekday["tables"][index][ri], holiday["tables"][index][ri], night),
                "drink": "ソフトドリンクバー付",
            })
        output.append({"band": band, "customers": CUSTOMERS, "plans": rows})
    def special(value):
        return {"学生会員": value, "会員": value, "シニア": value}
    output.append({"band": "会員限定・朝得", "customers": ["学生会員", "会員", "シニア"],
                   "plans": [{"name": "朝得フリータイム", "hours": "10:00～14:00", "last_entry": "12:00",
                              "drink": "ソフトドリンクバー付", "days": [
                                  {"day": "祝", "prices": special(holiday_morning)},
                                  {"day": "祝前", "prices": special(weekday_morning)},
                                  {"day": "土日", "prices": special(holiday_morning)},
                                  {"day": "月火水木金", "prices": special(weekday_morning)}]}]})
    return output


def extract(paths: list[Path]) -> dict:
    weekday, holiday, solo_weekday, solo_holiday = [
        read_page(path, solo=i >= 2) for i, path in enumerate(paths)
    ]
    for a, b in ((weekday, holiday), (solo_weekday, solo_holiday)):
        for i in range(2):
            for j in range(3):
                if any(w > h for w, h in zip(a["tables"][i][j], b["tables"][i][j])):
                    raise ExtractionError("平日・土日画像の順序が異なる疑いがあります")
        if a["morning"] >= b["morning"]:
            raise ExtractionError("朝得料金の平日・土日が逆の疑いがあります")
    return {
        "regular": plans(weekday, holiday, weekday["morning"], holiday["morning"]),
        "solo": plans(solo_weekday, solo_holiday, solo_weekday["morning"], solo_holiday["morning"]),
        "specials": [{
            "name": "学バン", "hours": "10:00～22:00", "stay_minutes": 180,
            "eligible": "高校生以下2名以上", "included_drink": "ソフトドリンクバー付",
            "days": [{"day": "祝", "price": holiday["school"]},
                     {"day": "祝前", "price": weekday["school"]},
                     {"day": "土日", "price": holiday["school"]},
                     {"day": "月火水木金", "price": weekday["school"]}],
            "note": "金曜・祝前日18時以降の入室は土日祝料金。条件付きのため自動見積り対象外",
        }],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shop-id", required=True)
    parser.add_argument("--weekday", type=Path, required=True)
    parser.add_argument("--weekend", type=Path, required=True)
    parser.add_argument("--solo-weekday", type=Path, required=True)
    parser.add_argument("--solo-weekend", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("source/banban_prices.json"))
    parser.add_argument("--chains", type=Path, default=Path("chains.json"))
    args = parser.parse_args()
    if not re.fullmatch(r"bb-\d{5}", args.shop_id):
        parser.error("BanBanの店舗ID (bb-00075 など) を指定してください")
    known = json.loads(args.chains.read_text(encoding="utf-8"))["shops"]
    if not any(item["id"] == args.shop_id and item.get("chain") == "BanBan" for item in known):
        raise ExtractionError(f"{args.shop_id}: chains.jsonに該当するBanBan店がありません")
    paths = [args.weekday, args.weekend, args.solo_weekday, args.solo_weekend]
    if len(set(paths)) != 4:
        raise ExtractionError("別々の4枚の画像を指定してください")
    shop = extract(paths)
    now = int(time.time())
    shop["checked_at"] = now
    shop["source_images"] = dict(zip(("weekday", "weekend", "solo_weekday", "solo_weekend"),
                                    (p.name for p in paths), strict=True))
    if args.output.exists():
        document = json.loads(args.output.read_text(encoding="utf-8"))
        if document.get("schema_version") != 1 or not isinstance(document.get("shops"), dict):
            raise ExtractionError("既存のBanBan JSON形式が異なります")
    else:
        document = {"schema_version": 1, "updated_at": now, "shops": {}}
    document["shops"][args.shop_id] = shop
    document["updated_at"] = now
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(args.output)
    print(f"抽出成功: {args.shop_id} 通常・ヒトカラ各24セル + 朝得 + 学バン → {args.output}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ExtractionError, OSError, ValueError, pytesseract.TesseractError) as error:
        print(f"抽出失敗（元のJSONは維持）: {error}", file=sys.stderr)
        sys.exit(1)
