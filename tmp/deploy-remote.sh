#!/bin/bash
# 云端侧执行：备份 -> 解包 -> 语法门禁 -> 清 __pycache__ -> 归属 -> 报告遗留文件
#
# 由 deploy-offline-asr.ps1 上传后在云主机上以 root 运行。之所以单独成文件：
# 备份脚本里有 $TS、<<'PY' 这类符号，塞进 PowerShell 的双引号字符串会被本地展开，
# 拆出来后两边都干净。
#
# 用法：bash /tmp/deploy-remote.sh /tmp/livenotes-code.tar.gz
set -euo pipefail

TARBALL=${1:?用法: deploy-remote.sh <tarball> [APP_DIR]}
APP_DIR=${2:-/opt/livenotes}
TS=$(date +%Y%m%d-%H%M%S)
# 备份根目录可用 BACKUP_ROOT 覆盖，方便在临时目录里演练。
BK=${BACKUP_ROOT:-/opt/backups}/$TS

if [ ! -d "$APP_DIR" ]; then
  echo "ERROR $APP_DIR 不存在" >&2
  exit 2
fi

mkdir -p "$BK"

echo "== 1/6 备份代码 =="
tar -czf "$BK/code.tar.gz" -C "$APP_DIR" app tools
echo "   $BK/code.tar.gz"

echo "== 2/6 备份 SQLite =="
if [ -f "$APP_DIR/data/livenotes.db" ]; then
  # 引擎可能在写，用 sqlite 的 backup API 拿一致性快照；直接 cp 会漏 WAL。
  "$APP_DIR/.venv/bin/python" - "$APP_DIR/data/livenotes.db" "$BK/livenotes.db" <<'PY'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
s = sqlite3.connect(src)
d = sqlite3.connect(dst)
with d:
    s.backup(d)
d.close()
s.close()
print("   ok")
PY
else
  echo "   没有 data/livenotes.db，跳过"
fi

echo "== 3/6 解包 =="
tar -xzf "$TARBALL" -C "$APP_DIR"

echo "== 4/6 语法门禁 =="
# 只做编译不执行：新模块导入时有没有副作用不在这一步管，先把语法错误挡在重启之前。
if ! "$APP_DIR/.venv/bin/python" -m compileall -q "$APP_DIR/app" "$APP_DIR/tools"; then
  echo "ERROR 有文件编译不过，什么都没重启；代码已覆盖，修好后重跑即可（备份在 $BK）" >&2
  exit 4
fi

echo "== 5/6 清理 __pycache__ 与归属 =="
find "$APP_DIR/app" "$APP_DIR/tools" -name '__pycache__' -type d -prune -exec rm -rf {} +
if id -u livenotes >/dev/null 2>&1; then
  chown -R livenotes:livenotes "$APP_DIR/app" "$APP_DIR/tools"
else
  echo "   警告：系统里没有 livenotes 用户，跳过 chown（本机演练时会这样）"
fi
chmod -R go-w "$APP_DIR/app" "$APP_DIR/tools"

echo "== 6/6 包内容核对 =="
tar -tzf "$TARBALL" | sed 's|^|./|' | sort > /tmp/_pkg.lst
( cd "$APP_DIR" && find app tools -name '*.py' -not -path '*/__pycache__/*' | sed 's|^|./|' | sort ) > /tmp/_disk.lst
echo "   包内 .py: $(grep -c '\.py$' /tmp/_pkg.lst || true) 个"
STALE=$(comm -13 /tmp/_pkg.lst /tmp/_disk.lst | sed 's|^\./||' || true)
if [ -n "$STALE" ]; then
  echo "   注意：服务器上这些文件不在本次包里，仍留在原地（可能是旧版遗留）："
  echo "$STALE" | sed '/^$/d;s/^/     /'
  echo "   它们是 .py 且没人 import 就无害；确认不用了可以手工删。"
else
  echo "   服务器上没有包外遗留文件"
fi

rm -f /tmp/_pkg.lst /tmp/_disk.lst
echo "BACKUP=$BK"
