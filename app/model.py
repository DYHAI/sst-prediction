"""本站自己的 SST 预报模型：线性逆模型（LIM / VAR）。

为什么选它：
  · 它是数据驱动的（不依赖任何数值模式），属于"统计 AI"这一族，
    和我们文献综述里引的 Penland & Sardeshmukh (1993, 1995) 是同一套方法。
  · 状态维度只有几十，纯 Python 就能拟合并求解，不需要 GPU、不需要 numpy。
  · 原理透明：南海海温异常的自回归结构 + 海区之间的耦合，一眼能看懂。

做法：
  1. 把南海盒子按 2° 粗化成若干粗格点，每个粗格点取 cos(纬度) 加权日均 SST。
  2. 减去逐日气候态（day-of-year，±15 天滑动窗口）得到距平。
  3. 对每个时效 τ ∈ {1,3,5} 天，拟合 X(t+τ) = A_τ X(t) + b（岭正则最小二乘）。
  4. 预报：用截止时刻能拿到的最新观测距平做初值，套 A_τ 外推，加回气候态，再按海区聚合。

注意：模型初值只能是**截止时刻已经发布的观测**。OISST 滞后 1–2 天，
所以真实业务预报里实际外推天数会比名义时效略大，训练和回测里都如实处理。
"""

from __future__ import annotations

import json
import os
from datetime import date

from . import grid as G
from . import regions as R

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL_PATH = os.path.join(BASE, "data", "model_lim.json")
COARSE_STEP = 8          # 8 × 0.25° = 2°
HALF_WINDOW = 15         # 气候态平滑半窗（天）


class LimModel:
    def __init__(self, grid, cells, cell_clim, cell_anom, coefs, region_map):
        self.grid = grid
        self.cells = cells
        self.cell_clim = cell_clim          # [cell][doy]
        self.cell_anom = cell_anom          # [cell][t]
        self.coefs = coefs                  # {tau: [coef_d][k or bias]}
        self.region_map = region_map        # [(region_code, [权重])]

    # ------------------------------------------------------------ 训练
    @classmethod
    def train(cls, grid) -> "LimModel":
        mask = grid.ocean_mask()
        cells = G.coarse_cells(grid, mask, COARSE_STEP)
        series = G.cell_series(grid, cells)
        clim = G.cell_climatology(series, grid.dates, HALF_WINDOW)
        doys = [G.doy_index(d) for d in grid.dates]

        anom = []
        for c, s in enumerate(series):
            row = []
            for t, v in enumerate(s):
                row.append(float("nan") if v != v else v - clim[c][doys[t]])
            anom.append(row)

        valid = set(t for t in range(grid.ntime) if all(a[t] == a[t] for a in anom))
        coefs = {}
        for tau in R.HORIZONS:
            x0, x1 = [], []
            for t in sorted(valid):
                if t + tau >= grid.ntime or (t + tau) not in valid:
                    continue
                x0.append([a[t] for a in anom])
                x1.append([a[t + tau] for a in anom])
            if len(x0) < 50:
                continue
            coefs[tau] = G.ridge_fit(x0, x1, lam=1e-2)

        return cls(grid, cells, clim, anom, coefs, _region_map(grid, cells))

    # ------------------------------------------------------------ 预报
    def rebind(self, grid) -> "LimModel":
        """换一份（更长的）观测网格来提供初值，模型系数与气候态保持不变。

        这样就能做真正的样本外验证：系数只用训练期数据拟合，
        而每天预报时用当时真实可得的观测做初值。
        """
        series = G.cell_series(grid, self.cells)
        doys = [G.doy_index(d) for d in grid.dates]
        anom = []
        for c, s in enumerate(series):
            row = []
            for t, v in enumerate(s):
                row.append(float("nan") if v != v else v - self.cell_clim[c][doys[t]])
            anom.append(row)
        return LimModel(grid, self.cells, self.cell_clim, anom, self.coefs, self.region_map)

    def forecast_region_means(self, target: str, as_of: str) -> dict:
        """用 as_of 当天的距平做初值，预报 target 当天的 7 个海区平均。"""
        tau = (date.fromisoformat(target) - date.fromisoformat(as_of)).days
        t0 = self.grid.date_index(as_of)
        if tau <= 0 or t0 < 0 or not self.coefs:
            return {}
        key = min(self.coefs, key=lambda k: abs(k - tau))
        coef = self.coefs[key]

        x = [a[t0] if a[t0] == a[t0] else 0.0 for a in self.cell_anom]
        pred = _apply(coef, x)
        step = self.coefs.get(1)
        if step:
            for _ in range(abs(tau - key)):
                pred = _apply(step, pred)

        doy = G.doy_index(target)
        out = {}
        for code, weights in self.region_map:
            tot = wsum = 0.0
            for c, w in enumerate(weights):
                if w <= 0:
                    continue
                tot += w * (self.cell_clim[c][doy] + pred[c])
                wsum += w
            if wsum > 0:
                out[code] = round(tot / wsum, 3)
        return out

    # ------------------------------------------------------------ 存取
    def save(self, path: str = MODEL_PATH) -> None:
        payload = {
            "coarse_step": COARSE_STEP,
            "half_window": HALF_WINDOW,
            "cells": [[c[0], round(c[1], 4), round(c[2], 4)] for c in self.cells],
            "cell_clim": [[round(v, 4) for v in row] for row in self.cell_clim],
            "coefs": {str(k): v for k, v in self.coefs.items()},
            "region_map": [[code, [round(w, 6) for w in ws]] for code, ws in self.region_map],
            "trained_on": [self.grid.dates[0], self.grid.dates[-1]],
            "n_days": self.grid.ntime,
            "n_cells": len(self.cells),
        }
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)


def _apply(coef, x) -> list:
    """pred[d] = sum_k coef[d][k] * x[k] + coef[d][-1]"""
    aug = list(x) + [1.0]
    return [sum(c * v for c, v in zip(coef[d], aug)) for d in range(len(x))]


def load_model(path: str = MODEL_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _region_map(grid, cells):
    """每个海区由哪些粗格点组成、各占多少权重（按实际海域面积）。"""
    out = []
    for reg in R.REGIONS:
        weights = [0.0] * len(cells)
        for c, (_code, _la, _lo, idx, _wsum) in enumerate(cells):
            w = 0.0
            for i, j, ww in idx:
                la, lo = grid.lat(i), grid.lon(j)
                if reg.lat_min <= la <= reg.lat_max and reg.lon_min <= lo <= reg.lon_max:
                    w += ww
            weights[c] = w
        tot = sum(weights)
        out.append((reg.code, [w / tot for w in weights] if tot > 0 else weights))
    return out
