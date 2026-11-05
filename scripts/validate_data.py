#!/usr/bin/env python3
"""Fail-closed validation before updating source data or publishing dist/.

The committed Git ref is the trusted comparison point. Do not silently accept
a parser/HTML/PDF/OCR format change merely because the number of shops is stable.
Existing unchanged legacy rows are tolerated, but newly introduced defects are not.
"""
from __future__ import annotations

import argparse
import difflib
import json
import lzma
import math
import re
import subprocess
import sys
import unicodedata
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
RAW = {
    "chains": "chains.json",
    "prices": "source/prices.json",
    "chain_prices": "source/chain_prices.json",
    "maneki_prices": "source/maneki_prices.json",
    "megabig_prices": "source/megabig_prices.json",
    "joyjoy_prices": "source/joyjoy_prices.json",
    "ocr": "source/ocr.json",
    "banban_prices": "source/banban_prices.json",
}
BUILT = {"chains": RAW["chains"], "jankara": RAW["prices"],
         "bigecho": "build/bigecho.json", "maneki": "build/maneki.json",
         "megabig": "build/megabig.json",
         "joyjoy": "build/joyjoy.json",
         "kan": "build/kan.json", "banban": RAW["banban_prices"]}
PREFIX = {
    "ビッグエコー": ("be-", "shop.big-echo.jp"),
    "カラオケ館": ("kan-", "karaokekan.jp"),
    "まねきねこ": ("mn-", "www.karaokemanekineko.jp"),
    "JOYSOUND": ("js-", "shop.joysound.com"),
    "コート・ダジュール": ("cd-", "www.cotedazur.jp"),
    "ROUND1": ("r1-", "www.round1.co.jp"),
    "BanBan": ("bb-", "karaoke-shin.jp"),
    "JOYJOY": ("jj-", "www.joyjoy.co.jp"),
    "CLUB DAM": ("dam-", "karaokeclub.jp"),
    "歌広場": ("uh-", "www.utahiro.com"),
    "メガビッグ": ("mb-", "megakara.jp"),
}
CLOCK = {"updated_at", "checked_at", "changed_at"}
JAPANESE_REGION = re.compile(r"^(?:北海道|東京都|大阪府|京都府|.{2,3}県)")
NO_HTML = re.compile(r"<(?:html|body|title|script|div|h[1-6]|!doctype)\b|(?:502 Bad Gateway|Access Denied|404 Not Found)", re.I)
PRICE_TEXT = re.compile(r"^(?:[¥￥]?\s*)?(\d{1,3}(?:,\d{3})*|\d+)(?:\s*円)?$")
MINIMUM_PRICE_TEXT = re.compile(r"^[¥￥]\s*(\d[\d,]*)~$")
MAX_ISSUES = 40


class DataRejected(ValueError):
    pass


def _meaning(value):
    if isinstance(value, dict):
        return {k: _meaning(v) for k, v in value.items() if k not in CLOCK}
    if isinstance(value, list):
        return [_meaning(v) for v in value]
    return value


def _json_bytes(raw: bytes, name: str) -> dict:
    try:
        doc = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise DataRejected(f"{name}: JSON を読み取れません: {exc}") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("shops"), (dict, list)):
        raise DataRejected(f"{name}: shops が配列または辞書ではありません")
    if doc.get("schema_version") not in (None, 1):
        raise DataRejected(f"{name}: 未対応の schema_version")
    if isinstance(doc.get("updated_at", 0), bool) or not isinstance(doc.get("updated_at", 0), int):
        raise DataRejected(f"{name}: updated_at が不正")
    return doc


def _records(doc: dict, name: str) -> dict:
    shops = doc["shops"]
    found = {}
    if isinstance(shops, dict):
        for key, item in shops.items():
            if not isinstance(key, str) or not key or not isinstance(item, dict):
                raise DataRejected(f"{name}: 店舗 ID またはレコードが不正")
            found[key] = item
    else:
        for item in shops:
            if not isinstance(item, dict) or isinstance(item.get("id"), bool) or not isinstance(item.get("id"), (int, str)):
                raise DataRejected(f"{name}: ID のない店舗データ")
            key = str(item["id"])
            if key in found:
                raise DataRejected(f"{name}: 店舗 ID 重複 {key}")
            found[key] = item
    if not found and name not in ("megabig_prices", "megabig", "joyjoy_prices", "joyjoy"):
        raise DataRejected(f"{name}: データが空です")
    return found


