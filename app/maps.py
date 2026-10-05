"""把海温场画成 PNG：真值图、本站模型预报图、误差图。

南海盒子在 0.25° 网格上是 82×56，放大 7 倍 → 574×392 的图，手机上也看得清。
陆地用 OISST 的缺测位置自动识别（缺测即陆地/冰），所以不用额外海岸线数据。
"""

from __future__ import annotations

import os
from datetime import date, timedelta

from . import grid as G
from . import model as M
from . import png
from . import regions as R

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAP_DIR = os.path.join(BASE, "data", "maps")

SCALE = 7
BORDER = 2
CBAR_H = 16
V_MIN, V_MAX = 24.0, 32.0     # 南海常见的海温范围
DIFF_LIM = 1.5                # 误差图色标 ±1.5°C

_grid_cache: dict = {}


def _grid() -> G.Grid:
    if "g" not in _grid_cache:
        _grid_cache["g"] = G.Grid()
    return _grid_cache["g"]


def _model():
    if "m" not in _grid_cache:
        g = _grid()
        _grid_cache["m"] = M.LimModel.train(_grid()).rebind(g)
    return _grid_cache["m"]


def _blank(width: int, height: int) -> png.Canvas:
    return png.Canvas(width, height, bg=(255, 255, 255))


def _draw_field(c: png.Canvas, g: G.Grid, field, ox: int, oy: int,
                vmin: float, vmax: float, diverging: bool = False) -> None:
    """field: 长度 nlat*nlon 的序列，NaN 表示陆地。"""
    for i in range(g.nlat):
        for j in range(g.nlon):
            v = field[i * g.nlon + j]
            if v != v:
                color = png.LAND
            elif diverging:
                color = png.diff_color(v, vmax)
            else:
                color = png.sst_color(v, vmin, vmax)
            x = ox + j * SCALE
            y = oy + (g.nlat - 1 - i) * SCALE       # 纬度自下而上
            c.rect(x, y, x + SCALE, y + SCALE, color)


def _draw_regions(c: png.Canvas, g: G.Grid, ox: int, oy: int) -> None:
    for reg in R.REGIONS:
        j0 = int((reg.lon_min - g.lon0) / g.dlon)
        j1 = int((reg.lon_max - g.lon0) / g.dlon)
        i0 = int((reg.lat_min - g.lat0) / g.dlat)
        i1 = int((reg.lat_max - g.lat0) / g.dlat)
        x0 = ox + max(0, j0) * SCALE
        x1 = ox + min(g.nlon, j1 + 1) * SCALE
        y0 = oy + (g.nlat - 1 - min(g.nlat - 1, i1)) * SCALE
        y1 = oy + (g.nlat - 1 - max(0, i0) + 1) * SCALE
        c.outline(x0, y0, x1, y1, (16, 38, 66), 2)


def _draw_colorbar(c: png.Canvas, ox: int, oy: int, width: int,
                   vmin: float, vmax: float, diverging: bool = False) -> None:
    for x in range(width):
        t = x / max(1, width - 1)
        v = vmin + t * (vmax - vmin)
        color = png.diff_color(v, vmax) if diverging else png.sst_color(v, vmin, vmax)
        for y in range(oy, oy + CBAR_H):
            c.set(ox + x, y, color)
    c.outline(ox, oy, ox + width, oy + CBAR_H, (16, 38, 66), 1)
    if diverging:   # 标出零误差位置
        mid = ox + width // 2
        for y in range(oy - 2, oy + CBAR_H + 2):
            c.set(mid, y, (16, 38, 66))


def _canvas_for(g: G.Grid, with_bar: bool = True) -> tuple[png.Canvas, int, int]:
    w = g.nlon * SCALE + BORDER * 2
    h = g.nlat * SCALE + BORDER * 2 + (CBAR_H + 12 if with_bar else 0)
    return _blank(w, h), BORDER, BORDER


def _auto_range(field, min_span: float = 1.5) -> tuple[float, float]:
    vals = [v for v in field if v == v]
    if not vals:
        return V_MIN, V_MAX
    lo, hi = min(vals), max(vals)
    if hi - lo < min_span:                      # 场太平，撑开到最小跨度
        mid = 0.5 * (lo + hi)
        lo, hi = mid - min_span / 2, mid + min_span / 2
    pad = (hi - lo) * 0.06
    lo, hi = lo - pad, hi + pad
    return round(lo * 2) / 2, round(hi * 2) / 2   # 取到 0.5°C


def render(field, kind: str, g: G.Grid | None = None) -> tuple[bytes, float, float]:
    g = g or _grid()
    c, ox, oy = _canvas_for(g)
    if kind == "diff":
        vmin, vmax = -DIFF_LIM, DIFF_LIM
        _draw_field(c, g, field, ox, oy, vmin, vmax, diverging=True)
    else:
        vmin, vmax = _auto_range(field)
        _draw_field(c, g, field, ox, oy, vmin, vmax)
    _draw_regions(c, g, ox, oy)
    if kind == "diff":
        _draw_colorbar(c, ox, oy + g.nlat * SCALE + 6, g.nlon * SCALE,
                       vmin, vmax, diverging=True)
    else:
        _draw_colorbar(c, ox, oy + g.nlat * SCALE + 6, g.nlon * SCALE, vmin, vmax)
    return c.to_png(), vmin, vmax


# ---------------------------------------------------------------- 对外接口

def truth_field(day: str):
    g = _grid()
    return g.daily_mean_field(day)


def model_field(target: str, horizon: int):
    """本站模型对 target 日的预报场。"""
    g = _grid()
    cutoff = (date.fromisoformat(target) - timedelta(days=horizon)).isoformat()
    as_of = g.latest_before(cutoff)
    if not as_of or as_of > cutoff:
        return None, None
    mdl = _model()
    t0 = g.date_index(as_of)
    tau = (date.fromisoformat(target) - date.fromisoformat(as_of)).days
    key = min(mdl.coefs, key=lambda k: abs(k - tau)) if mdl.coefs else None
    if key is None:
        return None, as_of
    x = [a[t0] if a[t0] == a[t0] else 0.0 for a in mdl.cell_anom]
    pred = M._apply(mdl.coefs[key], x)
    step = mdl.coefs.get(1)
    for _ in range(abs(tau - key)):
        pred = M._apply(step, pred)

    doy = G.doy_index(target)
    # 每个 0.25° 格点取它所属粗格点的模式值 -> 得到完整场
    cell_of = {}
    for c, (_code, _la, _lo, idx, _w) in enumerate(mdl.cells):
        for i, j, _w2 in idx:
            cell_of[(i, j)] = c
    out = [float("nan")] * (g.nlat * g.nlon)
    for (i, j), c in cell_of.items():
        out[i * g.nlon + j] = mdl.cell_clim[c][doy] + pred[c]
    return out, as_of


def save(kind: str, day: str, horizon: int | None = None) -> bytes | None:
    """按类型生成 PNG。返回 (字节流, 色标下限, 色标上限)，没有数据返回 None。"""
    g = _grid()
    if kind == "truth":
        f = truth_field(day)
        return render(f, "sst", g) if f else None
    if kind == "model":
        f, _asof = model_field(day, horizon or 1)
        return render(f, "sst", g) if f else None
    if kind == "diff":
        t = truth_field(day)
        m, _asof = model_field(day, horizon or 1)
        if not t or not m:
            return None
        d = [(m[k] - t[k]) if (m[k] == m[k] and t[k] == t[k]) else float("nan")
             for k in range(len(t))]
        return render(d, "diff", g)
    return None
