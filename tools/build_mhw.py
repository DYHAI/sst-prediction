"""算好海洋热浪事件与"热浪日"清单，存到 data/mhw_events.json。

每天随抓数任务重跑一次（真值更新后热浪判定也会变）。

用法：
    python3 -m tools.build_mhw [--rebuild-clim]
"""

from __future__ import annotations

import argparse
import sys

from app import mhw


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="构建海洋热浪事件表")
    ap.add_argument("--rebuild-clim", action="store_true",
                    help="重新计算 1982–2011 的 90 分位气候态（正常不需要）")
    args = ap.parse_args(argv)

    payload = mhw.build_and_save(rebuild_clim=args.rebuild_clim)
    ev = payload["events"]
    days = payload["mhw_days"]
    total_days = sum(len(v) for v in days.values())
    print(f"序列 {payload['series_range'][0]} ~ {payload['series_range'][1]}"
          f"（{payload['n_series_days']} 天），基准期 {payload['baseline'][0]}–{payload['baseline'][1]}")
    print(f"检出事件 {len(ev)} 次，热浪日合计 {total_days} 天")
    for r in ("beibu", "hainan_se", "pearl_river", "xisha", "zhongsha",
              "nansha_n", "scs_south"):
        print(f"  {r:12s} 事件 {sum(1 for e in ev if e['region'] == r):>4d} 次"
              f"  热浪日 {len(days.get(r, [])):>5d} 天")
    return 0


if __name__ == "__main__":
    sys.exit(main())
