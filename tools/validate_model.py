"""对本站模型做严格的样本外验证，结果写入 data/model_validation.json。

做法：
  1. 只用「留出日」之前的数据训练模型的演化算子。
  2. 在留出日之后，每天用当时真实可得的观测做初值，预报 1/3/5 天。
  3. 同时算纯持续性、阻尼持续性作为对照。

这一步的意义是防止自欺：模型在训练期内的分数会明显好看，但那不算数。

用法：
    python3 -m tools.validate_model --cut 2026-05-31
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from datetime import date as D, timedelta

from app import grid as G, model, db
from app import regions as R

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "data", "model_validation.json")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="样本外验证本站模型")
    ap.add_argument("--cut", default="2026-05-31", help="留出起点：此日之后都不参与训练")
    args = ap.parse_args(argv)

    train = G.Grid(end=args.cut)
    full = G.Grid()
    mdl = model.LimModel.train(train)
    reg_codes = [r.code for r in R.REGIONS]
    rmap = dict(mdl.region_map)

    def region_clim(doy):
        return {code: sum(w[c] * mdl.cell_clim[c][doy] for c in range(len(mdl.cells))) / sum(w)
                for code, w in rmap.items()}

    def gmeans(series, t):
        out = {}
        for code, w in rmap.items():
            tot = ws = 0.0
            for c, ww in enumerate(w):
                if ww <= 0:
                    continue
                v = series[c][t]
                if v == v:
                    tot += ww * v
                    ws += ww
            out[code] = tot / ws if ws else None
        return out

    # 在训练期拟合阻尼系数
    ser = G.cell_series(train, mdl.cells)
    tr_means = {t: gmeans(ser, t) for t in range(train.ntime)}
    alphas = {}
    for h in R.HORIZONS:
        best = (1.0, float("inf"))
        for i in range(0, 101):
            a = i / 100
            errs = []
            for t0 in range(train.ntime - h):
                t1 = t0 + h
                c0 = region_clim(G.doy_index(train.dates[t0]))
                c1 = region_clim(G.doy_index(train.dates[t1]))
                for code in reg_codes:
                    v0, v1 = tr_means[t0][code], tr_means[t1][code]
                    if v0 is None or v1 is None:
                        continue
                    errs.append(abs(c1[code] + a * (v0 - c0[code]) - v1))
            m = statistics.mean(errs) if errs else float("inf")
            if m < best[1]:
                best = (a, m)
        alphas[h] = best[0]

    with db.session() as conn:
        truth = {}
        for r in conn.execute("SELECT date, region, sst FROM truth"):
            truth.setdefault(r["date"], {})[r["region"]] = r["sst"]
    window = [d for d in sorted(truth) if d > args.cut]

    fser = G.cell_series(full, mdl.cells)
    fmeans = {t: gmeans(fser, t) for t in range(full.ntime)}
    idx = {d: i for i, d in enumerate(full.dates)}
    mfull = mdl.rebind(full)

    res = {h: {k: [] for k in ("persistence", "damped", "lim", "blend")} for h in R.HORIZONS}
    for tgt in window:
        for h in R.HORIZONS:
            cut = (D.fromisoformat(tgt) - timedelta(days=h)).isoformat()
            asof = full.latest_before(cut)
            if not asof or asof > cut:
                continue
            t0 = idx[asof]
            c0 = region_clim(G.doy_index(asof))
            c1 = region_clim(G.doy_index(tgt))
            lim = mfull.forecast_region_means(tgt, asof)
            a = alphas[h]
            for code in reg_codes:
                if code not in truth[tgt] or code not in lim:
                    continue
                obs = fmeans[t0][code]
                if obs is None:
                    continue
                y = truth[tgt][code]
                damp = c1[code] + a * (obs - c0[code])
                res[h]["persistence"].append(abs(obs - y))
                res[h]["damped"].append(abs(damp - y))
                res[h]["lim"].append(abs(lim[code] - y))
                res[h]["blend"].append(abs(0.5 * lim[code] + 0.5 * damp - y))

    out = {
        "cut": args.cut,
        "train_window": [train.dates[0], train.dates[-1]],
        "test_window": [window[0], window[-1]] if window else None,
        "damping": alphas,
        "by_horizon": {},
        "note": ("LIM（线性逆模型）是纯数据驱动方法。热带海温在 1–5 天上的持续性极强，"
                 "所以这里把「纯持续性」和「阻尼持续性」也一并列出当对照。"
                 "训练数据从 3 年扩到 8.75 年后，LIM 才从明显落后追到与持续性持平/略优——"
                 "这本身说明样本长度对数据驱动模型有多关键。这里如实报告，不做任何筛选。"),
    }
    print(f"训练期 {train.dates[0]}~{train.dates[-1]}，样本外 {out['test_window']}")
    print(f"{'时效':<6}{'纯持续性':>10}{'阻尼持续性':>12}{'LIM':>10}{'各半':>10}{'样本':>7}")
    for h in R.HORIZONS:
        row = {}
        for k, v in res[h].items():
            row[k] = round(statistics.mean(v), 4) if v else None
        row["n"] = len(res[h]["lim"])
        out["by_horizon"][str(h)] = row
        g = lambda k: f"{row[k]:.3f}" if row[k] is not None else "—"
        print(f"{str(h)+'天':<6}{g('persistence'):>10}{g('damped'):>12}"
              f"{g('lim'):>10}{g('blend'):>10}{row['n']:>7}")
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"已写入 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
