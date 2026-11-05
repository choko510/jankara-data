#!/usr/bin/env python3
"""B-30の4画像を取得し、目視確認済みの画像だけ料金データに反映する。

ocr/round1_b30_targets.json に登録した店舗を処理する。未確認・画像変更時は
work/round1_b30/<shop-id>/ に画像と確認用JSONを残し、古い料金データを維持する。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import requests
from PIL import Image, UnidentifiedImageError

ROOT = Path(__file__).resolve().parents[1]
DAYS = ("weekday", "friday", "saturday", "sunday")
MAX_BYTES = 2_000_000
ALLOWED_HOSTS = {"round1.co.jp", "www.round1.co.jp"}


def check_url(url: str) -> None:
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS
            or parsed.username is not None or parsed.password is not None
            or parsed.port not in (None, 443)):
        raise ValueError(f"未許可の画像URLです: {url}")


def fetch_image(session: requests.Session, url: str, path: Path) -> None:
    check_url(url)
    with session.get(url, stream=True, timeout=(15, 45), allow_redirects=False) as response:
        response.raise_for_status()
        if 300 <= response.status_code < 400:
            raise ValueError(f"画像URLがリダイレクトされました: {url}")
        if not response.headers.get("Content-Type", "").lower().startswith("image/"):
            raise ValueError(f"画像ではありません: {url}")
        content = bytearray()
        for chunk in response.iter_content(65536):
            content.extend(chunk)
            if len(content) > MAX_BYTES:
                raise ValueError(f"画像が大きすぎます: {url}")
        if not content:
            raise ValueError(f"空の画像です: {url}")
        path.write_bytes(content)
    try:
        with Image.open(path) as img:
            img.verify()
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError(f"画像が壊れています: {url}") from exc


def run_extractor(shop_id: str, directory: Path, verified: Path | None) -> dict | None:
    command = [sys.executable, str(ROOT / "scripts/extract_round1_b30.py")]
    for day in DAYS:
        command.extend([f"--{day}", str(directory / f"{day}.png")])
    command += ["--shop-id", shop_id, "--output", str(directory / "output.json"),
                "--audit", str(directory / "audit.json")]
    if verified is None:
        command += ["--draft", "--review-template", str(directory / "to_review.json")]
    else:
        command += ["--verified", str(verified)]
    done = subprocess.run(command, capture_output=True, text=True, check=False)
    print(f"{shop_id}: {done.stdout.strip()}", flush=True)
    if done.returncode:
        raise ValueError(f"{shop_id}: 画像OCR処理に失敗しました: {done.stderr.strip()}")
    if verified is None:
        return None
    doc = json.loads((directory / "output.json").read_text(encoding="utf-8"))
    record = doc["shops"][shop_id]
    # 時間帯は入室可能な時間。フリータイムの終了時刻は画像にないため、
    # 現行engine.jsではフリータイム料金として公開しない。
    record["regular"] = [table for table in record["regular"] if table["band"] == "30分"]
    if len(record["regular"]) != 1 or not record["regular"][0]["plans"]:
        raise ValueError(f"{shop_id}: 30分プランがありません")
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "ocr/round1_b30_targets.json")
    parser.add_argument("--output", type=Path, default=ROOT / "source/round1_b30.json")
    parser.add_argument("--work", type=Path, default=ROOT / "work/round1_b30")
    parser.add_argument("--status", type=Path, default=ROOT / "work/round1_b30/status.json")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    if config.get("schema_version") != 1 or not isinstance(config.get("shops"), dict):
        raise ValueError("b30_targets.json の形式が正しくありません")
    args.work.mkdir(parents=True, exist_ok=True)
    old = (json.loads(args.output.read_text(encoding="utf-8")) if args.output.exists()
           else {"schema_version": 1, "updated_at": 0, "shops": {}})
    if old.get("schema_version") != 1 or not isinstance(old.get("shops"), dict):
        raise ValueError("既存の source/round1_b30.json が不正です")
    from time import time
    now = int(time())
    updates = {}
    pending = []
    errors = []
    session = requests.Session()
    session.headers["User-Agent"] = "jankara-data/round1-b30/1.0"
    for shop_id, target in sorted(config["shops"].items()):
        if not re.fullmatch(r"r1-[A-Za-z0-9_-]+", shop_id):
            raise ValueError(f"店舗IDが不正: {shop_id}")
        if not isinstance(target, dict) or set(target) != {"images", "verified"}:
            raise ValueError(f"{shop_id}: imagesとverifiedが必要です")
        images = target["images"]
        if not isinstance(images, dict) or set(images) != set(DAYS):
            raise ValueError(f"{shop_id}: 4種類の画像URLが必要です")
        directory = args.work / shop_id
        directory.mkdir(parents=True, exist_ok=True)
        try:
            for day in DAYS:
                fetch_image(session, images[day], directory / f"{day}.png")
            review_name = target["verified"]
            reviewed = ROOT / review_name if review_name else None
            if reviewed and not reviewed.is_relative_to(ROOT):
                raise ValueError("確認済みJSONのパスがリポジトリ外です")
            if reviewed and not reviewed.is_file():
                reviewed = None
            if reviewed:
                manifest = json.loads(reviewed.read_text(encoding="utf-8"))
                import hashlib
                if any(hashlib.sha256((directory / f"{day}.png").read_bytes()).hexdigest()
                       != manifest.get(day, {}).get("_sha256") for day in DAYS):
                    reviewed = None
            result = run_extractor(shop_id, directory, reviewed)
            if result is None:
                pending.append(shop_id)
                print(f"::warning::{shop_id}: 未確認・変更画像。to_review.jsonの確認が必要です", flush=True)
            elif old["shops"].get(shop_id, {}).get("regular") != result["regular"]:
                updates[shop_id] = result
        except (ValueError, requests.RequestException, OSError) as exc:
            errors.append(f"{shop_id}: {exc}")
            print(f"::error::{shop_id}: {exc}", flush=True)

    # 一部の店だけ更新して部分配布することを避ける。
    published = bool(updates) and not pending and not errors
    if published:
        old["shops"].update(updates)
        old["updated_at"] = now
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temp = args.output.with_suffix(".json.tmp")
        temp.write_text(json.dumps(old, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        os.replace(temp, args.output)
    state = {"publish": published, "pending": pending, "errors": errors, "updated_shops": sorted(updates)}
    args.status.parent.mkdir(parents=True, exist_ok=True)
    args.status.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(state, ensure_ascii=False), flush=True)
    # 承認待ちは正常な定期実行として成功させない。
    # Actionsは失敗として通知するが、if:always()の成果物は保存される。
    if pending:
        print(f"::error::目視承認待ちのROUND1画像: {', '.join(pending)}", flush=True)
    return 1 if errors or pending else 0


if __name__ == "__main__":
    sys.exit(main())
