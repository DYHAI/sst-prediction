"""海洋热浪（Marine Heatwave, MHW）的判定与指标计算。

判定标准用 Hobday et al. (2016, Prog. Oceanogr.) 的层级定义：

  1. 对每个海区、每个 day-of-year，取 **1982–2011** 年气候态，
     在该日前后 ±5 天（共 11 天 × 30 年 ≈ 330 个样本）里算 **90 分位阈值**；
  2. 海温**连续 5 天以上**高于阈值，记一次热浪事件；
  3. 两个事件之间若只隔 ≤2 天（gap），合并成一次。

指标（都在阈值之上度量）：
  · 持续天数 duration
  · 平均强度 intensity_mean、最大强度 intensity_max（单位 °C）
  · 累计强度 severity = Σ(海温 − 阈值)，单位 °C·天

分级用 Hobday et al. (2018, Oceanography) 的办法，拿"阈值 − 气候态均值"
记为 dSST，按倍数分四档：
  I 中等（1–2×dSST）、II 强（2–3×）、III 严重（3–4×）、IV 极端（>4×）。

注意：这里是在**海区平均**的海温序列上判定，不是逐格点判定后再聚合。
好处是指标稳定、和擂台的结算口径完全一致；代价是会平滑掉小范围的热点。
区域尺度上这是常见做法（Yao et al. 2021 分析南海夏季热浪即用此思路）。
"""

from __future__ import annotations

import json
import math
import os
from datetime import date as _date
from dataclasses import dataclass

from . import grid as G
from . import regions as R

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERIES_PATH = os.path.join(BASE, "data", "region_series.json")
CLIM_PATH = os.path.join(BASE, "data", "mhw_clim.json")
EVENTS_PATH = os.path.join(BASE, "data", "mhw_events.json")

BASELINE = (1982, 2011)     # Hobday 约定的基准期
WINDOW = 5                  # day-of-year 前后各取 5 天
MIN_DURATION = 5            # 至少连续 5 天
MAX_GAP = 2                 # 允许中间断开 2 天


# ---------------------------------------------------------------- 数据加载

def load_series(include_grid: bool = True) -> dict[str, dict[str, float]]:
    """返回 {date: {region: sst}}。优先用回补的 1982 起序列，
    再用本地网格补最近的日期（两者口径一致：同一个盒子 + cos(纬度) 加权）。"""
    series: dict[str, dict[str, float]] = {}
    if os.path.exists(SERIES_PATH):
        with open(SERIES_PATH, encoding="utf-8") as f:
            series = json.load(f)
    if include_grid:
        try:
            from . import mlfeatures

            g = G.Grid()
            wmap = mlfeatures.region_weights(g)
            rser = mlfeatures.region_series(g, wmap)
            codes = [r.code for r in R.REGIONS]
            for t, d in enumerate(g.dates):
                row = series.setdefault(d, {})
                for c in codes:
                    if c not in row:
                        v = rser[c][t]
                        if v == v:
                            row[c] = round(v, 4)
        except Exception:  # noqa: BLE001
            pass
    return {d: v for d, v in series.items() if len(v) == len(R.REGIONS)}


def _percentile(sorted_vals: list[float], q: float) -> float:
    """线性插值分位数（和 numpy.percentile 的默认做法一致）。"""
    if not sorted_vals:
        return float("nan")
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (len(sorted_vals) - 1) * q
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


# ---------------------------------------------------------------- 气候态

def build_climatology(series, baseline=BASELINE, window: int = WINDOW):
    """每个海区、每个 day-of-year 的均值与 90 分位阈值。"""
    pools: dict[str, list[list[float]]] = {
        r.code: [[] for _ in range(G.NDOY)] for r in R.REGIONS
    }
    for d, row in series.items():
        y = int(d[:4])
        if not (baseline[0] <= y <= baseline[1]):
            continue
        doy = G.doy_index(d)
        for c, v in row.items():
            if c in pools:
                pools[c][doy].append(v)

    out: dict[str, dict[str, list[float]]] = {}
    for c, slots in pools.items():
        mean, p90 = [], []
        for k in range(G.NDOY):
            vals = []
            for off in range(-window, window + 1):
                vals += slots[(k + off) % G.NDOY]
            vals.sort()
            mean.append(sum(vals) / len(vals) if vals else float("nan"))
            p90.append(_percentile(vals, 0.90))
        out[c] = {"mean": mean, "p90": p90}
    return out


def load_climatology(rebuild: bool = False):
    if not rebuild and os.path.exists(CLIM_PATH):
        with open(CLIM_PATH, encoding="utf-8") as f:
            return json.load(f)
    series = load_series()
    clim = build_climatology(series)
    with open(CLIM_PATH, "w", encoding="utf-8") as f:
        json.dump({"baseline": list(BASELINE), "window": WINDOW, "regions": clim}, f)
    return {"baseline": list(BASELINE), "window": WINDOW, "regions": clim}


# ---------------------------------------------------------------- 事件

