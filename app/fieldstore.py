"""预报场仓库：把各产品的**网格场**（不只是海区均值）按天存下来。

为什么按记录分文件：抓数脚本用的是系统自带的 python（没有 numpy），
每天只新增一两条记录。每条记录一个 18 KB 的小文件，写起来是纯追加，
不需要重写整个二进制；读取端（训练脚本，在 .venv 里）用 numpy 直接读。

目录结构：
    data/fields/<product>/<run_date>_f<lead>.bin     float32, (nlat, nlon), 行优先
    data/fields/<product>/index.json                 已存的 (日期, 时效) 清单
"""

from __future__ import annotations

import array
import json
import os
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIELD_DIR = os.path.join(BASE, "data", "fields")

# 统一网格：和 OISST 一致（0.25°，105.125–118.875E / 3.125–23.375N）
NLAT, NLON = 82, 56
LAT0, LON0, D = 3.125, 105.125, 0.25


def product_dir(product: str) -> str:
    return os.path.join(FIELD_DIR, product)


def _index_path(product: str) -> str:
    return os.path.join(product_dir(product), "index.json")


def load_index(product: str) -> dict:
    p = _index_path(product)
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def save_index(product: str, idx: dict) -> None:
    d = product_dir(product)
    os.makedirs(d, exist_ok=True)
    p = _index_path(product)
    fd, tmp = tempfile.mkstemp(dir=d)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(idx, f)
    os.replace(tmp, p)


def field_path(product: str, run_date: str, lead: int) -> str:
    return os.path.join(product_dir(product), f"{run_date}_f{lead}.bin")


def save_field(product: str, run_date: str, lead: int, values) -> None:
    """values 长度必须是 NLAT*NLON，缺失用 NaN。"""
    if len(values) != NLAT * NLON:
        raise ValueError(f"场长度 {len(values)} != {NLAT*NLON}")
    d = product_dir(product)
    os.makedirs(d, exist_ok=True)
    arr = array.array("f", [float(v) for v in values])
    fd, tmp = tempfile.mkstemp(dir=d)
    with os.fdopen(fd, "wb") as f:
        arr.tofile(f)
    os.replace(tmp, field_path(product, run_date, lead))
    idx = load_index(product)
    key = f"{run_date}|{lead}"
    idx[key] = os.path.basename(field_path(product, run_date, lead))
    save_index(product, idx)


def has_field(product: str, run_date: str, lead: int) -> bool:
    return os.path.exists(field_path(product, run_date, lead))


def list_dates(product: str) -> list[str]:
    return sorted({k.split("|")[0] for k in load_index(product)})


# ---------------------------------------------------------------- 重采样

def _nk(lat: float, lon: float) -> tuple[int, int] | None:
    """经纬度 -> 统一网格索引（最近邻）。"""
    i = int(round((lat - LAT0) / D))
    j = int(round((lon - LON0) / D))
    if 0 <= i < NLAT and 0 <= j < NLON:
        return i, j
    return None


def points_to_box(points, half_shift: bool = False) -> array.array:
    """把 [(lat, lon, value)] 铺到统一网格上。

    half_shift=True 用于起点差半个格点的产品（例如 GFS 从 3.0°N 起），
    这时把坐标整体挪半格，让最近的网格点对得上。
    """
    out = array.array("f", [float("nan")] * (NLAT * NLON))
    off = 0.5 * D if half_shift else 0.0
    for lat, lon, v in points:
        if v is None or v != v:
            continue
        k = _nk(lat - off, lon - off)
        if k is None:
            continue
        i, j = k
        out[i * NLON + j] = v
    return out
