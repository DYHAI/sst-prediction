"""抓取 NOAA OISST v2.1 作为唯一真值，计算每个南海海区的日平均 SST。

数据源（两个都免注册、免 API key）：
  · ncdcOisst21NrtAgg —— 近实时版，滞后约 1–2 天
  · ncdcOisst21Agg    —— 最终版，滞后约 2–3 周，质量更高，发布后回填覆盖

用法：
    python3 -m tools.ingest_truth --days 30          # 回补最近 30 天
    python3 -m tools.ingest_truth --start 2026-09-01 --end 2026-10-03
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone

from app import db
from app import oisst
from app import regions as R


def ingest_day(conn, day: str) -> int:
    ds, rows = oisst.fetch_box(day)

    n = 0
    for reg in R.REGIONS:
        val, cells = oisst.weighted_mean(
            rows, (reg.lon_min, reg.lat_min, reg.lon_max, reg.lat_max)
        )
        if val is None:
            continue
        # 该海区盒子在 0.25° 网格上最多有多少格点，"有效格点占比"= 海域占比
        grid_cells = (int(round((reg.lat_max - reg.lat_min) / 0.25)) + 1) * (
            int(round((reg.lon_max - reg.lon_min) / 0.25)) + 1
        )
        valid_frac = cells / grid_cells if grid_cells else 0.0
        conn.execute(
            "INSERT INTO truth(region, date, sst, n_cells, valid_frac, source, computed_at)"
            " VALUES(?,?,?,?,?,?,?)"
            " ON CONFLICT(region, date) DO UPDATE SET"
            "   sst=excluded.sst, n_cells=excluded.n_cells, valid_frac=excluded.valid_frac,"
            "   source=excluded.source, computed_at=excluded.computed_at",
            (reg.code, day, round(val, 4), cells, round(valid_frac, 4),
             ds, db.now_iso()),
        )
        n += 1
    return n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="抓取 OISST 真值")
    ap.add_argument("--days", type=int, default=0, help="回补最近 N 天（含今天）")
    ap.add_argument("--start")
    ap.add_argument("--end")
    args = ap.parse_args(argv)

    db.init()
    today = datetime.now(timezone.utc).date()
    if args.days:
        start = today - timedelta(days=args.days - 1)
        end = today
    else:
        start = date.fromisoformat(args.start) if args.start else today
        end = date.fromisoformat(args.end) if args.end else start

    day = start
    ok = fail = 0
    with db.session() as conn:
        conn.execute("INSERT INTO runs(kind, started_at) VALUES('truth', ?)", (db.now_iso(),))
        run_id = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
        while day <= end:
            ds = day.isoformat()
            try:
                n = ingest_day(conn, ds)
                conn.commit()   # 每天一提交，别长时间占着写锁
                print(f"  {ds}  ok  {n} 个海区")
                ok += 1
            except Exception as e:  # noqa: BLE001
                print(f"  {ds}  --  {e}")
                fail += 1
            day += timedelta(days=1)
        conn.execute(
            "UPDATE runs SET ended_at=?, ok=?, detail=? WHERE id=?",
            (db.now_iso(), 1 if fail == 0 else 0, f"ok={ok} fail={fail}", run_id),
        )
    print(f"完成：成功 {ok} 天，失败 {fail} 天")
    return 0


if __name__ == "__main__":
    sys.exit(main())
