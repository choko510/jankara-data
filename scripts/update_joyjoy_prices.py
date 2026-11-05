#!/usr/bin/env python3
"""JOYJOY公式店舗ページの最低料金を取得して保存する。"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from bs4 import BeautifulSoup

import update_chains

ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = ROOT / "source" / "joyjoy_prices.json"
USER_AGENT = update_chains.USER_AGENT
ALLOWED_HOST = "www.joyjoy.co.jp"
SCHEMA_VERSION = 1
DELAY = 0.5
PARALLEL = 4
RECHECK_SECONDS = 20 * 3600
MIN_RATIO = 0.8
SAVE_EVERY = 20
PRICE = re.compile(r"^(?:[¥￥])?\s*(\d[\d,]*)\s*(?:円)?\s*[~～〜]$")
PERIODS = {"daytime": "DAY TIME", "nighttime": "NIGHT TIME", "freetime": "FREE TIME"}


class Throttled(RuntimeError):
    pass


class RequestPacer:
    """並列取得時もサイト全体へのリクエスト間隔を保つ。"""

    def __init__(self, interval: float):
        self.interval = interval
        self.next_at = 0.0
        self.lock = threading.Lock()

    def wait(self) -> None:
        with self.lock:
            due = max(time.monotonic(), self.next_at)
            self.next_at = due + self.interval
        delay = due - time.monotonic()
        if delay > 0:
            time.sleep(delay)


_thread_local = threading.local()


def worker_session() -> requests.Session:
    if not hasattr(_thread_local, "session"):
        _thread_local.session = requests.Session()
        _thread_local.session.headers.update({"User-Agent": USER_AGENT})
    return _thread_local.session


def normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value or "").split())


def lines(cell) -> list[str]:
    return [normalize(text) for text in cell.stripped_strings if normalize(text)]


def minimum_price(text: str) -> tuple[int, str]:
    display = normalize(text)
    match = PRICE.fullmatch(display)
    if not match:
        raise ValueError(f"最低価格を読み取れません: {display!r}")
    return int(match.group(1).replace(",", "")), display


def parse_page(url: str, markup: str) -> dict | None:
    """表の金額はすべて「～」付きの最低価格として保持する。"""
    soup = BeautifulSoup(markup, "html.parser")
    heading = next((item for item in soup.select("h5.h5_tit01")
                    if normalize(item.get_text(" ", strip=True)) == "料金表"), None)
    if heading is None:
        return None

    tables = []
    for node in heading.find_all_next():
        if node.name == "h5" and node is not heading:
            break
        if node.name != "table" or "price_info_list" not in node.get("class", []):
            continue
        title = node.select_one("th.daytime, th.nighttime, th.freetime")
        period = next((key for key, label in PERIODS.items()
                       if title is not None and label == normalize(title.get_text(" ", strip=True))), None)
        if period is None:
            raise ValueError("JOYJOY料金表の時間帯見出しが変わりました")

        row = next((tr for tr in node.select("tr")
                    if tr.find("th", recursive=False)
                    and tr.find("th", recursive=False) is not title
                    and len(tr.find_all("td", recursive=False)) == 2), None)
        if row is None:
            raise ValueError(f"{PERIODS[period]} の料金行が見つかりません")
        charge = normalize(row.find("th", recursive=False).get_text(" ", strip=True))
        schedule_cell, price_cell = row.find_all("td", recursive=False)
        schedules, price_texts = lines(schedule_cell), lines(price_cell)
        prices = [minimum_price(text) for text in price_texts]
        if not schedules or not prices:
            raise ValueError(f"{PERIODS[period]} の時間または金額が空です")

        if period == "freetime":
            if len(prices) != 1 or not schedules:
                raise ValueError("FREE TIMEの曜日・時間と最低価格の形式が変わりました")
            day_label = re.compile(r"^(?:日|月|火|水|木|金|土|平日|休日|祝|特日)")
            if len(schedules) % 2 == 0 and all(day_label.match(schedules[index])
                                               for index in range(0, len(schedules), 2)):
                windows = [
                    {"days": schedules[index], "hours": schedules[index + 1],
                     "minimum_yen": prices[0][0], "price_text": prices[0][1]}
                    for index in range(0, len(schedules), 2)
                ]
            else:
                # Some outlets name their free-time plan instead of listing clock hours.
                windows = [{"hours": " / ".join(schedules), "minimum_yen": prices[0][0],
                            "price_text": prices[0][1]}]
            unit = "per_person_per_freetime"
        else:
            if len(schedules) != len(prices):
                raise ValueError(f"{PERIODS[period]} の時間と最低価格の数が一致しません")
            windows = [
                {"hours": schedule, "minimum_yen": price[0], "price_text": price[1]}
                for schedule, price in zip(schedules, prices)
            ]
            unit = "per_person_30_minutes"

        included = []
        for description in node.select('td[colspan="3"]'):
            text = normalize(description.get_text(" ", strip=True))
            if text:
                included.append(text)
        tables.append({"period": period, "charge": charge, "unit": unit,
                       "windows": windows, "included": included})

    if not tables:
        raise ValueError("料金表見出しはありますが料金テーブルがありません")
    note = ""
    for paragraph in heading.find_all_next("p"):
        if paragraph.find_previous("h5", class_="h5_tit01") is not heading:
            break
        text = normalize(paragraph.get_text(" ", strip=True))
        if "料金" in text and ("曜日" in text or "時期" in text):
            note = text
            break
    return {"page": url, "rates": tables, "note": note, "checked_at": int(time.time())}


def load_previous() -> dict[str, dict]:
    if not DATA_FILE.exists():
        return {}
    return json.loads(DATA_FILE.read_text(encoding="utf-8")).get("shops", {})


def save(shops: dict[str, dict], updated_at: int) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": SCHEMA_VERSION, "updated_at": updated_at, "shops": shops}
    temporary = DATA_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
                         encoding="utf-8", newline="")
    os.replace(temporary, DATA_FILE)


def rate_count(shops: dict[str, dict]) -> int:
    return sum(bool(shop.get("rates")) for shop in shops.values())


def update(log=print) -> bool:
    previous = load_previous()
    chains = update_chains.load_previous()
    joyjoy = {key: item for key, item in chains.items() if item.get("chain") == "JOYJOY"}
    if not joyjoy:
        raise ValueError("chains.json にJOYJOY店舗がありません")

    previous = {key: value for key, value in previous.items() if key in joyjoy}
    previous_updated_at = 0
    if DATA_FILE.exists():
        previous_updated_at = json.loads(DATA_FILE.read_text(encoding="utf-8")).get("updated_at", 0)
    now = int(time.time())
    shops = dict(previous)
    due = [key for key, item in joyjoy.items()
           if now - shops.get(key, {}).get("checked_at", 0) >= RECHECK_SECONDS
           or shops.get(key, {}).get("page") != item.get("url")]
    pacer = RequestPacer(DELAY)
    throttled = threading.Event()

    def fetch(key: str) -> tuple[str, dict | None, str]:
        if throttled.is_set():
            return key, None, "公式サイトの制限で中止"
        url = joyjoy[key].get("url", "")
        if not url.startswith(f"https://{ALLOWED_HOST}/store/view/"):
            return key, None, "店舗URLがJOYJOY公式ドメインではありません"
        pacer.wait()
        if throttled.is_set():
            return key, None, "公式サイトの制限で中止"
        try:
            response = worker_session().get(url, timeout=35)
            if response.status_code in (429, 503):
                throttled.set()
                raise Throttled(f"HTTP {response.status_code}")
            response.raise_for_status()
            if not response.encoding or response.encoding.lower() == "iso-8859-1":
                response.encoding = "utf-8"
            return key, parse_page(url, response.text), ""
        except Throttled as exc:
            return key, None, str(exc)
        except (requests.RequestException, ValueError) as exc:
            return key, None, str(exc)

    failed = 0
    fetched = 0
    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        for key, record, error in pool.map(fetch, due):
            if error:
                log(f"  取得失敗 {key} ({error})")
                failed += 1
                continue
            if record is None:
                log(f"  料金表未掲載 {key}")
                continue
            shops[key] = record
            fetched += 1
            if fetched % SAVE_EVERY == 0 and rate_count(shops) >= rate_count(previous) * MIN_RATIO:
                save({key: shops[key] for key in joyjoy if key in shops}, previous_updated_at)

    shops = {key: shops[key] for key in joyjoy if key in shops}
    previous_count, current_count = rate_count(previous), rate_count(shops)
    if current_count < previous_count * MIN_RATIO:
        log(f"表を読めた店が異常に少ないので前回の値を残します ({previous_count} → {current_count})")
        return False
    updated_at = now if not failed else previous_updated_at
    save(shops, updated_at)
    log(f"JOYJOY: 最低料金 {current_count}/{len(joyjoy)} 店 (今回取得 {fetched}、失敗 {failed})")
    return not failed


def main() -> int:
    return 0 if update(log=lambda line: print(line, flush=True)) else 1


if __name__ == "__main__":
    sys.exit(main())