def _old_bytes(reference: str, path: str) -> bytes:
    result = subprocess.run(["git", "show", f"{reference}:{path}"], cwd=ROOT, capture_output=True)
    if result.returncode:
        if path in ("source/megabig_prices.json", "dist/megabig.json.xz", "source/joyjoy_prices.json", "dist/joyjoy.json.xz"):
            valid_ref = subprocess.run(["git", "rev-parse", "--verify", reference], cwd=ROOT, capture_output=True)
            if valid_ref.returncode:
                raise DataRejected(f"比較元 {reference} が不正です")
            empty = b'{"schema_version":1,"updated_at":0,"shops":{}}'
            return lzma.compress(empty) if path.startswith("dist/") else empty
        raise DataRejected(f"比較元 {reference}:{path} が読み取れません。保護のため中止")
    return result.stdout


def _string(value, max_len=180):
    return (isinstance(value, str) and 0 < len(value.strip()) <= max_len
            and not NO_HTML.search(value) and "\ufffd" not in value
            and all(ord(char) >= 32 or char == "\n" for char in value))


def _numbers(a, b):
    try:
        values = (float(a), float(b))
        if all(math.isfinite(v) for v in values):
            return values
    except (ValueError, TypeError):
        pass
    return None


def _distance_km(first, second):
    a = _numbers(first.get("lat"), first.get("lng"))
    b = _numbers(second.get("lat"), second.get("lng"))
    if not a or not b:
        return 0
    lat1, lng1 = map(math.radians, a)
    lat2, lng2 = map(math.radians, b)
    x = math.sin((lat2 - lat1) / 2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2)**2
    return 12742 * math.asin(min(1, math.sqrt(x)))


def _location(item, key, issues, *, chain=False):
    coords = _numbers(item.get("lat"), item.get("lng"))
    if not coords or not (20 <= coords[0] <= 46.5 and 122 <= coords[1] <= 154.5):
        issues.append(f"{key}: 緯度経度が日本国内の範囲外")
    if not _string(item.get("name"), 120):
        issues.append(f"{key}: 店舗名が空・HTML・不正な文字列")
    address = item.get("address")
    pref = item.get("prefecture")
    if not _string(pref, 12) or not JAPANESE_REGION.fullmatch(pref):
        issues.append(f"{key}: 都道府県が不正 ({pref!r})")
    if not _string(address, 180) or address.startswith("〒") or not address.startswith(pref if isinstance(pref, str) else "!!"):
        issues.append(f"{key}: 住所が不正／都道府県が住所と一致しません")
    if chain:
        group = item.get("chain")
        expected = PREFIX.get(group)
        if not expected or not str(key).startswith(expected[0]):
            issues.append(f"{key}: チェーン名と ID のプレフィックスが不一致")
        link = item.get("url")
        if not _string(link, 900):
            issues.append(f"{key}: 店舗 URL が不正")
        else:
            parsed = urlsplit(link)
            if not expected or parsed.scheme != "https" or parsed.hostname != expected[1] or not parsed.path.startswith("/"):
                issues.append(f"{key}: 店舗 URL のドメインがチェーンと不一致")
    else:
        link = item.get("url")
        if link and (urlsplit(link).hostname != "jankara.ne.jp"
                     or not link.startswith(f"https://jankara.ne.jp/shop/{key}/")):
            issues.append(f"{key}: ジャンカラの店舗番号と URL が不一致")


