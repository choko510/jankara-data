#!/usr/bin/env python3
"""公式のメガビッグ店舗ページから店舗情報と料金表を取得する。"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
CHAIN_FILE = ROOT / "chains.json"
PRICE_FILE = ROOT / "source" / "megabig_prices.json"
INDEX_URL = "https://megakara.jp/shop/"
CHAIN = "メガビッグ"
USER_AGENT = "karaoke-auto-getter/1.0 (public price data; GitHub Actions)"
DELAY = 0.35
REQUEST_TIMEOUT = 35
PREFIX = "mb-"
PREFECTURE = re.compile(r"^(北海道|東京都|大阪府|京都府|.{2,3}県)")
SHOP_PATH = re.compile(r"^/shop/(\d{3,6})/?$")
MAP_COORDINATES = re.compile(r"!2d(-?\d+(?:\.\d+)?)!3d(-?\d+(?:\.\d+)?)")


class FetchStopped(RuntimeError):
    """The official site asked the crawler to stop."""


def clean(value: str | None) -> str:
    return " ".join((value or "").replace("\xa0", " ").split())


def get(session: requests.Session, url: str) -> requests.Response:
    for attempt in range(3):
        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT)
            if response.status_code in (429, 503):
                raise FetchStopped(f"HTTP {response.status_code}: {url}")
            response.raise_for_status()
            response.encoding = "utf-8"
            return response
        except FetchStopped:
            raise
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))
    raise AssertionError("unreachable")


def field_text(soup: BeautifulSoup, label: str) -> str:
    for term in soup.select("dt"):
        if clean(term.get_text(" ", strip=True)).startswith(label):
            definition = term.find_next_sibling("dd")
            return clean(definition.get_text(" ", strip=True)) if definition else ""
    return ""


def parse_sections(soup: BeautifulSoup) -> list[dict]:
    root = soup.select_one("#price")
    if root is None:
        return []
    sections = []
    for wrapper in root.select(".tbl-wrap"):
        table = wrapper.select_one("table.tbl")
        titlebar = wrapper.select_one(".tbl-ttl")
        head = table.select_one("tr.fttitle") if table else None
        cells = head.select("th,td") if head else []
        if not table or not titlebar or len(cells) < 2:
            continue
        header = [clean(cells[0].get_text("", strip=True))]
        header.extend(clean(cell.get_text(" ", strip=True)) for cell in cells[1:])
        rows = []
        for row in table.select("tr.pricebox"):
            name = row.find("th")
            values = []
            for cell in row.select("td"):
                number = re.sub(r"[^0-9]", "", cell.get_text(" ", strip=True))
                values.append(number)
            if not name or len(values) != len(header) - 1 or not all(values):
                rows = []
                break
            rows.append([clean(name.get_text(" ", strip=True)), *values])
        if not rows:
            continue
        note = clean(" ".join(p.get_text(" ", strip=True) for p in table.select("tr.txtbox p")))
        heading = next((clean(h.get_text(" ", strip=True)) for h in wrapper.find_all_previous("h3")
                        if clean(h.get_text(" ", strip=True))), "")
        sections.append({
            "heading": heading,
            "time": clean(titlebar.get_text(" ", strip=True)),
            "title": header[0],
            "note": note,
            "header": header,
            "rows": rows,
        })
    return sections


def parse_shop(shop_id: str, url: str, markup: str) -> tuple[dict, dict]:
    soup = BeautifulSoup(markup, "html.parser")
    heading = next((h for h in soup.select("h1,h2")
                    if "メガビッグ" in clean(h.get_text(" ", strip=True))), None)
    if heading is None:
        raise ValueError(f"店舗名を読めません: {url}")
    name = clean(heading.get_text(" ", strip=True))
    name = re.sub(r"^カラオケ\s*メガビッグ\s*", "", name)
    address = unicodedata.normalize("NFKC", field_text(soup, "住所"))
    address = re.sub(r"〒?\s*\d{3}-\d{4}", "", address)
    address = re.sub(r"\s+", "", address)
    prefecture = PREFECTURE.match(address)
    hours = field_text(soup, "営業時間")
    map_frame = soup.select_one('iframe[src*="google.com/maps/embed"]')
    map_src = map_frame.get("src", "") if map_frame else ""
    coordinates = MAP_COORDINATES.search(map_src)
    if not prefecture or not address or not hours or not coordinates:
        raise ValueError(f"店舗情報または地図座標が欠けています: {shop_id}")
    booking = next((urljoin(url, a.get("href", "")) for a in soup.select("a[href]")
                    if "WEB予約" in clean(a.get_text(" ", strip=True))
                    and urlsplit(urljoin(url, a.get("href", ""))).scheme == "https"), "")
    shop = {
        "id": PREFIX + shop_id,
        "chain": CHAIN,
        "name": name,
        "prefecture": prefecture.group(1),
        "address": address,
        "hours": hours,
        "lat": round(float(coordinates.group(2)), 6),
        "lng": round(float(coordinates.group(1)), 6),
        "url": url,
        "reserve": booking,
    }
    sections = parse_sections(soup)
    if not any("30分" in section["title"] for section in sections):
        raise ValueError(f"30分料金表を読めません: {shop_id}")
    prices = {
        "hours": hours,
        "sections": sections,
        "page": url,
        "scope": "all",
        "checked_at": int(time.time()),
    }
    return shop, prices


def shop_urls(session: requests.Session) -> list[tuple[str, str]]:
    response = get(session, INDEX_URL)
    soup = BeautifulSoup(response.text, "html.parser")
    found = {}
    for link in soup.select("a[href]"):
        absolute = urljoin(INDEX_URL, link.get("href", ""))
        if urlsplit(absolute).hostname != "megakara.jp":
            continue
        match = SHOP_PATH.fullmatch(urlsplit(absolute).path)
        if match:
            shop_id = match.group(1)
            found[shop_id] = f"https://megakara.jp/shop/{shop_id}/"
    if not found:
        raise ValueError("公式の店舗一覧から店舗 URL が見つかりません")
    return sorted(found.items())


def read_json(path: Path, default: dict) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n",
                         encoding="utf-8")
    os.replace(temporary, path)


def update(log=print) -> bool:
    old_chains = read_json(CHAIN_FILE, {"schema_version": 1, "updated_at": 0, "shops": []})
    old_prices = read_json(PRICE_FILE, {"schema_version": 1, "updated_at": 0, "shops": {}})
    previous = {row["id"]: row for row in old_chains.get("shops", []) if row.get("chain") == CHAIN}
    prices = dict(old_prices.get("shops", {}))
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    urls = shop_urls(session)
    if previous and len(urls) < len(previous) * 0.8:
        raise ValueError(f"公式の店舗数が異常に減少: {len(previous)} → {len(urls)}")

    current = {}
    failed = []
    for index, (shop_id, url) in enumerate(urls):
        if index:
            time.sleep(DELAY)
        key = PREFIX + shop_id
        try:
            response = get(session, url)
            shop, price = parse_shop(shop_id, url, response.text)
        except FetchStopped as error:
            failed.append(key)
            if key in previous:
                current[key] = previous[key]
            log(f"メガビッグ {key}: 公式サイトの制限で停止 ({error})")
            for pending_id, _ in urls[index + 1:]:
                pending_key = PREFIX + pending_id
                failed.append(pending_key)
                if pending_key in previous:
                    current[pending_key] = previous[pending_key]
            break
        except (requests.RequestException, ValueError, KeyError, AttributeError) as error:
            failed.append(key)
            if key in previous:
                current[key] = previous[key]
            log(f"メガビッグ {key}: 取得失敗 ({error})")
            continue
        current[key] = shop
        prices[key] = price

    # 公式一覧から消えた閉店済み店は、店名の閉店表記を残して画面側で除外する。
    for key, shop in previous.items():
        if key not in current and re.search(r"【\d{4}/\d{1,2}/\d{1,2}閉店】", shop.get("name", "")):
            current[key] = shop
    if not current or len(current) < len(previous) * 0.8:
        raise ValueError(f"取得できた店舗数が異常です: {len(previous)} → {len(current)}")

    # 価格ページが一時的に失敗した店は、前回の表を保持する。
    previous_price_count = len(old_prices.get("shops", {}))
    if previous_price_count and len(prices) < previous_price_count * 0.8:
        raise ValueError(f"料金表の店舗数が異常です: {previous_price_count} → {len(prices)}")

    now = int(time.time())
    chain_rows = [row for row in old_chains.get("shops", []) if row.get("chain") != CHAIN]
    chain_rows.extend(current.values())
    chain_rows.sort(key=lambda row: (row.get("chain", ""), row.get("id", "")))
    write_json(CHAIN_FILE, {
        "schema_version": old_chains.get("schema_version", 1),
        "updated_at": old_chains.get("updated_at", 0) if failed else now,
        "shops": chain_rows,
    })
    write_json(PRICE_FILE, {
        "schema_version": 1,
        "updated_at": old_prices.get("updated_at", 0) if failed else now,
        "shops": prices,
    })
    log(f"メガビッグ: {len(current)} 店、料金表 {len(prices)} 店 (失敗 {len(failed)})")
    return not failed


def main() -> int:
    try:
        return 0 if update(log=lambda line: print(line, flush=True)) else 1
    except (FetchStopped, requests.RequestException, ValueError, KeyError, TypeError) as error:
        print(f"メガビッグの更新を中止: {error}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
