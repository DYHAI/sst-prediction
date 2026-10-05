"""把南海盒子的 OISST 逐日网格回补成一个紧凑的二进制文件，供本站模型训练使用。

输出（默认 data/oisst_grid/）：
  meta.json  —— 网格坐标与日期表
  data.bin   —— float32，形状 (ntime, nlat, nlon)，行优先，陆地/缺测为 NaN

这样存的好处：3 年的场大约 30 MB，读一次几秒钟，不需要 numpy 之外的任何依赖。

用法：
    python3 -m tools.backfill_grid --start 2023-10-01 --end 2026-10-04
"""

from __future__ import annotations

import argparse
import array
import csv
import io
import json
import math
import os
import struct
import sys
from datetime import date, datetime, timedelta, timezone

from app import oisst
from app.http_util import fetch_text

OUT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "oisst_grid"
)


def _chunk_url(dataset: str, d0: str, d1: str) -> str:
    lon0, lat0, lon1, lat1 = oisst.BOX
    return (
        f"{oisst.ERDDAP}/{dataset}.csv?"
        f"sst%5B({d0}):1:({d1})%5D%5B(0.0)%5D"
        f"%5B({lat0}):1:({lat1})%5D%5B({lon0}):1:({lon1})%5D"
    )


def fetch_chunk(d0: date, d1: date):
    """取一段日期，返回 {date: {(ilat, ilon): value}}, lats, lons。"""
    today = datetime.now(timezone.utc).date()
    prefer_final = (today - d1).days >= 21
    order = (oisst.FINAL, oisst.NRT) if prefer_final else (oisst.NRT, oisst.FINAL)

    last = None
    for ds in order:
        try:
            text = fetch_text(
                _chunk_url(ds, d0.isoformat(), d1.isoformat()),
                timeout=300, retries=3,
            )
        except Exception as e:  # noqa: BLE001
            last = e
            continue
        rdr = csv.reader(io.StringIO(text))
        next(rdr, None)
        next(rdr, None)
        lats, lons = set(), set()
        cells: dict[str, dict[tuple[float, float], float]] = {}
        for row in rdr:
            if len(row) < 5:
                continue
            try:
                day = row[0][:10]
                lat, lon, val = float(row[2]), float(row[3]), float(row[4])
            except ValueError:
                continue
            lats.add(lat)
            lons.add(lon)
            if math.isnan(val):
                continue
            cells.setdefault(day, {})[(lat, lon)] = val
        if lats and lons:
            return ds, sorted(lats), sorted(lons), cells
    raise RuntimeError(f"{d0}~{d1} 取不到数据：{last}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="回补 OISST 网格")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--out", default=OUT_DIR)
    args = ap.parse_args(argv)

    os.makedirs(args.out, exist_ok=True)
    d0 = date.fromisoformat(args.start)
    d1 = date.fromisoformat(args.end)

    lats: list[float] = []
    lons: list[float] = []
    dates: list[str] = []
    frames: list[array.array] = []

    cur = d0
    while cur <= d1:
        nxt = min(cur + timedelta(days=29), d1)
        print(f"  取 {cur} ~ {nxt} ...", flush=True)
        try:
            ds, clats, clons, cells = fetch_chunk(cur, nxt)
        except Exception as e:  # noqa: BLE001
            print(f"    失败：{e}", flush=True)
            cur = nxt + timedelta(days=1)
            continue
        if not lats:
            lats, lons = clats, clons
            print(f"    网格 {len(lats)}×{len(lons)}，数据集 {ds}", flush=True)
        lat_idx = {v: i for i, v in enumerate(lats)}
        lon_idx = {v: i for i, v in enumerate(lons)}

        day = cur
        while day <= nxt:
            key = day.isoformat()
            arr = array.array("f", [float("nan")] * (len(lats) * len(lons)))
            for (la, lo), val in cells.get(key, {}).items():
                i, j = lat_idx.get(la), lon_idx.get(lo)
                if i is not None and j is not None:
                    arr[i * len(lons) + j] = val
            dates.append(key)
            frames.append(arr)
            day += timedelta(days=1)
        cur = nxt + timedelta(days=1)

    if not frames:
        print("没有任何数据", file=sys.stderr)
        return 1

    with open(os.path.join(args.out, "data.bin"), "wb") as f:
        for arr in frames:
            f.write(arr.tobytes())
    meta = {
        "lat0": lats[0], "dlat": round(lats[1] - lats[0], 6) if len(lats) > 1 else 0.25,
        "nlat": len(lats),
        "lon0": lons[0], "dlon": round(lons[1] - lons[0], 6) if len(lons) > 1 else 0.25,
        "nlon": len(lons),
        "dates": dates,
        "source": "NOAA OISST v2.1",
    }
    with open(os.path.join(args.out, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)

    size = os.path.getsize(os.path.join(args.out, "data.bin"))
    print(f"完成：{len(dates)} 天 × {len(lats)}×{len(lons)} 格点，{size / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