@dataclass
class Event:
    region: str
    start: str
    end: str
    duration: int
    intensity_mean: float
    intensity_max: float
    severity: float
    category: int
    peak_date: str

    def as_dict(self) -> dict:
        return {
            "region": self.region,
            "region_cn": R.get(self.region).name_cn,
            "start": self.start, "end": self.end, "duration": self.duration,
            "intensity_mean": round(self.intensity_mean, 3),
            "intensity_max": round(self.intensity_max, 3),
            "severity": round(self.severity, 2),
            "category": self.category,
            "category_name": CATEGORY_NAMES[self.category],
            "peak_date": self.peak_date,
        }


CATEGORY_NAMES = {0: "无", 1: "I 中等", 2: "II 强", 3: "III 严重", 4: "IV 极端"}


def categorize(intensity: float, dsst: float) -> int:
    if dsst <= 0 or intensity <= 0:
        return 0
    ratio = intensity / dsst
    if ratio >= 4:
        return 4
    if ratio >= 3:
        return 3
    if ratio >= 2:
        return 2
    return 1


def detect(series, clim, region: str, start: str | None = None,
           end: str | None = None) -> list[Event]:
    """在给定日期范围内检测某个海区的热浪事件。"""
    mean = clim["regions"][region]["mean"]
    p90 = clim["regions"][region]["p90"]
    days = sorted(d for d in series if start is None or d >= start)
    if end:
        days = [d for d in days if d <= end]

    hot: list[tuple[str, float, float, float]] = []   # (date, sst, thr, dsst)
    for d in days:
        v = series[d].get(region)
        if v is None:
            continue
        k = G.doy_index(d)
        thr, mu = p90[k], mean[k]
        if thr != thr or v != v:
            continue
        if v > thr:
            hot.append((d, v, thr, thr - mu))

    events: list[Event] = []
    cur: list[tuple[str, float, float, float]] = []
    prev_day = None
    for item in hot:
        d = item[0]
        if cur and prev_day is not None:
            gap = (_date.fromisoformat(d) - _date.fromisoformat(prev_day)).days - 1
            if gap > MAX_GAP:
                events.append(_finish(region, cur))
                cur = []
        cur.append(item)
        prev_day = d
    if cur:
        events.append(_finish(region, cur))

    return [e for e in events if e.duration >= MIN_DURATION]


def _finish(region: str, items) -> Event:
    inten = [v - thr for _d, v, thr, _ds in items]
    dsst = sum(x[3] for x in items) / len(items)
    peak = max(range(len(items)), key=lambda i: inten[i])
    return Event(
        region=region,
        start=items[0][0], end=items[-1][0], duration=len(items),
        intensity_mean=sum(inten) / len(inten),
        intensity_max=max(inten),
        severity=sum(inten),
        category=categorize(max(inten), dsst),
        peak_date=items[peak][0],
    )


def all_events(series, clim, start: str | None = None, end: str | None = None):
    out = []
    for reg in R.REGIONS:
        out += [e.as_dict() for e in detect(series, clim, reg.code, start, end)]
    out.sort(key=lambda e: (e["start"], e["region"]), reverse=True)
    return out


def build_and_save(rebuild_clim: bool = False) -> dict:
    """算好全部事件与"热浪日"清单，存盘给榜单和前端用。"""
    series = load_series()
    clim = load_climatology(rebuild=rebuild_clim)
    events = all_events(series, clim)
    days: dict[str, list[str]] = {r.code: [] for r in R.REGIONS}
    for e in events:
        d = _date.fromisoformat(e["start"])
        end = _date.fromisoformat(e["end"])
        while d <= end:
            days[e["region"]].append(d.isoformat())
            d += _date.resolution
    payload = {
        "generated_at": max(series) if series else None,
        "series_range": [min(series), max(series)] if series else None,
        "baseline": list(BASELINE),
        "n_series_days": len(series),
        "events": events,
        "mhw_days": {k: sorted(set(v)) for k, v in days.items()},
    }
    with open(EVENTS_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    return payload


def load_saved() -> dict | None:
    if not os.path.exists(EVENTS_PATH):
        return None
    with open(EVENTS_PATH, encoding="utf-8") as f:
        return json.load(f)


def mhw_day_set(saved: dict | None = None) -> set[tuple[str, str]]:
    """{(region, date)} 的集合，用来给榜单记录打标签。"""
    saved = saved or load_saved() or {}
    out = set()
    for reg, days in (saved.get("mhw_days") or {}).items():
        for d in days:
            out.add((reg, d))
    return out


# ---------------------------------------------------------------- 当前状态

def status_table(series, clim, day: str) -> list[dict]:
    """某一天，7 个海区相对阈值的状态。"""
    k = G.doy_index(day)
    rows = []
    for reg in R.REGIONS:
        v = series.get(day, {}).get(reg.code)
        thr = clim["regions"][reg.code]["p90"][k]
        mu = clim["regions"][reg.code]["mean"][k]
        rows.append({
            "region": reg.code, "region_cn": reg.name_cn,
            "sst": None if v is None else round(v, 2),
            "threshold": None if thr != thr else round(thr, 2),
            "clim_mean": None if mu != mu else round(mu, 2),
            "anomaly": None if (v is None or mu != mu) else round(v - mu, 2),
            "above": bool(v is not None and thr == thr and v > thr),
            "margin": None if (v is None or thr != thr) else round(v - thr, 2),
        })
    return rows
