"""训练本站模型（LIM/VAR）并把它的预报写进数据库，让它像其它条目一样上榜单。

用法：
    python3 -m tools.train_model --train                    # 只训练，存模型
    python3 -m tools.train_model --backtest --days 12       # 训练 + 回补历史预报
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime, timedelta, timezone

from app import db, model, products
from app import grid as G
from app import regions as R


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="训练本站 LIM 模型")
    ap.add_argument("--train", action="store_true", help="重新训练")
    ap.add_argument("--backtest", action="store_true", help="回补历史预报到数据库")
    ap.add_argument("--days-back", type=int, default=12)
    ap.add_argument("--days-ahead", type=int, default=8)
    ap.add_argument("--embargo-days", type=int, default=30,
                    help="训练数据截止到 N 天前，保证榜单上的成绩都是样本外的")
    args = ap.parse_args(argv)

    db.init()
    t0 = time.time()
    today = datetime.now(timezone.utc).date()
    cutoff_date = today - timedelta(days=args.embargo_days)
    cutoff = cutoff_date.isoformat()
    train_grid = G.Grid(end=cutoff)      # 训练只用到 cutoff
    grid = G.Grid()                       # 完整观测，用于提供初值
    print(f"训练期 {train_grid.dates[0]} ~ {train_grid.dates[-1]}（{train_grid.ntime} 天）")
    print(f"观测网格 {grid.ntime} 天（至 {grid.dates[-1]}），{grid.nlat}×{grid.nlon}，"
          f"{time.time() - t0:.1f}s")

    if args.train or not args.backtest:
        t1 = time.time()
        mdl = model.LimModel.train(train_grid)
        mdl.save()
        print(f"训练完成：{len(mdl.cells)} 个粗格点，时效 {sorted(mdl.coefs)}，"
              f"{time.time() - t1:.1f}s -> data/model_lim.json")

    if not args.backtest:
        return 0

    payload = model.load_model()
    targets = [today + timedelta(days=k)
               for k in range(-args.days_back, args.days_ahead + 1)]

    # 用同一个算子换到完整观测上做初值；只写 cutoff 之后的目标日，
    # 这样榜单上的每一条都是真正的样本外预报。
    mdl = model.LimModel.train(train_grid).rebind(grid)
    n = 0
    with db.session() as conn:
        for target in targets:
            if target <= cutoff_date:
                continue
            for h in R.HORIZONS:
                # 初值 = 截止时刻(O-h 00Z)能拿到的最新观测；OISST 有滞后，如实处理
                cutoff = target - timedelta(days=h)
                as_of = grid.latest_before(cutoff.isoformat())
                if not as_of:
                    continue
                vals = mdl.forecast_region_means(target.isoformat(), as_of)
                if not vals:
                    continue
                for code, v in vals.items():
                    conn.execute(
                        "INSERT INTO products(product, region, horizon, run_date,"
                        " target_date, value, fetched_at) VALUES(?,?,?,?,?,?,?)"
                        " ON CONFLICT(product, region, horizon, target_date) DO UPDATE SET"
                        "   value=excluded.value, run_date=excluded.run_date,"
                        "   fetched_at=excluded.fetched_at",
                        ("ours", code, h, as_of, target.isoformat(), v, db.now_iso()),
                    )
                    n += 1
        conn.commit()
    print(f"回测写入 {n} 条（数据版本 {payload.get('trained_on')}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
