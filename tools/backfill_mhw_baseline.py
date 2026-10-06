"""回补 1982 年以来的海区日均 SST，用来算海洋热浪（MHW）的气候态基线。

为什么需要它：海洋热浪的判定标准（Hobday et al. 2016）是
"连续 5 天以上超过气候态 90 分位"，而这个气候态按惯例取 **1982–2011 年**。
我们本地网格只有 2018 年起的数据，算不出可靠的分位数，所以要把更早的补上。

只存 7 个海区的日均值（每天 7 个数），不存网格——体积可以忽略，
够判定热浪、算强度/持续天数/累计强度了。

用法：
    python3 -m tools.backfill_mhw_baseline --start 1982-01-01 --end 2017-12-31
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

from app import oisst
from app import regions as R
from app.http_util import fetch_text

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "data", "region_series.json")


def fetch_chunk(d0: date, d1: date):
    """取一段日期，返回 {date: {region: mean}}。"""
    lon0, lat0, lon1, lat1 = oisst.BOX
    url = (
        f"{oisst.ERDDAP}/{oisst.FINAL}.csv?"
        f"sst%5B({d0}):1:({d1})%5D%5B(0.0)%5D"
        f"%5B({lat0}):1:({lat1})%5D%5B({lon0}):1:({lon1})%5D"
    )
    text = fetch_text(url, timeout=300, retries=3)
    per_day: dict[str, dict[str, list]] = {}
    rdr = csv.reader(io.StringIO(text))
    next(rdr, None)
    next(rdr, None)
    for row in rdr:
        if len(row) < 5:
            continue
        try:
            d = row[0][:10]
            lat, lon, val = float(row[2]), float(row[3]), float(row[4])
        except ValueError:
            continue
        if math.isnan(val):
            continue
        per_day.setdefault(d, []).append((lat, lon, val))

    out = {}
    for d, pts in per_day.items():
        row = {}
        for reg in R.REGIONS:
            v, _n = oisst.weighted_mean(
                pts, (reg.lon_min, reg.lat_min, reg.lon_max, reg.lat_max))
            if v is not None:
                row[reg.code] = round(v, 4)
        if len(row) == len(R.REGIONS):
            out[d] = row
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="回补海区日均 SST（热浪基线用）")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--chunk-days", type=int, default=30)
    args = ap.parse_args(argv)

    d0, d1 = date.fromisoformat(args.start), date.fromisoformat(args.end)
    chunks = []
    cur = d0
    while cur <= d1:
        nxt = min(cur + timedelta(days=args.chunk_days - 1), d1)
        chunks.append((cur, nxt))
        cur = nxt + timedelta(days=1)

    # 已有结果接着补，支持断点续跑
    series: dict[str, dict[str, float]] = {}
    if os.path.exists(OUT):
        with open(OUT, encoding="utf-8") as f:
            series = json.load(f)
        print(f"已有 {len(series)} 天，继续补")

    todo = [(a, b) for a, b in chunks
            if not all((a + timedelta(days=i)).isoformat() in series
                       for i in range((b - a).days + 1))]
    print(f"共 {len(chunks)} 个分片，需要补 {len(todo)} 个，并发 {args.workers}")

    t0 = time.time()
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(fetch_chunk, a, b): (a, b) for a, b in todo}
        for fut in as_completed(futs):
            done += 1
            try:
                series.update(fut.result())
            except Exception as e:  # noqa: BLE001
                print(f"  分片失败 {futs[fut]}: {str(e)[:80]}", flush=True)
            if done % 20 == 0 or done == len(todo):
                el = time.time() - t0
                eta = el / done * (len(todo) - done) / 60 if done else 0
                print(f"  进度 {done}/{len(todo)}  已收 {len(series)} 天  "
                      f"用时 {el/60:.1f} 分  预计还需 {eta:.0f} 分", flush=True)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(series, f, sort_keys=True)
    days = sorted(series)
    print(f"完成：{len(days)} 天（{days[0]} ~ {days[-1]}）-> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
