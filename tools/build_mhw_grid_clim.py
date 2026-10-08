#!/usr/bin/env python
"""算逐格点的海洋热浪阈值（Hobday 2016 的基期气候态）。

为什么需要这个
--------------
`data/mhw_clim.json` 里只有 **7 个海区**的 90 分位阈值，画不出空间分布图。
要像 NOAA 的海洋热浪追踪器那样"哪个格点正处在热浪里、强度几级"，
就得有**逐格点**的 90 分位阈值，而这必须以 1982–2011 这 30 年为基期
（Hobday 的定义）——`data/oisst_grid/` 里只有 2018 年以后的场，不够用。

做法
----
1. 从 NOAA ERDDAP 按年下 1982–2011 的南海盒子（82×56，一年约 6.7 MB）
2. 拼成 (30 年, 366 天, 82, 56) 的立方体（约 200 MB 内存）
3. 对每个日历日取 ±5 天窗口 × 30 年 = 330 个样本，算 90 分位和均值
   （和 `app/grid.py` 里区域级气候态用的是同一套窗口逻辑）
4. 存成两个 float32 数组，供 web 端用标准库直接读

产物
----
  data/mhw_grid_clim.bin        365×82×56×4B × 2（p90 在前、气候态均值在后）≈ 13 MB
  data/mhw_grid_clim.meta.json  索引信息

用法
----
  .venv/bin/python -m tools.build_mhw_grid_clim            # 用代理
  .venv/bin/python -m tools.build_mhw_grid_clim --no-proxy # 直连
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
from scipy.io import netcdf_file

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import oisst  # noqa: E402

BASE = Path(__file__).resolve().parent.parent
CACHE = Path("/tmp/oisst_clim_cache")
OUT_BIN = BASE / "data" / "mhw_grid_clim.bin"
OUT_META = BASE / "data" / "mhw_grid_clim.meta.json"

Y0, Y1 = 1982, 2011          # Hobday 定义的标准基期
NDOY = 365
HALF_WINDOW = 5              # ±5 天，和区域级气候态保持一致


def year_url(year: int) -> str:
    lon0, lat0, lon1, lat1 = oisst.BOX
    d0, d1 = f"{year}-01-01", f"{year}-12-31"
    return (
        f"{oisst.ERDDAP}/{oisst.FINAL}.nc?"
        f"sst%5B({d0}):1:({d1})%5D%5B(0.0)%5D"
        f"%5B({lat0}):1:({lat1})%5D%5B({lon0}):1:({lon1})%5D"
    )


def download(year: int, proxy: str | None, retries: int = 3) -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{year}.nc"
    if path.exists() and path.stat().st_size > 100_000:
        return path
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    last: Exception | None = None
    for attempt in range(retries):
        try:
            with opener.open(year_url(year), timeout=300) as response:
                blob = response.read()
            if len(blob) < 100_000:
                raise RuntimeError(f"只拿到 {len(blob)} 字节")
            path.write_bytes(blob)
            return path
        except Exception as error:  # noqa: BLE001
            last = error
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"{year} 下载失败：{last}")


def doy_of(day: str) -> int:
    return (date.fromisoformat(day).timetuple().tm_yday - 1) % NDOY


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy", default="http://127.0.0.1:7890",
                        help="下载用的 HTTP 代理；实测比直连快约一倍")
    parser.add_argument("--no-proxy", action="store_true")
    parser.add_argument("--window", type=int, default=HALF_WINDOW)
    args = parser.parse_args()
    proxy = None if args.no_proxy else args.proxy

    t_start = time.time()
    cube = np.full((Y1 - Y0 + 1, 366, 82, 56), np.nan, dtype=np.float32)

    for i, year in enumerate(range(Y0, Y1 + 1)):
        path = download(year, proxy)
        with netcdf_file(str(path), "r", mmap=False) as handle:
            variable = handle.variables["sst"]
            raw = np.array(variable.data, dtype=np.float32)
            times = np.array(handle.variables["time"].data, dtype=np.int64)
            units = handle.variables["time"].units.decode()
            # 陆地/缺测在 .nc 里是 _FillValue（OISST 是 -9.99），不是 NaN。
            # 不屏蔽的话南海盒子里约 23% 的格点是 -10°C，会把 90 分位算废。
            fill = float(variable._attributes.get("_FillValue", -9.99))
            vmin = float(variable._attributes.get("valid_min", -3.0))
            vmax = float(variable._attributes.get("valid_max", 45.0))
        # ERDDAP 的 time 是 "seconds since 1970-01-01T00:00:00Z"
        epoch = datetime(1970, 1, 1)
        days = [(epoch + timedelta(seconds=int(t))).date() for t in times]
        for j, day in enumerate(days):
            field = raw[j, 0]
            field = np.where((field <= fill + 0.01) | (field < vmin) | (field > vmax),
                             np.nan, field).astype(np.float32)
            cube[i, doy_of(day.isoformat())] = field
        print(f"  {year} 已并入（{len(days)} 天，累计 {i + 1}/"
              f"{Y1 - Y0 + 1}，用时 {time.time() - t_start:.0f}s）", flush=True)

    half = args.window
    p90 = np.full((NDOY, 82, 56), np.nan, dtype=np.float32)
    mean = np.full((NDOY, 82, 56), np.nan, dtype=np.float32)
    for k in range(NDOY):
        slots = [cube[:, (k + off) % NDOY, :, :] for off in
                 range(-half, half + 1)]
        pooled = np.concatenate(slots, axis=0)          # (30×11, 82, 56)
        with np.errstate(all="ignore"):
            p90[k] = np.nanpercentile(pooled, 90, axis=0)
            mean[k] = np.nanmean(pooled, axis=0)
        if k % 60 == 0:
            print(f"  气候态 {k + 1}/{NDOY}", flush=True)

    OUT_BIN.parent.mkdir(parents=True, exist_ok=True)
    with OUT_BIN.open("wb") as handle:
        handle.write(p90.astype("<f4").tobytes())
        handle.write(mean.astype("<f4").tobytes())
    OUT_META.write_text(json.dumps({
        "base_period": [Y0, Y1],
        "ndoy": NDOY,
        "window_days": args.window,
        "nlat": 82, "nlon": 56,
        "lat0": 3.125, "dlat": 0.25,
        "lon0": 105.125, "dlon": 0.25,
        "layout": "p90[365][82][56] float32 LE, then clim_mean[365][82][56]",
        "source": "NOAA OISST v2.1 (ncdcOisst21Agg), ERDDAP",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    size = OUT_BIN.stat().st_size / 1024 / 1024
    print(f"\n完成：{OUT_BIN}（{size:.1f} MB）  总用时 {time.time() - t_start:.0f}s")
    valid = np.isfinite(p90).mean()
    print(f"  p90 有效率 {valid * 100:.1f}%  取值范围 "
          f"{np.nanmin(p90):.2f} ~ {np.nanmax(p90):.2f} °C")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
