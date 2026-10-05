"""解析外部提交的预报文件，换算成 7 个海区的日均海温。

支持两种输入形态：
  A. 已经算好的海区值：CSV 列 target_date, horizon, region, value
     （region 可以写代码 beibu，也可以写中文名 北部湾）
  B. 网格场：CSV / NetCDF / Parquet，列 date + lat + lon + sst
     我们自己在服务端按同样的 cos(纬度) 加权规则聚合成海区平均。

网格文件解析：
  · CSV      —— 标准库直接读
  · NetCDF   —— 优先用 ncdump（brew netcdf 自带），兼容 NetCDF3/4
  · Parquet  —— 需要 pyarrow；项目 .venv 里有的话会自动用
"""

from __future__ import annotations

import csv
import io
import math
import os
import re
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta

from . import oisst
from . import regions as R

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 允许从项目 .venv 里借用 pyarrow（主服务本身仍是零依赖）
_py = f"python{sys.version_info.major}.{sys.version_info.minor}"
for _sp in (os.path.join(BASE, ".venv", "lib", _py, "site-packages"),
            os.path.join(BASE, ".venv", "lib64", _py, "site-packages")):
    if os.path.isdir(_sp) and _sp not in sys.path:
        sys.path.append(_sp)

ALIAS = {}
for _r in R.REGIONS:
    for _k in (_r.code, _r.name_cn, _r.name_en, _r.name_en.lower()):
        ALIAS[_k] = _r.code
ALIAS.update({"海南": "hainan_se", "海南岛东南": "hainan_se",
              "珠江口": "pearl_river", "东沙": "pearl_river",
              "西沙": "xisha", "中沙": "zhongsha", "黄岩岛": "zhongsha",
              "南沙": "nansha_n", "南海南部": "scs_south", "南部": "scs_south"})


class IngestError(Exception):
    pass


_MASK_CACHE: list = []


def _ocean_mask():
    """用最近一天的 OISST 生成海陆掩膜。

    上传的网格场如果没做过海陆处理（例如直接从大气模式导出的地表温度），
    会把中南半岛、婆罗洲的陆地温度混进海区平均，必须按同一套掩膜剔掉。
    """
    if _MASK_CACHE:
        return _MASK_CACHE[0]
    mask = None
    try:
        from datetime import timezone as _tz
        today = datetime.now(_tz.utc).date()
        for back in range(2, 10):
            day = (today - timedelta(days=back)).isoformat()
            try:
                _ds, rows = oisst.fetch_box(day)
                mask = oisst.ocean_mask(rows)
                break
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        mask = None
    _MASK_CACHE.append(mask)
    return mask


def _to_celsius(v: float) -> float:
    """多数海洋/大气网格用开尔文，海温不可能超过 100°C，据此自动换算。"""
    return v - 273.15 if v > 100.0 else v


def _norm_region(text: str) -> str | None:
    t = (text or "").strip()
    if t in ALIAS:
        return ALIAS[t]
    for k, v in ALIAS.items():
        if k and k in t:
            return v
    return None


def _norm_date(text: str) -> str | None:
    t = (text or "").strip().replace("/", "-")
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return None


# ------------------------------------------------------------------ 形态 A

def parse_region_table(text: str, default_horizon: int | None) -> list[dict]:
    rdr = csv.DictReader(io.StringIO(text))
    if not rdr.fieldnames:
        raise IngestError("CSV 没有表头")
    cols = {c.strip().lower(): c for c in rdr.fieldnames}

    def pick(*names):
        for n in names:
            if n in cols:
                return cols[n]
        return None

    c_date = pick("target_date", "date", "日期", "目标日")
    c_h = pick("horizon", "lead", "时效")
    c_reg = pick("region", "area", "海区", "区域")
    c_val = pick("value", "sst", "temp", "预报值", "温度")
    if not (c_date and c_reg and c_val):
        raise IngestError("海区表需要 target_date / region / value 三列")

    out = []
    for row in rdr:
        d = _norm_date(row.get(c_date, ""))
        reg = _norm_region(row.get(c_reg, ""))
        if not d or not reg:
            continue
        try:
            v = _to_celsius(float(row[c_val]))
            h = int(float(row[c_h])) if c_h and row.get(c_h) else default_horizon
        except (TypeError, ValueError):
            continue
        if h not in R.HORIZONS:
            continue
        if not (-5 <= v <= 45):
            continue
        out.append({"target_date": d, "horizon": h, "region": reg, "value": round(v, 3)})
    return out


