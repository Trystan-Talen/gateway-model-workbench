#!/usr/bin/env bash
# 备份运行数据目录，保留最近 N 天。服务运行中也可执行：
# SQLite 数据库用在线备份接口导出一致副本，其余文件直接复制。
#
# 用法：deploy/backup.sh [数据目录] [备份目录] [保留天数]
# 定时：0 3 * * * /opt/gateway-workbench/deploy/backup.sh >> /var/log/gateway-workbench-backup.log 2>&1
set -euo pipefail

DATA_DIR="${1:-${ROUTE_TEST_DATA_DIR:-/srv/gateway-workbench-data}}"
BACKUP_ROOT="${2:-/srv/gateway-workbench-backup}"
KEEP_DAYS="${3:-14}"
PYTHON="${PYTHON:-python3}"

if [ ! -d "$DATA_DIR" ]; then
  echo "数据目录不存在：$DATA_DIR" >&2
  exit 1
fi

TARGET="$BACKUP_ROOT/$(date +%Y%m%d-%H%M%S)"
umask 077
mkdir -p "$TARGET"

"$PYTHON" - "$DATA_DIR" "$TARGET" <<'PY'
import pathlib, shutil, sqlite3, sys

source, target = map(pathlib.Path, sys.argv[1:3])
DATABASES = ('.sqlite3', '.db')
SKIP = ('-wal', '-shm', '.tmp')

for path in source.rglob('*'):
    if not path.is_file() or path.name.endswith(SKIP):
        continue
    out = target / path.relative_to(source)
    out.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix in DATABASES:
        # 在线备份：包含尚在日志文件中的已提交数据，且不受并发写入影响。
        src, dst = sqlite3.connect(path), sqlite3.connect(out)
        try:
            src.backup(dst)
        finally:
            dst.close(); src.close()
    else:
        shutil.copy2(path, out)
PY

find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -mtime +"$KEEP_DAYS" -exec rm -rf {} +
echo "$(date '+%F %T') 备份完成：$TARGET（$(du -sh "$TARGET" | cut -f1)）"