def _identity(old, new, key, issues):
    if not old:
        return
    if old.get("chain") != new.get("chain") and "chain" in old:
        issues.append(f"{key}: 同じ ID でチェーンが変化")
    if old.get("prefecture") and new.get("prefecture") != old.get("prefecture"):
        # Remove a previously leaked postal mark (CLUB DAM 黒磯店 etc.)
        # without treating the correction as a cross-prefecture relocation.
        before = str(old["prefecture"]).lstrip("〒").strip()
        if before != new.get("prefecture"):
            issues.append(f"{key}: 同じ ID で都道府県が変化")
    if _distance_km(old, new) > 10:
        issues.append(f"{key}: 同じ ID の座標が10km以上移動")
    old_address, new_address = old.get("address"), new.get("address")
    if isinstance(old_address, str) and isinstance(new_address, str):
        before = old_address.lstrip("〒").replace(" ", "").replace("　", "")
        after = new_address.lstrip("〒").replace(" ", "").replace("　", "")
        if before and after and before != after:
            ratio = difflib.SequenceMatcher(a=before, b=after).ratio()
            if ratio < 0.42 and _distance_km(old, new) < 1:
                issues.append(f"{key}: 同じ座標で住所が大きく変化 ({old_address!r} → {new_address!r})")
    first, second = old.get("name"), new.get("name")
    if isinstance(first, str) and isinstance(second, str) and first and second and first != second:
        ratio = difflib.SequenceMatcher(a=unicodedata.normalize("NFKC", first),
                                       b=unicodedata.normalize("NFKC", second)).ratio()
        if ratio < 0.30:
            issues.append(f"{key}: 店名が極端に変化 ({first!r} → {second!r})")
    if old.get("url") and new.get("url") and old["url"] != new["url"]:
        if urlsplit(old["url"]).hostname != urlsplit(new["url"]).hostname:
            issues.append(f"{key}: 店舗 URL のホストが変化")


def _price_values(tables, key, issues, *, min_price=0, allow_sparse=False):
    """Validate a source or converted price table.

    Raw HTML tables must have all source header columns. Converted tariffs may
    intentionally omit inapplicable customer types and insert empty day rows to
    stop prices being used outside their valid days. Even in converted tables,
    every populated key must belong to the declared headers, and a plan must
    contain at least one usable price.
    """
    if not isinstance(tables, list):
        issues.append(f"{key}: 料金表が配列ではありません")
        return {}
    found = {}
    for ti, table in enumerate(tables):
        path = f"{key}.table[{ti}]"
        if not isinstance(table, dict) or not isinstance(table.get("plans"), list):
            issues.append(f"{path}: 表・plans が不正")
            continue
        customers = table.get("customers")
        # 変換済みデータは customers が存在しないことがある。
        if customers is not None and (not isinstance(customers, list)
            or not customers or any(not _string(x, 70) for x in customers)
            or len(set(customers)) != len(customers)):
            issues.append(f"{path}: 料金区分の列見出しが不正")
            continue
        for pi, plan in enumerate(table["plans"]):
            rowpath = f"{path}.plans[{pi}]"
            if not isinstance(plan, dict) or not _string(plan.get("name"), 90):
                issues.append(f"{rowpath}: プラン名が不正")
                continue
            days = plan.get("days")
            if not isinstance(days, list) or not days:
                issues.append(f"{rowpath}: 曜日料金が空")
                continue
            usable_in_plan = False
            for di, day in enumerate(days):
                dp = f"{rowpath}.days[{di}]"
                if not isinstance(day, dict) or not _string(day.get("day"), 100) or not isinstance(day.get("prices"), dict):
                    issues.append(f"{dp}: 曜日または金額列が不正")
                    continue
                values = day["prices"]
                if customers is not None:
                    if allow_sparse:
                        # The converted result is not a rectangular source
                        # table; omitted customer types are intentional.
                        if not set(values).issubset(customers):
                            issues.append(f"{dp}: 定義されていない料金区分が混入")
                    elif set(values) != set(customers):
                        issues.append(f"{dp}: 金額の列数または見出しが一致しません")
                if not values and not allow_sparse:
                    issues.append(f"{dp}: 金額が空")
                bound = 2000 if "30分" in plan["name"] else 30000
                for customer, price in values.items():
                    if not _string(customer, 70):
                        issues.append(f"{dp}: 料金区分名が不正")
                        continue
                    if price is not None and (type(price) is not int or not min_price <= price <= bound):
                        issues.append(f"{dp}/{customer}: 不自然な料金 {price!r} (上限 {bound}円)")
                    else:
                        found[(ti, pi, di, customer)] = price
                        if price is not None:
                            usable_in_plan = True
            if allow_sparse and not usable_in_plan:
                issues.append(f"{rowpath}: 有効な料金がありません")
    return found


