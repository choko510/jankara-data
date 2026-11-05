# jankara-data

カラオケナビが使う店舗と料金のデータを公開するリポジトリです。
GitHub Actions が毎日 04:10（日本時間）に各チェーンの公式サイトから作ります。

## アプリへ配るもの（`dist/`）

| ファイル | 内容 |
|---|---|
| `manifest.json` | 各ファイルの sha256、大きさ、店の数、確認日時 |
| `chains.json.xz` | 11 チェーンの店舗（名前・住所・座標・公式の予約ページ） |
| `jankara.json.xz` | ジャンカラの料金 |
| `bigecho.json.xz` | ビッグエコーの金額（変換できた店だけ） |
| `maneki.json.xz` | まねきねこの金額（変換できた店だけ） |
| `megabig.json.xz` | メガビッグの金額（変換できた店だけ） |
| `joyjoy.json.xz` | JOYJOY公式ページの最低料金（「～」付き。確定料金ではありません） |
| `kan.json.xz` | カラオケ館の金額（変換できた店だけ） |
| `banban.json.xz` | BanBanの通常・ヒトカラ料金（画像で確認できた店舗のみ） |

通信量を抑えるため、アプリは毎日 `manifest.json` だけを読み、sha256 が変わったファイルだけをダウンロードします。
確認日時は `manifest.json` にあり、確認できなかった店にだけ店ごとの `checked_at` が残ります。

料金は参考情報です。正しい料金は各チェーンの公式サイトで確認してください。

## 作り方

- `chains.json` と `source/` は公式サイトから読んだ元データです。確認日時だけの変更はコミットしません。
- `scripts/convert.mjs` が料金表を金額へ変換し、読めない表は配りません。
- `scripts/update_megabig.py` がメガビッグの公式店舗一覧と料金表を取得します。
- `scripts/update_joyjoy_prices.py` がJOYJOY各店舗の公式最低料金を取得します。確定額としては扱いません。
- `scripts/publish.py` が xz で圧縮して `dist/` に置きます。店の数が 1 割以上減ったファイルは前回のものを残します。
- `scripts/web/bigecho.js` はアプリ本体と同じものです。変更するときは両方を直します。

## 安全な更新とデータの異常検査

`refresh.yml` と `ocr.yml` は共通の `scripts/push_data.sh` を経由し、更新前と圧縮前に `scripts/validate_data.py` で検査します。検査が 1 件でも失敗したら **push しません**。最新の `main` を比較元とし、更新内容が安全なことを確認できなければ `dist/` とデータを前回のまま維持します。

- 各チェーンの ID・店舗名・住所と都道府県・日本国内の座標・公式の店舗 URL を検査
- 店舗数（チェーンごと）と前回との差分、座標・店名・住所が突然別店舗になっていないかを検査
- ジャンカラの料金表：プラン／曜日／料金区分の列数・料金の異常値・大幅な価格変動・セル欠落を検査
- ビッグエコーの HTML 表、まねきねこの PDF 文字座標、カラオケ館等の OCR 出力を検査
- 配布データも前回公開した XZ と比較し、変換結果の異常な金額・大量欠落を検出

既存の未変更レコードは検査の基準に使い、更新・追加されたレコードを厳格に検査します。これにより、従来の不備が更新を妨げることを防ぎながら、新しい不正データは拒否します。

手動検査：`python scripts/validate_data.py --stage sources --baseline HEAD`（ソース更新後）／`python scripts/validate_data.py --stage full --baseline origin/main`（変換後）。実際の移転・料金改定で検出された場合は公式サイトを確認し、必要なら検証ロジックを調整してください。自動的な閾値緩和はしません。


## BanBanの画像料金をGitHub Actionsで更新

通常料金（平日・土日祝）とヒトカラ料金（平日・土日祝）の4枚をOCRします。画像なしで料金を推定したり、自動更新することはありません。

### 方法A：画像をコミットして自動更新

source/banban_images/bb-00075/ 以下に次の4枚のPNG画像を置いてmainにコミットすると、BanBan image OCR / price publishing が動きます。

- weekday.png：通常・平日
- weekend.png：通常・土日祝
- solo_weekday.png：ヒトカラ・平日
- solo_weekend.png：ヒトカラ・土日祝

### 方法B：GitHub Actionsで手動実行

1. Actions → BanBan image OCR / price publishing → Run workflow を開く。
2. shop_id は bb-00075 を指定。
3. weekday / weekend / solo_weekday / solo_weekend に画像パスか公開HTTPS画像URLを指定する。GitHubや公式サイト系のホストのみ許可。
4. 実行後、Python・Tesseractで画像を解析し、データ検証に通った場合のみ料金JSONとXZ配信を更新する。結果JSONはActionsのArtifactsにも出力する。

各画像入力を省略すると方法Aの配置先を使います。どれか1枚欠けていれば失敗して既存データを維持します。料金が同じ場合は確認日時だけの変更をコミットしません。

**重要：** このチャットにアップロードされた4枚の画像はGitHubには自動転送されません。Actionsで実行するにはリポジトリに画像をコミットするか、公開画像のURLを指定してください。

### ローカルでの実行

    python -m pip install -r requirements-banban.txt
    # Ubuntuなどでは先に tesseract-ocr をインストール
    python scripts/run_banban_ocr.py --shop-id bb-00075

画像形式・セル配置・金額範囲の異常があれば途中で止まり、料金を更新しません。
