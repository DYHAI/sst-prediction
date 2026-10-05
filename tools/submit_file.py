"""命令行提交预报文件（和网页上传走同一套解析与结算逻辑）。

典型用法：
    # 首次提交，登记一个模型名字，会打印一个 token（以后复用）
    python3 -m tools.submit_file --name "我的ConvLSTM" --file pred.csv

    # 之后每天自动提交
    python3 -m tools.submit_file --token <TOKEN> --file pred.nc

    # 研究用途：允许回填历史日期（会标注为"回测"）
    python3 -m tools.submit_file --token <TOKEN> --file history.csv --allow-late

CSV 两种格式都支持：
  海区表：target_date,horizon,region,value
  网格场：time,lat,lon,sst
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

from app import db, ingest_file, service


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="提交 SST 预报文件")
    ap.add_argument("--file", required=True)
    ap.add_argument("--name", help="第一次提交时给模型起个名字")
    ap.add_argument("--affiliation", default="")
    ap.add_argument("--token", help="已有身份就用 token")
    ap.add_argument("--horizon", type=int, choices=(1, 3, 5),
                    help="文件里没有 horizon 列时，统一按这个时效提交")
    ap.add_argument("--allow-late", action="store_true",
                    help="允许提交已截止的日期（仅用于回测，会标注）")
    args = ap.parse_args(argv)

    db.init()
    if not os.path.isfile(args.file):
        print(f"文件不存在：{args.file}", file=sys.stderr)
        return 2
    with open(args.file, "rb") as f:
        raw = f.read()
    filename = os.path.basename(args.file)

    try:
        entries, fmt = ingest_file.parse_upload(filename, raw, args.horizon)
    except ingest_file.IngestError as e:
        print(f"解析失败：{e}", file=sys.stderr)
        return 2
    if not entries:
        print("解析出来是空的，请检查列名与日期格式", file=sys.stderr)
        return 2

    summary = ingest_file.summarise(entries)
    print(f"识别为 {fmt}：{summary['n']} 条，"
          f"{summary['n_dates']} 天（{summary['dates'][0]} ~ {summary['dates'][1]}），"
          f"时效 {summary['horizons']}，海区 {len(summary['regions'])} 个")

    now = datetime.now(timezone.utc)
    written = rejected = 0
    with db.session() as conn:
        me = db.player_by_token(conn, args.token) if args.token else None
        if me is None:
            if not args.name:
                print("第一次提交请用 --name 起个名字，或用 --token 指定已有身份",
                      file=sys.stderr)
                return 2
            dup = conn.execute(
                "SELECT id, token FROM players WHERE name=? AND kind='model'", (args.name,)
            ).fetchone()
            if dup:
                me = {"id": dup["id"], "token": dup["token"], "name": args.name}
            else:
                me = db.create_player(conn, args.name, "model", args.affiliation)
            print(f"已登记模型「{args.name}」，token = {me['token']}")

        for e in entries:
            if not args.allow_late and not db.is_open(e["target_date"], e["horizon"], at=now):
                rejected += 1
                continue
            conn.execute(
                "INSERT INTO submissions(player_id, region, horizon, target_date,"
                " value, comment, created_at) VALUES(?,?,?,?,?,?,?)"
                " ON CONFLICT(player_id, region, horizon, target_date) DO UPDATE SET"
                "   value=excluded.value, comment=excluded.comment,"
                "   created_at=excluded.created_at",
                (me["id"], e["region"], e["horizon"], e["target_date"], e["value"],
                 (f"命令行 {filename}" + ("｜回测" if args.allow_late else ""))[:120],
                 db.now_iso()),
            )
            written += 1
        conn.commit()

    service.invalidate()
    print(f"写入 {written} 条" + (f"，跳过 {rejected} 条已截止的" if rejected else ""))
    with db.session() as conn:
        score = service.entity_detail(conn, f"player:{me['id']}")
    if score["n"]:
        print(f"当前成绩：综合分 {score['score']}，MAE {score['mae']}，"
              f"RMSE {score['rmse']}，偏差 {score['bias']}，命中率 {score['hit']}，"
              f"样本 {score['n']}")
    else:
        print("当前还没有已结算的记录（真值滞后 1–2 天，等几天就会出分）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