# ------------------------------------------------------------------ 形态 B

def _aggregate_points(points, default_horizon: int | None, today: date) -> list[dict]:
    """points: [(date, lat, lon, value)] -> 海区平均。"""
    buckets: dict[str, list] = {}
    mask = _ocean_mask()
    for d, la, lo, v in points:
        if not d or la is None or lo is None or v is None:
            continue
        try:
            if not (math.isfinite(la) and math.isfinite(lo) and math.isfinite(v)):
                continue
        except TypeError:
            continue
        if not (-90 <= la <= 90 and -180 <= lo <= 360):
            continue
        if lo > 180:
            lo -= 360
        if mask is not None and not oisst.is_ocean(mask, la, lo):
            continue
        buckets.setdefault(d, []).append((la, lo, _to_celsius(v)))

    out = []
    for d, pts in buckets.items():
        for reg in R.REGIONS:
            val, n = oisst.weighted_mean(
                pts, (reg.lon_min, reg.lat_min, reg.lon_max, reg.lat_max)
            )
            if val is None or n < 8:
                continue
            h = default_horizon
            if h is None:
                h = (date.fromisoformat(d) - today).days
            if h not in R.HORIZONS:
                h = min(R.HORIZONS, key=lambda x: abs(x - h))
            out.append({"target_date": d, "horizon": h, "region": reg.code,
                        "value": round(val, 3)})
    return out


def parse_grid_csv(text: str, default_horizon: int | None, today: date) -> list[dict]:
    rdr = csv.DictReader(io.StringIO(text))
    if not rdr.fieldnames:
        raise IngestError("CSV 没有表头")
    cols = {c.strip().lower(): c for c in rdr.fieldnames}

    def pick(*names):
        for n in names:
            if n in cols:
                return cols[n]
        return None

    c_t = pick("time", "date", "datetime", "日期", "时间")
    c_la = pick("lat", "latitude", "纬度")
    c_lo = pick("lon", "longitude", "经度")
    c_v = pick("sst", "value", "temp", "temperature", "海温", "温度")
    if not all((c_t, c_la, c_lo, c_v)):
        raise IngestError("网格 CSV 需要 time/date + lat + lon + sst 四列")

    pts = []
    for row in rdr:
        d = _norm_date(row.get(c_t, ""))
        try:
            la = float(row[c_la]); lo = float(row[c_lo]); v = float(row[c_v])
        except (TypeError, ValueError, KeyError):
            continue
        pts.append((d, la, lo, _to_celsius(v)))
    return _aggregate_points(pts, default_horizon, today)


def _which(name: str) -> str | None:
    for d in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"):
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    return None


def parse_netcdf(raw: bytes, default_horizon: int | None, today: date) -> list[dict]:
    """用 ncdump 把 NetCDF 转成文本再解析（NetCDF3/4 都吃）。"""
    nd = _which("ncdump")
    if not nd:
        raise IngestError("这台机器没有 ncdump（brew install netcdf），请改用 CSV")
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "in.nc")
        with open(p, "wb") as f:
            f.write(raw)
        try:
            out = subprocess.run([nd, p], capture_output=True, text=True,
                                 timeout=180).stdout
        except Exception as e:  # noqa: BLE001
            raise IngestError(f"ncdump 解析失败：{e}") from e
    if not out.strip():
        raise IngestError("ncdump 没有输出，文件可能不是有效的 NetCDF")
    return _parse_ncdump(out, default_horizon, today)


_NUM = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")


