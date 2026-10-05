"""回补历史 GFS 预报场（南海盒子），给深度学习订正模型当训练数据。

怎么做到便宜地拿 8 年前的预报：
  · NOAA 在 AWS 上有公开的 GFS 存档桶 noaa-gfs-bdp-pds（免注册、免 key）
  · 每个 GRIB 文件旁边有一个 .idx 索引，列出每个变量的字节偏移
  · 只要 TMP:surface 这一条（洋面上就是 SST），用 HTTP Range 只下那 600 KB
  · eccodes 解码后裁出南海盒子存起来

实测单个 (日期, 起报, 时效) 约 3 秒，其中下载 580KB、解码 0.3 秒。

用法：
    python3 -m tools.backfill_gfs --start 2025-10-01 --end 2026-09-30
"""

from __future__ import annotations

import argparse
import array
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

from app import oisst
from app.http_util import fetch_text, fetch

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(BASE, "data", "gfs_grid")
BUCKET = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"
ECCODES = "/opt/homebrew/bin"
LEADS = (24, 72, 120)      # 对应 1 / 3 / 5 天时效
VAR = ":TMP:surface:"

BOX = oisst.BOX            # (lon0, lat0, lon1, lat1) = (105, 3, 118.75, 23.25)


def _grid_shape():
    lon0, lat0, lon1, lat1 = BOX
    nlon = int(round((lon1 - lon0) / 0.25)) + 1
    nlat = int(round((lat1 - lat0) / 0.25)) + 1
    return nlat, nlon


def fetch_field(day: date, lead: int):
    """返回 (nlat*nlon) 的 float32 数组（摄氏度），拿不到返回 None。"""
    stamp = day.strftime("%Y%m%d")
    base = f"{BUCKET}/gfs.{stamp}/00/atmos/gfs.t00z.pgrb2.0p25.f{lead:03d}"
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

    nlat, nlon = _grid_shape()
    lon0, lat0, _, _ = BOX
    out = array.array("f", [float("nan")] * (nlat * nlon))
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "m.grb2")
        with open(p, "wb") as f:
            f.write(raw)
        try:
            proc = subprocess.run(
                [os.path.join(ECCODES, "grib_get_data"), "-m", "nan", p],
                capture_output=True, text=True, timeout=300,
            )
        except Exception:  # noqa: BLE001
            return None
    for line in proc.stdout.split("\n")[1:]:
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            la, lo, v = float(parts[0]), float(parts[1]), float(parts[2])
        except ValueError:
            continue
        if not (lat0 <= la <= lat0 + (nlat - 1) * 0.25):
            continue
        if not (lon0 <= lo <= lon0 + (nlon - 1) * 0.25):
            continue
        i = int(round((la - lat0) / 0.25))
        j = int(round((lo - lon0) / 0.25))
        if v == v and v > 100:      # 开尔文
            v -= 273.15
        out[i * nlon + j] = v
    if all(x != x for x in out):
        return None
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="回补历史 GFS 南海盒子预报场")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--out", default=OUT_DIR)
    ap.add_argument("--workers", type=int, default=8, help="并发线程数")
    args = ap.parse_args(argv)

    os.makedirs(args.out, exist_ok=True)
    d0, d1 = date.fromisoformat(args.start), date.fromisoformat(args.end)
    nlat, nlon = _grid_shape()

    days = []
    day = d0
    while day <= d1:
        days.append(day)
        day += timedelta(days=1)

    results: dict[str, list] = {}
    t0 = time.time()
    done = 0

    def one(d: date):
        got = []
        for lead in LEADS:
            f = fetch_field(d, lead)
            if f is None:
                return d.isoformat(), None
            got.append(f)
        return d.isoformat(), got

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(one, d): d for d in days}
        for fut in as_completed(futs):
            ds, got = fut.result()
            done += 1
            if got:
                results[ds] = got
            if done % 20 == 0 or done == len(days):
                el = time.time() - t0
                eta = el / done * (len(days) - done) / 60
                print(f"  进度 {done}/{len(days)}，已收 {len(results)} 天，"
                      f"用时 {el/60:.1f} 分，预计还需 {eta:.0f} 分", flush=True)

    dates = sorted(results)
    frames = []
    for ds in dates:
        frames.extend(results[ds])

    if not frames:
        print("没有收到任何数据", file=sys.stderr)
        return 1
    with open(os.path.join(args.out, "data.bin"), "wb") as f:
        for arr in frames:
            f.write(arr.tobytes())
    meta = {
        "source": "NOAA GFS 0.25deg TMP:surface (AWS open data)",
        "lat0": BOX[1], "lon0": BOX[0], "dlat": 0.25, "dlon": 0.25,
        "nlat": nlat, "nlon": nlon, "leads": list(LEADS), "dates": dates,
    }
    with open(os.path.join(args.out, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f)
    size = os.path.getsize(os.path.join(args.out, "data.bin"))
    print(f"完成：{len(dates)} 天 × {len(LEADS)} 时效 × {nlat}×{nlon}，{size/1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
