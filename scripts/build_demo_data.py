# -*- coding: utf-8 -*-
"""
生成 GitHub Pages 静态 Demo 所需的 JSON 快照。

把 data/seed_pool_*.json 转成前端可直接 fetch 的静态文件，
字段与 api/v1/endpoints/screening.py 的返回保持一致：

    <out>/demo-data/dates.json              {"dates": ["20260316", ...]}   倒序
    <out>/demo-data/seed_pool_<date>.json   种子池（含 triggered_count）

用法：
    python scripts/build_demo_data.py apps/dsa-web/dist-demo [--keep 30]
"""

import argparse
import glob
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")


def build(out_root: str, keep: int) -> int:
    out_dir = os.path.join(out_root, "demo-data")
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    files = sorted(glob.glob(os.path.join(DATA_DIR, "seed_pool_*.json")), reverse=True)
    if keep > 0:
        files = files[:keep]

    dates = []
    for path in files:
        date = os.path.basename(path).replace("seed_pool_", "").replace(".json", "")
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:  # 坏文件不阻断整体构建
            print(f"[warn] 跳过 {path}: {e}", file=sys.stderr)
            continue

        entries = data.get("entries", [])
        payload = {
            "date": data.get("date", ""),
            "created_at": data.get("created_at", ""),
            "count": len(entries),
            "triggered_count": sum(1 for e in entries if e.get("phase2_triggered")),
            "entries": entries,
        }
        with open(os.path.join(out_dir, f"seed_pool_{date}.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        dates.append(date)

    with open(os.path.join(out_dir, "dates.json"), "w", encoding="utf-8") as f:
        json.dump({"dates": dates}, f, ensure_ascii=False)

    print(f"[demo-data] {len(dates)} 个交易日 -> {out_dir}")
    return len(dates)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out", help="前端构建输出目录（demo-data 会写在其下）")
    ap.add_argument("--keep", type=int, default=30, help="最多保留最近 N 个交易日，0 表示全部")
    args = ap.parse_args()
    build(args.out, args.keep)


if __name__ == "__main__":
    main()