def _nums_after(text: str, header: str, limit: int = 2_000_000) -> list[float]:
    # 只在 "data:" 之后找，否则会命中头部的维度声明，例如
    #   dimensions:  latitude = 81 ;   ← 这里返回的是 [81]，不是网格
    i = text.find("data:")
    if i < 0:
        i = 0
    i = text.find(header, i)
    if i < 0:
        return []
    seg = text[i:i + limit]
    eq = seg.find("=")
    if eq < 0:
        return []
    body = seg[eq + 1:]
    end = body.find(";")
    if end >= 0:
        body = body[:end]
    out = []
    for x in _NUM.findall(body):
        try:
            out.append(float(x))
        except ValueError:
            pass
    return out


def _parse_ncdump(text: str, default_horizon: int | None, today: date) -> list[dict]:
    """从 ncdump 输出里捞出 lat / lon / 数据变量。

    只处理最常见的布局：time×lat×lon 或单层 lat×lon。
    """
    lats = _nums_after(text, "lat =")
    lons = _nums_after(text, "lon =")
    if not lats or not lons:
        lats = _nums_after(text, "latitude =")
        lons = _nums_after(text, "longitude =")
    if not lats or not lons:
        raise IngestError("NetCDF 里找不到 lat / lon 变量（需要标准命名）")

    var, vname = None, None
    for name in ("sst", "SST", "analysed_sst", "temperature", "temp", "thetao"):
        vals = _nums_after(text, f"{name} =")
        if vals:
            var, vname = vals, name
            break
    if var is None:
        # 兜底：在 ncdump 里找数据量最大的那个数组，跳过坐标变量。
        # 用户从 GRIB 转出来的 nc，变量名常常是 t / t_surface 之类。
        skip = {"lat", "latitude", "lon", "longitude", "time", "depth", "level",
                "lev", "nv", "bounds", "height", "valid_time"}
        best = (None, None, 0)
        for name in set(re.findall(r"\n\s+([A-Za-z_][A-Za-z_0-9]*)\s*=\s*", text)):
            if name.lower() in skip:
                continue
            vals = _nums_after(text, f"{name} =")
            if len(vals) > best[2]:
                best = (vals, name, len(vals))
        if best[0]:
            var, vname = best[0], best[1]
    if var is None:
        raise IngestError("NetCDF 里找不到可用的数据变量（试过 sst/temp/t 等）")
    if len(var) < len(lats) * len(lons):
        raise IngestError(f"变量 {vname} 的数据长度（{len(var)}）小于 "
                          f"lat×lon（{len(lats)*len(lons)}），可能不是网格场")

    # NetCDF 常把数据压成 short/int，用 scale_factor + add_offset 还原；
    # 不还原的话拿到的是一堆几千几万的整数，数值全错。
    def attr(aname: str) -> float | None:
        m = re.search(rf"{re.escape(vname)}:{aname}\s*=\s*({_NUM.pattern})", text)
        if not m:
            m = re.search(rf":{aname}\s*=\s*({_NUM.pattern})", text)
        return float(m.group(1)) if m else None

    scale = attr("scale_factor")
    offset = attr("add_offset")
    fill = attr("_FillValue")
    if scale is not None or offset is not None:
        s = scale if scale is not None else 1.0
        o = offset if offset is not None else 0.0
        var = [v * s + o for v in var]
    if fill is not None:
        var = [float("nan") if abs(v - fill) < 1e-6 else v for v in var]

    times = _nums_after(text, "time =")
    nlat, nlon = len(lats), len(lons)
    nspace = nlat * nlon
    ntime = max(1, len(var) // nspace)

    m = re.search(r'units\s*=\s*"([^"]*since[^"]*)"', text)
    units = m.group(1) if m else "days"
    base = _time_base(units)
    unit = "days" if "day" in units else ("hours" if "hour" in units else "seconds")
    mult = {"days": 1.0, "hours": 1 / 24, "seconds": 1 / 86400}[unit]

    def to_date(t) -> str | None:
        if base is None:
            return None
        try:
            return (base + timedelta(days=float(t) * mult)).date().isoformat()
        except Exception:  # noqa: BLE001
            return None

    pts = []
    for ti in range(ntime):
        chunk = var[ti * nspace:(ti + 1) * nspace]
        if len(chunk) < nspace:
            break
        d = to_date(times[ti]) if ti < len(times) else None
        if d is None and times:
            d = _norm_date(str(times[ti]))
        for i in range(nlat):
            for j in range(nlon):
                pts.append((d, lats[i], lons[j], chunk[i * nlon + j]))
    return _aggregate_points(pts, default_horizon, today)


def _time_base(units: str):
    m = re.search(r"since\s+(\d{4})-(\d{2})-(\d{2})[ T]?(\d{2})?:?(\d{2})?:?(\d{2})?", units)
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    hh = int(m.group(4) or 0); mm = int(m.group(5) or 0); ss = int(m.group(6) or 0)
    return datetime(y, mo, d, hh, mm, ss)


def parse_parquet(raw: bytes, default_horizon: int | None, today: date) -> list[dict]:
    try:
        import pyarrow.parquet as pq  # type: ignore
    except ImportError as e:
        raise IngestError(
            "Parquet 需要 pyarrow：先跑 deploy/setup_parquet.sh，或另存为 CSV 再提交"
        ) from e
    table = pq.read_table(io.BytesIO(raw))
    names = {n.lower(): n for n in table.column_names}

    def col(*cands):
        for c in cands:
            if c in names:
                return names[c]
        return None

    c_t = col("time", "date", "datetime", "target_date")
    c_la, c_lo = col("lat", "latitude"), col("lon", "longitude")
    c_v = col("sst", "value", "temp", "temperature", "analysed_sst")
    if not all((c_t, c_la, c_lo, c_v)):
        raise IngestError("Parquet 需要 time/date + lat + lon + sst/value 列")
    cols = table.to_pydict()
    pts = []
    for i in range(table.num_rows):
        raw_t = cols[c_t][i]
        d = _norm_date(str(raw_t))
        if d is None and hasattr(raw_t, "date"):
            d = raw_t.date().isoformat()
        try:
            la = float(cols[c_la][i]); lo = float(cols[c_lo][i])
            v = _to_celsius(float(cols[c_v][i]))
        except (TypeError, ValueError):
            continue
        pts.append((d, la, lo, v))
    return _aggregate_points(pts, default_horizon, today)


# ------------------------------------------------------------------ 总入口

def parse_upload(filename: str, raw: bytes, default_horizon: int | None = None
                 ) -> tuple[list[dict], str]:
    """返回 (条目列表, 识别出的格式说明)。"""
    name = (filename or "").lower()
    today = date.today()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = raw.decode("gbk")
        except UnicodeDecodeError:
            text = None

    if name.endswith((".csv", ".txt", ".tsv")) or (
            text and "," in text.split("\n", 1)[0] and name.endswith("")):
        if text is None:
            raise IngestError("文本文件编码无法识别，请存成 UTF-8 CSV")
        head = text.split("\n", 1)[0].lower()
        if re.search(r"\blat\b|latitude|纬度", head):
            return parse_grid_csv(text, default_horizon, today), "网格 CSV"
        return parse_region_table(text, default_horizon), "海区表 CSV"
    if name.endswith((".nc", ".nc4", ".cdf", ".netcdf")):
        return parse_netcdf(raw, default_horizon, today), "NetCDF"
    if name.endswith((".parquet", ".pq")):
        return parse_parquet(raw, default_horizon, today), "Parquet"
    raise IngestError("只认 CSV / NetCDF(.nc) / Parquet(.parquet)")


def summarise(entries: list[dict]) -> dict:
    if not entries:
        return {"n": 0}
    dates = sorted({e["target_date"] for e in entries})
    return {
        "n": len(entries),
        "dates": [dates[0], dates[-1]],
        "n_dates": len(dates),
        "horizons": sorted({e["horizon"] for e in entries}),
        "regions": sorted({e["region"] for e in entries}),
    }
