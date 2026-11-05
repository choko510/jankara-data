#!/usr/bin/env python3
"""GitHub Actions用: 料金表画像4枚の安全な取り込みとOCRの起動。

画像は source/banban_images/<shop-id>/{weekday,weekend,solo_weekday,solo_weekend}.png
にコミットするか、workflow_dispatchで公開HTTPS URLを指定する。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
INPUT_DIR = ROOT / "source" / "banban_images"
MAX_IMAGE_BYTES = 8 * 1024 * 1024
ALLOWED_HOSTS = {
    "github.com", "raw.githubusercontent.com", "user-images.githubusercontent.com",
    "private-user-images.githubusercontent.com", "karaoke-shin.jp", "www.karaoke-shin.jp",
}
ROLES = ("weekday", "weekend", "solo_weekday", "solo_weekend")
APPROVALS = ROOT / "ocr" / "banban_approved.json"
REVIEW_ROOT = ROOT / "work" / "banban_ocr"


class ReviewRequired(ValueError):
    pass


class NoRedirect(HTTPRedirectHandler):
    """HTTPリダイレクトを追う前に拒否する。想定外のホストに接続しない。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("リダイレクト先への画像取得は許可していません")


def review_identity(shop: dict, images: dict[str, str]) -> dict:
    """料金計算に使う欄と元画像を両方固定。時刻・一時ファイル名は署名対象外。"""
    fields = {key: shop[key] for key in ("regular", "solo", "specials")}
    canonical = json.dumps(fields, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    return {"images": images, "prices_sha256": hashlib.sha256(canonical).hexdigest()}


def is_approved(shop_id: str, identity: dict) -> bool:
    if not APPROVALS.is_file():
        return False
    approved = json.loads(APPROVALS.read_text(encoding="utf-8"))
    if approved.get("schema_version") != 1 or not isinstance(approved.get("shops"), dict):
        raise ValueError("OCR承認台帳の形式が不正です")
    return approved["shops"].get(shop_id) == identity


def publish_approved(shop_id: str, shop: dict) -> None:
    path = ROOT / "source" / "banban_prices.json"
    now = int(__import__("time").time())
    if path.exists():
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("schema_version") != 1 or not isinstance(document.get("shops"), dict):
            raise ValueError("既存のBanBan JSON形式が不正です")
    else:
        document = {"schema_version": 1, "shops": {}}
    document["shops"][shop_id] = shop
    document["updated_at"] = now
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def allowed_url(value: str) -> bool:
    parsed = urlsplit(value)
    hostname = (parsed.hostname or "").lower()
    try:
        return (parsed.scheme == "https" and not parsed.username and not parsed.password
                and parsed.port in (None, 443)
                and (hostname in ALLOWED_HOSTS or hostname.endswith(".githubusercontent.com")))
    except ValueError:
        return False


def image_path(source: str, shop_id: str, role: str, stage: Path) -> Path:
    source = source.strip() or f"source/banban_images/{shop_id}/{role}.png"
    if "://" in source:
        if not allowed_url(source):
            raise ValueError(f"{role}: HTTPSのGitHub/公式画像URLのみ指定できます")
        target = stage / f"{role}.png"
        request = Request(source, headers={"User-Agent": "jankara-data-banban-ocr/1.0"})
        # urllibのデフォルト動作では、応答の転送先へ先に接続してしまう。
        # 外部URLへの自動リダイレクトは初回リクエストの段階で禁止する。
        with build_opener(NoRedirect()).open(request, timeout=30) as response:
            if not allowed_url(response.url):
                raise ValueError(f"{role}: 最終画像URLが許可されていません")
            if int(response.headers.get("Content-Length", 0)) > MAX_IMAGE_BYTES:
                raise ValueError(f"{role}: 画像サイズが大きすぎます")
            data = response.read(MAX_IMAGE_BYTES + 1)
        if not data or len(data) > MAX_IMAGE_BYTES:
            raise ValueError(f"{role}: 画像が空、または8MBを超えています")
        target.write_bytes(data)
    else:
        if Path(source).is_absolute():
            raise ValueError(f"{role}: リポジトリ内の相対パスを指定してください")
        target = (ROOT / source).resolve()
        if not target.is_relative_to(INPUT_DIR.resolve()):
            raise ValueError(f"{role}: source/banban_images/ 配下のファイルに限定しています")
        if not target.is_file():
            raise FileNotFoundError(f"{role}: 画像がありません: {source}")
        if target.stat().st_size > MAX_IMAGE_BYTES:
            raise ValueError(f"{role}: 画像が8MBを超えています")
    # HTMLのエラーページや偽装ファイルをOCRに渡さない。
    from PIL import Image
    with Image.open(target) as image:
        if image.format not in {"PNG", "JPEG", "WEBP"}:
            raise ValueError(f"{role}: PNG/JPEG/WebP画像ではありません")
        image.verify()
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shop-id", default="bb-00075")
    for role in ROLES:
        parser.add_argument("--" + role.replace("_", "-"), default="")
    args = parser.parse_args()
    if not args.shop_id.startswith("bb-") or len(args.shop_id) != 8 or not args.shop_id[3:].isdigit():
        parser.error("店舗IDはbb-00075の形式を指定してください")
    with tempfile.TemporaryDirectory(prefix="banban-ocr-") as temp:
        stage = Path(temp)
        inputs = [image_path(getattr(args, role), args.shop_id, role, stage) for role in ROLES]
        images = {role: hashlib.sha256(path.read_bytes()).hexdigest()
                  for role, path in zip(ROLES, inputs, strict=True)}
        # 人が目視承認するまでは、絶対にsource/を書き換えない。
        candidate = stage / "candidate.json"
        command = [sys.executable, str(ROOT / "scripts" / "extract_banban.py"),
                   "--shop-id", args.shop_id, "--output", str(candidate)]
        for role, path in zip(ROLES, inputs, strict=True):
            command += ["--" + role.replace("_", "-"), str(path)]
        subprocess.run(command, cwd=ROOT, check=True)
        document = json.loads(candidate.read_text(encoding="utf-8"))
        shop = document["shops"][args.shop_id]
        identity = review_identity(shop, images)
        review_dir = REVIEW_ROOT / args.shop_id
        review_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(candidate, review_dir / "candidate.json")
        (review_dir / "to_review.json").write_text(
            json.dumps({"shop_id": args.shop_id, **identity}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        for role, path in zip(ROLES, inputs, strict=True):
            shutil.copyfile(path, review_dir / f"{role}.png")
        if not is_approved(args.shop_id, identity):
            raise ReviewRequired(
                f"{args.shop_id}: 未承認の料金画像です。Actionsの候補と4画像を目視確認し、"
                "ocr/banban_approved.jsonのshopsにto_review.jsonの内容（shop_idを除く）を追加してください"
            )
        publish_approved(args.shop_id, shop)
    print(f"{args.shop_id}: 画像と料金のハッシュが目視承認済み台帳に一致したため公開対象にしました")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError) as error:
        print(f"::error::BanBan画像抽出失敗: {error}", file=sys.stderr)
        sys.exit(1)
