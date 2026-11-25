#!/bin/bash
# 安全重启引擎：把「能不能重启」和「谁来重启」都收在一处。
#
# 为什么不直接 systemctl restart livenotes：
# 下游三个服务（capture / feishu / offline-asr）都跟着引擎走。若它们写着
# Requires=livenotes.service，restart 会连带重启它们，而它们一起来就猛连
# 127.0.0.1:8765，新引擎在端口探测阶段看到占用就退出，systemd 再重试，来回
# 拉锯（实测 6 次、30 秒）。这里的顺序是：先停下游 → 重启引擎 → 等端口真的
# LISTEN → 再拉起下游。
#
# 用法：restart-engine.sh [API_BASE]
set -euo pipefail

API=${1:-http://127.0.0.1:8765}
DOWNSTREAM="livenotes-capture livenotes-feishu livenotes-offline-asr"

# 1) 有场次在录就不动。引擎没起来时 curl 失败，视为可以继续。
if out=$(python3 /tmp/live-guard.py "$API" 2>/dev/null); then
  if [ "${out%% *}" = "RUNNING" ]; then
    echo "REFUSED $out"
    exit 3
  fi
fi

# 2) 先停下游，别让它们在新引擎探测端口时来连接
systemctl stop $DOWNSTREAM 2>/dev/null || true

# 3) 重启引擎
systemctl restart livenotes

# 4) 等端口真的开始监听（最长 60 秒），而不是固定 sleep
ok=0
for i in $(seq 1 60); do
  if curl -s --max-time 2 -o /dev/null "$API/api/health"; then ok=1; break; fi
  sleep 1
done
if [ "$ok" != "1" ]; then
  echo "ENGINE_NOT_UP after 60s"
  systemctl --no-pager --full status livenotes | head -20
  exit 4
fi
echo "ENGINE_UP"

# 5) 再拉起下游
systemctl start $DOWNSTREAM
echo "DOWNSTREAM_STARTED"
