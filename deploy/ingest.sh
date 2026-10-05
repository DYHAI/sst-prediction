#!/bin/sh
# 每日数据更新：先补真值（OISST），再抓官方预报产品。
# 失败不退出，下一次定时任务会自动重试。
set -u

APP=/Users/dingding/srv/SST_Prediction
PY=/opt/homebrew/bin/python3

cd "$APP" || exit 1
mkdir -p logs

echo "===== $(date '+%Y-%m-%d %H:%M:%S') 开始 ====="
"$PY" -m tools.ingest_truth --days 4      || echo "真值抓取有失败（可忽略，隔天会补）"
"$PY" -m tools.ingest_products --days-ahead 8 || echo "产品抓取有失败"
# CFv2 的 GRIB 每天 25MB，不清理缓存会一直涨
# 多源后处理：每天重跑，样本会自动变多
[ -x .venv/bin/python ] && .venv/bin/python -m tools.train_postproc --backtest || echo "后处理训练跳过"
# U-Net：每周日重训一次（比较费时）
if [ "$(date +%u)" = "7" ] && [ -x .venv/bin/python ]; then
  .venv/bin/python -m tools.train_unet --epochs 60 --backtest || echo "U-Net 训练跳过"
fi
"$PY" -m tools.prune_cache --days 7       || echo "缓存清理失败（不影响数据）"
echo "===== $(date '+%Y-%m-%d %H:%M:%S') 结束 ====="