def _price_delta(before, after, key, issues):
    for position in before.keys() & after.keys():
        old, new = before[position], after[position]
        if not isinstance(old, int) or not isinstance(new, int) or old < 100:
            continue
        if new > old * 3 and new - old > 400:
            issues.append(f"{key}: 料金が急騰 {old}円 → {new}円 ({position})")
            break
        if 0 < new < old / 4 and old - new > 400:
            issues.append(f"{key}: 料金が急落 {old}円 → {new}円 ({position})")
            break


def _jankara(item, old, key, issues):
    _location(item, key, issues)
    if not str(key).isdigit() or item.get("id") != int(key):
        issues.append(f"{key}: ジャンカラ店舗番号が不一致")
    if type(item.get("closed", False)) is not bool:
        issues.append(f"{key}: closed が boolean ではありません")
    values = {}
    count = 0
    for kind in ("regular", "solo"):
        table = _price_values(item.get(kind), f"{key}.{kind}", issues)
        count += sum(v is not None for v in table.values())
        values.update({(kind,) + k: v for k, v in table.items()})
    if not any(v is not None for k, v in values.items() if k[0] == "regular") and not item.get("closed"):
        issues.append(f"{key}: 通常料金がすべて欠損")
    if old:
        old_values = {}
        for kind in ("regular", "solo"):
            for k, v in _price_values(old.get(kind), f"{key}.old.{kind}", [], min_price=0).items():
                old_values[(kind,) + k] = v
        if sum(v is not None for v in old_values.values()) >= 5 and count < sum(v is not None for v in old_values.values()) * 0.65:
            issues.append(f"{key}: 料金セルが大量に欠落")
        _price_delta(old_values, values, key, issues)


def _bigecho_raw(item, old, key, issues):
    sections = item.get("sections")
    if not isinstance(sections, list):
        issues.append(f"{key}: sections が配列ではありません")
        return
    if old and old.get("sections") and len(sections) < len(old["sections"]) * 0.6:
        issues.append(f"{key}: ビッグエコーの料金表が大量欠落")
    for i, section in enumerate(sections):
        if not isinstance(section, dict):
            issues.append(f"{key}: 料金セクションが不正")
            continue
        header, rows = section.get("header"), section.get("rows")
        if not isinstance(header, list) or len(header) < 2 or not isinstance(rows, list):
            issues.append(f"{key}.sections[{i}]: 表・ヘッダーの形式が変化")
            continue
        if any(not isinstance(x, str) for x in header):
            issues.append(f"{key}.sections[{i}]: 見出しの型が不正")
        for row in rows:
            if not isinstance(row, list) or len(row) > max(len(header) + 2, 12) or any(not isinstance(v, str) for v in row):
                issues.append(f"{key}.sections[{i}]: 行列の形が不正")
                continue
            for text in row[1:]:
                match = PRICE_TEXT.fullmatch(unicodedata.normalize("NFKC", text).replace(" ", ""))
                if match and int(match[1].replace(",", "")) > 30000:
                    issues.append(f"{key}.sections[{i}]: 料金に見えない巨大な金額")
    page = item.get("page")
    if page and (urlsplit(page).scheme != "https" or urlsplit(page).hostname not in ("big-echo.jp", "shop.big-echo.jp")):
        issues.append(f"{key}: 料金ページ URL が異常")


