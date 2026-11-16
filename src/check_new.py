"""增量檢查：輸入沒變就跳過重運算，節省 Actions 分鐘數。"""
import argparse
import json
import os
from pathlib import Path

from pystac_client import Client

from config import STAC_URL, fingerprint, search_items


def set_output(k, v):
    print(f"{k}={v}")
    if os.getenv("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"{k}={v}\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", type=int, required=True)
    ap.add_argument("--current", type=int, required=True)
    ap.add_argument("--prev", default="prev/manifest.json")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    cat = Client.open(STAC_URL)
    ids = {y: [i.id for i in search_items(cat, y)] for y in (a.base, a.current)}
    fp = fingerprint(ids)
    print(f"影像數：{ {y: len(v) for y, v in ids.items()} }，指紋 {fp}")

    prev_fp = None
    try:
        prev_fp = json.loads(Path(a.prev).read_text()).get("fingerprint")
    except Exception:
        print("找不到上一次的 manifest（首次執行或網站尚未部署）")

    if a.force:
        set_output("changed", "true"); print("原因：手動強制重跑")
    elif prev_fp != fp:
        set_output("changed", "true"); print(f"原因：指紋改變 {prev_fp} -> {fp}")
    else:
        set_output("changed", "false"); print("輸入沒有變化，跳過")


if __name__ == "__main__":
    main()
