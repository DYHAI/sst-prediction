"""读取回补好的 OISST 网格，做季节循环拟合与距平分解（本站模型的地基）。

数据文件由 tools/backfill_grid.py 生成：meta.json + data.bin(float32)。
纯标准库，用 array 模块按需读，不做整块常驻内存。
"""

from __future__ import annotations

import array
import json
import math
import os
from datetime import date, datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GRID_DIR = os.path.join(BASE, "data", "oisst_grid")

# 统一用 365 个日历槽（0..364），闰年第 366 天映射到最后一天，
# 避免 366 越界这种低级错误。
NDOY = 365


def doy_index(day: str) -> int:
    return (date.fromisoformat(day).timetuple().tm_yday - 1) % NDOY


class Grid:
    """一个固定经纬网格上的逐日 SST 场。"""

    def __init__(self, path: str = GRID_DIR, end: str | None = None):
        with open(os.path.join(path, "meta.json"), encoding="utf-8") as f:
            meta = json.load(f)
        self.meta = meta
        self.lat0, self.dlat, self.nlat = meta["lat0"], meta["dlat"], meta["nlat"]
        self.lon0, self.dlon, self.nlon = meta["lon0"], meta["dlon"], meta["nlon"]
        self.dates: list[str] = meta["dates"]
        n_all = len(self.dates)
        if end:
            # 截断到 end（含），用于做真正的样本外验证
            keep = 0
            for i, d in enumerate(self.dates):
                if d <= end:
                    keep = i + 1
                else:
                    break
            self.dates = self.dates[:keep]
        self.n_available = n_all
        self._raw = array.array("f")
        with open(os.path.join(path, "data.bin"), "rb") as f:
            self._raw.fromfile(f, os.path.getsize(os.path.join(path, "data.bin")) // 4)
        self.ntime = len(self.dates)

    # ---------------------------------------------------------------- 索引
    def date_index(self, day: str) -> int:
        try:
            return self.dates.index(day)
        except ValueError:
            return -1

    def latest_before(self, day: str) -> str | None:
        """返回 <= day 的最后一个有数据的日期。"""
        lo, hi, best = 0, self.ntime - 1, None
        while lo <= hi:
            mid = (lo + hi) // 2
            if self.dates[mid] <= day:
                best = self.dates[mid]
                lo = mid + 1
            else:
                hi = mid - 1
        return best

    def frame(self, t: int) -> array.array:
        n = self.nlat * self.nlon
        return self._raw[t * n:(t + 1) * n]

    def value(self, t: int, ilat: int, ilon: int) -> float:
        return self._raw[t * self.nlat * self.nlon + ilat * self.nlon + ilon]

    def lat(self, ilat: int) -> float:
        return self.lat0 + self.dlat * ilat

    def lon(self, ilon: int) -> float:
        return self.lon0 + self.dlon * ilon

    def index_at(self, lat: float, lon: float) -> tuple[int, int] | None:
        ilat = round((lat - self.lat0) / self.dlat)
        ilon = round((lon - self.lon0) / self.dlon)
        if 0 <= ilat < self.nlat and 0 <= ilon < self.nlon:
            return ilat, ilon
        return None

    # ---------------------------------------------------------------- 掩膜
    def ocean_mask(self, min_valid: float = 0.9) -> list[bool]:
        """哪个格点是稳定的海（不是陆地、也不是时有时无的冰/缺测）。"""
        n = self.nlat * self.nlon
        cnt = [0] * n
        for t in range(self.ntime):
            fr = self.frame(t)
            for k in range(n):
                v = fr[k]
                if v == v:  # not NaN
                    cnt[k] += 1
        need = self.ntime * min_valid
        return [c >= need for c in cnt]

    # ---------------------------------------------------------------- 日均场
    def daily_mean_field(self, day: str) -> list[float] | None:
        """某个日期的日均 SST 场（缺测 -> NaN）。"""
        t = self.date_index(day)
        if t < 0:
            return None
        return list(self.frame(t))


# -------------------------------------------------------------------- 粗化

def coarse_cells(grid: Grid, mask: list[bool], step: int = 8):
    """把 0.25° 网格按 step 合并成粗格点（step=8 → 2°）。

    返回 [(code, lat, lon, [以 cos(lat) 加权的 0.25° 格点索引]), ...]
    只保留海域占比足够高的粗格点。
    """
    nlon, nlat = grid.nlon, grid.nlat
    cells = []
    for i0 in range(0, nlat, step):
        for j0 in range(0, nlon, step):
            idx = []
            wsum = 0.0
            wlat = 0.0
            wlon = 0.0
            total = 0
            for i in range(i0, min(i0 + step, nlat)):
                for j in range(j0, min(j0 + step, nlon)):
                    total += 1
                    if not mask[i * nlon + j]:
                        continue
                    w = math.cos(math.radians(grid.lat(i)))
                    idx.append((i, j, w))
                    wsum += w
                    wlat += w * grid.lat(i)
                    wlon += w * grid.lon(j)
            if total == 0 or len(idx) < 0.5 * total or wsum <= 0:
                continue
            cells.append((f"c{i0}_{j0}", wlat / wsum, wlon / wsum, idx, wsum))
    return cells


def cell_series(grid: Grid, cells) -> list[list[float]]:
    """每个粗格点的逐日 cos(lat) 加权平均序列。

    时间在外层循环：每个时次只取一次数据切片，否则 1100 次 × 70 个格点的
    重复切片会把内存带宽吃光。
    """
    ncell = len(cells)
    nlon = grid.nlon
    series = [[float("nan")] * grid.ntime for _ in range(ncell)]
    for t in range(grid.ntime):
        fr = grid.frame(t)
        for c in range(ncell):
            tot = 0.0
            ws = 0.0
            for i, j, w in cells[c][3]:
                v = fr[i * nlon + j]
                if v == v:
                    tot += w * v
                    ws += w
            if ws > 0:
                series[c][t] = tot / ws
    return series


def cell_climatology(series: list[list[float]], dates: list[str],
                     half_window: int = 15) -> list[list[float]]:
    """逐日气候态：对每个 day-of-year，取前后 half_window 天内所有年份的样本做平均。

    比谐波拟合更贴合南海的季节形态，也不需要假设周期形状。
    """
    doys = [doy_index(d) for d in dates]

    out = []
    for s in series:
        acc = [0.0] * NDOY
        cnt = [0] * NDOY
        for v, doy in zip(s, doys):
            if v != v:
                continue
            for off in range(-half_window, half_window + 1):
                k = (doy + off) % NDOY
                acc[k] += v
                cnt[k] += 1
        clim = [float("nan")] * NDOY
        for k in range(NDOY):
            if cnt[k]:
                clim[k] = acc[k] / cnt[k]
        # 极少数槽位可能全缺，用前一个有效值填
        last = next((c for c in clim if c == c), float("nan"))
        for k in range(NDOY):
            if clim[k] != clim[k]:
                clim[k] = last
            else:
                last = clim[k]
        out.append(clim)
    return out


# ------------------------------------------------------------ 季节循环拟合

def seasonal_fit(series: list[float], dates: list[str], n_harm: int = 2):
    """对单条时间序列拟合 年均值 + 年/半年谐波，返回 (预测函数, 残差标准差)。

    比"按 day-of-year 直接平均"稳得多：我们只有 3 年数据，直接平均每天只有 3 个样本。
    """
    rows, ys = [], []
    for v, d in zip(series, dates):
        if v != v:
            continue
        doy = date.fromisoformat(d).timetuple().tm_yday
        ang = 2 * math.pi * doy / 365.25
        r = [1.0]
        for k in range(1, n_harm + 1):
            r += [math.cos(k * ang), math.sin(k * ang)]
        rows.append(r)
        ys.append(v)
    n_par = 1 + 2 * n_harm
    if len(rows) < n_par * 3:
        mean = sum(ys) / len(ys) if ys else float("nan")
        return (lambda _doy: mean), 0.0

    # 正规方程
    ata = [[0.0] * n_par for _ in range(n_par)]
    atb = [0.0] * n_par
    for r, y in zip(rows, ys):
        for a in range(n_par):
            atb[a] += r[a] * y
            for b in range(n_par):
                ata[a][b] += r[a] * r[b]
    coef = _solve(ata, atb)
    resid = []
    for r, y in zip(rows, ys):
        resid.append(y - sum(c * x for c, x in zip(coef, r)))
    var = sum(x * x for x in resid) / max(1, len(resid) - n_par)

    def predict(doy: int) -> float:
        ang = 2 * math.pi * doy / 365.25
        r = [1.0]
        for k in range(1, n_harm + 1):
            r += [math.cos(k * ang), math.sin(k * ang)]
        return sum(c * x for c, x in zip(coef, r))

    return predict, math.sqrt(var)


# ---------------------------------------------------------------- 线性代数

def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """高斯消元（带部分主元）。规模只有几十，够用。"""
    n = len(a)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-12:
            m[piv][col] = 1e-12
        m[col], m[piv] = m[piv], m[col]
        pv = m[col][col]
        for r in range(n):
            if r == col:
                continue
            f = m[r][col] / pv
            if f == 0:
                continue
            for c in range(col, n + 1):
                m[r][c] -= f * m[col][c]
    return [m[i][n] / m[i][i] for i in range(n)]


def ridge_fit(x0: list[list[float]], x1: list[list[float]], lam: float = 1e-3):
    """解 X1 ≈ A X0 + b 的最小二乘（带岭正则）。

    x0/x1 都是 [样本][维度]。返回 (A, b)。
    """
    n = len(x0[0])
    p = n + 1
    # 正规方程左边只算一次（所有输出维度共用）
    a = [[0.0] * p for _ in range(p)]
    for u in x0:
        uu = list(u) + [1.0]
        for i in range(p):
            ui = uu[i]
            row = a[i]
            for j in range(p):
                row[j] += ui * uu[j]
    for i in range(1, p):
        a[i][i] += lam

    coef = []
    for d in range(len(x1[0])):
        rhs = [0.0] * p
        for u, v in zip(x0, x1):
            uu = list(u) + [1.0]
            for i in range(p):
                rhs[i] += uu[i] * v[d]
        coef.append(_solve([row[:] for row in a], rhs))
    # coef[d][k]：第 d 维 = sum_k coef[d][k]*x0[k] + coef[d][-1]
    return coef
