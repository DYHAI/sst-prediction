"""2023–2026 长时段回测：1–10 天时效，各模型同场对比。

设计（刻意做得简单、能复现）：
  · 训练期 2023-01-01 ~ 2024-12-31，测试期 2025-01-01 ~ 2026-09-30
  · 所有模型只用训练期数据拟合，在测试期上评估 —— 同一把尺子
  · 每个模型对每个 (目标日, 时效, 海区) 出一次预报，用 OISST 海区均值验证

参与对比的：
  climatology  气候态（1982–2011 的日均值）      —— 下限参照
  persistence  持续性（把 as_of 观测搬过去）      —— 海温最难打败的简单基准
  damped       阻尼持续性（α 按训练期拟合）
  gfs          NCEP GFS 的 TMP:surface 场         —— 唯一能长时段回补的官方产品
  lim          线性逆模型（VAR，逐时效拟合）
  xgb          XGBoost（距平轨迹特征）
  unet         U-Net（多源场订正，残差式）

为什么只有 GFS：HYCOM 的上游 FMRC 只保留约 9 次起报、年归档是原始 restart 二进制；
CFSv2 的 NOMADS 只留约 10 天，NCEI 的 operational 归档路径已 404。
这两个产品只能等每日抓数慢慢攒（网站上也标了"还差多少天"）。

用法：
    .venv/bin/python -m tools.backtest_long --models clim persistence damped gfs lim xgb
    .venv/bin/python -m tools.backtest_long --models unet      # U-Net 单独跑（慢）
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from datetime import date, timedelta

import numpy as np

from app import fieldstore, grid as G, mhw, mlfeatures
from app import regions as R

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "data", "backtest_long.json")

TRAIN = ("2023-01-01", "2024-12-31")
TEST = ("2025-01-01", "2026-09-30")
LEADS = tuple(range(1, 11))
SOURCES = ("gfs",)          # 目前只有 GFS 有长历史场


# ---------------------------------------------------------------- 数据

def load_region_truth():
    """从本地网格取 7 个海区的逐日真值（和擂台结算同一口径）。"""
    g = G.Grid()
    wmap = mlfeatures.region_weights(g)
    series = mlfeatures.region_series(g, wmap)
    codes = [r.code for r in R.REGIONS]
    out = {}
    for t, d in enumerate(g.dates):
        row = {c: series[c][t] for c in codes}
        if all(v == v for v in row.values()):
            out[d] = row
    return g, out


def load_gfs_region_means(run_dates):
    """把场库里的 GFS 场聚合成海区均值：{(run_date, lead): {region: value}}。"""
    g = G.Grid()
    wmap = mlfeatures.region_weights(g)
    lat = np.array([g.lat(i) for i in range(g.nlat)])
    out = {}
    for i, d in enumerate(run_dates):
        for lead in LEADS:
            p = fieldstore.field_path("gfs", d, lead)
            if not os.path.exists(p):
                continue
            a = np.fromfile(p, dtype="<f4")
            if a.size != fieldstore.NLAT * fieldstore.NLON:
                continue
            f = a.reshape(fieldstore.NLAT, fieldstore.NLON)
            row = {}
            for reg in R.REGIONS:
                ks, ws = zip(*wmap[reg.code])
                vals = f.ravel()[list(ks)]
                good = np.isfinite(vals)
                if good.sum() < 8:
                    continue
                row[reg.code] = float((vals[good] * np.array(ws)[good]).sum()
                                      / np.array(ws)[good].sum())
            if len(row) == len(R.REGIONS):
                out[(d, lead)] = row
    return out


# ---------------------------------------------------------------- 模型

def as_of_for(truth, target, lead):
    cut = (date.fromisoformat(target) - timedelta(days=lead)).isoformat()
    cands = [d for d in truth if d <= cut]
    return cands[-1] if cands else None


def anomaly(truth, clim, day, code):
    v = truth[day][code]
    m = clim[code]["mean"][G.doy_index(day)]
    return v - m


def fit_damped_alpha(truth, clim, lead):
    """阻尼持续性：预测 = 气候态 + α·(当前距平)。α 只用训练期拟合。"""
    best = (1.0, float("inf"))
    for i in range(0, 121):
        a = i / 100
        errs = []
        for d in sorted(truth):
            if not (TRAIN[0] <= d <= TRAIN[1]):
                continue
            t0 = (date.fromisoformat(d) - timedelta(days=lead)).isoformat()
            if t0 not in truth:
                continue
            for c in truth[d]:
                pred = clim[c]["mean"][G.doy_index(d)] + a * anomaly(truth, clim, t0, c)
                errs.append(abs(pred - truth[d][c]))
        if not errs:
            break
        m = statistics.mean(errs)
        if m < best[1]:
            best = (a, m)
    return best[0]


def fit_var(truth, clim, lead, codes, lam: float = 1e-2):
    """线性逆模型：7 维距平状态上拟合 x(t+lead) = A·x(t) + b（训练期）。"""
    x0, x1 = [], []
    for d in sorted(truth):
        if not (TRAIN[0] <= d <= TRAIN[1]):
            continue
        t0 = (date.fromisoformat(d) - timedelta(days=lead)).isoformat()
        if t0 not in truth:
            continue
        if any(truth[d].get(c) is None or truth[t0].get(c) is None for c in codes):
            continue
        x0.append([anomaly(truth, clim, t0, c) for c in codes])
        x1.append([anomaly(truth, clim, d, c) for c in codes])
    if len(x0) < 60:
        return None
    return G.ridge_fit(x0, x1, lam=lam)


def build_xgb_dataset(truth, clim, fb, lead, codes):
    """XGB 训练集：特征 → **相对持续性基线的修正量**。

    直接学距平会严重过拟合（线上实测训练 0.137 / 验证 0.301，而持续性只有 0.18）。
    这里和线上训练脚本保持同一个设计，否则就是拿稻草人在比。
    """
    X, y = [], []
    for d in sorted(truth):
        if not (TRAIN[0] <= d <= TRAIN[1]):
            continue
        for c in codes:
            feats, base, _aof = fb.features_only(d, lead, c)
            if feats is None:
                continue
            X.append(feats)
            y.append(truth[d][c] - clim[c]["mean"][G.doy_index(d)] - base)
    return X, y


# ---------------------------------------------------------------- U-Net

def run_unet(truth, grid, test_days, codes):
    """训练 U-Net（多源场订正）并在测试期出预报，返回 {(target,lead,region): 值}。

    训练用 TRAIN 期，测试用 TEST 期，中间留 LEAD 天隔离，保证是样本外。
    为控制显存/内存，训练样本隔天抽样。
    """
    import torch
    import torch.nn as nn

    from app.unet import UNet
    from tools.train_unet import build_oisst

    dev = torch.device("mps" if torch.backends.mps.is_available() else "cpu")
    g, ocean, clim_grid, anom = build_oisst()
    tindex = {d: i for i, d in enumerate(g.dates)}
    wmap = mlfeatures.region_weights(g)
    reg_idx = {c: (np.array([k for k, _ in wmap[c]]),
                   np.array([w for _, w in wmap[c]])) for c in codes}

    def region_means(field):
        out = {}
        for c in codes:
            ks, ws = reg_idx[c]
            vals = field.ravel()[ks]
            ok = np.isfinite(vals)
            if ok.sum() >= 8:
                out[c] = float((vals[ok] * ws[ok]).sum() / ws[ok].sum())
        return out

    def sample(target, lead):
        ti = tindex.get(target)
        if ti is None:
            return None
        cut = (date.fromisoformat(target) - timedelta(days=lead)).isoformat()
        aof = as_of_for(truth, target, lead)
        if aof is None:
            return None
        t0 = tindex[aof]
        doy = G.doy_index(target)
        pers = anom[t0]
        run = (date.fromisoformat(target) - timedelta(days=lead)).isoformat()
        p = fieldstore.field_path("gfs", run, lead)
        gf = np.zeros_like(pers)
        gm = np.zeros_like(pers)
        if os.path.exists(p):
            a = np.fromfile(p, dtype="<f4")
            if a.size == fieldstore.NLAT * fieldstore.NLON:
                f2 = a.reshape(fieldstore.NLAT, fieldstore.NLON)
                ok = np.isfinite(f2) & ocean
                gf = np.where(ok, f2 - clim_grid[doy], 0.0)
                gm = ok.astype(np.float32)
        ang = 2 * np.pi * doy / G.NDOY
        H, W = pers.shape
        ch = [np.nan_to_num(pers, nan=0.0) / 1.5, np.nan_to_num(gf, nan=0.0) / 1.5, gm,
              np.full((H, W), np.sin(ang), dtype=np.float32),
              np.full((H, W), np.cos(ang), dtype=np.float32),
              np.full((H, W), lead / 10.0, dtype=np.float32)]
        x = np.nan_to_num(np.stack(ch).astype(np.float32), nan=0.0)
        y = np.nan_to_num(anom[ti], nan=0.0).astype(np.float32)[None] / 1.5
        return x, y

    tr_days = [d for d in sorted(truth) if TRAIN[0] <= d <= TRAIN[1]]
    tr_days = tr_days[::2]                      # 隔天抽样，够用且省内存
    print(f"  U-Net 训练样本：{len(tr_days)} 天 × {len(LEADS)} 时效", flush=True)

    model = UNet(cin=6, base=24, depth=3).to(dev)
    opt = torch.optim.AdamW(model.parameters(), lr=2e-3, weight_decay=1e-4)
    mask_t = torch.from_numpy(ocean.astype(np.float32))[None, None].to(dev)
    bs = 24

    def batch_iter(days, shuffle):
        idx = [(d, l) for d in days for l in LEADS]
        if shuffle:
            np.random.shuffle(idx)
        xs, ys = [], []
        for d, l in idx:
            s = sample(d, l)
            if s is None:
                continue
            xs.append(s[0]); ys.append(s[1])
            if len(xs) == bs:
                yield np.stack(xs), np.stack(ys)
                xs, ys = [], []
        if xs:
            yield np.stack(xs), np.stack(ys)

    EPOCHS = 12
    for ep in range(1, EPOCHS + 1):
        model.train(); tot = cnt = 0
        for xb, yb in batch_iter(tr_days, True):
            xb = torch.from_numpy(xb).to(dev); yb = torch.from_numpy(yb).to(dev)
            # 产品通道随机丢弃 30%：否则遇到没有 GFS 场的日期会崩
            xb = xb.clone()
            drop = torch.rand(len(xb), device=dev) < 0.3
            if drop.any():
                xb[drop, 1] = 0.0
                xb[drop, 2] = 0.0
            opt.zero_grad()
            out = model(xb)
            loss = (((out - yb) ** 2) * mask_t).sum() / (mask_t.sum() * len(xb))
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 2.0)
            opt.step()
            tot += float(loss) * len(xb); cnt += len(xb)
        print(f"    epoch {ep:2d}/{EPOCHS}  MSE {tot/max(cnt,1):.5f}", flush=True)

    model.eval()
    preds = {}
    for target in test_days:
        for lead in LEADS:
            s = sample(target, lead)
            if s is None:
                continue
            with torch.no_grad():
                out = model(torch.from_numpy(s[0])[None].to(dev))[0, 0].cpu().numpy() * 1.5
            field = np.where(ocean, out, np.nan) + clim_grid[G.doy_index(target)]
            for c, v in region_means(field).items():
                preds[(target, lead, c)] = float(v)
    return preds


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="长时段回测")
    ap.add_argument("--models", nargs="+",
                    default=["clim", "persistence", "damped", "gfs"])
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args(argv)

    t0 = time.time()
    grid, truth = load_region_truth()
    codes = [r.code for r in R.REGIONS]
    print(f"真值 {len(truth)} 天（{min(truth)} ~ {max(truth)}），{time.time()-t0:.1f}s")

    test_days = [d for d in sorted(truth) if TEST[0] <= d <= TEST[1]]
    print(f"测试期 {test_days[0]} ~ {test_days[-1]}，共 {len(test_days)} 天")

    run_dates = sorted({(date.fromisoformat(d) - timedelta(days=l)).isoformat()
                        for d in test_days for l in LEADS})
    t1 = time.time()
    gfs_means = load_gfs_region_means(run_dates)
    print(f"GFS 场聚合完成：{len(gfs_means)} 条 (起报,时效)，{time.time()-t1:.1f}s")

    clim = mhw.load_climatology()["regions"]
    results: dict[str, dict] = {}

    def store(model, target, lead, region, pred):
        results.setdefault(model, {})[(target, lead, region)] = pred

    # ---- 逐个模型建好、预测 ----
    alphas, vars_, xgbs = {}, {}, {}
    if "damped" in args.models:
        for lead in LEADS:
            alphas[lead] = fit_damped_alpha(truth, clim, lead)
        print("  阻尼系数 α：", {l: round(a, 2) for l, a in alphas.items()}, flush=True)
    if "lim" in args.models:
        for lead in LEADS:
            v = fit_var(truth, clim, lead, codes)
            if v:
                vars_[lead] = v
        print(f"  LIM 拟合完成（{len(vars_)} 个时效）", flush=True)
    fb = None
    if "xgb" in args.models:
        from xgboost import XGBRegressor

        fb = mlfeatures.FeatureBuilder(grid)
        for lead in LEADS:
            X, y = build_xgb_dataset(truth, clim, fb, lead, codes)
            if len(X) < 200:
                continue
            m = XGBRegressor(n_estimators=400, max_depth=4, learning_rate=0.05,
                             subsample=0.85, colsample_bytree=0.85, reg_lambda=1.5,
                             n_jobs=8, random_state=0)
            m.fit(X, y)
            xgbs[lead] = m
        print(f"  XGB 训练完成（{len(xgbs)} 个时效）", flush=True)

    for model in args.models:
        t2 = time.time()
        n = 0
        for target in test_days:
            doy = G.doy_index(target)
            for lead in LEADS:
                aof = as_of_for(truth, target, lead)
                if aof is None:
                    continue
                run = (date.fromisoformat(target) - timedelta(days=lead)).isoformat()
                x_vec = None
                if model == "lim" and lead in vars_:
                    x_vec = [anomaly(truth, clim, aof, c) for c in codes]
                if model == "xgb" and lead in xgbs and fb is not None:
                    got = [fb.features_only(target, lead, c) for c in codes]
                    feats = [g[0] for g in got]
                    bases = [g[1] for g in got]
                for c in codes:
                    if model == "clim":
                        p = clim[c]["mean"][doy]
                    elif model == "persistence":
                        p = truth[aof][c]
                    elif model == "damped":
                        p = (clim[c]["mean"][doy]
                             + alphas.get(lead, 1.0) * anomaly(truth, clim, aof, c))
                    elif model == "gfs":
                        row = gfs_means.get((run, lead))
                        if not row:
                            continue
                        p = row[c]
                    elif model == "lim":
                        if x_vec is None:
                            continue
                        coef = vars_[lead]
                        d = codes.index(c)
                        p = clim[c]["mean"][doy] + sum(
                            coef[d][k] * v for k, v in enumerate(x_vec)) + coef[d][-1]
                    elif model == "xgb":
                        if lead not in xgbs:
                            continue
                        i = codes.index(c)
                        f = feats[i]
                        if f is None:
                            continue
                        p = (bases[i] + float(xgbs[lead].predict([f])[0])
                             + clim[c]["mean"][doy])
                    else:
                        continue
                    if p != p:
                        continue
                    store(model, target, lead, c, float(p))
                    n += 1
        print(f"  {model:12s} 产出 {n} 条，{time.time()-t2:.1f}s", flush=True)

    # ---- U-Net（单独训练，输入是多源场）----
    if "unet" in args.models:
        t3 = time.time()
        unet_preds = run_unet(truth, grid, test_days, codes)
        results["unet"] = unet_preds
        print(f"  {'unet':12s} 产出 {len(unet_preds)} 条，{time.time()-t3:.0f}s", flush=True)

    # ---- 评估 ----
    saved = mhw.load_saved() or {}
    hot = mhw.mhw_day_set(saved)
    out = {"design": {"train": TRAIN, "test": TEST, "leads": list(LEADS),
                      "sources": list(SOURCES), "n_test_days": len(test_days)},
           "by_lead": {}, "by_year": {}}
    for model, recs in results.items():
        rows = []
        for lead in LEADS:
            errs, errs_hot, errs_norm = [], [], []
            hit = miss = fa = cr = 0
            for (target, l, c), p in recs.items():
                if l != lead or target not in truth:
                    continue
                y = truth[target][c]
                e = p - y
                errs.append(e)
                is_hot = (c, target) in hot
                (errs_hot if is_hot else errs_norm).append(e)
                t = clim[c]["p90"][G.doy_index(target)]
                ph = p > t
                if ph and is_hot:
                    hit += 1
                elif ph and not is_hot:
                    fa += 1
                elif not ph and is_hot:
                    miss += 1
                else:
                    cr += 1
            if not errs:
                continue
            n = len(errs)
            h_rand = (hit + miss) * (hit + fa) / n if n else 0
            den = hit + miss + fa - h_rand
            rows.append({
                "lead": lead, "n": n,
                "mae": round(statistics.mean(abs(e) for e in errs), 4),
                "rmse": round((statistics.mean(e * e for e in errs)) ** 0.5, 4),
                "bias": round(statistics.mean(errs), 4),
                "mae_hot": (round(statistics.mean(abs(e) for e in errs_hot), 4)
                            if errs_hot else None),
                "mae_norm": (round(statistics.mean(abs(e) for e in errs_norm), 4)
                             if errs_norm else None),
                "n_hot": len(errs_hot),
                "pod": round(hit / (hit + miss), 3) if hit + miss else None,
                "far": round(fa / (hit + fa), 3) if hit + fa else None,
                "ets": round((hit - h_rand) / den, 3) if den > 0 else None,
            })
        out["by_lead"][model] = rows
        print(f"  {model:12s} 评估完成（{len(rows)} 个时效）", flush=True)

    # 分年
    for model, recs in results.items():
        years = {}
        for (target, l, c), p in recs.items():
            y = truth[target][c]
            years.setdefault(target[:4], []).append(abs(p - y))
        out["by_year"][model] = [
            {"year": int(k), "mae": round(statistics.mean(v), 4), "n": len(v)}
            for k, v in sorted(years.items())]

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"已写入 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
