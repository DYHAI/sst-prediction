"""训练梯度提升模型（XGBoost / CatBoost / LightGBM）预报南海海温，并回补到榜单。

设计要点：
  · 特征只用截止时刻能拿到的观测（见 app/mlfeatures.py），没有未来信息。
  · 默认留 30 天隔离期：训练数据截止到 N 天前，榜单上的成绩全部是样本外的。
  · 特征里**不含**官方产品值时，它是一个独立预报模型（和 LIM、持续性可比）；
    加上官方产品值（--postproc）就变成后处理模型（类似论文里的最优组合）。
  · 训练离线跑，只把结果写进数据库；Web 服务本身仍然零依赖。

用法：
    python3 -m tools.train_ml --model xgboost --backtest
    python3 -m tools.train_ml --model catboost --postproc --backtest
 .venv/bin/python -m tools.train_ml ...   # 用装了 ml 库的解释器
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from datetime import date, datetime, timedelta, timezone

from app import db, grid as G, mlfeatures, regions as R

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL_DIR = os.path.join(BASE, "data", "models")

TRAINABLE = {"xgboost": "XGBRegressor", "catboost": "CatBoostRegressor",
             "lightgbm": "LGBMRegressor"}


def make_model(kind: str, n_features: int):
    if kind == "xgboost":
        from xgboost import XGBRegressor
        return XGBRegressor(
            n_estimators=700, max_depth=4, learning_rate=0.035,
            subsample=0.85, colsample_bytree=0.85, reg_lambda=1.5,
            min_child_weight=4, objective="reg:squarederror", n_jobs=8,
            random_state=0,
        )
    if kind == "catboost":
        from catboost import CatBoostRegressor
        return CatBoostRegressor(
            iterations=900, depth=5, learning_rate=0.04, l2_leaf_reg=4,
            loss_function="RMSE", verbose=False, random_seed=0,
            allow_writing_files=False,
        )
    if kind == "lightgbm":
        from lightgbm import LGBMRegressor
        return LGBMRegressor(
            n_estimators=700, num_leaves=15, learning_rate=0.035,
            subsample=0.85, colsample_bytree=0.85, reg_lambda=1.5,
            min_child_samples=12, random_state=0, n_jobs=8, verbose=-1,
        )
    raise SystemExit(f"不支持的模型类型：{kind}")


def load_product_lookup():
    """后处理模式用：查官方产品在 (target, horizon, region) 上的预报值。"""
    table: dict[tuple, dict[str, float]] = {}
    with db.session() as conn:
        for r in conn.execute(
                "SELECT product, region, horizon, target_date, value FROM products"
                " WHERE product IN ('hycom','gfs','cfs')"):
            table.setdefault(
                (r["target_date"], r["horizon"], r["region"]), {})[r["product"]] = r["value"]
    order = ("hycom", "gfs", "cfs")

    def lookup(target, horizon, region):
        row = table.get((target, horizon, region))
        if not row:
            return [None, None, None]
        return [row.get(p) for p in order]

    return lookup, list(order)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="训练梯度提升 SST 模型")
    ap.add_argument("--model", default="xgboost", choices=sorted(TRAINABLE))
    ap.add_argument("--embargo-days", type=int, default=30)
    ap.add_argument("--postproc", action="store_true",
                    help="特征里加入官方产品预报值（后处理模式）")
    ap.add_argument("--backtest", action="store_true", help="回补预报到数据库")
    ap.add_argument("--days-back", type=int, default=30)
    ap.add_argument("--days-ahead", type=int, default=8)
    args = ap.parse_args(argv)

    db.init()
    os.makedirs(MODEL_DIR, exist_ok=True)
    today = datetime.now(timezone.utc).date()
    cutoff = (today - timedelta(days=args.embargo_days)).isoformat()

    t0 = time.time()
    grid = G.Grid()
    fb = mlfeatures.FeatureBuilder(grid)
    extra_lookup, extra_names = (load_product_lookup() if args.postproc else (None, []))
    print(f"网格 {grid.ntime} 天（{grid.dates[0]} ~ {grid.dates[-1]}），"
          f"{time.time() - t0:.1f}s")

    train_d0 = grid.dates[0]
    X, y_abs, base, meta = fb.build(train_d0, cutoff, R.HORIZONS, extra_lookup)
    # 让树只学"相对持续性基线的修正量"。直接从零学距平会严重过拟合：
    # 实测训练 MAE 0.137、验证 0.301，而持续性基线是 0.179。
    y = [a - b for a, b in zip(y_abs, base)]
    print(f"训练样本 {len(X)}，特征 {len(X[0])} 维，训练期截止 {cutoff}")
    if len(X) < 500:
        print("样本太少，先跑 tools/backfill_grid.py 回补网格", file=sys.stderr)
        return 1

    # 内部再切出一段做早停/验证（仍然早于 cutoff，不算作弊）
    split = int(len(X) * 0.85)
    model = make_model(args.model, len(X[0]))
    t1 = time.time()
    fit_kw = {}
    if args.model == "xgboost":
        fit_kw["eval_set"] = [(X[split:], y[split:])]
        model.set_params(early_stopping_rounds=40)
        fit_kw["verbose"] = False
    model.fit(X[:split], y[:split], **fit_kw)
    train_sec = time.time() - t1
    pred_val = model.predict(X[split:])
    # 评价的是"基线 + 修正"之后的绝对误差，和榜单口径一致
    val_mae = float(statistics.mean(
        abs(float(a) + b0 - b1) for a, b0, b1 in zip(pred_val, base[split:], y_abs[split:])))
    base_mae = float(statistics.mean(
        abs(b0 - b1) for b0, b1 in zip(base[split:], y_abs[split:])))
    print(f"训练完成 {train_sec:.1f}s｜内部验证 MAE：模型 {val_mae:.4f} vs 持续性基线 {base_mae:.4f}")

    name = args.model + ("_post" if args.postproc else "")
    path = os.path.join(MODEL_DIR, f"{name}.json")
    if args.model == "catboost":
        path = os.path.join(MODEL_DIR, f"{name}.cbm")
        model.save_model(path)
    elif args.model == "xgboost":
        model.save_model(path)
    else:
        model.booster_.save_model(path)
    meta_info = {
        "model": args.model, "postproc": args.postproc, "path": path,
        "features": len(X[0]), "samples": len(X), "cutoff": cutoff,
        "val_mae_model": round(val_mae, 4), "val_mae_persistence": round(base_mae, 4),
        "trained_sec": round(train_sec, 1),
        "extra_features": extra_names, "trained_at": db.now_iso(),
    }
    with open(os.path.join(MODEL_DIR, f"{name}.meta.json"), "w") as f:
        json.dump(meta_info, f, ensure_ascii=False, indent=1)
    print(f"模型已存：{path}")

    if not args.backtest:
        return 0

    # ---- 全量重训（用上到 cutoff 为止的全部数据）后回补 ----
    model = make_model(args.model, len(X[0]))
    fit_kw.pop("eval_set", None)
    try:
        model.fit(X, y, **fit_kw)
    except TypeError:
        model.fit(X, y)

    targets = [today + timedelta(days=k)
               for k in range(-args.days_back, args.days_ahead + 1)]
    code = args.model + ("_post" if args.postproc else "")
    n = 0
    rows_by_target: dict[str, dict[str, float]] = {}
    for target in targets:
        ds = target.isoformat()
        if ds <= cutoff:
            continue           # 保证榜单上都是样本外
        for h in R.HORIZONS:
            if fb.as_of(ds, h) is None:
                continue
            feats, regions_ok, bases_ok = [], [], []
            for r in fb.codes:
                extra = extra_lookup(ds, h, r) if extra_lookup else None
                # 用 features_only：预报未来日期时真值还不存在，
                # 走 vector() 会因为拿不到 y 而直接跳过，导致模型永远没有未来预报。
                x, base_anom, _asof = fb.features_only(ds, h, r, extra)
                if x is None:
                    continue
                feats.append(x)
                regions_ok.append(r)
                bases_ok.append(base_anom)
            if not feats:
                continue
            preds = model.predict(feats)
            for r, pa, b0 in zip(regions_ok, preds, bases_ok):
                doy = G.doy_index(ds)
                value = b0 + float(pa) + fb.clim[r][doy]
                rows_by_target.setdefault(ds, {})[(h, r)] = round(value, 3)

    with db.session() as conn:
        for ds, items in rows_by_target.items():
            for (h, r), v in items.items():
                conn.execute(
                    "INSERT INTO products(product, region, horizon, run_date,"
                    " target_date, value, fetched_at) VALUES(?,?,?,?,?,?,?)"
                    " ON CONFLICT(product, region, horizon, target_date) DO UPDATE SET"
                    "   value=excluded.value, run_date=excluded.run_date,"
                    "   fetched_at=excluded.fetched_at",
                    (code, r, h, fb.as_of(ds, h), ds, v, db.now_iso()),
                )
                n += 1
        conn.commit()
    print(f"回补 {n} 条（产品代码 {code}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
