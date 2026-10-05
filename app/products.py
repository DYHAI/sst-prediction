"""官方海表温度预报产品的取数适配器。

三个产品（都是免注册、免 key 的公开源）：

  hycom  —— 美国海军/NOAA HYCOM ESPC-D-V02 全球 1/12° 海洋预报，逐小时，8 天。
            走 THREDDS 的 OPeNDAP ASCII，拿到的是地表 sst 变量。
  gfs    —— NCEP GFS 0.25° 大气模式的海表温度场（洋面上 TMP@surface 即 SST），16 天。
            走 NOMADS 的 GRIB 子区域筛选，再用 eccodes 解码。
  cfs    —— NCEP CFSv2 耦合模式的海洋 5m 温度（ocnsst），6 小时步长，45 天。
            直接下每日 GRIB，用 eccodes 抽取指定预报时效。

每个适配器返回 {target_date: {region_code: value}}，并附带实际使用的起报时间，
因为上游发布节奏不受我们控制，榜单上要如实显示"这条预报是哪次起报"。
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import urllib.parse
from datetime import datetime, timedelta

from . import oisst
from . import regions as R
from . import fieldstore
from .http_util import fetch, fetch_text

ECCODES_BIN = os.environ.get("ECCODES_BIN", "/opt/homebrew/bin")

# 真值 OISST 是"日平均"，所以产品也必须取日均，否则拿瞬时值去比日平均是两套口径。
# 做法：取目标日 00/06/12/18 UTC 四个时次求平均。
DAILY_HOURS = (0, 6, 12, 18)

PRODUCTS: dict[str, dict] = {
    "hycom": {
        "name": "HYCOM ESPC-D-V02",
        "org": "美国海军 FNMOC / NOAA",
        "note": "全球 1/12° 海洋环流预报，逐小时，8 天",
        "max_horizon": 8,
    },
    "gfs": {
        "name": "NCEP GFS",
        "org": "NOAA/NCEP",
        "note": "0.25° 大气模式海表温度场，16 天",
        "max_horizon": 10,
    },
    "cfs": {
        "name": "NCEP CFSv2",
        "org": "NOAA/NCEP",
        "note": "耦合模式 5m 位温（近似 SST），6 小时步长，45 天",
        "max_horizon": 40,
    },
    # 本站自己训练的模型。它上榜单但不参与"基准"的计算——
    # 基准必须只由官方产品决定，否则就是自己给自己判卷。
    "ours": {
        "name": "本站模型 · LIM",
        "org": "本站（线性逆模型 / VAR，纯数据驱动）",
        "note": "用 3 年 OISST 拟合南海海温距平的线性演化算子，2° 粗格点、逐日气候态",
        "max_horizon": 8,
        "reference": False,
        "fetch": False,
    },
    # 梯度提升模型（XGBoost / CatBoost / LightGBM）。
    # 不带 _post：只用观测历史，是独立预报；带 _post：特征里含官方产品，属后处理。
    "xgboost": {
        "name": "本站 ML · XGBoost",
        "org": "本站（梯度提升树，输入为海温距平轨迹）",
        "note": "7 个海区 × 近 7 天的距平轨迹 + 季节项，输出目标日距平",
        "max_horizon": 8, "reference": False, "fetch": False,
    },
    "catboost": {
        "name": "本站 ML · CatBoost",
        "org": "本站（梯度提升树，输入为海温距平轨迹）",
        "note": "同上特征集，换 CatBoost 的对称树与有序提升",
        "max_horizon": 8, "reference": False, "fetch": False,
    },
    "lightgbm": {
        "name": "本站 ML · LightGBM",
        "org": "本站（梯度提升树，输入为海温距平轨迹）",
        "note": "同上特征集，换 LightGBM 的直方图分裂",
        "max_horizon": 8, "reference": False, "fetch": False,
    },
    "xgboost_post": {
        "name": "本站 ML · XGBoost 后处理",
        "org": "本站（梯度提升树 + 官方产品预报值）",
        "note": "在距平轨迹之外，还把 HYCOM / GFS / CFSv2 的预报当特征，学一个最优订正",
        "max_horizon": 8, "reference": False, "fetch": False,
    },
    "catboost_post": {
        "name": "本站 ML · CatBoost 后处理",
        "org": "本站（梯度提升树 + 官方产品预报值）",
        "note": "同上，CatBoost 版本",
        "max_horizon": 8, "reference": False, "fetch": False,
    },
    "lightgbm_post": {
        "name": "本站 ML · LightGBM 后处理",
        "org": "本站（梯度提升树 + 官方产品预报值）",
        "note": "同上，LightGBM 版本",
        "max_horizon": 8, "reference": False, "fetch": False,
    },
    # 多源后处理：把 HYCOM / GFS / CFSv2 的预报值 + 持续性当输入，学一组融合权重。
    "stack": {
        "name": "本站融合 · 多源后处理",
        "org": "本站（岭回归 / 凸组合，输入为官方产品预报值）",
        "note": "对应论文里的「全公开产品最优加权组合」，按 (海区,时效) 分组、留一天交叉验证选模型",
        "max_horizon": 8, "reference": False, "fetch": False,
    },
    # U-Net：把模式预报场订正到 OISST 真值场。
    "unet": {
        "name": "本站深度学习 · U-Net 订正",
        "org": "本站（U-Net，输入为持续性场 + GFS 预报场）",
        "note": "空间订正：吃 82×56 的场，学误差的空间结构，输出订正后的海温场",
        "max_horizon": 8, "reference": False, "fetch": False,
    },
}

# 需要联网抓取的官方产品（本站模型由 tools/train_model.py 生成，不走抓取）
FETCHABLE: tuple[str, ...] = tuple(
    code for code, meta in PRODUCTS.items() if meta.get("fetch", True)
)

# 参与"基准"计算的官方产品
REFERENCE_PRODUCTS: tuple[str, ...] = tuple(
    code for code, meta in PRODUCTS.items() if meta.get("reference", True)
)


# ------------------------------------------------------------------ 工具

def _eccodes(cmd: str) -> str:
    return os.path.join(ECCODES_BIN, cmd)


def _run_grib(cmd: list[str]) -> str:
    env = dict(os.environ)
    env["PATH"] = ECCODES_BIN + os.pathsep + env.get("PATH", "")
    p = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=600)
    if p.returncode != 0:
        raise RuntimeError(f"{cmd[0]} 失败: {p.stderr[:400]}")
    return p.stdout


def parse_dap2_ascii(text: str) -> tuple[list[list[float]], dict[str, list[float]]]:
    """解析 OPeNDAP DAP2 的 .ascii 输出。

    返回 (二维数据行, {坐标名: 值列表})。
    """
    body = text.split("---------------------------------------------", 1)
    if len(body) < 2:
        raise ValueError("不是 DAP2 ascii 输出")
    sections = re.split(r"\n\s*\n", body[1].strip())

    data: list[list[float]] = []
    maps: dict[str, list[float]] = {}
    for sec in sections:
        lines = [ln for ln in sec.strip().split("\n") if ln.strip()]
        if not lines:
            continue
        header, rest = lines[0].strip(), lines[1:]
        if not rest:
            continue
        if rest[0].lstrip().startswith("["):
            for ln in rest:
                parts = ln.split(",")
                vals = [p.strip() for p in parts[1:]]
                row = []
                for v in vals:
                    if not v:
                        continue
                    try:
                        row.append(float(v))
                    except ValueError:
                        row.append(float("nan"))
                if row:
                    data.append(row)
        else:
            name = re.sub(r"\[.*\]", "", header).split(".")[-1].strip()
            vals = []
            for v in ",".join(rest).split(","):
                v = v.strip()
                if not v:
                    continue
                try:
                    vals.append(float(v))
                except ValueError:
                    vals.append(float("nan"))
            maps[name] = vals
    return data, maps


def _grid_points(rows: list[list[float]], lat: list[float], lon: list[float]):
    """把 DAP2 的二维数组展开成 (lat, lon, value) 列表。"""
    out = []
    for i, row in enumerate(rows):
        la = lat[i] if i < len(lat) else None
        if la is None:
            continue
        for j, v in enumerate(row):
            if j < len(lon):
                out.append((la, lon[j], v))
    return out


def _region_means(points) -> dict[str, float]:
    out: dict[str, float] = {}
    for reg in R.REGIONS:
        val, _cells = oisst.weighted_mean(
            points, (reg.lon_min, reg.lat_min, reg.lon_max, reg.lat_max)
        )
        if val is not None:
            out[reg.code] = round(val, 3)
    return out


def _mean_fields(fields):
    """多个时次的场做逐点平均（跳过缺测）。纯标准库。"""
    n = fieldstore.NLAT * fieldstore.NLON
    tot = [0.0] * n
    cnt = [0] * n
    for f in fields:
        for k in range(n):
            v = f[k]
            if v == v:
                tot[k] += v
                cnt[k] += 1
    return [tot[k] / cnt[k] if cnt[k] else float("nan") for k in range(n)]


# ------------------------------------------------------------------ HYCOM

HYCOM_URL = (
    "https://tds.hycom.org/thredds/dodsC/"
    "FMRC_ESPC-D-V02_ice/FMRC_ESPC-D-V02_ice_best.ncd"
)
HYCOM_RUN_URL = (
    "https://tds.hycom.org/thredds/dodsC/"
    "FMRC_ESPC-D-V02_ice/runs/FMRC_ESPC-D-V02_ice_RUN_{stamp}"
)
# 索引：lat = -80 + 0.04*i, lon = 0.08*j（南海盒子 3–23.25N, 105–119E）
_HI_LAT = (int((3.0 + 80) / 0.04), int((23.25 + 80) / 0.04))
_HI_LON = (int(105.0 / 0.08), int(119.0 / 0.08))
# 取场时步长要小到能把 0.25° 的统一网格填满（0.08° 的 1/12° 模式取 2 → 0.16°）
_HI_STRIDE = 2


def _hycom_time_axis() -> tuple[list[float], list[float]]:
    txt = fetch_text(
        f"{HYCOM_URL}.ascii?time%5B0:1:360%5D,time_run%5B0:1:360%5D",
        timeout=90, cache=True, cache_ext=".txt", ttl=1800,
    )
    body = txt.split("---------------------------------------------", 1)[1]
    sections = [s for s in re.split(r"\n\s*\n", body.strip()) if s.strip()]
    series = []
    for sec in sections[:2]:
        lines = [ln for ln in sec.strip().split("\n") if ln.strip()]
        series.append([float(x) for x in ",".join(lines[1:]).split(",") if x.strip()])
    return series[0], series[1]


def _hycom_axes() -> tuple[datetime, list[datetime], list[datetime]]:
    """返回 (基准时间, 有效时间列表, 起报时间列表)。"""
    das = fetch_text(f"{HYCOM_URL}.das", timeout=60, cache=True, cache_ext=".das", ttl=1800)
    m = re.search(r'units\s+"hours since (\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})', das)
    if not m:
        raise RuntimeError("HYCOM DAS 里找不到时间基准")
    base = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M:%S")
    valid, runs = _hycom_time_axis()
    return (
        base,
        [base + timedelta(hours=v) for v in valid],
        [base + timedelta(hours=r) for r in runs],
    )


def hycom_runs() -> dict[str, int]:
    """返回 {起报时间: best 聚合里的索引}（仅用于了解上游节奏）。"""
    _base, _valid, runs = _hycom_axes()
    out: dict[str, int] = {}
    for i, ts_dt in enumerate(runs):
        out.setdefault(ts_dt.strftime("%Y-%m-%dT%H:%M:%SZ"), i)
    return out


def hycom_run_times() -> list[datetime]:
    """上游目前保留的起报时刻（升序）。

    注意：不能用 best.ncd 的 (valid, run) 配对来取"某次起报对某天的预报"——
    best 聚合对每个有效时刻只保留最新的一次起报，会把所有时效都变成最短时效，
    等于给官方产品开小灶。必须回到单次起报的数据集。
    """
    _base, _valid, runs = _hycom_axes()
    return sorted(set(runs))


def _hycom_run_valid_times(run_dt: datetime) -> tuple[datetime, list[datetime]]:
    stamp = run_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    url = HYCOM_RUN_URL.format(stamp=stamp)
    das = fetch_text(f"{url}.das", timeout=60, cache=True, cache_ext=".das", ttl=86400)
    m = re.search(r'units\s+"hours since (\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})', das)
    base = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M:%S")
    # 不加约束直接取整条 time 轴：各次起报的时间步数不一样，硬写区间会越界
    txt = fetch_text(f"{url}.ascii?time", timeout=90,
                     cache=True, cache_ext=".txt", ttl=86400)
    body = txt.split("---------------------------------------------", 1)[1]
    lines = [ln for ln in body.strip().split("\n") if ln.strip()]
    vals = [float(x) for x in ",".join(lines[1:]).split(",") if x.strip()]
    return base, [base + timedelta(hours=v) for v in vals]


def hycom_fetch(target_dates: list[str], prefer_run: dict[str, str] | None = None,
                horizon: int | None = None
                ) -> dict[str, dict]:
    """抓 HYCOM。prefer_run 给出每个目标日理想起报时间，实际取 <= 它的最近一次。"""
    runs = hycom_run_times()
    if not runs:
        return {}

    plan: dict[datetime, dict[str, int]] = {}
    chosen: dict[str, datetime] = {}
    for target in target_dates:
        want_valid = datetime.fromisoformat(target + "T00:00:00")
        want = None
        if prefer_run and target in prefer_run:
            want = datetime.fromisoformat(prefer_run[target].replace("Z", ""))
        if want is None:
            continue
        usable = [r for r in runs if r <= want]
        if not usable:
            continue
        run = max(usable)
        chosen[target] = run
        plan.setdefault(run, {})[target] = 0

    results: dict[str, dict] = {}
    for run, targets in plan.items():
        try:
            base, valid = _hycom_run_valid_times(run)
        except Exception:  # noqa: BLE001
            continue
        stamp = run.strftime("%Y-%m-%dT%H:%M:%SZ")
        url_base = HYCOM_RUN_URL.format(stamp=stamp)
        for target in targets:
            want_valid = datetime.fromisoformat(target + "T00:00:00")
            # 四个时次求平均，凑成"日均"
            per_hour: dict[str, list[float]] = {}
            fields = []
            for hh in DAILY_HOURS:
                try:
                    idx = valid.index(want_valid + timedelta(hours=hh))
                except ValueError:
                    continue
                url = (
                    f"{url_base}.ascii?"
                    f"sst%5B{idx}:1:{idx}%5D"
                    f"%5B{_HI_LAT[0]}:{_HI_STRIDE}:{_HI_LAT[1]}%5D"
                    f"%5B{_HI_LON[0]}:{_HI_STRIDE}:{_HI_LON[1]}%5D"
                )
                try:
                    txt = fetch_text(url, timeout=180)
                    rows, maps = parse_dap2_ascii(txt)
                except Exception:  # noqa: BLE001
                    continue
                pts = _grid_points(rows, maps.get("lat", []), maps.get("lon", []))
                fields.append(fieldstore.points_to_box(pts))
                for code, v in _region_means(pts).items():
                    per_hour.setdefault(code, []).append(v)
            if not per_hour:
                continue
            if fields and horizon:
                fieldstore.save_field("hycom", run.strftime("%Y-%m-%d"),
                                      horizon, _mean_fields(fields))
            info = {"_run": stamp}
            for code, vals in per_hour.items():
                info[code] = round(sum(vals) / len(vals), 3)
            results[target] = info
    return results


# ------------------------------------------------------------------ GFS

GFS_FILTER = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl"


def gfs_fetch(target_dates: list[str], prefer_run: dict[str, str] | None = None,
              mask: set | None = None, horizon: int | None = None) -> dict[str, dict]:
    results: dict[str, dict] = {}
    for target in target_dates:
        run = (prefer_run or {}).get(target)
        if not run:
            continue
        run_dt = datetime.fromisoformat(run.replace("Z", ""))
        lead_h = int((datetime.fromisoformat(target + "T00:00:00") - run_dt).total_seconds() // 3600)
        if lead_h <= 0 or lead_h > 384:
            continue
        per_hour: dict[str, list[float]] = {}
        fields = []
        for hh in DAILY_HOURS:
            step = lead_h + hh
            if step % 3:            # GFS 0.25° 只有 3 小时步长
                continue
            params = {
                "dir": f"/gfs.{run_dt.strftime('%Y%m%d')}/{run_dt.strftime('%H')}/atmos",
                "file": f"gfs.t{run_dt.strftime('%H')}z.pgrb2.0p25.f{step:03d}",
                "var_TMP": "on",
                "lev_surface": "on",
                "subregion": "",
                "leftlon": "104",
                "rightlon": "120",
                "toplat": "24",
                "bottomlat": "2",
            }
            url = GFS_FILTER + "?" + urllib.parse.urlencode(params)
            try:
                raw = fetch(url, timeout=120, cache=True, cache_ext=".grb2")
                pts = _grib_points_celsius(raw, mask)
            except Exception:  # noqa: BLE001
                continue
            fields.append(fieldstore.points_to_box(pts, half_shift=True))
            for code, v in _region_means(pts).items():
                per_hour.setdefault(code, []).append(v)
        if not per_hour:
            continue
        if fields and horizon:
            fieldstore.save_field("gfs", run_dt.strftime("%Y-%m-%d"), horizon,
                                  _mean_fields(fields))
        info = {"_run": run_dt.strftime("%Y-%m-%dT%H:%M:%SZ")}
        for code, vals in per_hour.items():
            info[code] = round(sum(vals) / len(vals), 3)
        results[target] = info
    return results


def _grib_points_celsius(raw: bytes, mask: set | None):
    with tempfile.TemporaryDirectory() as td:
        gp = os.path.join(td, "in.grb2")
        with open(gp, "wb") as f:
            f.write(raw)
        out = _run_grib([_eccodes("grib_get_data"), "-m", "nan", gp])
    pts = []
    for ln in out.splitlines()[1:]:
        parts = ln.split()
        if len(parts) < 3:
            continue
        try:
            lat, lon, val = float(parts[0]), float(parts[1]), float(parts[2])
        except ValueError:
            continue
        if val != val:  # nan
            continue
        if mask is not None and not oisst.is_ocean(mask, lat, lon):
            continue
        pts.append((lat, lon, val - 273.15))
    return pts


# ------------------------------------------------------------------ CFSv2

CFS_BASE = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/cfs/prod"


def cfs_fetch(target_dates: list[str], prefer_run: dict[str, str] | None = None,
              mask: set | None = None, horizon: int | None = None) -> dict[str, dict]:
    results: dict[str, dict] = {}
    by_run: dict[str, list[str]] = {}
    for target in target_dates:
        run = (prefer_run or {}).get(target)
        if run:
            by_run.setdefault(run, []).append(target)

    for run, targets in by_run.items():
        run_dt = datetime.fromisoformat(run.replace("Z", ""))
        stamp = run_dt.strftime("%Y%m%d%H")
        day = run_dt.strftime("%Y%m%d")
        url = f"{CFS_BASE}/cfs.{day}/{stamp[8:10]}/time_grib_01/ocnsst.01.{stamp}.daily.grb2"
        try:
            raw = fetch(url, timeout=300, cache=True, cache_ext=".grb2")
        except Exception:  # noqa: BLE001
            continue
        with tempfile.TemporaryDirectory() as td:
            gp = os.path.join(td, "cfs.grb2")
            with open(gp, "wb") as f:
                f.write(raw)
            for target in targets:
                lead_h = int(
                    (datetime.fromisoformat(target + "T00:00:00") - run_dt).total_seconds() // 3600
                )
                per_hour: dict[str, list[float]] = {}
                fields = []
                for hh in DAILY_HOURS:
                    step = lead_h + hh
                    small = os.path.join(td, f"f{step}.grb2")
                    try:
                        _run_grib([_eccodes("grib_copy"), "-w", f"stepRange={step}", gp, small])
                        out = _run_grib([_eccodes("grib_get_data"), "-m", "nan", small])
                    except Exception:  # noqa: BLE001
                        continue
                    pts = []
                    for ln in out.splitlines()[1:]:
                        parts = ln.split()
                        if len(parts) < 3:
                            continue
                        try:
                            lat, lon, val = float(parts[0]), float(parts[1]), float(parts[2])
                        except ValueError:
                            continue
                        if val != val:
                            continue
                        if mask is not None and not oisst.is_ocean(mask, lat, lon):
                            continue
                        if val > 200.0:      # CFS ocnsst 用开尔文
                            val -= 273.15
                        pts.append((lat, lon, val))
                    for code, v in _region_means(pts).items():
                        per_hour.setdefault(code, []).append(v)
                    fields.append(fieldstore.points_to_box(pts))
                if not per_hour:
                    continue
                if fields and horizon:
                    fieldstore.save_field("cfs", run_dt.strftime("%Y-%m-%d"),
                                          horizon, _mean_fields(fields))
                info = {"_run": run}
                for code, vals in per_hour.items():
                    info[code] = round(sum(vals) / len(vals), 3)
                results[target] = info
    return results