def _maneki_raw(item, old, key, issues):
    tables = item.get("tables")
    if not isinstance(tables, list):
        issues.append(f"{key}: PDF tokens が配列ではありません")
        return
    if old and old.get("tables") and not tables:
        issues.append(f"{key}: PDF から料金表が突然消失")
    for page in tables:
        if not isinstance(page, list):
            issues.append(f"{key}: PDF ページが配列ではありません")
            continue
        for token in page:
            if not isinstance(token, list) or len(token) != 5 or not isinstance(token[0], str) or not token[0].strip() or len(token[0]) > 400:
                issues.append(f"{key}: PDF 文字の形式が不正")
                break
            if not all(isinstance(x, (int, float)) and math.isfinite(x) for x in token[1:]):
                issues.append(f"{key}: PDF 文字座標が不正")
                break
            if token[1] > token[3] or token[2] > token[4]:
                issues.append(f"{key}: PDF 文字の座標が逆転")
                break
    pdf = item.get("pdf")
    if pdf and (urlsplit(pdf).scheme != "https" or urlsplit(pdf).hostname != "www.karaokemanekineko.jp"
                or not urlsplit(pdf).path.lower().endswith(".pdf")):
        issues.append(f"{key}: PDF の配布元が不正")


def _megabig_raw(item, old, key, issues):
    sections = item.get("sections")
    if not isinstance(sections, list) or not sections:
        issues.append(f"{key}: 料金表がありません")
        return
    if old and old.get("sections") and len(sections) < len(old["sections"]) * 0.6:
        issues.append(f"{key}: メガビッグの料金表が大量欠落")
    has_slot = False
    for i, section in enumerate(sections):
        if not isinstance(section, dict):
            issues.append(f"{key}: 料金セクションが不正")
            continue
        header, rows = section.get("header"), section.get("rows")
        if not isinstance(header, list) or len(header) < 2 or not isinstance(rows, list):
            issues.append(f"{key}.sections[{i}]: 表・ヘッダーの形式が変化")
            continue
        if any(not isinstance(value, str) for value in header):
            issues.append(f"{key}.sections[{i}]: 見出しの型が不正")
        if "30分" in unicodedata.normalize("NFKC", header[0]):
            has_slot = True
        if not rows:
            issues.append(f"{key}.sections[{i}]: 金額行がありません")
        for row in rows:
            if (not isinstance(row, list) or len(row) != len(header)
                    or any(not isinstance(value, str) for value in row)):
                issues.append(f"{key}.sections[{i}]: 金額の列数が見出しと一致しません")
                continue
            for value in row[1:]:
                match = PRICE_TEXT.fullmatch(unicodedata.normalize("NFKC", value).replace(" ", ""))
                if not match or int(match[1].replace(",", "")) > 30000:
                    issues.append(f"{key}.sections[{i}]: 金額が不正 ({value!r})")
    if not has_slot:
        issues.append(f"{key}: 30分料金表がありません")
    page = item.get("page")
    if page and (urlsplit(page).scheme != "https" or urlsplit(page).hostname != "megakara.jp"):
        issues.append(f"{key}: 料金ページ URL が異常")


def _ocr(item, old, key, issues):
    if item.get("chain") not in ("カラオケ館", "まねきねこ"):
        issues.append(f"{key}: OCR チェーンが不正")
    prefix = "kan-" if item.get("chain") == "カラオケ館" else "mn-"
    if not key.startswith(prefix):
        issues.append(f"{key}: OCR 店舗 ID とチェーンが不一致")
    reads = item.get("reads")
    if not isinstance(reads, list) or not 1 <= len(reads) <= 2:
        issues.append(f"{key}: OCR 回数が不正")
        return
    for read in reads:
        if not isinstance(read, str) or not 20 <= len(read) <= 200000 or "<table" not in read.lower():
            issues.append(f"{key}: OCR が料金表 HTML を返していません")
            break
    url = item.get("url")
    if url and (urlsplit(url).scheme != "https" or urlsplit(url).hostname not in (
        "karaokekan.net", "karaokekan.jp", "www.karaokemanekineko.jp")):
        issues.append(f"{key}: OCR 取得元 URL が不正")


