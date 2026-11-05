#!/usr/bin/env python3
"""アプリへ配るファイルを dist/ に作る。

アプリはまず manifest.json（数百バイト）だけを読み、sha256 が変わったファイルだけをダウンロードする。
確認日時はファイルの外（manifest.json）に置くので、料金が同じなら確認しただけではダウンロードが起きない。
"""
from __future__ import annotations

import hashlib
import json
import lzma
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
MANIFEST_VERSION = 2
SHOPS_VERSION = 1
SOURCES = {
    "chains": ROOT / "chains.json",
    "jankara": ROOT / "source" / "prices.json",
    "bigecho": ROOT / "build" / "bigecho.json",
    "maneki": ROOT / "build" / "maneki.json",
    "kan": ROOT / "build" / "kan.json",
    "megabig": ROOT / "build" / "megabig.json",
    "joyjoy": ROOT / "build" / "joyjoy.json",
    "round1": ROOT / "build" / "round1.json",
}
# 前回よりこの割合を下回ったら、取得か変換の異常とみなして前回のファイルを残す
MIN_RATIO = 0.9
# この回の巡回で確かめた店とみなす幅。確認できなかった店にだけ checked_at を残す
# 取得スクリプトが 20 時間以内の店を飛ばすので、それより長くする
RUN_SECONDS = 21 * 3600
COORD_DIGITS = 5  # 約 1m。距離の計算にはこれで足りる
# 辞書を小さくしておくと、スマホが展開するときのメモリも辞書の大きさで済む
XZ_FILTERS = [{"id": lzma.FILTER_LZMA2, "preset": 9 | lzma.PRESET_EXTREME, "dict_size": 1 << 21,
               "lc": 4, "lp": 0, "pb": 0}]


def compact_shop(shop: dict, updated_at: int) -> dict:
    shop = dict(shop)
    if shop.get("checked_at", 0) >= updated_at - RUN_SECONDS:
        shop.pop("checked_at", None)
    for key in ("lat", "lng"):
        if isinstance(shop.get(key), float):
            shop[key] = round(shop[key], COORD_DIGITS)
    return shop


def compact(document: dict) -> dict:
    updated_at, shops = document["updated_at"], document["shops"]
    if isinstance(shops, dict):
        compacted = {key: compact_shop(shop, updated_at) for key, shop in shops.items()}
    else:
        compacted = [compact_shop(shop, updated_at) for shop in shops]
    return {"schema_version": SHOPS_VERSION, "shops": compacted}


def encode(document: dict) -> bytes:
    # key の順序を変えると、アプリが公式の表と比べるときに料金が変わったと誤認する
    text = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
    return lzma.compress(text.encode("utf-8"), format=lzma.FORMAT_XZ, check=lzma.CHECK_CRC32, filters=XZ_FILTERS)


def publish_file(name: str, document: dict, old: dict | None, dist: Path = DIST) -> tuple[dict, bool]:
    """新しい manifest の項目と、公開できたかどうかを返す。"""
    count = len(document["shops"])
    if old and count < old["count"] * MIN_RATIO:
        print(f"::error::{name}: 店の数が異常です ({old['count']} -> {count})。前回のファイルを残します", flush=True)
        return old, False
    data = encode(compact(document))
    (dist / f"{name}.json.xz").write_bytes(data)
    checked_at = max(document["updated_at"], old["checked_at"] if old else 0)
    print(f"{name}: {count} 店 {len(data):,} バイト", flush=True)
    return {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "count": count, "checked_at": checked_at}, True


def main() -> int:
    DIST.mkdir(exist_ok=True)
    path = DIST / "manifest.json"
    old = json.loads(path.read_text(encoding="utf-8"))["files"] if path.exists() else {}
    if "jankara" not in old and "prices" in old:
        old["jankara"] = old.pop("prices")
    legacy_file, jankara_file = DIST / "prices.json.xz", DIST / "jankara.json.xz"
    if legacy_file.exists():
        if not jankara_file.exists():
            legacy_file.replace(jankara_file)
        else:
            legacy_file.unlink()
    files, published = {}, True
    for name, source in SOURCES.items():
        # 未登録のROUND1店を空データとして配信しない。配信開始後に元データが
        # 欠損した場合は失敗して前回分を維持する。
        if name == "round1" and not source.exists() and name not in old:
            continue
        document = json.loads(source.read_text(encoding="utf-8"))
        files[name], ok = publish_file(name, document, old.get(name))
        published = published and ok
    manifest = {"schema_version": MANIFEST_VERSION, "files": files}
    path.write_text(json.dumps(manifest, separators=(",", ":"), sort_keys=True), encoding="utf-8", newline="")
    return 0 if published else 1


if __name__ == "__main__":
    sys.exit(main())
