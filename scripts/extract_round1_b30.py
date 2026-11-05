#!/usr/bin/env python3
"""ラウンドワン B-30 型の4画像を、カラオケナビ互換のJSONに変換。

対象: 提供された「平日(月～木) / 金・祝前日 / 土・三連休中日 / 日・祝」の4表。
セルの結合を格子線で識別し、金額だけをTesseractでOCRする。
未読・形の変化・異常値があれば保存しない（fail-closed）。
OCRは候補生成専用で、公開用データはSHA256で画像に結び付けた確認済みJSONが必要。

pip install pillow numpy pytesseract
別途 Tesseract OCR のインストールが必要。金額のOCRは英数字のみ。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

try:
    import pytesseract
except ImportError as exc:
    raise SystemExit("pip install pillow numpy pytesseract を実行してください") from exc

WIDTH = 1026
COLS = ((239, 435), (435, 632), (632, 821), (821, 1026))
COLUMN_NAMES = ("student_slot", "student_free", "general_slot", "general_free")
EXPECTED_HEADER_COLORS = {
    "weekday": (0, 117, 194),
    "friday": (243, 154, 0),
    "saturday": (19, 179, 183),
    "sunday": (232, 82, 152),
}
BANDS = {
    "weekday": (("開店", "13:00"), ("13:00", "18:00"), ("18:00", "30:00")),
    "friday": (("開店", "13:00"), ("13:00", "18:00"), ("18:00", "30:00")),
    "saturday": (("6:00", "9:00"), ("9:00", "12:00"), ("12:00", "18:00"), ("18:00", "30:00")),
    "sunday": (("6:00", "9:00"), ("9:00", "12:00"), ("12:00", "18:00"), ("18:00", "30:00")),
}
# 祝前日・三連休中日の詳細は画像の注記として保持。
# 既存 engine.js は「三連休中日」を解釈できないため、曜日の適用範囲を
# 過大に推測せず、「祝前」は金曜料金に含め、土曜料金を土曜日だけに使う。
CUSTOMERS = ["学生会員", "学生", "会員", "シニア", "一般"]
# 平日と金曜の表には祝日の適用と別表の重複を避けるため空の行を入れる。
DAYS = ["日祝", "土", "金祝前", "月火水木", "平日"]


class ReadError(ValueError):
    pass


def foreground(rgb: np.ndarray) -> np.ndarray:
    """黒背景に印刷された茶・青・赤色だけ抽出。罫線は枠を切って除く。"""
    r, g, b = (rgb[:, :, n].astype(np.int16) for n in range(3))
    dark = (r >= 14) & (r < 70) & (g >= 9) & (g < 55) & (b < 50) & (r > g + 4)
    red = (r > 75) & (g < 95) & (b < 140) & (r > g + 35)
    blue = (b > 70) & (r < 90) & (g < 110) & (b > r + 60)
    return dark | red | blue


def runs(present: np.ndarray) -> list[tuple[int, int]]:
    d = np.diff(np.pad(present.astype(np.int8), (1, 1)))
    return list(zip(np.flatnonzero(d == 1).tolist(), np.flatnonzero(d == -1).tolist()))


def row_lines(rgb: np.ndarray, kind: str) -> list[int]:
    """左端の時間帯欄の罫線から行境界を検出する。"""
    count = len(BANDS[kind])
    h = rgb.shape[0]
    approx = np.linspace(192 if count == 3 else 193, h - 1, count + 1)
    boundaries = []
    for y in approx[1:-1]:
        ys = range(max(194, int(y) - 4), min(h - 1, int(y) + 5))
        best = max(
            ys,
            key=lambda n: np.mean(np.all(rgb[n, 5:232] < 220, axis=1)),
        )
        coverage = np.mean(np.all(rgb[best, 5:232] < 220, axis=1))
        if coverage < 0.78:
            raise ReadError(f"{kind}: 時間帯の横罫線が見つかりません (y≈{y:.0f})")
        boundaries.append(best)
    return [191 if count == 3 else 192, *boundaries, h - 1]


def has_divider(rgb: np.ndarray, x1: int, x2: int, y: int) -> bool:
    # 結合セルには横罫線がない。長い線があるときだけ別セルと判断。
    for yy in range(y - 1, y + 2):
        color = np.all(rgb[yy, x1 + 9:x2 - 9] == (35, 24, 21), axis=1)
        if color.mean() >= 0.77:
            return True
    return False


def price_ocr(rgb: np.ndarray, *, red_morning: bool) -> int:
    """値段のある最下段を取り、末尾の「円」を除いて数字だけOCR。"""
    mask = foreground(rgb)
    y_groups = runs(mask.sum(axis=1) >= 5)
    y_groups = [(a, b) for a, b in y_groups if b - a >= 12]
    if not y_groups:
        raise ReadError("金額がある文字行を検出できません")
    # 結合セルの「朝割」2行表示は下の金額行だけを使用する。
    a, b = y_groups[-1]
    line = mask[a:b, :]
    x_groups = runs(line.sum(axis=0) >= 5)
    x_groups = [(l, r) for l, r in x_groups if r - l >= 2]
    if len(x_groups) < 2:
        raise ReadError(f"数字と円の文字分離に失敗しました: {x_groups}")
    # 最右の「円」グリフを除く。横書きの赤い「朝割580円」では
    # 先頭2文字（朝割）も除外する。
    body = x_groups[:-1]
    if red_morning and len(body) >= 5:
        body = body[2:]
    if not 1 <= len(body) <= 6:
        raise ReadError(f"価格の桁数が不正です: {body}")
    left, right = max(0, body[0][0] - 5), min(line.shape[1], body[-1][1] + 5)
    selected = line[:, left:right]
    # OCR結果はあくまで確認用の候補。公開用JSONは必ず --verified で目視照合する。
    def recognize(scale: int, psm: int) -> int | None:
        padded = np.pad(selected, ((15, 15), (15, 15)), constant_values=False)
        enlarged = np.kron(padded, np.ones((scale, scale), dtype=bool))
        img = Image.fromarray((~enlarged).astype(np.uint8) * 255)
        raw = pytesseract.image_to_string(
            img, lang="eng", config=f"--oem 1 --psm {psm} -c tessedit_char_whitelist=0123456789,"
        ).strip()
        digits = re.sub(r"[,\s]", "", raw)
        value = int(digits) if digits.isdigit() else None
        return value if value is not None and 30 <= value <= 6000 and value % 10 == 0 else None

    value = recognize(3, 13)
    if value is None:
        value = recognize(4, 13)
    if value is None:
        raise ReadError("数字の認識に失敗しました")
    if not 30 <= value <= 6000 or value % 10:
        raise ReadError(f"金額が範囲外・10円刻みでありません: {value}")
    return value


def extract_one(path: Path, kind: str, verified: dict | None = None, *, draft: bool = False) -> list[dict]:
    if verified is not None:
        got = hashlib.sha256(path.read_bytes()).hexdigest()
        if got != verified.get("_sha256"):
            raise ReadError(f"{kind}: 確認済みデータの画像SHA256が一致しません。古い料金を流用しないでください")
    image = Image.open(path).convert("RGB")
    if image.width != WIDTH or image.height not in (388, 389, 456):
        raise ReadError(f"{path.name}: 未対応の画像サイズ {image.size}（幅1026pxのB-30表専用）")
    rgb = np.asarray(image)
    color = np.asarray(EXPECTED_HEADER_COLORS[kind], dtype=int)
    if np.max(np.abs(rgb[30, 200].astype(int) - color)) > 8:
        raise ReadError(f"{path.name}: 曜日と見出しの色が一致しません")
    if kind in ("weekday", "friday") and image.height >= 400:
        raise ReadError(f"{path.name}: 3行の平日料金表ではありません")
    if kind in ("saturday", "sunday") and image.height < 400:
        raise ReadError(f"{path.name}: 4行の休日料金表ではありません")
    bounds = row_lines(rgb, kind)
    result = []
    matched = set()
    for col, (x1, x2) in enumerate(COLS):
        start = 0
        for boundary_index in range(1, len(bounds)):
            final = boundary_index == len(bounds) - 1
            if not final and not has_divider(rgb, x1, x2, bounds[boundary_index]):
                continue
            y1, y2 = bounds[start], bounds[boundary_index]
            chunk = rgb[y1 + 5:y2 - 3, x1 + 6:x2 - 6]
            red = (
                np.count_nonzero(
                    (chunk[:, :, 0] > 160) &
                    (chunk[:, :, 1] < 80) &
                    (chunk[:, :, 2] < 110)
                ) > 75
            )
            key = (COLUMN_NAMES[col], start, boundary_index)
            approval = None
            if verified is not None:
                choices = verified.get(COLUMN_NAMES[col], [])
                approval = next((row for row in choices
                                 if (row.get("from_row"), row.get("to_row")) == (start, boundary_index)), None)
                if approval is None or key in matched:
                    raise ReadError(f"{kind} {key}: 確認済みの行構成と異なります")
                matched.add(key)
                if type(approval.get("yen")) is not int or not 30 <= approval["yen"] <= 6000:
                    raise ReadError(f"{kind} {key}: 確認済み金額が不正")
                if approval.get("morning") is not bool(red):
                    raise ReadError(f"{kind} {key}: 朝割ラベルの色が確認済みの内容と異なります")
            try:
                ocr_price = price_ocr(chunk, red_morning=red)
                issue = None
            except ReadError as exc:
                ocr_price = None
                issue = str(exc)
                if approval is None and not draft:
                    raise ReadError(f"{kind} {key}: {exc}") from exc
            price = approval["yen"] if approval is not None else ocr_price
            if approval is not None and price != ocr_price:
                issue = f"確認済み値 {price} 円を採用／OCR {ocr_price} 円（{issue or '数値相違'}）"
            result.append({
                "kind": kind,
                "column": COLUMN_NAMES[col],
                "from_row": start,
                "to_row": boundary_index,
                "start": BANDS[kind][start][0],
                "end": BANDS[kind][boundary_index - 1][1],
                "yen": price,
                "morning": bool(red),
                "ocr_candidate": ocr_price,
                "review_issue": issue,
            })
            start = boundary_index
        if start != len(BANDS[kind]):
            raise ReadError(f"{kind}: 列 {col} に料金がありません")
        spans = [r for r in result if r["column"] == COLUMN_NAMES[col]]
        if spans[0]["from_row"] != 0 or spans[-1]["to_row"] != len(BANDS[kind]):
            raise ReadError(f"{kind}: 行が欠落しています")
        if any(a["to_row"] != b["from_row"] for a, b in zip(spans, spans[1:])):
            raise ReadError(f"{kind}: 行に隙間があります")
    if verified is not None:
        total = sum(len(verified.get(n, [])) for n in COLUMN_NAMES)
        if len(matched) != total:
            raise ReadError(f"{kind}: 確認済みデータに余分な行があります")
    return result


def to_tables(cells: list[dict]) -> list[dict]:
    """カラオケナビのregular[].plans[].days[].pricesの形にまとめる。"""
    grouped = {}
    for item in cells:
        slot = item["column"].endswith("_slot")
        band = "30分" if slot else "フリータイム"
        name = "30分" if slot else ("朝割フリータイム" if item["morning"] else "フリータイム")
        # 金額の間違った曜日への流用を防ぐため、適用除外の空価格行を先に並べる。
        if item["kind"] == "weekday":
            allowed = "月火水木"
        elif item["kind"] == "friday":
            allowed = "金祝前"
        elif item["kind"] == "saturday":
            allowed = "土"
        else:
            allowed = "日祝"
        window = f'{item["start"]}～{item["end"]}'
        key = (band, name, window)
        if key not in grouped:
            grouped[key] = {key: {} for key in DAYS}
        prices = grouped[key][allowed]
        group_names = ("学生会員", "学生", "シニア") if item["column"].startswith("student") else ("一般",)
        for group in group_names:
            old = prices.get(group)
            if old is not None and old != item["yen"]:
                raise ReadError(f"同じ曜日・時間・区分に価格が2件あります: {key}")
            prices[group] = item["yen"]
    table = []
    for band in ("30分", "フリータイム"):
        plans = []
        for (kind_band, name, window), day_map in sorted(grouped.items()):
            if kind_band != band:
                continue
            # 日祝や祝前日は「平日」より優先。空価格行も必要。
            day_rows = [{"day": day, "prices": day_map[day]} for day in DAYS]
            plans.append({
                "name": name,
                "hours": window,
                "last_entry": "",
                "days": day_rows,
                "drink": "ドリンク条件は公式の表で確認",
            })
        table.append({
            "band": band,
            "customers": CUSTOMERS,
            "plans": plans,
            "note": (
                "画像の曜日区分: 月～木／金・祝前日／土・三連休中日／日・祝日。"
                "三連休中日の判定は現行アプリ未対応。"
                "朝割は5時から受付（週末・祝日）。"
            ),
        })
    return table


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    for day in BANDS:
        p.add_argument("--" + day, type=Path, required=True)
    p.add_argument("--shop-id", required=True, help="店舗ID（例 r1-1234）。B-30は店舗IDではないので指定必須")
    p.add_argument("--output", type=Path, default=Path("round1_b30.json"))
    p.add_argument("--audit", type=Path, default=Path("round1_b30_audit.json"))
    p.add_argument("--merge", action="store_true", help="既存の出力JSONの他店舗を残して対象店舗だけ更新")
    p.add_argument("--verified", type=Path, help="画像SHA256付きの目視確認済みの照合JSON（OCR誤読を補正）")
    p.add_argument("--draft", action="store_true", help="OCRのたたき台を作る（配布用のJSONは作らない）")
    p.add_argument("--review-template", type=Path, default=Path("round1_b30_to_review.json"),
                   help="--draft で出力する目視確認用の下書きJSON")
    args = p.parse_args()
    if not re.fullmatch(r"r1-[a-zA-Z0-9_-]+", args.shop_id):
        p.error("--shop-id には r1- から始まる実際の店舗IDを指定してください")
    if args.draft and args.verified:
        p.error("--draft と --verified は同時に指定できません")
    if not args.draft and not args.verified:
        p.error("未確認のOCR結果を料金公開用JSONには変換しません。--draft または --verified を指定してください")
    try:
        verified = json.loads(args.verified.read_text(encoding="utf-8")) if args.verified else {}
        cells = [v for day in BANDS for v in extract_one(
            getattr(args, day), day, verified.get(day), draft=args.draft)]
        if args.draft:
            review = {}
            for day in BANDS:
                review[day] = {"_sha256": hashlib.sha256(getattr(args, day).read_bytes()).hexdigest()}
                for col in COLUMN_NAMES:
                    review[day][col] = [{"from_row": row["from_row"], "to_row": row["to_row"],
                                         "yen": row["yen"], "morning": row["morning"]}
                                        for row in cells if row["kind"] == day and row["column"] == col]
            args.review_template.write_text(json.dumps(review, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            args.audit.write_text(json.dumps({"shop_id": args.shop_id, "cells": cells},
                                              ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            issues = [c for c in cells if c.get("review_issue")]
            print(f"DRAFT: {len(cells)}セル、OCR失敗 {len(issues)}件。{args.review_template} を画像と照合し、"
                  f"正しい料金に直してから --verified で再実行してください。")
            return 0
        tables = to_tables(cells)
        now = int(time.time())
        payload = {"updated_at": now, "shops": {}}
        if args.merge and args.output.exists():
            payload = json.loads(args.output.read_text(encoding="utf-8"))
            if not isinstance(payload.get("shops"), dict):
                raise ReadError("既存JSONのshopsが辞書ではありません")
        payload["shops"][args.shop_id] = {"checked_at": now, "regular": tables}
        # 全部成功した後だけファイルを作る。
        args.audit.write_text(
            json.dumps({"shop_id": args.shop_id, "cells": cells}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        issues = [c for c in cells if c.get("review_issue")]
        print(f"OK: {len(cells)}セル抽出（OCR相違/保留 {len(issues)} 件）→ {args.output} / {args.audit}")
        return 0
    except (ReadError, OSError, ValueError) as exc:
        print(f"REJECT: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())