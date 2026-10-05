"""抓取官方预报产品，写进 products 表。

对每个 (目标日 D, 时效 h)，我们要求产品必须"在截止时间之前就已经发布"——
也就是起报时间 <= D-h 的 00:00 UTC。取满足条件里最新的一次起报。
这样产品和人玩的是同一套信息截止规则，比较才公平。

用法：
    python3 -m tools.ingest_products                    # 未来 7 天、全部产品
    python3 -m tools.ingest_products --products hycom gfs
    python3 -m tools.ingest_products --days-ahead 3 --target 2026-10-06
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone

from app import db, products
from app import regions as R


def _ocean_mask():
    """用最近一天的 OISST 生成海陆掩膜（GFS/CFS 在陆地上也有值，必须剔掉）。"""
    from app import oisst

    for back in range(2, 8):
        day = (datetime.now(timezone.utc).date() - timedelta(days=back)).isoformat()
        try:
            _ds, rows = oisst.fetch_box(day)
            return oisst.ocean_mask(rows)
        except Exception:  # noqa: BLE001
            continue
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="抓取官方 SST 预报产品")
    ap.add_argument("--products", nargs="*", default=list(products.FETCHABLE))
    ap.add_argument("--days-ahead", type=int, default=7)
    ap.add_argument("--days-back", type=int, default=0, help="回补过去 N 天（用于建榜）")
    ap.add_argument("--target", nargs="*", help="只处理这些目标日（YYYY-MM-DD）")
    args = ap.parse_args(argv)

    db.init()
    today = datetime.now(timezone.utc).date()
    if args.target:
        targets = [date.fromisoformat(t) for t in args.target]
    else:
        targets = [today + timedelta(days=k)
                   for k in range(-args.days_back, args.days_ahead + 1)]

    mask = None
    if {"gfs", "cfs"} & set(args.products):
        mask = _ocean_mask()
        print(f"海陆掩膜：{'已构建' if mask else '缺失（将不过滤陆地）'}")

    total = 0
    with db.session() as conn:
        for code in args.products:
            adapter = {
                "hycom": products.hycom_fetch,
                "gfs": products.gfs_fetch,
                "cfs": products.cfs_fetch,
            }[code]
            for h in R.HORIZONS:
                group = list(targets)
                if not group:
                    continue
                # 理想起报 = 目标日往前 h 天的 00Z；但不能晚于"现在"，
                # 否则远端还没有这次起报。日更跑会把每条逐步换成更靠近截止的起报。
                now_floor = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                prefer = {
                    d.isoformat(): min(
                        f"{(d - timedelta(days=h)).isoformat()}T00:00:00Z", now_floor
                    )
                    for d in group
                }
                kw = {"prefer_run": prefer}
                if code in ("gfs", "cfs"):
                    kw["mask"] = mask
                kw["horizon"] = h
                try:
                    res = adapter([d.isoformat() for d in group], **kw)
                except Exception as e:  # noqa: BLE001
                    print(f"  {code} h={h}: 失败 {e}")
                    continue
                n = 0
                for target, info in sorted(res.items()):
                    run = info.pop("_run", None)
                    info.pop("_step", None)
                    if not run:
                        continue
                    run_day = run[:10]
                    for region, value in info.items():
                        conn.execute(
                            "INSERT INTO products(product, region, horizon, run_date,"
                            " target_date, value, fetched_at) VALUES(?,?,?,?,?,?,?)"
                            " ON CONFLICT(product, region, horizon, target_date) DO UPDATE SET"
                            "   value=excluded.value, target_date=excluded.target_date,"
                            "   run_date=excluded.run_date,"
                            "   fetched_at=excluded.fetched_at",
                            (code, region, h, run_day, target, value, db.now_iso()),
                        )
                        n += 1
                total += n
                # 立刻提交：抓取可能持续十几分钟，不能一直攥着写锁，
                # 否则网站这边玩家提交会被卡住。
                conn.commit()
                print(f"  {code} h={h}: {n} 条")
    print(f"共写入 {total} 条产品预报")
    return 0


if __name__ == "__main__":
    sys.exit(main())
