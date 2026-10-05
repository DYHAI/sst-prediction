"""清理 data/cache/http 里的过期缓存。

CFSv2 每天的 GRIB 文件有 25 MB，不清理的话缓存会以每天几百 MB 的速度涨。
每次抓取用的 URL 都带日期，旧文件不会再用，所以按天数删是安全的。

用法：
    python3 -m tools.prune_cache --days 7
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from app.http_util import CACHE_DIR


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="清理 HTTP 缓存")
    ap.add_argument("--days", type=float, default=7.0, help="保留最近 N 天（默认 7）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    d = os.path.join(CACHE_DIR, "http")
    if not os.path.isdir(d):
        print("没有缓存目录")
        return 0

    cutoff = time.time() - args.days * 86400
    removed = kept = 0
    freed = 0
    for name in os.listdir(d):
        p = os.path.join(d, name)
        try:
            st = os.stat(p)
        except OSError:
            continue
        if st.st_mtime < cutoff:
            freed += st.st_size
            removed += 1
            if not args.dry_run:
                os.remove(p)
        else:
            kept += 1
    print(f"{'[试运行] ' if args.dry_run else ''}删除 {removed} 个，保留 {kept} 个，"
          f"释放 {freed / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
