"""训练 U-Net 做**多源**海温场订正，并把结果写进榜单。

输入 10 个通道（都对齐在 OISST 的 82×56 网格上，距平口径）：
    0 持续性场     1 GFS 场    2 GFS 有无
    3 HYCOM 场     4 HYCOM 有无
    5 CFSv2 场     6 CFSv2 有无
    7 季节 sin     8 季节 cos   9 时效/5
输出 1 个通道：订正后的距平场（残差式，即输出 = 持续性场 + 网络修正量）。

各产品历史场存在 data/fields/<product>/ 里，由每日抓数自动积累；
HISTORY 越长，通道越有效。当前 GFS 有 730 天（AWS 公开存档回补），
HYCOM / CFSv2 只有上游保留的约 9–10 天，靠每天攒。

评估：只保留样本外时段，把预测场聚合成 7 个海区平均，再和真值比，
      同时和「持续性」「GFS 原始」两个基线对照。

用法：
    .venv/bin/python -m tools.train_unet --epochs 60 --backtest
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
import torch
import torch.nn as nn

from app import db, fieldstore, grid as G, mlfeatures, regions as R
from app.unet import UNet

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL_PATH = os.path.join(BASE, "data", "models", "unet.pt")
META_PATH = os.path.join(BASE, "data", "models", "unet.meta.json")
SCALE = 1.5          # 距平归一化尺度（°C）
SOURCES = ("gfs", "hycom", "cfs")     # 对应通道 1-6
NCH = 10
# 一个产品的历史场至少要攒到这么多天，才把它的通道打开。
# 否则训练集里这个通道 99% 都是 0，模型学不会用它；
# 推理时突然喂进来真场，反而像噪声——实测把成绩从 0.24 拉到 0.44。
MIN_HISTORY_DAYS = 90


def product_field(product: str, run_date: str, lead: int):
    """从场库读一个产品在某次起报、某个时效上的场（已是统一网格）。"""
    p = fieldstore.field_path(product, run_date, lead)
    if not os.path.exists(p):
        return None
    arr = np.fromfile(p, dtype="<f4")
    if arr.size != fieldstore.NLAT * fieldstore.NLON:
        return None
    f = arr.reshape(fieldstore.NLAT, fieldstore.NLON)
    return f if np.isfinite(f).mean() > 0.2 else None


def build_oisst(climatology_smooth: int = 15):
    """把 OISST 网格读成 numpy，并算好逐格点的气候态与距平。"""
    g = G.Grid()
    raw = np.frombuffer(g._raw, dtype="<f4").reshape(g.ntime, g.nlat, g.nlon).copy()
    ocean = np.isfinite(raw).mean(axis=0) > 0.9
    filled = np.where(np.isfinite(raw), raw, np.nan)

    doys = np.array([G.doy_index(d) for d in g.dates])
    clim = np.zeros((G.NDOY, g.nlat, g.nlon), dtype=np.float32)
    for k in range(G.NDOY):
        sel = filled[doys == k]
        with np.errstate(all="ignore"):
            clim[k] = np.nanmean(sel, axis=0) if len(sel) else np.nan
    # 在 day-of-year 方向做环形平滑，压掉单日样本少带来的噪点
    for _ in range(climatology_smooth // 5):
        clim = np.nanmean(np.stack([np.roll(clim, s, axis=0) for s in (-2, -1, 0, 1, 2)]),
                          axis=0)
    # 极区/陆地补全
    bad = ~np.isfinite(clim)
    if bad.any():
        fallback = np.nanmean(np.where(np.isfinite(clim), clim, np.nan), axis=0)
        clim = np.where(np.isfinite(clim), clim, fallback[None])
    anom = np.where(np.isfinite(raw), raw - clim[doys], np.nan).astype(np.float32)
    return g, ocean, clim.astype(np.float32), anom


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="训练 U-Net 订正模型")
    ap.add_argument("--epochs", type=int, default=60)
    ap.add_argument("--batch", type=int, default=32)
    ap.add_argument("--lr", type=float, default=2e-3)
    ap.add_argument("--embargo-days", type=int, default=30)
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--days-ahead", type=int, default=8)
    args = ap.parse_args(argv)

    db.init()
    t0 = time.time()
    grid, ocean, clim, anom = build_oisst()
    have = {p: set(fieldstore.list_dates(p)) for p in SOURCES}
    active = {p: (len(have[p]) >= MIN_HISTORY_DAYS) for p in SOURCES}
    print(f"OISST {grid.ntime} 天；场库天数 " +
          "  ".join(f"{p}:{len(have[p])}" for p in SOURCES) +
          f"，{time.time()-t0:.1f}s")
    print("启用通道：" + "  ".join(
        f"{p}={'开' if active[p] else '关（历史不足）'}" for p in SOURCES))
    if not any(have.values()):
        print("场库是空的：先跑 tools/ingest_products.py 或 tools/backfill_gfs.py",
              file=sys.stderr)
        return 1

    tindex = {d: i for i, d in enumerate(grid.dates)}
    leads = [1, 3, 5]                      # 与 R.HORIZONS 对应

    # ---- 构造样本 ----
    def sample(target: str, h: int):
        ti = tindex.get(target)
        if ti is None:
            return None
        cut = (date.fromisoformat(target) - timedelta(days=h)).isoformat()
        as_of = grid.latest_before(cut)
        if not as_of or as_of > cut:
            return None
        t0i = tindex[as_of]
        pers = anom[t0i]

        doy = G.doy_index(target)
        run_date = (date.fromisoformat(target) - timedelta(days=h)).isoformat()
        H, W = pers.shape
        # 各产品通道：场 → 距平，只在海格点上用；缺数据就填 0 + 掩膜 0
        prod_ch, prod_masks = [], []
        for p in SOURCES:
            f = product_field(p, run_date, h) if active[p] else None
            if f is None:
                prod_ch.append(np.zeros((H, W), dtype=np.float32))
                prod_masks.append(np.zeros((H, W), dtype=np.float32))
                continue
            a = np.where(np.isfinite(f), f - clim[doy], 0.0)
            mask = (np.isfinite(f) & ocean).astype(np.float32)
            prod_ch.append(np.nan_to_num(a, nan=0.0).astype(np.float32))
            prod_masks.append(mask)
        ang = 2 * np.pi * doy / G.NDOY
        ch = [np.nan_to_num(pers, nan=0.0) / SCALE]
        for a, m in zip(prod_ch, prod_masks):
            ch += [a / SCALE, m]
        ch += [
            np.full((H, W), np.sin(ang), dtype=np.float32),
            np.full((H, W), np.cos(ang), dtype=np.float32),
            np.full((H, W), h / 5.0, dtype=np.float32),
        ]
        x = np.nan_to_num(np.stack(ch).astype(np.float32), nan=0.0,
                          posinf=0.0, neginf=0.0)
        y = np.nan_to_num(anom[ti], nan=0.0).astype(np.float32)[None] / SCALE
        return x, y, pers, anom[ti], prod_ch[0], prod_masks[0]

    samples = []
    all_runs = sorted(set().union(*have.values()))
    for run in all_runs:
        for h in leads:
            target = (date.fromisoformat(run) + timedelta(days=h)).isoformat()
            if target not in tindex:
                continue
            s = sample(target, h)
            if s is not None:
                samples.append((target, h, s))
    # 去重（同一天可能由不同起报日产生同一目标日）
    seen = set()
    uniq = []
    for t, h, s in samples:
        if (t, h) in seen:
            continue
        seen.add((t, h))
        uniq.append((t, h, s))
    samples = uniq
    print(f"可用训练样本 {len(samples)}")
    if len(samples) < 60:
        print("样本太少", file=sys.stderr)
        return 1

    # ---- 按时间切分（后面 15% 做验证），并留隔离期 ----
    samples.sort(key=lambda z: (z[0], z[1]))
    cutoff = (date.today() - timedelta(days=args.embargo_days)).isoformat()
    train = [s for s in samples if s[0] <= cutoff]
    if len(train) < 60:
        print(f"隔离期后只剩 {len(train)} 条，先把 --embargo-days 调小", file=sys.stderr)
        return 1
    split = int(len(train) * 0.85)
    Xtr = np.stack([s[2][0] for s in train[:split]])
    Ytr = np.stack([s[2][1] for s in train[:split]])
    Xva = np.stack([s[2][0] for s in train[split:]])
    Yva = np.stack([s[2][1] for s in train[split:]])
    print(f"训练 {len(Xtr)} / 验证 {len(Xva)}")

    dev = torch.device("mps" if torch.backends.mps.is_available() else "cpu")
    model = UNet(cin=NCH, base=24, depth=3).to(dev)
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=args.epochs)
    mask_t = torch.from_numpy(ocean.astype(np.float32))[None, None].to(dev)

    Xtr_t = torch.from_numpy(Xtr); Ytr_t = torch.from_numpy(Ytr)
    Xva_t = torch.from_numpy(Xva).to(dev); Yva_t = torch.from_numpy(Yva).to(dev)
    n = len(Xtr_t)
    best = (float("inf"), None)
    # 模态随机丢弃：训练时按 30% 概率把 GFS 通道清零。
    # 不做这一步的话，模型只在"有 GFS"的样本上训过，
    # 遇到 GFS 存档还没覆盖到的未来日期（掩膜=0）就会崩——
    # 实测榜单 MAE 0.34，比持续性还差，就是这个问题。
    # 每个产品通道（1-6 中的奇数位）独立按 30% 概率丢弃
    chan_keep = [1]
    for i in range(len(SOURCES)):
        chan_keep += [1 + 2 * i, 2 + 2 * i]
    for ep in range(1, args.epochs + 1):
        model.train()
        perm = torch.randperm(n)
        tot = 0.0
        for i in range(0, n, args.batch):
            idx = perm[i:i + args.batch]
            xb = Xtr_t[idx].to(dev); yb = Ytr_t[idx].to(dev)
            xb = xb.clone()
            for i in range(len(SOURCES)):
                drop = (torch.rand(len(idx), device=dev) < 0.3)
                if drop.any():
                    xb[drop, 1 + 2 * i] = 0.0
                    xb[drop, 2 + 2 * i] = 0.0
            opt.zero_grad()
            out = model(xb)
            loss = (((out - yb) ** 2) * mask_t).sum() / (mask_t.sum() * len(idx))
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 2.0)
            opt.step()
            tot += float(loss) * len(idx)
        sched.step()
        model.eval()
        with torch.no_grad():
            v = 0.0
            for i in range(0, len(Xva_t), 64):
                xb = Xva_t[i:i + 64]; yb = Yva_t[i:i + 64]
                out = model(xb)
                v += float((((out - yb) ** 2) * mask_t).sum())
            v /= (float(mask_t.sum()) * len(Xva_t))
        if v < best[0]:
            best = (v, {k: t.detach().clone() for k, t in model.state_dict().items()})
        if ep % 10 == 0 or ep == 1:
            print(f"  epoch {ep:3d}  训练 MSE {tot/n:.5f}  验证 MSE {v:.5f}  "
                  f"（最好 {best[0]:.5f}）", flush=True)
    model.load_state_dict(best[1])
    print(f"训练完成，最好验证 MSE {best[0]:.5f}")

    # ---- 样本外评估：预测场 → 海区平均，和真值比 ----
    wmap = mlfeatures.region_weights(grid)
    codes = [r.code for r in R.REGIONS]
    idx_of = {c: (np.array([k for k, _ in wmap[c]]),
                  np.array([w for _, w in wmap[c]])) for c in codes}

    def region_means(field):
        out = {}
        for c in codes:
            ks, ws = idx_of[c]
            vals = field.ravel()[ks]
            ok = np.isfinite(vals)
            out[c] = float((vals[ok] * ws[ok]).sum() / ws[ok].sum()) if ok.any() else None
        return out

    test = [s for s in samples if s[0] > cutoff]
    if not test:
        test = samples[split:]
    err_u, err_p, err_g = [], [], []
    for target, h, (x, y, pers, truth, gfield, gmask) in test:
        with torch.no_grad():
            out = model(torch.from_numpy(x)[None].to(dev))[0, 0].cpu().numpy() * SCALE
        pred_field = np.where(ocean, out, np.nan)
        pm = region_means(pred_field)
        pm_p = region_means(pers)
        pm_g = region_means(gfield) if gmask.mean() > 0.5 else {}
        tm = region_means(truth)
        for c in codes:
            if tm.get(c) is None or pm.get(c) is None:
                continue
            err_u.append(abs(pm[c] - tm[c]))
            if pm_p.get(c) is not None:
                err_p.append(abs(pm_p[c] - tm[c]))
            if pm_g.get(c) is not None:
                err_g.append(abs(pm_g[c] - tm[c]))

    res = {
        "unet": statistics.mean(err_u) if err_u else None,
        "persistence": statistics.mean(err_p) if err_p else None,
        "gfs": statistics.mean(err_g) if err_g else None,
        "n": len(err_u),
    }
    print(f"\n样本外（{test[0][0]} ~ {test[-1][0]}，{res['n']} 条）：")
    for k, v in res.items():
        if k != "n":
            print(f"  {k:12s} MAE {v:.4f}" if v is not None else f"  {k:12s} —")

    torch.save({"state": model.state_dict(), "cin": NCH, "base": 24, "depth": 3,
                "sources": list(SOURCES),
                "scale": SCALE}, MODEL_PATH)
    meta = {"trained_at": db.now_iso(), "samples": len(samples),
            "train": len(Xtr), "val": len(Xva), "best_val_mse": best[0],
            "test_window": [test[0][0], test[-1][0]], "out_of_sample": res,
            "device": str(dev), "scale": SCALE}
    with open(META_PATH, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    print(f"模型已存 {MODEL_PATH}")

    if not args.backtest:
        return 0

    # ---- 回补：对开放轮次（以及最近的历史）出订正预报 ----
    today = date.today()
    targets = [(today + timedelta(days=k)).isoformat()
               for k in range(-30, args.days_ahead + 1)]
    n = 0
    with db.session() as conn:
        for ds in targets:
            for h in leads:
                s = sample(ds, h)
                if s is None:
                    continue
                x = s[0]
                with torch.no_grad():
                    out = model(torch.from_numpy(x)[None].to(dev))[0, 0].cpu().numpy() * SCALE
                pred_field = np.where(ocean, out, np.nan)
                doy = G.doy_index(ds)
                full = pred_field + clim[doy]           # 距平 + 气候态 = 绝对温度
                pm = region_means(full)
                as_of = grid.latest_before(
                    (date.fromisoformat(ds) - timedelta(days=h)).isoformat())
                for c, v in pm.items():
                    if v is None:
                        continue
                    conn.execute(
                        "INSERT INTO products(product, region, horizon, run_date,"
                        " target_date, value, fetched_at) VALUES(?,?,?,?,?,?,?)"
                        " ON CONFLICT(product, region, horizon, target_date) DO UPDATE SET"
                        "   value=excluded.value, run_date=excluded.run_date,"
                        "   fetched_at=excluded.fetched_at",
                        ("unet", c, h, as_of, ds, round(v, 3), db.now_iso()))
                    n += 1
        conn.commit()
    print(f"回补 U-Net 预报 {n} 条（产品代码 unet）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
