"""多源预报后处理（在官方产品基础上学一个最优订正）。

思路就是 Crosier (2026) 里那个"全公开产品的最优加权组合"：把 HYCOM / GFS / CFSv2
的预报值加上持续性基线一起当输入，用最小二乘/岭回归学一组权重，输出订正后的预报。
区别是这里有官方产品的**实际历史**，权重是按 (海区, 时效) 分组、只在历史数据上拟合的。

做法上的两个关键点：
  · 全部在**距平**上做，绝对温标会被季节循环主导，权重会失真。
  · 缺哪个产品的值就用持续性距平补上，并额外给一个"在不在"的标志位。

评估用**留一天交叉验证**（LODO）：每次抽掉一整天，用其余天拟合，预测被抽掉的那天。
这是最贴近真实业务的做法——你不可能提前知道当天真值。

数据量说明：官方产品只保留约 10–15 天历史，所以现在样本很少。
这个脚本可以每天重跑，样本会自动变多、成绩会随之变化。

用法：
    .venv/bin/python -m tools.train_postproc --backtest
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone

from app import db, grid as G, regions as R

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "data", "models", "postproc.json")
PRODUCTS = ("hycom", "gfs", "cfs")


def load_samples(min_products: int = 1):
    """拼出训练表：每条 = (目标日, 时效, 海区) 的真值 + 各产品预报值。"""
    with db.session() as conn:
        truth = {}
        for r in conn.execute("SELECT date, region, sst FROM truth"):
            truth[(r["date"], r["region"])] = r["sst"]
        prod = {}
        for r in conn.execute(
                "SELECT product, region, horizon, target_date, value FROM products"
                " WHERE product IN ('hycom','gfs','cfs')"):
            prod[(r["target_date"], r["horizon"], r["region"], r["product"])] = r["value"]

    keys = set()
    for (d, h, reg, p) in prod:
        keys.add((d, h, reg))

    fb_clim, fb_pers = climatology_and_persistence()
    rows = []
    for (d, h, reg) in sorted(keys):
        y = truth.get((d, reg))
        if y is None:
            continue
        clim = fb_clim.get((reg, G.doy_index(d)))
        if clim is None:
            continue
        pers = fb_pers.get((d, h, reg))
        vals = {p: prod.get((d, h, reg, p)) for p in PRODUCTS}
        present = sum(1 for p in PRODUCTS if vals[p] is not None)
        if present < min_products:
            continue
        rows.append({
            "date": d, "horizon": h, "region": reg,
            "y": y - clim,
            "clim": clim,
            "vals": vals,
            "pers": pers,
        })
    return rows


def climatology_and_persistence():
    """海区气候态 + 持续性距平（用回补的 OISST 网格算）。"""
    from app import mlfeatures

    grid = G.Grid()
    wmap = mlfeatures.region_weights(grid)
    series = mlfeatures.region_series(grid, wmap)
    clim_tab = mlfeatures.region_climatology(series, grid.dates)
    tindex = {d: i for i, d in enumerate(grid.dates)}

    def clim(region, doy):
        return clim_tab[region][doy]

    def pers(target, horizon, region):
        t = tindex.get(target)
        if t is None:
            return None
        # 初值 = 不晚于 (目标日 − 时效) 的最后一个观测
        cut = (datetime.fromisoformat(target) - timedelta(days=horizon)).date().isoformat()
        t0 = None
        for d in grid.dates:
            if d <= cut:
                t0 = tindex[d]
            else:
                break
        if t0 is None:
            return None
        v = series[region][t0]
        if v != v:
            return None
        return v - clim_tab[region][G.doy_index(grid.dates[t0])]

    clim_map = {}
    for reg in [r.code for r in R.REGIONS]:
        for doy in range(G.NDOY):
            clim_map[(reg, doy)] = clim_tab[reg][doy]

    pers_map = {}
    for d in grid.dates:
        for h in R.HORIZONS:
            for reg in [r.code for r in R.REGIONS]:
                p = pers(d, h, reg)
                if p is not None:
                    pers_map[(d, h, reg)] = p
    return clim_map, pers_map


def feature(row):
    """距平特征：3 个产品 + 持续性 + 3 个缺失标志。"""
    pers = row["pers"] if row["pers"] is not None else 0.0
    xs, flags = [], []
    for p in PRODUCTS:
        v = row["vals"][p]
        if v is None:
            xs.append(pers)
            flags.append(1.0)          # 缺失标志
        else:
            xs.append(v - row["clim"])
            flags.append(0.0)
    xs.append(pers)
    return xs + flags


def as_of_for(target: str, horizon: int) -> str:
    """截止时刻能拿到的最新观测日（只用来记录，不参与计算）。"""
    cut = (datetime.fromisoformat(target) - timedelta(days=horizon)).date().isoformat()
    return cut


def ridge_fit(X, y, lam=1.0):
    import numpy as np

    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=float)
    A = X.T @ X + lam * np.eye(X.shape[1])
    b = X.T @ y
    return np.linalg.solve(A, b)


def convex_weights(X, y, step: float = 0.05):
    """在单纯形上搜权重：w ≥ 0 且 Σw = 1（凸组合）。

    样本少的时候，无约束最小二乘会给出很大的正负权重（等于在拟合噪声），
    Smith & Wallis (2009) 指出这种情况简单平均常常更好——论文里也复现了这一点。
    凸组合是"允许学、但限制在合理范围内"的折中。
    """
    import numpy as np

    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=float)
    k = X.shape[1]
    grid = np.arange(0, 1.0 + 1e-9, step)
    cands = []
    if k == 1:
        cands = [[1.0]]
    elif k == 2:
        for a in grid:
            cands.append([a, 1 - a])
    elif k == 3:
        for a in grid:
            for b in grid:
                if a + b <= 1 + 1e-9:
                    cands.append([a, b, 1 - a - b])
    else:
        for a in grid:
            for b in grid:
                if a + b > 1 + 1e-9:
                    continue
                for c in grid:
                    if a + b + c <= 1 + 1e-9:
                        cands.append([a, b, c, 1 - a - b - c])
    W = np.asarray(cands, dtype=float)
    preds = X @ W.T                      # (n, n_cand)
    mse = ((preds - y[:, None]) ** 2).mean(axis=0)
    return W[int(np.argmin(mse))]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="多源预报后处理")
    ap.add_argument("--backtest", action="store_true", help="把订正预报写进数据库")
    ap.add_argument("--min-products", type=int, default=1)
    args = ap.parse_args(argv)

    db.init()
    rows = load_samples(args.min_products)
    if len(rows) < 30:
        print(f"样本只有 {len(rows)} 条，官方产品历史还不够，先跳过。"
              f"（每天会自动重跑，攒够 30 条就会开始训练）")
        return 0

    days = sorted({r["date"] for r in rows})
    print(f"样本 {len(rows)} 条，覆盖 {len(days)} 天（{days[0]} ~ {days[-1]}）")
    print(f"各产品可用率：" + "  ".join(
        f"{p} {sum(1 for r in rows if r['vals'][p] is not None)}/{len(rows)}"
        for p in PRODUCTS))

    X = [feature(r) for r in rows]
    y = [r["y"] for r in rows]

    # ---- 留一天交叉验证：三种融合方式 + 各单产品 ----
    import numpy as np

    Xs = np.asarray(X, dtype=float)
    ys = np.asarray(y, dtype=float)
    # 凸组合和等权只吃"产品+持续性"这 4 列，不吃缺失标志
    k_main = len(PRODUCTS) + 1
    preds = {k: [None] * len(rows) for k in ("ridge", "convex", "equal")}
    for d in days:
        tr = [i for i, r in enumerate(rows) if r["date"] != d]
        te = [i for i, r in enumerate(rows) if r["date"] == d]
        if len(tr) < 20:
            continue
        Xtr, ytr = Xs[tr], ys[tr]
        wr = ridge_fit(Xtr, ytr, lam=1.0)
        wc = convex_weights(Xtr[:, :k_main], ytr)
        we = np.full(k_main, 1.0 / k_main)
        for i in te:
            preds["ridge"][i] = float(np.dot(wr, Xs[i]))
            preds["convex"][i] = float(np.dot(wc, Xs[i, :k_main]))
            preds["equal"][i] = float(np.dot(we, Xs[i, :k_main]))

    def mae(vals):
        v = [abs(a) for a in vals if a is not None]
        return statistics.mean(v) if v else float("nan")

    errs = {k: [preds[k][i] - y[i] for i in range(len(rows)) if preds[k][i] is not None]
            for k in preds}
    ok = [i for i in range(len(rows)) if preds["convex"][i] is not None]
    for p in PRODUCTS:
        errs[p] = [(rows[i]["vals"][p] - rows[i]["clim"]) - rows[i]["y"]
                   for i in ok if rows[i]["vals"][p] is not None]
    errs["persistence"] = [rows[i]["pers"] - rows[i]["y"]
                           for i in ok if rows[i]["pers"] is not None]

    print("\n留一天交叉验证 MAE（°C，距平口径）：")
    best_key = min(("ridge", "convex", "equal"), key=lambda k: mae(errs[k]))
    base = mae(errs[best_key])
    for k in ("ridge", "convex", "equal") + PRODUCTS + ("persistence",):
        v = mae(errs[k])
        tag = "  ← 当前最好" if k == best_key else ""
        imp = "" if k == best_key or base != base or v != v else f"  （相对最好 {(1 - v/base)*100:+.1f}%）"
        print(f"  {k:12s} {v:.4f}  n={len(errs[k])}{imp}{tag}")

    # ---- 全量拟合，用于出预报 ----
    if best_key == "ridge":
        w = ridge_fit(X, y, lam=1.0)
        decode = lambda feats: float(np.dot(w, feats))
    else:
        kk = k_main
        w = convex_weights(Xs[:, :kk], ys)
        if best_key == "equal":
            w = np.full(kk, 1.0 / kk)
        decode = lambda feats: float(np.dot(w, feats[:kk]))
    names = PRODUCTS + ("persistence",) + tuple(f"{p}_missing" for p in PRODUCTS)
    print("\n全量拟合权重（预报距平 = Σ w·特征 + 常数项已在特征里）：")
    for n, wi in zip(names, w):
        print(f"  {n:16s} {wi:+.3f}")

    meta = {
        "trained_at": db.now_iso(), "samples": len(rows), "days": len(days),
        "date_range": [days[0], days[-1]],
        "weights": {n: float(v) for n, v in zip(names, w)},
        "lodo_mae": {k: (None if mae(errs[k]) != mae(errs[k]) else round(mae(errs[k]), 4))
                     for k in ("ridge", "convex", "equal") + PRODUCTS + ("persistence",)},
        "chosen": best_key,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    print(f"\n已写入 {OUT}")

    if not args.backtest:
        return 0

    # ---- 回补：**走前向**拟合权重 ----
    # 关键点：给历史某一天出预报时，权重只能用那一天**之前**的数据拟合。
    # 如果拿全量数据拟合再回头报历史，那是样本内成绩，会虚高
    # （实测这样算出来 MAE 0.151，比 HYCOM 的 0.172 还好，是假的）。
    with db.session() as conn:
        rows_all = []
        for r in conn.execute(
                "SELECT target_date, horizon, region, product, value FROM products"
                " WHERE product IN ('hycom','gfs','cfs')"):
            rows_all.append(dict(r))
        clim_map, pers_map = climatology_and_persistence()
        table = {}
        for r in rows_all:
            key = (r["target_date"], r["horizon"], r["region"])
            table.setdefault(key, {})[r["product"]] = r["value"]

        n = 0
        all_dates = sorted({d for (d, _h, _r) in table})
        train_dates = sorted({r["date"] for r in rows})     # 有真值、能用来拟合的日期
        for d in all_dates:
            past = [r for r in rows if r["date"] < d]
            if len(past) >= 20:
                if best_key == "ridge":
                    wd = ridge_fit([feature(r) for r in past],
                                   [r["y"] for r in past], lam=1.0)
                    loc = lambda feats: float(np.dot(wd, feats))
                else:
                    wd = convex_weights(
                        np.asarray([feature(r) for r in past])[:, :k_main],
                        np.asarray([r["y"] for r in past]))
                    loc = lambda feats, wd=wd: float(np.dot(wd, feats[:k_main]))
            elif d > max(train_dates):
                loc = decode            # 未来日期：用全部历史拟合的权重
            else:
                continue                # 历史太早，样本不够，宁可不报
            for (dd, h, reg), vals in ((k, v) for k, v in table.items() if k[0] == d):
                clim = clim_map.get((reg, G.doy_index(d)))
                if clim is None:
                    continue
                pers = pers_map.get((d, h, reg), 0.0)
                xs, flags = [], []
                for p in PRODUCTS:
                    v = vals.get(p)
                    if v is None:
                        xs.append(pers); flags.append(1.0)
                    else:
                        xs.append(v - clim); flags.append(0.0)
                xs.append(pers)
                yhat = loc(xs + flags) + clim
                conn.execute(
                "INSERT INTO products(product, region, horizon, run_date, target_date,"
                " value, fetched_at) VALUES(?,?,?,?,?,?,?)"
                " ON CONFLICT(product, region, horizon, target_date) DO UPDATE SET"
                "   value=excluded.value, run_date=excluded.run_date,"
                "   fetched_at=excluded.fetched_at",
                    ("stack", reg, h, as_of_for(d, h),
                     d, round(yhat, 3), db.now_iso()))
                n += 1
        conn.commit()
    print(f"回补订正预报 {n} 条（产品代码 stack）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
