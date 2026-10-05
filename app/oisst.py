"""OISST v2.1 取数与海区平均（其它模块共用）。"""

from __future__ import annotations

import csv
import io
import math
from datetime import date, datetime, timezone

from .http_util import fetch_text

ERDDAP = "https://coastwatch.pfeg.noaa.gov/erddap/griddap"
NRT = "ncdcOisst21NrtAgg"      # 近实时，滞后 1–2 天
FINAL = "ncdcOisst21Agg"       # 最终版，滞后 2–3 周，质量更高

# 一次把整个南海盒子拉下来，本地再切海区
BOX = (105.0, 3.0, 118.75, 23.25)   # lon_min, lat_min, lon_max, lat_max


def url(dataset: str, day: str) -> str:
    lon0, lat0, lon1, lat1 = BOX
    return (
        f"{ERDDAP}/{dataset}.csv?"
        f"sst%5B({day}):1:({day})%5D%5B(0.0)%5D"
        f"%5B({lat0}):1:({lat1})%5D%5B({lon0}):1:({lon1})%5D"
    )


def parse_box_csv(text: str) -> list[tuple[str, float, float, float]]:
    """返回 [(日期, lat, lon, sst), ...]。日期一定要留着——

    ERDDAP 对"数据集里还没有的日期"不会报错，而是**就近吸附**到最近的可用时次。
    如果我们丢掉日期列，就会把 10-03 的数据当成 10-04 存下来（实测踩过这个坑）。
    """
    rows: list[tuple[str, float, float, float]] = []
    rdr = csv.reader(io.StringIO(text))
    next(rdr, None)
    next(rdr, None)
    for row in rdr:
        if len(row) < 5:
            continue
        try:
            day = row[0][:10]
            lat, lon, val = float(row[2]), float(row[3]), float(row[4])
        except ValueError:
            continue
        if math.isnan(val):
            continue
        rows.append((day, lat, lon, val))
    return rows


def fetch_box(day: str, *, prefer_final: bool | None = None, strict: bool = True):
    """返回 (数据集名, [(date, lat, lon, sst), ...])。

    strict=True 时会核对返回日期是否真的等于请求日期；ERDDAP 的"就近吸附"
    会让缺失日期静默返回邻居的数据，不核对就会污染数据库。
    """
    today = datetime.now(timezone.utc).date()
    age = (today - date.fromisoformat(day)).days
    if prefer_final is None:
        prefer_final = age >= 21
    order = (FINAL, NRT) if prefer_final else (NRT, FINAL)

    last: Exception | None = None
    for ds in order:
        try:
            text = fetch_text(url(ds, day), timeout=90, cache=True, cache_ext=".csv")
        except Exception as e:  # noqa: BLE001
            last = e
            continue
        rows = parse_box_csv(text)
        if not rows:
            continue
        if strict:
            days = {r[0] for r in rows}
            rows = [r for r in rows if r[0] == day]
            if not rows:
                last = RuntimeError(f"{day} 在 {ds} 里不存在（返回的是 {sorted(days)[:3]}）")
                continue
        return ds, rows
    raise RuntimeError(f"{day}: 两个数据集都取不到数据 ({last})")


def weighted_mean(
    points, box: tuple[float, float, float, float]
) -> tuple[float | None, int]:
    """cos(lat) 加权的海区平均。points 可以是 (lat,lon,value) 或 (date,lat,lon,value)。"""
    lon0, lat0, lon1, lat1 = box
    total = 0.0
    wsum = 0.0
    seen = 0
    for p in points:
        lat, lon, val = (p[-3], p[-2], p[-1])
        if not (lat0 <= lat <= lat1 and lon0 <= lon <= lon1):
            continue
        if val is None or (isinstance(val, float) and math.isnan(val)):
            continue
        seen += 1
        w = math.cos(math.radians(lat))
        total += w * val
        wsum += w
    if wsum <= 0:
        return None, 0
    return total / wsum, seen


def ocean_mask(rows) -> set[tuple[float, float]]:
    """从 OISST 盒子数据里提取"是海"的 0.25° 格点集合。"""
    return {(round(p[-3] * 4) / 4, round(p[-2] * 4) / 4) for p in rows}


def is_ocean(mask: set[tuple[float, float]], lat: float, lon: float, tol: float = 0.2) -> bool:
    """邻近查找：产品网格和 OISST 网格不一定对齐。"""
    la = round(lat * 4) / 4
    lo = round(lon * 4) / 4
    if (la, lo) in mask:
        return True
    step = tol
    for dla in (-step, 0.0, step):
        for dlo in (-step, 0.0, step):
            if (round((la + dla) * 4) / 4, round((lo + dlo) * 4) / 4) in mask:
                return True
    return False
