# -*- coding: utf-8 -*-
"""起一个真实 run.py（LIVENOTES_CAPTURE_ONLY=1），走一遍开始/推流/停止/列表。"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

import psutil

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PORT = 8898
BASE = "http://127.0.0.1:%d" % PORT
DATA = tempfile.mkdtemp(prefix="ln-smoke-")
env = dict(os.environ,
           LIVENOTES_DATA_DIR=DATA, LIVENOTES_CAPTURE_ONLY="1",
           LIVENOTES_NO_BROWSER="1", LIVENOTES_PORT=str(PORT),
           PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
log = open(os.path.join(DATA, "run.log"), "wb")
p = subprocess.Popen([sys.executable, os.path.join(ROOT, "run.py")],
                     cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=5) as r:
        return json.loads(r.read())


def post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


try:
    for _ in range(80):
        try:
            info = get("/api/live/info")
            break
        except Exception:
            if p.poll() is not None:
                raise SystemExit("run.py 退出：%s" % open(os.path.join(DATA, "run.log"), encoding="utf-8").read())
            time.sleep(0.25)
    else:
        raise SystemExit("等不到服务启动")
    proc = psutil.Process(p.pid)
    fam = [proc] + proc.children(recursive=True)
    print("rss start: %.1f MB; procs=%s" % (
        sum(x.memory_info().rss for x in fam) / 1e6,
        [(x.pid, x.name(), round(x.memory_info().rss / 1e6, 1)) for x in fam]))
    print("info:", {k: info[k] for k in ("ready", "capture_only", "engine", "asr")})
    out = post("/api/live/start", {"title": "冒烟仅采集", "source_mode": "direct"})
    sid = out["session_id"]
    print("start:", {k: out.get(k) for k in ("session_id", "capture_only", "engine", "sample_rate")})
    pcm = (b"\x10\x27" * 4096)  # 10000
    for _ in range(4):
        req = urllib.request.Request(BASE + "/api/live/chunk", data=pcm, method="POST",
                                     headers={"Content-Type": "application/octet-stream",
                                              "X-Live-Id": sid})
        urllib.request.urlopen(req, timeout=5).read()
    time.sleep(0.5)
    st = get("/api/live/status?id=" + sid)
    fam = [proc] + proc.children(recursive=True)
    print("rss recording: %.1f MB; procs=%s" % (
        sum(x.memory_info().rss for x in fam) / 1e6,
        [(x.pid, x.name(), round(x.memory_info().rss / 1e6, 1)) for x in fam]))
    print("status:", {k: st.get(k) for k in ("capture_only", "elapsed", "audible_s", "n_utterances", "speakers", "engine")})
    end = post("/api/live/stop", {"session_id": sid})
    print("stop:", {k: end.get(k) for k in ("capture_only", "n_utterances", "elapsed")})
    rows = get("/api/sessions")["sessions"]
    row = next(x for x in rows if x["id"] == sid)
    print("db:", {k: row.get(k) for k in ("capture_only", "duration_s", "n_utterances")})
    txt = open(os.path.join(DATA, "run.log"), encoding="utf-8", errors="replace").read()
    print("banner:", "[仅采集]" in txt)
finally:
    p.terminate()
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()
    log.close()
