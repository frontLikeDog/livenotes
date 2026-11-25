"""重启引擎前的闸门：有场次正在录就不许动。

退出码约定（PowerShell 侧据此决定继续还是抛错）：
  0  没有 running 的场次，或引擎根本没起来 —— 重启不会打断录音
  3  有场次在跑，stdout 列出 id —— 重启会打断录音

只看 ``/api/live/list``：它是引擎内存里活着的会话，和数据库里已结束的场次无关。
"""
import json
import sys
import urllib.request

API = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"

try:
    with urllib.request.urlopen(API + "/api/live/list", timeout=10) as r:
        data = json.load(r)
except Exception as e:  # 引擎没起来：没有录音可打断
    print("ENGINE_DOWN %s" % e)
    sys.exit(0)

run = [s for s in (data.get("sessions") or []) if s.get("running")]
if run:
    print("RUNNING %d %s" % (len(run), " ".join(str(s.get("id") or "?") for s in run)))
    sys.exit(3)

print("IDLE")
sys.exit(0)
