#!/bin/sh
# 让"上传 Parquet"这个功能可用。
#
# 主服务本身零第三方依赖；Parquet 是唯一需要额外库的格式，
# 所以单独建一个 .venv 放 pyarrow，app/ingest_file.py 会自动去里面找。
set -eu

APP="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP"

echo "在 $APP/.venv 里安装 pyarrow（约 40 MB）…"
python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet pyarrow

.venv/bin/python -c "import pyarrow; print('pyarrow', pyarrow.__version__, '安装成功')"

echo
echo "做一次真实验证："
python3 -c "from app import ingest_file; print('  ingest_file 导入正常')"
python3 -c "import sys; sys.path.insert(0, '.'); from app import ingest_file; import pyarrow; print('  pyarrow', pyarrow.__version__, '已可用')"
echo
echo "完成。现在上传 .parquet 文件即可。"
