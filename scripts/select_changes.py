#!/usr/bin/env python3
"""確認日時しか変わっていない元データは、コミットせず前回のものに戻す。"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = ("chains.json", "source/prices.json", "source/chain_prices.json", "source/maneki_prices.json",
         "source/banban_prices.json", "source/megabig_prices.json", "source/joyjoy_prices.json")
CLOCK_KEYS = {"updated_at", "checked_at"}


def without_clock(value):
    if isinstance(value, dict):
        return {key: without_clock(item) for key, item in value.items() if key not in CLOCK_KEYS}
    if isinstance(value, list):
        return [without_clock(item) for item in value]
    return value


def main() -> None:
    for name in FILES:
        shown = subprocess.run(["git", "show", f"HEAD:{name}"], cwd=ROOT, capture_output=True)
        if shown.returncode != 0:
            continue
        new = json.loads((ROOT / name).read_text(encoding="utf-8"))
        if without_clock(new) == without_clock(json.loads(shown.stdout)):
            print(f"{name}: 確認日時だけなので前回のまま", flush=True)
            subprocess.run(["git", "checkout", "HEAD", "--", name], cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