def _generated(item, old, key, issues):
    if not isinstance(item, dict):
        issues.append(f"{key}: 変換済みレコードが不正")
        return
    values = {}
    for kind in ("regular", "solo"):
        if kind in item:
            for position, value in _price_values(item[kind], f"{key}.{kind}", issues, allow_sparse=True).items():
                values[(kind,) + position] = value
    if not any(v is not None for v in values.values()):
        issues.append(f"{key}: 配布する料金が空")
    if old:
        before = {}
        for kind in ("regular", "solo"):
            if kind in old:
                for position, value in _price_values(old[kind], f"{key}.old.{kind}", [], allow_sparse=True).items():
                    before[(kind,) + position] = value
        if before and len(values) < len(before) * 0.65:
            issues.append(f"{key}: 配布料金セルが大量に欠落")
        _price_delta(before, values, key, issues)


def _joyjoy_values(item):
    values = {}
    rates = item.get("rates", []) if isinstance(item, dict) else []
    if not isinstance(rates, list):
        return values
    for rate in rates:
        if not isinstance(rate, dict) or not isinstance(rate.get("windows"), list):
            continue
        for window in rate["windows"]:
            if isinstance(window, dict):
                values[(rate.get("period"), window.get("days", ""), window.get("hours", ""))] = window.get("minimum_yen")
    return values


def _joyjoy(item, old, key, issues, *, require_checked_at=False):
    if not isinstance(item, dict):
        issues.append(f"{key}: JOYJOY料金レコードが不正")
        return
    page = item.get("page")
    parsed = urlsplit(page) if isinstance(page, str) else None
    if (not _string(page, 900) or not parsed or parsed.scheme != "https"
            or parsed.hostname != "www.joyjoy.co.jp" or not parsed.path.startswith("/store/view/")):
        issues.append(f"{key}: JOYJOY公式店舗ページ URL が不正")
    checked = item.get("checked_at")
    if require_checked_at and (type(checked) is not int or checked <= 0):
        issues.append(f"{key}: checked_at が不正")
    rates = item.get("rates")
    if not isinstance(rates, list) or len(rates) != 3:
        issues.append(f"{key}: JOYJOY料金区分が不正")
        return
    seen = set()
    values = {}
    for ri, rate in enumerate(rates):
        path = f"{key}.rates[{ri}]"
        if not isinstance(rate, dict):
            issues.append(f"{path}: 料金区分が不正")
            continue
        period = rate.get("period")
        if period not in ("daytime", "nighttime", "freetime") or period in seen:
            issues.append(f"{path}: 時間帯が不正または重複")
            continue
        seen.add(period)
        expected_unit = "per_person_per_freetime" if period == "freetime" else "per_person_30_minutes"
        if rate.get("unit") != expected_unit or not _string(rate.get("charge"), 60):
            issues.append(f"{path}: 利用単位が不正")
        windows = rate.get("windows")
        if not isinstance(windows, list) or not 1 <= len(windows) <= 4:
            issues.append(f"{path}: 時間・曜日の行が不正")
            continue
        for wi, window in enumerate(windows):
            wp = f"{path}.windows[{wi}]"
            if not isinstance(window, dict) or not _string(window.get("hours"), 100):
                issues.append(f"{wp}: 時間帯が不正")
                continue
            if period == "freetime" and window.get("days") and not _string(window.get("days"), 100):
                issues.append(f"{wp}: 曜日区分が不正")
            amount = window.get("minimum_yen")
            bound = 30000 if period == "freetime" else 2000
            if type(amount) is not int or not 1 <= amount <= bound:
                issues.append(f"{wp}: 最低価格が不正 ({amount!r})")
            display = window.get("price_text")
            if not _string(display, 40):
                issues.append(f"{wp}: 公式価格表記が不正")
            else:
                match = MINIMUM_PRICE_TEXT.fullmatch(unicodedata.normalize("NFKC", display).replace(" ", ""))
                if not match or int(match[1].replace(",", "")) != amount:
                    issues.append(f"{wp}: 公式表記と最低価格が一致しません")
            values[(period, window.get("days", ""), window.get("hours", ""))] = amount
        included = rate.get("included", [])
        if not isinstance(included, list) or any(not _string(text, 180) for text in included):
            issues.append(f"{path}: 付帯サービスの説明が不正")
    if seen != {"daytime", "nighttime", "freetime"}:
        issues.append(f"{key}: DAY・NIGHT・FREE TIMEのいずれかが欠落")
    note = item.get("note", "")
    if note and not _string(note, 180):
        issues.append(f"{key}: 公式料金の注記が不正")
    if old:
        _price_delta(_joyjoy_values(old), values, key, issues)


