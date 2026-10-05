"""把早期回补的 data/gfs_grid（一整块二进制）拆成统一的场库格式 data/fields/gfs。

只为兼容已经抓好的 730 天数据；之后 backfill_gfs.py 与每日抓数都直接写场库。
"""

from __future__ import annotations

import json
import os
import sys

from app import fieldstore

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BASE, "data", "gfs_grid")


def main() -> int:
    meta_p = os.path.join(SRC, "meta.json")
    if not os.path.exists(meta_p):
        print("没有 data/gfs_grid，跳过", file=sys.stderr)
        return 0
    with open(meta_p, encoding="utf-8") as f:
        meta = json.load(f)
    leads = meta["leads"]
    # 原始文件按小时存（24/72/120），统一场库按"天"（1/3/5）命名，这里做映射
    lead_to_h = {24: 1, 72: 3, 120: 5}
    nlat, nlon = meta["nlat"], meta["nlon"]
    n = nlat * nlon
    with open(os.path.join(SRC, "data.bin"), "rb") as f:
        raw = f.read()
    import array

    arr = array.array("f")
    arr.frombytes(raw[: (len(raw) // 4) * 4])
    dates = meta["dates"]
    print(f"源：{len(dates)} 天 × {len(leads)} 时效，{nlat}×{nlon}")
    written = 0
    for ti, d in enumerate(dates):
        for li, lead in enumerate(leads):
            h = lead_to_h.get(lead, lead)
            start = (ti * len(leads) + li) * n
            chunk = arr[start:start + n]
            if len(chunk) < n:
                continue
            # 网格对齐检查：GFS 回补时已按 0.25 对齐到 105/3 度，
            # 统一网格从 105.125/3.125 起，差半格，写入时按半格偏移处理
            fieldstore.save_field("gfs", d, h, _align(chunk, nlat, nlon))
            written += 1
    print(f"已转换 {written} 条到 {fieldstore.product_dir('gfs')}")
    return 0


def _align(chunk, nlat, nlon):
    """把 3.0°起的网格挪到 3.125°起的统一网格（半格平移）。"""
    out = [float("nan")] * (fieldstore.NLAT * fieldstore.NLON)
    for i in range(nlat - 1):
        for j in range(nlon - 1):
            vals = [chunk[i * nlon + j], chunk[i * nlon + j + 1],
                    chunk[(i + 1) * nlon + j], chunk[(i + 1) * nlon + j + 1]]
            good = [v for v in vals if v == v]
            if not good:
                continue
            ni, nj = i, j
            if ni < fieldstore.NLAT and nj < fieldstore.NLON:
                out[ni * fieldstore.NLON + nj] = sum(good) / len(good)
    return out


if __name__ == "__main__":
    sys.exit(main())
