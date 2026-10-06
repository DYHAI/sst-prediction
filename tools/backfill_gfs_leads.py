"""把 GFS 历史预报场补到 1–10 天时效，直接写进统一场库 data/fields/gfs/。

和 backfill_gfs.py 的区别：
  · 支持任意时效列表（默认 1–10 天），不只是 1/3/5
  · 直接写场库、**已存在的跳过**，所以可以断点续跑、反复执行
  · 多线程并发（S3 + Range 请求，每场约 600 KB）

用法：
    python3 -m tools.backfill_gfs_leads --start 2023-01-01 --end 2026-10-03 \
        --leads 1 2 3 4 5 6 7 8 9 10 --workers 10
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import numpy as np

from app import fieldstore, oisst
from app.http_util import fetch, fetch_text

try:
    import eccodes  # 直接取数组，比 grib_get_data 输出百万行文本快一个数量级
except ImportError:  # pragma: no cover
    eccodes = None

BUCKET = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"
ECCODES = "/opt/homebrew/bin"
VAR = ":TMP:surface:"
BOX = oisst.BOX


def _shape():
    lon0, lat0, lon1, lat1 = BOX
    return (int(round((lat1 - lat0) / 0.25)) + 1,
            int(round((lon1 - lon0) / 0.25)) + 1)


def fetch_field(day: date, lead_days: int):
    """返回 82×56 的 float32 列表（摄氏度），失败返回 None。"""
    stamp = day.strftime("%Y%m%d")
    fhour = lead_days * 24
    base = f"{BUCKET}/gfs.{stamp}/00/atmos/gfs.t00z.pgrb2.0p25.f{fhour:03d}"
    try:
        idx = fetch_text(base + ".idx", timeout=30, retries=2)
    except Exception:  # noqa: BLE001
        return None
    lines = idx.strip().split("\n")
    start = end = None
    for i, line in enumerate(lines):
        if VAR in line:
            start = int(line.split(":")[1])
            if i + 1 < len(lines):
                end = int(lines[i + 1].split(":")[1]) - 1
            break
    if start is None:
        return None
    rng = f"{start}-{end}" if end else f"{start}-"
    try:
        raw = fetch(base, timeout=90, retries=3, headers={"Range": f"bytes={rng}"})
    except Exception:  # noqa: BLE001
        return None
    if len(raw) < 1000:
        return None

    if eccodes is None:
        return None
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "m.grb2")
        with open(p, "wb") as f:
            f.write(raw)
        with open(p, "rb") as f:
            try:
                gid = eccodes.codes_grib_new_from_file(f)
                ni = int(eccodes.codes_get(gid, "Ni"))
                nj = int(eccodes.codes_get(gid, "Nj"))
                vals = np.asarray(eccodes.codes_get_values(gid), dtype="f4")
                eccodes.codes_release(gid)
            except Exception:  # noqa: BLE001
                return None
    if ni * nj != vals.size:
        return None
    a = vals.reshape(nj, ni)          # lat 自北向南，lon 0→359.75
    # 南海盒子在 0.25° 全网格上的下标：lat 90→3.0/23.25，lon 0→105/118.75
    i0, i1 = 267, 349                 # lat 23.25 .. 3.00（含）
    j0, j1 = 420, 476                 # lon 105.00 .. 118.75（含）
    if i1 > nj or j1 > ni:
        return None
    sub = a[i0:i1, j0:j1][::-1]       # 翻成纬度递增
    nlat = nlon = None
    NLAT, NLON = fieldstore.NLAT, fieldstore.NLON
    if sub.shape != (NLAT, NLON):
        # 尺寸对不上就退回按坐标逐点填
        lat0, lon0 = BOX[1], BOX[0]
        pts = [(90 - 0.25 * i, 0.25 * j, float(a[i, j]))
               for i in range(i0, min(i1, nj)) for j in range(j0, min(j1, ni))]
        shifted = fieldstore.points_to_box(pts, half_shift=True)
        return [v - 273.15 if v == v and v > 100 else v for v in shifted]
    g = np.pad(sub, ((0, 1), (0, 1)), mode="edge")
    sh = 0.25 * (g[:-1, :-1] + g[:-1, 1:] + g[1:, :-1] + g[1:, 1:])
    sh = np.where(sh > 100, sh - 273.15, sh)
    if not np.isfinite(sh).any():
        return None
    return sh.ravel().tolist()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="回补 GFS 1–10 天预报场")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--leads", type=int, nargs="+", default=list(range(1, 11)))
    ap.add_argument("--workers", type=int, default=10)
    args = ap.parse_args(argv)

    d0, d1 = date.fromisoformat(args.start), date.fromisoformat(args.end)
    days = []
    cur = d0
    while cur <= d1:
        days.append(cur)
        cur += timedelta(days=1)

    tasks = [(d, l) for d in days for l in args.leads
             if not fieldstore.has_field("gfs", d.isoformat(), l)]
    print(f"共 {len(days)} 天 × {len(args.leads)} 时效，需补 {len(tasks)} 场，"
          f"并发 {args.workers}", flush=True)
    if not tasks:
        return 0

    t0 = time.time()
    done = ok = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(fetch_field, d, l): (d, l) for d, l in tasks}
        for fut in as_completed(futs):
            d, l = futs[fut]
            done += 1
            try:
                f = fut.result()
            except Exception:  # noqa: BLE001
                f = None
            if f:
                fieldstore.save_field("gfs", d.isoformat(), l, f)
                ok += 1
            if done % 200 == 0 or done == len(tasks):
                el = time.time() - t0
                eta = el / done * (len(tasks) - done) / 60
                print(f"  进度 {done}/{len(tasks)}  成功 {ok}  用时 {el/60:.1f} 分  "
                      f"预计还需 {eta:.0f} 分", flush=True)
    print(f"完成：写入 {ok} 场")
    return 0


if __name__ == "__main__":
    sys.exit(main())