def _validate_doc(name, current, previous, issues):
    fresh = _records(current, name)
    old = _records(previous, name) if previous.get("shops") else {}
    old_count, fresh_count = len(old), len(fresh)
    if fresh_count < old_count * (0.95 if name != "ocr" else 0.90):
        issues.append(f"{name}: 店舗数が異常に減少 ({old_count} → {fresh_count})")
    if old_count and fresh_count > old_count * 1.25 + 10:
        issues.append(f"{name}: 店舗数が異常に増加 ({old_count} → {fresh_count})")
    changed = [key for key, value in fresh.items() if _meaning(value) != _meaning(old.get(key))]
    if old_count > 50 and len(changed) > max(20, old_count * 0.35):
        issues.append(f"{name}: 変更店舗が全体の35%を超えています ({len(changed)}/{old_count})")
    if name == "chains":
        before = Counter(item.get("chain") for item in old.values())
        after = Counter(item.get("chain") for item in fresh.values())
        for group in PREFIX:
            if before[group] and after[group] < before[group] * 0.93:
                issues.append(f"{name}: {group} の店舗数が7%以上減少 ({before[group]} → {after[group]})")
            if before[group] and after[group] > before[group] * 1.22 + 5:
                issues.append(f"{name}: {group} の店舗数が異常増加")
    for key in changed:
        item, was = fresh[key], old.get(key)
        if name == "chains":
            _location(item, key, issues, chain=True)
            if item.get("id") != key:
                issues.append(f"{key}: 店舗辞書キーと内部 ID が不一致")
            _identity(was, item, key, issues)
        elif name == "prices":
            _jankara(item, was, key, issues)
            _identity(was, item, key, issues)
        elif name == "chain_prices":
            if not key.startswith("be-"):
                issues.append(f"{key}: Big Echo 以外の料金情報")
            _bigecho_raw(item, was, key, issues)
        elif name == "maneki_prices":
            if not key.startswith("mn-"):
                issues.append(f"{key}: まねきねこ以外の料金情報")
            _maneki_raw(item, was, key, issues)
        elif name == "megabig_prices":
            if not key.startswith("mb-"): issues.append(f"{key}: メガビッグ以外の料金情報")
            _megabig_raw(item, was, key, issues)
        elif name == "ocr":
            _ocr(item, was, key, issues)
        elif name == "banban_prices":
            if not key.startswith("bb-"):
                issues.append(f"{key}: BanBan以外の料金情報")
            _generated(item, was, key, issues)
            for special in item.get("specials", []):
                if not isinstance(special, dict) or not _string(special.get("name"), 90) or not _string(special.get("eligible"), 90):
                    issues.append(f"{key}: 条件付きプランの形式が不正")
                    continue
                if type(special.get("stay_minutes")) is not int or not 30 <= special["stay_minutes"] <= 720:
                    issues.append(f"{key}: 条件付きプランの滞在時間が不正")
                days = special.get("days")
                if not isinstance(days, list) or len(days) != 4 or any(not isinstance(d, dict) or not _string(d.get("day")) or type(d.get("price")) is not int or not 100 <= d["price"] <= 30000 for d in days):
                    issues.append(f"{key}: 条件付きプランの日別料金が不正")
        elif name in ("joyjoy_prices", "joyjoy"):
            _joyjoy(item, was, key, issues, require_checked_at=(name == "joyjoy_prices"))
        else:
            _generated(item, was, key, issues)
    return fresh, len(changed)


def run_validation(reference: str, stage: str) -> tuple[list[str], list[str]]:
    issues, summary = [], []
    raw = {}
    for name, file in RAW.items():
        current = _json_bytes((ROOT / file).read_bytes(), file)
        try:
            previous = _json_bytes(_old_bytes(reference, file), f"{reference}:{file}")
        except DataRejected:
            if name != "banban_prices":
                raise
            previous = {"shops": {}}
        raw[name], changes = _validate_doc(name, current, previous, issues)
        summary.append(f"{name}: {len(raw[name])} 件 (変更 {changes} 件)")
    known = raw["chains"]
    for kind, group in (("chain_prices", "ビッグエコー"), ("maneki_prices", "まねきねこ"), ("megabig_prices", "メガビッグ"), ("banban_prices", "BanBan")):
        for key in raw[kind]:
            if key in known and known[key].get("chain") != group:
                issues.append(f"{kind}/{key}: 店舗一覧とチェーンが矛盾")
            elif key not in known and key not in _records(_json_bytes(_old_bytes(reference, RAW[kind]), RAW[kind]), kind):
                issues.append(f"{kind}/{key}: 店舗一覧にない新規料金レコード")
    for key, row in raw["ocr"].items():
        if key in known and known[key].get("chain") != row.get("chain"):
            issues.append(f"ocr/{key}: 店舗一覧とチェーンが矛盾")
    for key, row in raw["joyjoy_prices"].items():
        if not key.startswith("jj-") or key not in known or known[key].get("chain") != "JOYJOY":
            issues.append(f"joyjoy_prices/{key}: 店舗一覧にないJOYJOY店舗")
        elif known[key].get("url") != row.get("page"):
            issues.append(f"joyjoy_prices/{key}: 料金表URLが店舗一覧と一致しません")
    if stage == "full":
        for name, file in BUILT.items():
            if name in ("chains", "jankara"):
                continue  # raw と同一: 既に検査済み
            current = _json_bytes((ROOT / file).read_bytes(), file)
            old_file = f"dist/{name}.json.xz"
            try:
                previous = _json_bytes(lzma.decompress(_old_bytes(reference, old_file)), f"{reference}:{old_file}")
            except DataRejected:
                if name != "banban":
                    raise
                previous = {"shops": {}}
            except lzma.LZMAError as exc:
                raise DataRejected(f"{name}: 前回の公開 XZ が読み取れません") from exc
            fresh, changes = _validate_doc(name, current, previous, issues)
            chain = {"bigecho": "ビッグエコー", "maneki": "まねきねこ", "kan": "カラオケ館", "megabig": "メガビッグ", "joyjoy": "JOYJOY", "banban": "BanBan"}[name]
            for key in fresh:
                if key not in known or known[key].get("chain") != chain:
                    if key not in _records(previous, name):
                        issues.append(f"build/{name}/{key}: 店舗一覧に存在しない店舗の料金")
            summary.append(f"build/{name}: {len(fresh)} 件 (変更 {changes} 件)")
        # Removing a source or unexpectedly losing a major part of the published
        # dataset must be stopped, even if the raw shop count stayed constant.
    return issues, summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate karaoke source and published data against a trusted git ref")
    parser.add_argument("--baseline", default="HEAD", help="trusted committed git ref, e.g. HEAD or origin/main")
    parser.add_argument("--stage", choices=("sources", "full"), default="full")
    args = parser.parse_args()
    try:
        issues, summary = run_validation(args.baseline, args.stage)
    except (DataRejected, OSError) as error:
        print(f"::error::データ検査を実行できません: {error}", file=sys.stderr)
        return 1
    for line in summary:
        print(f"[data-check] {line}", flush=True)
    if issues:
        print(f"::error::データ検査で {len(issues)} 件の異常を検出。既存データは公開せず維持します。",
              file=sys.stderr, flush=True)
        for issue in issues[:MAX_ISSUES]:
            print(f"::error::{issue}", file=sys.stderr)
        if len(issues) > MAX_ISSUES:
            print(f"... 以下 {len(issues) - MAX_ISSUES} 件省略", file=sys.stderr)
        return 1
    print("[data-check] PASS: 形式・値・店舗同一性・差分・公開料金を検査", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
